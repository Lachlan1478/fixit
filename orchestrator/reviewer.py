"""
reviewer.py — Three reviewer personas that analyse each Claude Code build
and return a structured verdict. Uses Claude Code CLI so no separate API key
is required — reuses the existing Claude Code authentication.

Verdicts:
  pass          — build meets the spec, ship it
  iterate       — fixable issues found, send follow-up prompt to Claude
  human_needed  — something ambiguous or blocking that needs a human decision

Aggregation:
  any human_needed → human_needed
  2+ pass votes    → pass
  otherwise        → iterate
"""

import asyncio
import json
import logging
import re

import claude_session as cs

logger = logging.getLogger(__name__)

_REVIEWER_DEFS = [
    {
        "name": "QA Engineer",
        "role": (
            "You are a QA engineer reviewing an AI-generated build. "
            "Pass only if: (1) all requested MVP features appear to be present in the files written, "
            "(2) no test failures are visible in the command output, "
            "(3) no unhandled exceptions or import errors occurred. "
            "Be pragmatic — minor style issues are not grounds for 'iterate'."
        ),
        "follow_up_hint": "specific fix instruction",
    },
    {
        "name": "Spec Checker",
        "role": (
            "You compare a build report against the original product spec. "
            "Flag only clear gaps — features listed in the spec that are completely absent. "
            "Ignore missing nice-to-haves. Vote 'human_needed' only if the spec is contradictory "
            "or fundamentally unclear."
        ),
        "follow_up_hint": "specific missing feature to add",
    },
    {
        "name": "Integration Tester",
        "role": (
            "You check that the built code integrates correctly: no broken imports, "
            "no hardcoded absolute paths that won't work, no missing dependency installs. "
            "If test output confirms the app started and tests passed, vote 'pass'."
        ),
        "follow_up_hint": "specific integration fix",
    },
]


def build_report(spec: dict, iteration: int, events: list[dict]) -> str:
    """Assemble a human-readable build report from collected claude_session events."""
    name = spec.get("product_name", "Product") if spec else "Product"
    lines = [
        f"BUILD REPORT — {name} — Iteration {iteration}",
        "=" * 60,
        "",
    ]

    if spec:
        lines.append("SPEC:")
        lines.append(f"  Pitch: {spec.get('one_sentence_pitch', '')}")
        for b in spec.get("mvp_bullets", []):
            lines.append(f"  - {b}")
        lines.append("")

    files_written: list[str] = []
    bash_runs: list[dict] = []
    errors: list[str] = []
    done_text = ""

    for ev in events:
        t = ev.get("type")
        if t == "tool" and ev.get("name") in ("Write", "MultiEdit"):
            path = ev.get("input", {}).get("file_path", "")
            if path:
                files_written.append(path)
        elif t == "tool" and ev.get("name") == "Bash":
            bash_runs.append({
                "cmd": ev.get("input", {}).get("command", "")[:200],
                "output": "",
            })
        elif t == "tool_result" and bash_runs:
            bash_runs[-1]["output"] = ev.get("content", "")[:500]
        elif t == "done":
            done_text = ev.get("result", "")
        elif t == "error":
            errors.append(ev.get("message", ""))

    if files_written:
        lines.append("FILES WRITTEN:")
        for f in files_written:
            lines.append(f"  {f}")
        lines.append("")

    if bash_runs:
        lines.append("COMMANDS RUN:")
        for run in bash_runs:
            lines.append(f"  $ {run['cmd']}")
            if run["output"]:
                lines.append(f"    -> {run['output'][:300]}")
        lines.append("")

    if done_text:
        lines.append("FINAL OUTPUT:")
        lines.append(done_text[:800])
        lines.append("")

    if errors:
        lines.append("ERRORS:")
        for e in errors:
            lines.append(f"  {e[:300]}")
        lines.append("")

    return "\n".join(lines)


