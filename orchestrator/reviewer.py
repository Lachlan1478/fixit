"""
reviewer.py — Three Anthropic-powered reviewer personas that analyse each
Claude Code build and return a structured verdict.

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
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"

_REVIEWER_DEFS = [
    {
        "name": "QA Engineer",
        "system": (
            "You are a QA engineer reviewing an AI-generated build. "
            "Pass only if: (1) all requested MVP features appear to be present in the files written, "
            "(2) no test failures are visible in the command output, "
            "(3) no unhandled exceptions or import errors occurred. "
            "Be pragmatic — minor style issues are not grounds for 'iterate'. "
            "Respond with valid JSON only: "
            '{\"verdict\": \"pass\"|\"iterate\"|\"human_needed\", '
            '\"reasoning\": \"one sentence\", '
            '\"follow_up\": \"specific fix instruction or empty string\"}'
        ),
    },
    {
        "name": "Spec Checker",
        "system": (
            "You compare a build report against the original product spec. "
            "Flag only clear gaps (features listed in the spec that are completely absent). "
            "Ignore missing-nice-to-haves. Vote 'human_needed' only if the spec is contradictory "
            "or fundamentally unclear. "
            "Respond with valid JSON only: "
            '{\"verdict\": \"pass\"|\"iterate\"|\"human_needed\", '
            '\"reasoning\": \"one sentence\", '
            '\"follow_up\": \"specific missing feature to add or empty string\"}'
        ),
    },
    {
        "name": "Integration Tester",
        "system": (
            "You check that the built code integrates correctly: no broken imports, "
            "no hardcoded absolute paths that won't work, no missing dependency installs. "
            "If test output confirms the app started and tests passed, vote 'pass'. "
            "Respond with valid JSON only: "
            '{\"verdict\": \"pass\"|\"iterate\"|\"human_needed\", '
            '\"reasoning\": \"one sentence\", '
            '\"follow_up\": \"specific integration fix or empty string\"}'
        ),
    },
]


def build_report(spec: dict, iteration: int, events: list[dict]) -> str:
    """
    Assemble a human-readable build report from collected claude_session events.
    """
    name = spec.get("product_name", "Product") if spec else "Product"
    lines = [
        f"BUILD REPORT — {name} — Iteration {iteration}",
        "=" * 60,
        "",
    ]

    # Spec summary
    if spec:
        lines.append("SPEC:")
        lines.append(f"  Pitch: {spec.get('one_sentence_pitch', '')}")
        for b in spec.get("mvp_bullets", []):
            lines.append(f"  - {b}")
        lines.append("")

    # Collect tool events
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
            # Attach output to last bash run
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
                lines.append(f"    → {run['output'][:300]}")
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
    """Extract JSON verdict from LLM response, with fallback."""
    try:
        # Try direct parse
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    # Try extracting JSON block
    match = re.search(r'\{[^{}]+\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    # Fallback: if "pass" appears in text, assume pass
    text_lower = text.lower()
    if "human_needed" in text_lower:
        verdict = "human_needed"
    elif "iterate" in text_lower:
        verdict = "iterate"
    else:
        verdict = "pass"
    return {"verdict": verdict, "reasoning": text[:100], "follow_up": ""}


async def _call_reviewer(client: anthropic.AsyncAnthropic, defn: dict, report: str) -> dict:
    """Call one reviewer persona and return its parsed verdict dict."""
    try:
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=256,
            system=defn["system"],
            messages=[{"role": "user", "content": report}],
        )
        raw = response.content[0].text
        result = _parse_verdict(raw)
        result["persona"] = defn["name"]
        return result
    except Exception as exc:
        logger.error("Reviewer %s failed: %s", defn["name"], exc)
        return {
            "persona": defn["name"],
            "verdict": "iterate",
            "reasoning": f"Reviewer error: {exc}",
            "follow_up": "",
        }


def aggregate_verdicts(verdicts: list[dict]) -> tuple[str, list[str]]:
    """
    Aggregate three reviewer verdicts into a single verdict + list of follow-up notes.

    Returns:
        (aggregate_verdict, follow_up_notes)
    """
    counts = {"pass": 0, "iterate": 0, "human_needed": 0}
    follow_ups = []

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

    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic()

    async def review(
        self,
        spec: dict,
        iteration: int,
        events: list[dict],
    ) -> dict:
        """
        Run all three reviewers against the build and return:
            {
                "verdict": "pass"|"iterate"|"human_needed",
                "verdicts": [individual verdict dicts],
                "follow_up_notes": [str],
                "report": str   # the build report text
            }
        """
        report = build_report(spec, iteration, events)

        individual = await asyncio.gather(
            *[_call_reviewer(self._client, d, report) for d in _REVIEWER_DEFS]
        )
        individual = list(individual)

        agg_verdict, follow_up_notes = aggregate_verdicts(individual)

        return {
            "verdict": agg_verdict,
            "verdicts": individual,
            "follow_up_notes": follow_up_notes,
            "report": report,
        }
