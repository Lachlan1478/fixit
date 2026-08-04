"""
claude_session.py — Claude Code CLI streaming session.

Spawns `claude --print --output-format stream-json --verbose --dangerously-skip-permissions`
and yields simplified events for the phone UI. Uses --resume <session_id> to maintain
multi-turn conversation history per agent_id natively via Claude Code.

Logging
-------
Two structured JSONL logs are written to logs/:

  events.jsonl  — one entry per raw event from Claude (detailed trace)
  sessions.jsonl — one entry per completed task (summary: timing, tokens, cost, tools)
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import analytics
import rate_limit as rl

logger = logging.getLogger(__name__)

# Browsable/containment root (kept in sync with server.py); override with AGENT_WORKSPACE.
WORKSPACE_ROOT = os.path.abspath(os.environ.get("AGENT_WORKSPACE") or os.path.join(os.path.dirname(__file__), ".."))
# Where the claude CLI actually runs (its cwd + advertised project root).
# Defaults to the browsable root; override with AGENT_HOME to anchor Claude in a
# subdirectory while still allowing the Explorer to browse the wider workspace.
SESSION_CWD = os.path.abspath(os.environ.get("AGENT_HOME") or WORKSPACE_ROOT)
LOGS_DIR = os.path.join(os.path.dirname(__file__), "logs")

# Seconds of stdout silence before the subprocess is considered hung
_DEFAULT_IDLE_TIMEOUT_S = 600.0

# Bytes read per stderr drain iteration
_STDERR_CHUNK_SIZE = 64 * 1024

# Maps agent_id → Claude Code session_id for --resume
_agent_sessions: dict[str, str] = {}

# Conversation history per agent_id
_conversation_history: dict[str, list[dict]] = {}

# Per-agent locks: serialise concurrent tasks that target the same agent_id
_agent_locks: dict[str, asyncio.Lock] = {}

# Image extensions that trigger an inline image event in the UI
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

# System prompt injected into every Claude session so it can exploit the UI features
_SYSTEM_PROMPT = f"""\
You are running inside **Claude Agent** — a mobile-first developer interface. \
Images you produce appear inline in the user's feed as tappable thumbnails.

## Screenshot reflex — do this automatically, no explanation needed

Whenever the user says anything like "show me", "screenshot", "what does X look like", \
"preview", "visualise", "capture", or asks about the appearance of a URL or app — \
immediately run:

    python claude-agent/screenshot.py <url> <short_name>

That's it. Don't explain what you're doing, don't ask for confirmation, just run the \
command. The image will appear in the feed automatically.

Examples:
- "show me google.com"  →  python claude-agent/screenshot.py https://google.com google
- "screenshot the app"  →  python claude-agent/screenshot.py http://localhost:3000 app
- "what does the habit tracker look like"  →  python claude-agent/screenshot.py http://localhost:8007/static/habits.html habits

If playwright isn't installed, install it first:
    pip install playwright && python -m playwright install chromium

## Other UI capabilities

- **HTML preview** — files written to claude-agent/static/ auto-load in a live iframe.
- **Any image file** you write (.png .jpg .gif .svg .webp) appears inline in the feed.
- **TodoWrite** renders as a live checklist — use it to show your plan before multi-step tasks.
- **Bash output** is expandable — users tap to see full stdout/stderr.

## Workspace

