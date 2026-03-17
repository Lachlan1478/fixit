import json
import logging
import os
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

VALID_ACTION_TYPES = {"read_file", "write_file", "shell", "none"}
_LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_LOG_FILE = os.path.join(_LOGS_DIR, "actions.log")
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)


def _ensure_log_dir() -> None:
    os.makedirs(_LOGS_DIR, exist_ok=True)

_ensure_log_dir()


def _strip_fence(text: str) -> str:
    text = text.strip()
    m = _FENCE_RE.match(text)
    return m.group(1).strip() if m else text


def parse(raw: str) -> dict | None:
    """Parse Claude's JSON response. Requires 'thought' and 'actions' fields."""
    cleaned = _strip_fence(raw)
    for candidate in [cleaned, cleaned[cleaned.find("{"):] if "{" in cleaned else ""]:
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and "thought" in data and "actions" in data:
                return data
        except json.JSONDecodeError:
            pass
    return None


def _parse_response(raw: str) -> dict | None:
    """Legacy parse that also requires 'final_answer'."""
    cleaned = _strip_fence(raw)
    for candidate in [cleaned, cleaned[cleaned.find("{"):] if "{" in cleaned else ""]:
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and all(k in data for k in ("thought", "actions", "final_answer")):
                return data
        except json.JSONDecodeError:
            pass
    return None


def validate_actions(actions) -> list[dict]:
    """Validate and normalise an actions list. Public API."""
    return _validate_actions(actions)


def _validate_actions(actions) -> list[dict]:
    if not isinstance(actions, list):
        logger.warning("middleware: 'actions' is not a list")
        return [{"type": "none", "input": "", "reason": "actions field was malformed"}]
    validated = []
    for i, entry in enumerate(actions):
        if not isinstance(entry, dict):
            logger.warning("middleware: actions[%d] is not a dict, skipping", i)
            continue
        action_type = entry.get("type", "")
        if action_type not in VALID_ACTION_TYPES:
            logger.warning("middleware: actions[%d] unknown type %r, coercing to 'none'", i, action_type)
            action_type = "none"
        validated.append({
            "type": action_type,
            "input": str(entry.get("input", "")),
            "reason": str(entry.get("reason", "")),
            # preserve content for write_file
            **({"content": entry["content"]} if "content" in entry else {}),
        })
    return validated or [{"type": "none", "input": "", "reason": "no valid actions found"}]


def _write_log_entry(entry: dict) -> None:
    try:
        with open(_LOG_FILE, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.error("middleware: failed to write log: %s", e)


def log_round(prompt: str, round_num: int, parsed: dict | None, raw: str, *, is_final: bool) -> None:
    """Log one execution round to actions.log."""
    now = datetime.now(timezone.utc).isoformat()
    if parsed is not None:
        entry = {
            "ts": now,
            "round": round_num,
            "is_final": is_final,
            "prompt_snippet": prompt[:120],
            "thought": parsed.get("thought", ""),
            "actions": parsed.get("actions", []),
            "final_answer_snippet": str(parsed.get("final_answer", ""))[:120],
            "parse_ok": True,
        }
    else:
        entry = {
            "ts": now,
            "round": round_num,
            "is_final": is_final,
            "prompt_snippet": prompt[:120],
            "thought": None,
            "actions": [],
            "final_answer_snippet": raw[:120],
            "parse_ok": False,
        }
    _write_log_entry(entry)


def process(prompt: str, raw: str) -> str:
    """Parse Claude's structured output, log it, and return final_answer.
    Falls back to returning raw if parsing fails. Kept for backward compat."""
    parsed = _parse_response(raw)
    if parsed is not None:
        parsed["actions"] = _validate_actions(parsed["actions"])
    else:
        logger.warning("middleware: unstructured response — returning raw")

    now = datetime.now(timezone.utc).isoformat()
    if parsed is not None:
        entry = {
            "ts": now,
            "prompt_snippet": prompt[:120],
            "thought": parsed.get("thought", ""),
            "actions": parsed.get("actions", []),
            "final_answer_snippet": str(parsed.get("final_answer", ""))[:120],
            "parse_ok": True,
        }
    else:
        entry = {
            "ts": now,
            "prompt_snippet": prompt[:120],
            "thought": None,
            "actions": [],
            "final_answer_snippet": raw[:120],
            "parse_ok": False,
        }
    _write_log_entry(entry)

    if parsed is None:
        return raw
    final_answer = parsed.get("final_answer", "")
    if not isinstance(final_answer, str):
        final_answer = str(final_answer)
    return final_answer if final_answer.strip() else raw
