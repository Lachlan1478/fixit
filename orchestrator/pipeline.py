"""
pipeline.py — End-to-end pipeline state machine.

States:
  IDLE → IDEATING → SPEC_REVIEW → BUILDING → REVIEWING →
    ITERATING → BUILDING  (loop, max 5)
    AWAITING_HUMAN → BUILDING (on human_input)
    COMPLETE
    ERROR
"""

import asyncio
import logging
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

# ── sys.path setup ─────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_AGENT_DIR = os.path.join(_HERE, "..", "claude-agent")
_ASSEMBLY_DIR = os.path.join(_HERE, "..", "..", "Assembly")

for _p in [_ASSEMBLY_DIR, _CLAUDE_AGENT_DIR]:
    _abs = os.path.abspath(_p)
    if _abs not in sys.path:
        sys.path.insert(0, _abs)
_here_abs = os.path.abspath(_HERE)
if _here_abs in sys.path:
    sys.path.remove(_here_abs)
sys.path.insert(0, _here_abs)

# ── project imports ────────────────────────────────────────────────────────────
import claude_session as cs
import rate_limit as rl
from notifications import send_notification
from src.idea_generation.generator import multiple_llm_idea_generator
from src.dashboard.event_emitter import DashboardEventEmitter, DashboardLogger

from spec_builder import build_prompt, build_followup_prompt
from reviewer import ReviewerManager
import session_store as store

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 2
_SPEC_APPROVAL_TIMEOUT = 300  # seconds before auto-approve
_EXECUTOR = ThreadPoolExecutor(max_workers=4)
_EVENT_QUEUE_MAXSIZE = 1000  # bound memory if no SSE client is consuming

# GPT-4o-mini pricing: ~$0.15/1M input, $0.60/1M output (blended ~$0.30/1M)
_OPENAI_COST_PER_TOKEN = 0.30 / 1_000_000


class PipelineState(str, Enum):
    IDLE           = "idle"
    IDEATING       = "ideating"
    SPEC_REVIEW    = "spec_review"
    BUILDING       = "building"
    REVIEWING      = "reviewing"
    ITERATING      = "iterating"
    AWAITING_HUMAN = "awaiting_human"
    COMPLETE       = "complete"
    ERROR          = "error"
    CANCELLED      = "cancelled"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PipelineSession:
    session_id: str
    problem: str
    state: PipelineState
    mode: str
    auto_approve_spec: bool
    purpose: str = "product"   # "product" | "personal"

    # Assembly outputs
    ideas: list = field(default_factory=list)
    convergence_output: Optional[dict] = None
    spec: Optional[dict] = None           # approved convergence_output

    # Build tracking
    iteration_count: int = 0
    build_outputs: list = field(default_factory=list)   # [{iteration, prompt, events, verdict}]

    # Reviewer tracking
    reviewer_verdicts: list = field(default_factory=list)

    # Notification log
    notifications_sent: list = field(default_factory=list)

    # Cost & usage tracking
    claude_cost_usd: float = 0.0
    claude_input_tokens: int = 0
    claude_output_tokens: int = 0
    openai_tokens_used: int = 0          # from Assembly turn_complete events
    openai_cost_usd: float = 0.0         # estimated from tokens

    # Timing
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    ideation_started_at: Optional[str] = None
    ideation_finished_at: Optional[str] = None
    build_started_at: Optional[str] = None
    build_finished_at: Optional[str] = None

    # Runtime (not persisted) ────────────────────────────────────────────
    event_queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)
    )
    spec_approved_event: asyncio.Event = field(default_factory=asyncio.Event)
    human_input_event: asyncio.Event = field(default_factory=asyncio.Event)
    human_input_text: str = ""
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    # Ideation progress (updated live during IDEATING)
    ideation_phases_total: int = 0
    ideation_phases_done: int = 0
    ideation_current_turn: int = 0
    ideation_max_turns: int = 0

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def to_summary(self) -> dict:
        ideation_ms = None
        if self.ideation_started_at and self.ideation_finished_at:
            from datetime import datetime, timezone
            s = datetime.fromisoformat(self.ideation_started_at)
            e = datetime.fromisoformat(self.ideation_finished_at)
            ideation_ms = int((e - s).total_seconds() * 1000)
        build_ms = None
        if self.build_started_at and self.build_finished_at:
            from datetime import datetime, timezone
            s = datetime.fromisoformat(self.build_started_at)
            e = datetime.fromisoformat(self.build_finished_at)
            build_ms = int((e - s).total_seconds() * 1000)

        return {
            "session_id": self.session_id,
            "problem": self.problem,
            "state": self.state,
            "mode": self.mode,
            "iteration_count": self.iteration_count,
            "convergence_output": self.convergence_output,
            "spec": self.spec,
            "reviewer_verdicts": self.reviewer_verdicts,
            "notifications_sent": self.notifications_sent,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "costs": {
                "claude_cost_usd": round(self.claude_cost_usd, 4),
                "claude_input_tokens": self.claude_input_tokens,
                "claude_output_tokens": self.claude_output_tokens,
                "openai_tokens_used": self.openai_tokens_used,
                "openai_cost_usd": round(self.openai_cost_usd, 4),
                "total_cost_usd": round(self.claude_cost_usd + self.openai_cost_usd, 4),
            },
            "timing": {
                "ideation_ms": ideation_ms,
                "build_ms": build_ms,
                "ideation_started_at": self.ideation_started_at,
                "ideation_finished_at": self.ideation_finished_at,
                "build_started_at": self.build_started_at,
                "build_finished_at": self.build_finished_at,
            },
        }