- Your current working directory IS the project root: {SESSION_CWD}
- Write files using paths relative to that directory (e.g. claude-agent/static/result.html), \
or absolute paths that start with the exact root above.
- Never write to a similarly-named sibling directory. Files written outside this root do NOT \
appear in the user's file browser or live preview, so the work looks lost.
"""

# Short name → full model ID
_MODELS: dict[str, str] = {
    "haiku":  "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-6",
    "fable":  "claude-fable-5",
}

# UI permission modes → the CLI's --permission-mode value.
#   auto        — full autonomy, nothing is asked (the historical behaviour)
#   acceptEdits — file edits auto-apply; other tools (Bash…) ask for approval
#   plan        — read-only; Claude returns a plan and makes no changes
_PERMISSION_MODES: dict[str, str] = {
    "auto":        "bypassPermissions",
    "acceptEdits": "acceptEdits",
    "plan":        "plan",
}
_DEFAULT_MODE = "auto"

# The CLI can't do interactive per-tool approval in headless (--print) mode —
# it emits no permission prompts to answer. So "acceptEdits" is enforced as a
# static policy: file edits + reads run automatically, but the shell tools are
# removed from Claude's toolset entirely, so no commands can run.
_ACCEPT_EDITS_BLOCK = ["Bash", "PowerShell"]


def get_history(agent_id: str) -> list[dict]:
    return _conversation_history.get(agent_id, [])


# ── Previous chats (persistence + resume) ──────────────────────────────────────
# The in-memory maps above are wiped on restart. sessions.jsonl, however, records
# every completed task (agent_id, session_id, is_resume, prompt, result, ts). We
# reconstruct past conversations from it so tabs can auto-restore on startup and
# the UI can list/reopen ("--resume") any prior chat.

def _reconstruct_conversations() -> list[dict]:
    """Rebuild conversation threads from sessions.jsonl, in start order.

    A new thread begins whenever a task ran without --resume (is_resume False);
    subsequent resumed tasks extend it. Each thread carries its running turn list
    and the latest session_id, which is the value to --resume it from.
    """
    path = os.path.join(LOGS_DIR, "sessions.jsonl")
    if not os.path.exists(path):
        return []
    current: dict[str, dict] = {}   # agent_id → thread being extended
    threads: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                agent_id = e.get("agent_id") or "default"
                sid = e.get("session_id")
                prompt = (e.get("prompt") or "").strip()
                result = e.get("result") or ""
                ts = e.get("ts")
                thread = current.get(agent_id)
                if thread is None or not e.get("is_resume"):
                    first_line = prompt.splitlines()[0] if prompt else "(no prompt)"
                    thread = {
                        "agent_id": agent_id,
                        "session_id": sid,
                        "title": first_line[:80],
                        "started_ts": ts,
                        "last_ts": ts,
                        "turns": [],
                    }
                    current[agent_id] = thread
                    threads.append(thread)
                thread["turns"].append({"role": "user", "content": prompt, "ts": ts})
                if result:
                    thread["turns"].append({"role": "assistant", "content": result, "ts": ts})
                if sid:
                    thread["session_id"] = sid
                thread["last_ts"] = ts
    except OSError as exc:
        logger.error("could not read sessions.jsonl: %s", exc)
        return []
    return threads


def list_conversations() -> list[dict]:
    """Metadata for every past chat, newest activity first (for the picker)."""
    items = [
        {
            "session_id": t["session_id"],
            "agent_id": t["agent_id"],
            "title": t["title"],
            "started_ts": t["started_ts"],
            "last_ts": t["last_ts"],
            "turn_count": len(t["turns"]),
        }
        for t in _reconstruct_conversations()
        if t["session_id"]
    ]
    items.sort(key=lambda x: x["last_ts"] or "", reverse=True)
    return items


def open_conversation(agent_id: str, session_id: str) -> list[dict] | None:
    """Point an agent tab at a past chat: set its --resume target and restore its
    turns. Returns the turn list, or None if the session_id is unknown."""
    for t in _reconstruct_conversations():
        if t["session_id"] == session_id:
            _agent_sessions[agent_id] = session_id
            _conversation_history[agent_id] = list(t["turns"])
            logger.info("Opened past chat | agent=%s session=%s turns=%d",
                        agent_id, session_id, len(t["turns"]))
            return t["turns"]
    return None


def hydrate_state() -> None:
    """On startup, restore each agent's most recent chat (session_id + turns) so
    conversations survive a server restart."""
    latest: dict[str, dict] = {}
    for t in _reconstruct_conversations():
        if not t["session_id"]:
            continue
        prev = latest.get(t["agent_id"])
        if prev is None or (t["last_ts"] or "") >= (prev["last_ts"] or ""):
            latest[t["agent_id"]] = t
    for agent_id, t in latest.items():
        _agent_sessions[agent_id] = t["session_id"]
        _conversation_history[agent_id] = list(t["turns"])
    if latest:
        logger.info("Hydrated %d agent conversation(s) from disk: %s",
                    len(latest), ", ".join(sorted(latest)))


def _get_agent_lock(agent_id: str) -> asyncio.Lock:
    """Return the (lazily created) lock that serialises tasks for one agent."""
    lock = _agent_locks.get(agent_id)
    if lock is None:
        lock = asyncio.Lock()
        _agent_locks[agent_id] = lock
    return lock


# Tool name → human-readable label for the UI feed
_TOOL_LABELS = {
    "Write":      "Writing",
    "Edit":       "Editing",
    "MultiEdit":  "Editing",
    "Read":       "Reading",
    "Bash":       "Running",
    "Glob":       "Searching",
    "Grep":       "Searching",
    "LS":         "Listing",
    "WebFetch":   "Fetching",
    "WebSearch":  "Searching web",
    "TodoWrite":  "Updating todos",
    "Task":       "Spawning agent",
}


# ── Logging helpers ───────────────────────────────────────────────────────────

def _ensure_logs_dir() -> None:
    os.makedirs(LOGS_DIR, exist_ok=True)


def _write_jsonl(filename: str, entry: dict) -> None:
    _ensure_logs_dir()
    path = os.path.join(LOGS_DIR, filename)
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.error("Failed to write log %s: %s", filename, e)


async def _write_jsonl_async(filename: str, entry: dict) -> None:
    """Async wrapper: run the blocking JSONL append in a worker thread."""
    await asyncio.to_thread(_write_jsonl, filename, entry)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> float:
    return time.monotonic() * 1000


def _idle_timeout_s() -> float:
    """Inactivity timeout for subprocess stdout reads (env-overridable)."""
    raw = os.environ.get("CLAUDE_IDLE_TIMEOUT_S", "")
    if not raw:
        return _DEFAULT_IDLE_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "Invalid CLAUDE_IDLE_TIMEOUT_S=%r — using default %.0fs",
            raw, _DEFAULT_IDLE_TIMEOUT_S,
        )
        return _DEFAULT_IDLE_TIMEOUT_S


async def _drain_stderr(stderr: asyncio.StreamReader, chunks: list[bytes]) -> None:
    """Continuously read stderr so the child never blocks on a full pipe."""
    try:
        while True:
            chunk = await stderr.read(_STDERR_CHUNK_SIZE)
            if not chunk:
                return
            chunks.append(chunk)
    except (OSError, ValueError) as e:
        logger.error("stderr drain failed: %s", e)


# ── Tool summariser ───────────────────────────────────────────────────────────

def _summarise_tool(name: str, inp: dict) -> str:
    label = _TOOL_LABELS.get(name, name)
    if name in ("Write", "Edit", "MultiEdit", "Read"):
        path = inp.get("file_path") or inp.get("path") or ""
        return f"{label} {path}"
    if name == "Bash":
        cmd = inp.get("command", "")
        return f"{label}: {cmd[:80]}"
    if name in ("Glob", "Grep"):
        pattern = inp.get("pattern") or inp.get("regex", "")
        return f"{label}: {pattern}"
    if name == "WebFetch":
        return f"{label}: {inp.get('url', '')[:60]}"
    if name == "WebSearch":
        return f"{label}: {inp.get('query', '')}"
    return label


# ── Main streaming function ───────────────────────────────────────────────────

async def stream_task(prompt: str, agent_id: str = "default", model: str = "sonnet", mode: str = _DEFAULT_MODE) -> AsyncIterator[dict]:
    """
    Run Claude Code non-interactively and yield UI-ready events:

      {"type": "tool",  "name": str, "summary": str, "input": dict}
      {"type": "text",  "content": str}
      {"type": "done",  "result": str}
      {"type": "error", "message": str}

    Conversation history is maintained automatically via --resume <session_id>.

    `mode` is one of _PERMISSION_MODES ("auto", "acceptEdits", "plan"); it maps
    to the CLI's --permission-mode. "plan" is read-only and skips session
    persistence so a planning turn never pollutes the conversation.

    Tasks that target the same agent_id are serialised via a per-agent lock so
    concurrent requests cannot corrupt session state.
    """
    if mode not in _PERMISSION_MODES:
        mode = _DEFAULT_MODE
    async with _get_agent_lock(agent_id):
        inner = _stream_task_impl(prompt, agent_id, model, mode)
        try:
            async for event in inner:
                yield event
        finally:
            # Guarantee inner cleanup (subprocess kill) runs even if the SSE
            # client disconnects (GeneratorExit) mid-stream.
            await inner.aclose()


async def _stream_task_impl(prompt: str, agent_id: str, model: str, mode: str) -> AsyncIterator[dict]:
    plan_mode = mode == "plan"
    session_id = None if plan_mode else _agent_sessions.get(agent_id)
    is_resume = session_id is not None
    task_start_ms = _now_ms()
    task_start_ts = _now_iso()

    model_id = _MODELS.get(model, _MODELS["sonnet"])
    cmd = [
        "claude", "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", _PERMISSION_MODES.get(mode, "bypassPermissions"),
        "--model", model_id,
        "--system-prompt", _SYSTEM_PROMPT,
    ]
    if mode == "acceptEdits":
        # Block the shell tools so edits auto-apply but no commands run.
        cmd += ["--disallowedTools", *_ACCEPT_EDITS_BLOCK]
    if session_id:
        cmd += ["--resume", session_id]

    if plan_mode:
        prompt = (
            "PLAN MODE: Do NOT write any files, run any commands, or make any changes. "
            "Do NOT use Write, Edit, MultiEdit, or Bash tools under any circumstances. "
            "You may use Read, Glob, and Grep only if you need to understand the codebase. "
            "Respond with a detailed step-by-step implementation plan as plain text, then stop.\n\n"
            f"Task: {prompt}"
        )

    logger.info(
        "Task start | agent=%s session=%s resume=%s prompt=%r",
        agent_id, session_id, is_resume, prompt[:80],
    )

    # ── Per-task accumulators ─────────────────────────────────────────────
    tool_calls: list[dict] = []          # {name, summary, input, ts_ms}
    text_blocks: list[str] = []
    first_tool_ms: float | None = None
    first_text_ms: float | None = None
    system_init_ms: float | None = None   # when system event fired
    raw_event_count = 0
    result_text = ""
    total_cost_usd: float | None = None
    usage: dict = {}
    num_turns: int | None = None
    duration_api_ms: float | None = None
    timed_out = False

    # Track last tool call timestamp to measure tool execution time
    last_tool_call_ts: dict[str, float] = {}   # tool_use_id → ts_ms
    # Track tool_use_id → tool name so results can be matched to calls
    tool_id_to_name: dict[str, str] = {}        # tool_use_id → name
    # Mirror of tool_calls enriched with exec_ms after result arrives
    _tool_analytics: list[dict] = []            # for analytics.record_session()

    # ── Spawn subprocess ──────────────────────────────────────────────────
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=SESSION_CWD,
            limit=10 * 1024 * 1024,  # 10 MB — Claude's stream-json lines can exceed the 64 KB default
        )
    except FileNotFoundError:
        err_event = {"type": "error", "message": "claude CLI not found — is Claude Code installed?"}
        await _write_jsonl_async("events.jsonl", {
            "ts": task_start_ts, "agent_id": agent_id, "event": "spawn_failed",
            "error": err_event["message"],
        })
        yield err_event
        return

    # Drain stderr concurrently so the child can never deadlock on a full
    # stderr pipe while we are still reading stdout.
    stderr_chunks: list[bytes] = []
    stderr_task = asyncio.create_task(_drain_stderr(proc.stderr, stderr_chunks))

    idle_timeout = _idle_timeout_s()

    try:
        spawn_ms = _now_ms() - task_start_ms
        logger.info("Subprocess spawned in %.0f ms (PID %s)", spawn_ms, proc.pid)

        await _write_jsonl_async("events.jsonl", {
            "ts": task_start_ts,
            "agent_id": agent_id,
            "event": "task_start",
            "session_id": session_id,
            "is_resume": is_resume,
            "prompt": prompt,
            "prompt_len": len(prompt),
            "spawn_ms": round(spawn_ms, 1),
        })

        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        # ── Read stream ───────────────────────────────────────────────────
        while True:
            try:
                raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=idle_timeout)
            except asyncio.TimeoutError:
                timed_out = True
                timeout_msg = (
                    f"Claude produced no output for {idle_timeout:.0f}s — task aborted"
                )
                logger.error(
                    "Idle timeout | agent=%s pid=%s timeout_s=%.0f",
                    agent_id, proc.pid, idle_timeout,
                )
                proc.kill()
                await proc.wait()
                await _write_jsonl_async("events.jsonl", {
                    "ts": _now_iso(), "agent_id": agent_id,
                    "event": "idle_timeout", "timeout_s": idle_timeout,
                })
                yield {"type": "error", "message": timeout_msg}
                break

            if not raw_line:
                break

            line = raw_line.decode(errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                await _write_jsonl_async("events.jsonl", {
                    "ts": _now_iso(), "agent_id": agent_id,
                    "event": "parse_error", "raw": line[:200],
                })
                continue

            raw_event_count += 1
            event_type = event.get("type")
            elapsed_ms = round(_now_ms() - task_start_ms, 1)

            # Save session_id for future --resume (skip in plan mode — keeps context clean)
            if not plan_mode and "session_id" in event and event["session_id"]:
                new_sid = event["session_id"]
                if new_sid != session_id:
                    _agent_sessions[agent_id] = new_sid
                    session_id = new_sid

            # ── system init ──────────────────────────────────────────────
            if event_type == "system":
                model = event.get("model", "")
                tools_available = len(event.get("tools", []))
                system_init_ms = elapsed_ms
                logger.info("Claude init | model=%s tools=%d", model, tools_available)
                await _write_jsonl_async("events.jsonl", {
                    "ts": _now_iso(), "agent_id": agent_id,
                    "event": "system_init", "model": model,
                    "tools_available": tools_available,
                    "elapsed_ms": elapsed_ms,
                })
                yield {
                    "type": "status",
                    "message": f"Ready ({model.replace('claude-', '')} · {tools_available} tools)",
                    "elapsed_ms": round(elapsed_ms),
                }

            # ── assistant turn ───────────────────────────────────────────
            elif event_type == "assistant":
                content = event.get("message", {}).get("content", [])
                msg_usage = event.get("message", {}).get("usage", {})

                for block in content:
                    btype = block.get("type")

                    if btype == "tool_use":
                        name = block.get("name", "")
                        inp = block.get("input") or {}
                        tool_use_id = block.get("id", "")
                        summary = _summarise_tool(name, inp)

                        if first_tool_ms is None:
                            first_tool_ms = elapsed_ms

                        tool_entry = {
                            "name": name,
                            "summary": summary,
                            "input": inp,
                            "tool_use_id": tool_use_id,
                            "elapsed_ms": elapsed_ms,
                        }
                        tool_calls.append(tool_entry)
                        last_tool_call_ts[tool_use_id] = _now_ms()
                        tool_id_to_name[tool_use_id] = name
                        # Start analytics record (exec_ms filled in on result)
                        _tool_analytics.append({
                            "ts": _now_iso(),
                            "name": name,
                            "elapsed_ms": elapsed_ms,
                            "input": inp,
                            "tool_use_id": tool_use_id,
                            "exec_ms": None,
                            "is_error": False,
                        })

                        logger.info("Tool call | %s | %s", name, summary)
                        await _write_jsonl_async("events.jsonl", {
                            "ts": _now_iso(), "agent_id": agent_id,
                            "event": "tool_call", **tool_entry,
                        })

                        yield {
                            "type": "tool",
                            "name": name,
                            "summary": summary,
                            "input": inp,
                            "tool_use_id": tool_use_id,
                            "elapsed_ms": round(elapsed_ms),
                        }

                        # Emit inline image event when an image file is written
                        if name == "Write":
                            file_path = inp.get("file_path", "")
                            if os.path.splitext(file_path)[1].lower() in _IMAGE_EXTS:
                                yield {
                                    "type": "image",
                                    "path": file_path,
                                    "url": f"/image?path={file_path}",
                                }

                        # Emit todos panel update for TodoWrite
                        if name == "TodoWrite":
                            todos = inp.get("todos", [])
                            if todos:
                                yield {"type": "todos", "items": todos}

                    elif btype == "text":
                        text = block.get("text", "").strip()
                        if text:
                            if first_text_ms is None:
                                first_text_ms = elapsed_ms
                            text_blocks.append(text)
                            await _write_jsonl_async("events.jsonl", {
                                "ts": _now_iso(), "agent_id": agent_id,
                                "event": "text_block",
                                "text": text,
                                "length": len(text),
                                "elapsed_ms": elapsed_ms,
                            })
                            yield {"type": "text", "content": text}

                # Log token usage from assistant turn if present
                if msg_usage:
                    await _write_jsonl_async("events.jsonl", {
                        "ts": _now_iso(), "agent_id": agent_id,
                        "event": "turn_usage", "usage": msg_usage,
                        "elapsed_ms": elapsed_ms,
                    })

            # ── user turn (tool results) ──────────────────────────────────
            elif event_type == "user":
                content = event.get("message", {}).get("content", [])
                for block in content:
                    if block.get("type") == "tool_result":
                        tool_use_id = block.get("tool_use_id", "")
                        is_error = block.get("is_error", False)
                        result_content = block.get("content", "")
                        result_content_full = result_content if result_content else ""

                        exec_ms = None
                        if tool_use_id in last_tool_call_ts:
                            exec_ms = round(_now_ms() - last_tool_call_ts.pop(tool_use_id), 1)

                        tool_name = tool_id_to_name.get(tool_use_id, "")

                        # Back-fill exec_ms + is_error on the analytics record
                        for ta in _tool_analytics:
                            if ta.get("tool_use_id") == tool_use_id:
                                ta["exec_ms"] = exec_ms
                                ta["is_error"] = is_error
                                break
                        logger.info(
                            "Tool result | id=%s name=%s error=%s exec_ms=%s",
                            tool_use_id[:8], tool_name, is_error, exec_ms,
                        )
                        await _write_jsonl_async("events.jsonl", {
                            "ts": _now_iso(), "agent_id": agent_id,
                            "event": "tool_result",
                            "tool_use_id": tool_use_id,
                            "tool_name": tool_name,
                            "is_error": is_error,
                            "result_content": result_content_full,
                            "exec_ms": exec_ms,
                            "elapsed_ms": elapsed_ms,
                        })

                        # Emit bash output for expandable UI cards
                        if tool_name == "Bash":
                            if isinstance(result_content, list):
                                content_str = "\n".join(
                                    b.get("text", "") for b in result_content if b.get("type") == "text"
                                )[:2000]
                            elif isinstance(result_content, str):
                                content_str = result_content[:2000]
                            else:
                                content_str = ""
                            yield {
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "name": tool_name,
                                "content": content_str,
                                "is_error": is_error,
                            }

            # ── rate limit ────────────────────────────────────────────────
            elif event_type == "rate_limit_event":
                info = event.get("rate_limit_info", {})
                logger.info(
                    "Rate limit | status=%s type=%s overage=%s",
                    info.get("status"), info.get("rateLimitType"), info.get("overageStatus"),
                )
                await _write_jsonl_async("events.jsonl", {
                    "ts": _now_iso(), "agent_id": agent_id,
                    "event": "rate_limit", "info": info,
                    "elapsed_ms": elapsed_ms,
                })

            # ── final result ──────────────────────────────────────────────
            elif event_type == "result":
                result_text = event.get("result", "")
                total_cost_usd = event.get("total_cost_usd")
                usage = event.get("usage", {})
                num_turns = event.get("num_turns")
                duration_api_ms = event.get("duration_api_ms")
                is_error = event.get("subtype") == "error" or event.get("is_error", False)

                if is_error:
                    if rl.is_rate_limit_message(result_text):
                        reset_at = rl.extract_reset_time(result_text)
                        yield {
                            "type": "rate_limited",
                            "reset_at": reset_at.isoformat(),
                            "message": result_text,
                        }
                    else:
                        yield {"type": "error", "message": result_text}
                else:
                    yield {"type": "done", "result": result_text}
                    if total_cost_usd is not None or usage:
                        yield {
                            "type": "usage",
                            "total_cost_usd": total_cost_usd,
                            "input_tokens": usage.get("input_tokens"),
                            "output_tokens": usage.get("output_tokens"),
                            "num_turns": num_turns,
                        }

        # ── Post-stream ───────────────────────────────────────────────────
        await stderr_task
        stderr_text = b"".join(stderr_chunks).decode(errors="replace").strip()
        if stderr_text:
            logger.warning("Claude stderr: %s", stderr_text[:500])
            await _write_jsonl_async("events.jsonl", {
                "ts": _now_iso(), "agent_id": agent_id,
                "event": "stderr", "text": stderr_text[:500],
            })

        await proc.wait()
        exit_code = proc.returncode
        total_ms = round(_now_ms() - task_start_ms, 1)

        # Handle unexpected exit (idle timeout already yielded its own error)
        if exit_code != 0 and not result_text and not timed_out:
            if rl.is_rate_limit_message(stderr_text):
                reset_at = rl.extract_reset_time(stderr_text)
                yield {
                    "type": "rate_limited",
                    "reset_at": reset_at.isoformat(),
                    "message": stderr_text or "Claude usage limit reached",
                }
            else:
                err_msg = f"Claude exited with code {exit_code}: {stderr_text[:200]}"
                logger.error(err_msg)
                yield {"type": "error", "message": err_msg}

        # ── Write session summary ─────────────────────────────────────────
        session_summary = {
            "ts": task_start_ts,
            "agent_id": agent_id,
            "session_id": session_id,
            "is_resume": is_resume,
            "prompt": prompt,
            "prompt_len": len(prompt),
            # Timing
            "total_ms": total_ms,
            "duration_api_ms": duration_api_ms,
            "system_init_ms": system_init_ms,
            "first_tool_ms": first_tool_ms,
            "inference_ms": round(first_tool_ms - system_init_ms, 1) if first_tool_ms and system_init_ms else None,
            "first_text_ms": first_text_ms,
            # Tool calls
            "tool_call_count": len(tool_calls),
            "tools_used": [t["name"] for t in tool_calls],
            "tool_calls": tool_calls,
            # Output
            "result_len": len(result_text),
            "result": result_text,
            "text_block_count": len(text_blocks),
            "num_turns": num_turns,
            # Cost / tokens
            "total_cost_usd": total_cost_usd,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
            # Process
            "exit_code": exit_code,
            "raw_event_count": raw_event_count,
            "had_stderr": bool(stderr_text),
            "timed_out": timed_out,
        }
        await _write_jsonl_async("sessions.jsonl", session_summary)

        # Persist to analytics DB (best-effort, off the event loop)
        await asyncio.to_thread(analytics.record_session, session_summary, _tool_analytics)

        # Append to in-memory conversation history (skip for plan mode)
        if not plan_mode:
            turns = _conversation_history.setdefault(agent_id, [])
            turns.append({"role": "user", "content": prompt, "ts": task_start_ts})
            if result_text:
                turns.append({"role": "assistant", "content": result_text, "ts": _now_iso(), "model": model_id})

        logger.info(
            "Task done | agent=%s total_ms=%.0f tools=%d turns=%s cost=$%.4f exit=%s",
            agent_id, total_ms, len(tool_calls), num_turns,
            total_cost_usd or 0, exit_code,
        )

    finally:
        # Never orphan the subprocess: kill it if the generator is closed
        # early (client disconnect / GeneratorExit) or an exception escapes.
        if proc.returncode is None:
            logger.warning("Killing orphaned claude subprocess (PID %s)", proc.pid)
            proc.kill()
            await proc.wait()
        if not stderr_task.done():
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass


def reset_session(agent_id: str) -> None:
    """Clear stored session_id and conversation history for a fresh start."""
    _agent_sessions.pop(agent_id, None)
    _conversation_history.pop(agent_id, None)
    logger.info("Session reset for agent_id=%r", agent_id)
    _write_jsonl("events.jsonl", {
        "ts": _now_iso(), "agent_id": agent_id, "event": "session_reset",
    })