def _parse_verdict(text: str) -> dict:
    """Extract JSON verdict from response text, with keyword fallback."""
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{[^{}]+\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    text_lower = text.lower()
    if "human_needed" in text_lower:
        verdict = "human_needed"
    elif "pass" in text_lower:
        verdict = "pass"
    else:
        verdict = "iterate"
    return {"verdict": verdict, "reasoning": text[:100], "follow_up": ""}


async def _call_reviewer(defn: dict, report: str, pipeline_session_id: str, iteration: int) -> dict:
    """
    Call one reviewer persona via Claude Code CLI and return its parsed verdict.

    Uses a unique agent_id per call so each review starts a fresh Claude session
    with no --resume, reusing the existing Claude Code authentication.
    """
    persona_slug = defn["name"].lower().replace(" ", "_")
    agent_id = f"reviewer-{pipeline_session_id}-{persona_slug}-i{iteration}"

    # Cap report to avoid hitting Claude Code's stdin chunk limit
    report_truncated = report[:3000] + ("\n[...truncated]" if len(report) > 3000 else "")

    prompt = (
        f"You are a {defn['name']}. {defn['role']}\n\n"
        "Review the build report below and respond with ONLY a single JSON object "
        "(no markdown, no explanation, just the JSON):\n"
        '{"verdict": "pass", "reasoning": "one sentence", '
        f'"follow_up": "{defn["follow_up_hint"]} or empty string"}}\n\n'
        "Valid verdict values: pass | iterate | human_needed\n\n"
        f"{report_truncated}"
    )

    result_text = ""
    try:
        async for event in cs.stream_task(
            prompt=prompt,
            agent_id=agent_id,
            model="haiku",
            plan_mode=False,
        ):
            if event.get("type") == "done":
                result_text = event.get("result", "")
            elif event.get("type") == "text" and not result_text:
                result_text += event.get("content", "")
    except Exception as exc:
        cs.reset_session(agent_id)
        raise
        logger.error("Reviewer %s CLI call failed: %s", defn["name"], exc)
        cs.reset_session(agent_id)
        return {
            "persona": defn["name"],
            "verdict": "iterate",
            "reasoning": f"Reviewer error: {exc}",
            "follow_up": "",
        }

    if not result_text:
        logger.warning("Reviewer %s returned no text", defn["name"])
        return {
            "persona": defn["name"],
            "verdict": "iterate",
            "reasoning": "Reviewer returned no output",
            "follow_up": "",
        }

    cs.reset_session(agent_id)
    result = _parse_verdict(result_text)
    result["persona"] = defn["name"]
    return result


def aggregate_verdicts(verdicts: list[dict]) -> tuple[str, list[str]]:
    """
    Aggregate three reviewer verdicts into a single verdict + follow-up notes.

    Returns: (aggregate_verdict, follow_up_notes)
    """
    counts: dict[str, int] = {"pass": 0, "iterate": 0, "human_needed": 0}
    follow_ups: list[str] = []

    for v in verdicts:
        verdict = v.get("verdict", "iterate")
        counts[verdict] = counts.get(verdict, 0) + 1
        note = v.get("follow_up", "").strip()
        if note:
            follow_ups.append(f"[{v.get('persona', '?')}] {note}")

    if counts["human_needed"] > 0:
        return "human_needed", follow_ups
    if counts["pass"] >= 2:
        return "pass", []
    return "iterate", follow_ups


class ReviewerManager:
    """Runs all three reviewers concurrently and returns aggregate result."""

    async def review(
        self,
        spec: dict,
        iteration: int,
        events: list[dict],
        session_id: str = "unknown",
    ) -> dict:
        """
        Run all three reviewers against the build and return:
            {
                "verdict": "pass"|"iterate"|"human_needed",
                "verdicts": [individual verdict dicts],
                "follow_up_notes": [str],
                "report": str
            }
        """
        report = build_report(spec, iteration, events)

        # Run reviewers sequentially — concurrent subprocesses deadlock on Windows ProactorEventLoop
        individual = []
        for d in _REVIEWER_DEFS:
            individual.append(await _call_reviewer(d, report, session_id, iteration))

        agg_verdict, follow_up_notes = aggregate_verdicts(individual)

        return {
            "verdict": agg_verdict,
            "verdicts": individual,
            "follow_up_notes": follow_up_notes,
            "report": report,
        }