class _AssemblyEventBridge:
    """
    Queue-shaped adapter handed to Assembly's DashboardEventEmitter/DashboardLogger.

    Assembly pushes events with `loop.call_soon_threadsafe(queue.put_nowait, ev)`,
    so `put_nowait` always runs on the pipeline's event loop. The bridge updates
    the pipeline's progress/cost counters at emission time and then forwards the
    event to the real session queue — no peeking at asyncio.Queue internals.
    """

    def __init__(self, pipeline: "Pipeline") -> None:
        self._pipeline = pipeline

    def put_nowait(self, event: dict) -> None:
        self._pipeline._track_assembly_event(event)
        self._pipeline._emit(event)


class Pipeline:
    """
    Orchestrates one end-to-end product-building pipeline session.
    Call `asyncio.create_task(pipeline.run())` after construction.
    """

    def __init__(self, session: PipelineSession) -> None:
        self.s = session
        self._reviewer = ReviewerManager()

    # ── Public control methods (called from HTTP endpoints) ────────────────────

    def approve_spec(self, edited_spec: Optional[dict] = None) -> None:
        """Approve (and optionally edit) the spec, transitioning out of SPEC_REVIEW."""
        if edited_spec:
            self.s.convergence_output = edited_spec
        self.s.spec = self.s.convergence_output
        self.s.spec_approved_event.set()

    def provide_human_input(self, message: str) -> None:
        """Provide human guidance when pipeline is AWAITING_HUMAN."""
        self.s.human_input_text = message
        self.s.human_input_event.set()

    def cancel(self) -> None:
        """Request cancellation of the pipeline (works in any state)."""
        self.s.cancel_event.set()

    # ── Main run loop ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        try:
            await self._run_ideation()
            if self.s.cancel_event.is_set():
                raise asyncio.CancelledError
            await self._wait_for_spec_approval()
            await self._run_build_loop()
        except asyncio.CancelledError:
            if self.s.state != PipelineState.CANCELLED:
                self._set_state(PipelineState.CANCELLED)
            self._emit({"type": "pipeline_cancelled", "message": "Pipeline stopped."})
        except Exception as exc:
            logger.exception("Pipeline %s fatal error", self.s.session_id)
            self._set_state(PipelineState.ERROR)
            self._emit({"type": "error", "message": str(exc)})
            await self._notify(
                "pipeline_error",
                f"Pipeline error on '{self.s.problem[:60]}': {exc}",
            )
        finally:
            # persist() blocks on SQLite — keep it off the event loop.
            await asyncio.to_thread(store.persist, self.s)

    # ── Phase: IDEATING ────────────────────────────────────────────────────────

    def _track_assembly_event(self, event: dict) -> None:
        """
        Update ideation progress and OpenAI cost counters from a single Assembly
        event. Called by _AssemblyEventBridge at the moment each event is emitted
        (always on the pipeline's event loop via call_soon_threadsafe).
        """
        if not isinstance(event, dict):
            logger.warning("Non-dict event from Assembly emitter: %r", event)
            return

        t = event.get("type")
        if t == "phases_generated":
            phases = event.get("phases") or []
            if phases and self.s.ideation_phases_total == 0:
                self.s.ideation_phases_total = len(phases)
                self._emit_progress()
        elif t == "phase_complete":
            self.s.ideation_phases_done += 1
            self._emit_progress()
        elif t == "turn_start":
            self.s.ideation_current_turn = event.get("turn_num", 0)
            self.s.ideation_max_turns = event.get("max_turns", 0)
            self._emit_progress()
        elif t == "turn_complete":
            try:
                tokens = int(event.get("tokens_used") or 0)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid tokens_used in turn_complete event: %r",
                    event.get("tokens_used"),
                )
                return
            self.s.openai_tokens_used += tokens
            self.s.openai_cost_usd = self.s.openai_tokens_used * _OPENAI_COST_PER_TOKEN

    def _emit_progress(self) -> None:
        self._emit({
            "type": "ideation_progress",
            "phases_total": self.s.ideation_phases_total,
            "phases_done": self.s.ideation_phases_done,
            "current_turn": self.s.ideation_current_turn,
            "max_turns": self.s.ideation_max_turns,
        })

    async def _run_ideation(self) -> None:
        self._set_state(PipelineState.IDEATING)
        self.s.ideation_started_at = _now_iso()
        loop = asyncio.get_event_loop()

        # The bridge tracks progress/cost counters as Assembly emits each event,
        # then forwards to the real session queue (see _AssemblyEventBridge).
        bridge = _AssemblyEventBridge(self)
        emitter = DashboardEventEmitter(queue=bridge, loop=loop)
        dash_logger = DashboardLogger(
            queue=bridge,
            loop=loop,
            base_dir=os.path.join(_HERE, "conversation_logs"),
        )

        try:
            result = await loop.run_in_executor(
                _EXECUTOR,
                lambda: multiple_llm_idea_generator(
                    inspiration=_frame_problem(self.s.problem, self.s.purpose),
                    number_of_ideas=1,
                    mode=self.s.mode,
                    monitor=emitter,
                    logger=dash_logger,
                    config_overrides={"enable_convergence_phase": True},
                ),
            )
        except Exception as exc:
            raise RuntimeError(f"Assembly ideation failed: {exc}") from exc

        # Extract convergence output
        if isinstance(result, dict):
            self.s.ideas = result.get("ideas", [])
            self.s.convergence_output = result.get("convergence") or {}
        else:
            self.s.ideas = result if isinstance(result, list) else []
            self.s.convergence_output = {}

        self.s.ideation_finished_at = _now_iso()
        # OpenAI token/cost tallying happens live in _track_assembly_event.

        if not self.s.convergence_output:
            raise RuntimeError(
                "Assembly did not produce a convergence output. "
                "Try a more specific problem description or 'medium'/'standard' mode."
            )

        self._emit({
            "type": "spec_ready",
            "spec": self.s.convergence_output,
        })

        product_name = self.s.convergence_output.get("product_name", "your product")
        await self._notify(
            "spec_ready",
            f"Assembly spec ready: '{product_name}'. "
            f"Review at http://localhost:8002 — approve to start building.",
        )

        self._set_state(PipelineState.SPEC_REVIEW)
        self.s.touch()
        await asyncio.to_thread(store.persist, self.s)

    # ── Phase: SPEC_REVIEW ─────────────────────────────────────────────────────

    async def _wait_for_spec_approval(self) -> None:
        if self.s.auto_approve_spec:
            self.approve_spec()
            return

        try:
            await asyncio.wait_for(
                self.s.spec_approved_event.wait(),
                timeout=_SPEC_APPROVAL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.info("Spec approval timed out — auto-approving.")
            self.approve_spec()

    # ── Phase: BUILD LOOP ──────────────────────────────────────────────────────

    async def _run_build_loop(self) -> None:
        self.s.build_started_at = _now_iso()
        while True:
            self.s.iteration_count += 1
            prompt = self._build_current_prompt()

            self._set_state(PipelineState.BUILDING)
            self._emit({
                "type": "build_start",
                "iteration": self.s.iteration_count,
                "prompt": prompt,
            })
            await self._notify(
                "iteration_started",
                f"Build iteration {self.s.iteration_count}/{_MAX_ITERATIONS} "
                f"for '{self.s.spec.get('product_name', 'product')}'",
            )

            events = await self._stream_claude(prompt)

            self._set_state(PipelineState.REVIEWING)
            review = await self._reviewer.review(
                spec=self.s.spec,
                iteration=self.s.iteration_count,
                events=events,
                session_id=self.s.session_id,
            )

            # Emit individual verdicts
            for v in review["verdicts"]:
                self._emit({"type": "review_result", **v})

            # Store verdicts
            self.s.reviewer_verdicts.append({
                "iteration": self.s.iteration_count,
                "verdicts": review["verdicts"],
                "aggregate": review["verdict"],
                "follow_up_notes": review["follow_up_notes"],
            })
            self.s.build_outputs[-1]["verdict"] = review["verdict"]

            self._emit({
                "type": "aggregate_verdict",
                "verdict": review["verdict"],
                "follow_up_prompt": "\n".join(review["follow_up_notes"]),
                "iteration": self.s.iteration_count,
            })

            await self._handle_verdict(review)

            if self.s.state in (PipelineState.COMPLETE, PipelineState.AWAITING_HUMAN):
                self.s.build_finished_at = _now_iso()
                break
            # ITERATING → loop back to BUILDING

    def _build_current_prompt(self) -> str:
        if self.s.iteration_count == 1:
            return build_prompt(self.s.spec, purpose=self.s.purpose)

        # Subsequent iterations: use reviewer follow-up notes
        last_review = self.s.reviewer_verdicts[-1] if self.s.reviewer_verdicts else {}
        notes = last_review.get("follow_up_notes", [])
        # Include human input if this iteration was triggered by human
        if self.s.human_input_text:
            notes = [f"[Human] {self.s.human_input_text}"] + notes
            self.s.human_input_text = ""
        return build_followup_prompt(self.s.spec, self.s.iteration_count, notes)

    # ── Streaming Claude Code ──────────────────────────────────────────────────

    async def _stream_claude(self, prompt: str) -> list[dict]:
        """Stream a Claude Code task and return all collected events."""
        events: list[dict] = []
        # Fresh agent_id per iteration — no --resume across iterations so context
        # doesn't compound and drive up token costs.
        agent_id = f"{self.s.session_id}-iter{self.s.iteration_count}"

        try:
            async for event in cs.stream_task(
                prompt=prompt,
                agent_id=agent_id,
                model="sonnet",
                plan_mode=False,
            ):
                events.append(event)
                self._emit({"type": "claude_feed", "event": event})

                if event.get("type") == "usage":
                    self.s.claude_cost_usd += event.get("total_cost_usd") or 0.0
                    self.s.claude_input_tokens += event.get("input_tokens") or 0
                    self.s.claude_output_tokens += event.get("output_tokens") or 0
                    self._emit({"type": "cost_update", "costs": {
                        "claude_cost_usd": round(self.s.claude_cost_usd, 4),
                        "claude_input_tokens": self.s.claude_input_tokens,
                        "claude_output_tokens": self.s.claude_output_tokens,
                        "openai_tokens_used": self.s.openai_tokens_used,
                        "openai_cost_usd": round(self.s.openai_cost_usd, 4),
                        "total_cost_usd": round(self.s.claude_cost_usd + self.s.openai_cost_usd, 4),
                    }})
                if event.get("type") == "rate_limited":
                    await self._handle_rate_limited(event)

        except Exception as exc:
            logger.error("Claude stream error: %s", exc)
            events.append({"type": "error", "message": str(exc)})
            self._emit({"type": "claude_feed", "event": {"type": "error", "message": str(exc)}})

        self.s.build_outputs.append({
            "iteration": self.s.iteration_count,
            "prompt": prompt[:500],
            "events": events,
            "verdict": None,  # filled in after review
        })
        return events

    async def _handle_rate_limited(self, event: dict) -> None:
        from datetime import datetime, timezone
        reset_at_str = event.get("reset_at", "")
        try:
            reset_at_dt = datetime.fromisoformat(reset_at_str) if reset_at_str else None
        except ValueError:
            reset_at_dt = None

        reset_label = reset_at_dt.strftime("%H:%M UTC") if reset_at_dt else "unknown time"
        msg = (
            f"Claude usage limit hit — pipeline paused until {reset_label}. "
            f"You'll be notified automatically when it resets."
        )
        self._set_state(PipelineState.AWAITING_HUMAN)
        self._emit({"type": "pivotal_moment", "moment": "rate_limited", "message": msg})
        await self._notify("rate_limited", msg)

        # Start background watcher — it sleeps until reset_at then sends a notification
        # and signals wait_until_clear(). Fall back to human_input if no reset time known.
        if reset_at_dt:
            rl_state = rl.get_state()
            rl_state.set_limited(reset_at_dt)
            asyncio.create_task(rl.watch_and_clear(reset_at_dt))
            # Wait for automatic reset signal OR human override
            done, _ = await asyncio.wait(
                [
                    asyncio.create_task(rl_state.wait_until_clear()),
                    asyncio.create_task(self.s.human_input_event.wait()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
        else:
            # No reset time — wait for human to manually signal
            self.s.human_input_event.clear()
            await self.s.human_input_event.wait()

        self.s.human_input_text = ""
        self.s.human_input_event.clear()
        await self._notify("rate_limit_cleared", "✅ Claude usage limit cleared — resuming build.")
        self._set_state(PipelineState.BUILDING)

    # ── Verdict handling ───────────────────────────────────────────────────────

    async def _await_human_or_cancel(self) -> str:
        """
        Block until human input arrives or cancellation is requested,
        whichever comes first. Returns "human" or "cancel".
        """
        self.s.human_input_event.clear()
        human_task = asyncio.create_task(self.s.human_input_event.wait())
        cancel_task = asyncio.create_task(self.s.cancel_event.wait())
        done, pending = await asyncio.wait(
            {human_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        return "cancel" if cancel_task in done else "human"

    async def _handle_verdict(self, review: dict) -> None:
        verdict = review["verdict"]
        name = self.s.spec.get("product_name", "product") if self.s.spec else "product"

        if verdict == "pass":
            self._set_state(PipelineState.COMPLETE)
            self._emit({"type": "pipeline_complete", "summary": self.s.to_summary()})
            await self._notify(
                "pipeline_complete",
                f"'{name}' is complete after {self.s.iteration_count} iteration(s)! "
                f"View at http://localhost:8002",
            )
            await asyncio.to_thread(store.persist, self.s)

        elif verdict == "human_needed":
            reason = review["follow_up_notes"][0] if review["follow_up_notes"] else "Reviewer needs guidance."
            self._set_state(PipelineState.AWAITING_HUMAN)
            self._emit({"type": "pivotal_moment", "moment": "human_input_needed", "message": reason})
            await self._notify(
                "human_input_needed",
                f"Reviewer needs your input on '{name}': {reason[:200]}",
            )
            # Wait for human guidance — or a cancel request
            if await self._await_human_or_cancel() == "cancel":
                self._set_state(PipelineState.CANCELLED)
                raise asyncio.CancelledError
            self._set_state(PipelineState.ITERATING)

        elif self.s.iteration_count >= _MAX_ITERATIONS:
            self._set_state(PipelineState.AWAITING_HUMAN)
            self._emit({
                "type": "pivotal_moment",
                "moment": "max_iterations_hit",
                "message": f"Reached {_MAX_ITERATIONS} iterations without passing review.",
            })
            await self._notify(
                "max_iterations_hit",
                f"'{name}' hit {_MAX_ITERATIONS} iterations without review passing. "
                f"Manual review needed at http://localhost:8002",
            )
            # Wait for human to unblock — or a cancel request
            if await self._await_human_or_cancel() == "cancel":
                self._set_state(PipelineState.CANCELLED)
                raise asyncio.CancelledError
            self._set_state(PipelineState.ITERATING)

        else:
            # Normal iterate
            self._set_state(PipelineState.ITERATING)
            await self._notify(
                "tests_failing",
                f"Build iteration {self.s.iteration_count} needs fixes for '{name}'. "
                f"Starting iteration {self.s.iteration_count + 1}.",
            )

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _set_state(self, new_state: PipelineState) -> None:
        old = self.s.state
        self.s.state = new_state
        self.s.touch()
        self._emit({"type": "state_change", "old": old, "new": new_state})
        logger.info("Pipeline %s: %s → %s", self.s.session_id, old, new_state)

    def _emit(self, event: dict) -> None:
        try:
            self.s.event_queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Event queue full for session %s", self.s.session_id)

    async def _notify(self, moment: str, message: str) -> None:
        self.s.notifications_sent.append({"moment": moment, "message": message, "ts": _now_iso()})
        self._emit({"type": "pivotal_moment", "moment": moment, "message": message})
        asyncio.create_task(send_notification(f"[Assembly×Claude] {message}"))


_PERSONAL_PREFIX = (
    "IMPORTANT CONTEXT: This is a personal-use tool, not a startup or commercial product. "
    "Focus entirely on solving the problem for one person. Do NOT discuss monetisation, "
    "target market, ICP, investors, or GTM strategy. The 'product name' can just be a "
    "descriptive label. The 'pitch' should describe what the tool does, not why it will "
    "succeed commercially. MVP features should be the simplest thing that solves the problem.\n\n"
    "PROBLEM TO SOLVE:\n"
)


def _frame_problem(problem: str, purpose: str) -> str:
    """Wrap the problem with purpose-specific framing for Assembly."""
    if purpose == "personal":
        return _PERSONAL_PREFIX + problem
    return problem


def create_session(
    problem: str,
    mode: str = "medium",
    auto_approve_spec: bool = False,
    purpose: str = "product",
) -> tuple[PipelineSession, "Pipeline"]:
    """Create a new session + pipeline. Caller must `asyncio.create_task(pipeline.run())`."""
    session_id = str(uuid.uuid4())[:8]
    session = PipelineSession(
        session_id=session_id,
        problem=problem,
        state=PipelineState.IDLE,
        mode=mode,
        auto_approve_spec=auto_approve_spec,
        purpose=purpose,
    )
    store.put(session)
    pipeline = Pipeline(session)
    return session, pipeline
