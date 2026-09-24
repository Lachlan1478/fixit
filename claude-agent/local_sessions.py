"""Claude Code's own session store (~/.claude/projects/<cwd slug>/<id>.jsonl).

Sessions started in VS Code or a terminal live here, keyed by the folder they
ran in; listing them lets the dashboard resume any of them with --resume.
"""

import json
import os
import re

CLAUDE_PROJECTS = os.path.join(os.path.expanduser("~"), ".claude", "projects")
_SYSTEM_TAG = re.compile(r"<(local-command-caveat|command-name|command-message|command-args|local-command-stdout|system-reminder)[^>]*>.*?</\1>", re.S)
_TAG = re.compile(r"<[^>]+>")


def project_dir(cwd: str) -> str:
    """Claude Code names the store after the folder, one dash per path separator or odd char."""
    slug = re.sub(r"[^A-Za-z0-9-]", "-", os.path.abspath(cwd))
    return os.path.join(CLAUDE_PROJECTS, slug)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _clean(text: str) -> str:
    """Drop slash-command / caveat messages entirely; strip stray tags elsewhere."""
    if text.lstrip().startswith(("<command-", "<local-command")):
        return ""
    return _TAG.sub("", _SYSTEM_TAG.sub("", text)).strip()


def load_local_turns(path: str) -> list[dict]:
    """User/assistant text turns from one session file, oldest first (tool traffic skipped)."""
    turns: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("type") not in ("user", "assistant") or e.get("isSidechain"):
                    continue
                text = _clean(_text((e.get("message") or {}).get("content")))
                if not text:
                    continue
                turns.append({"role": e["type"], "content": text, "ts": e.get("timestamp")})
    except OSError:
        return []
    return turns


def list_local_sessions(cwd: str, limit: int = 30) -> list[dict]:
    """Newest-first summaries of Claude Code sessions for `cwd`."""
    folder = project_dir(cwd)
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".jsonl")]
    except OSError:
        return []
    paths = sorted(
        (os.path.join(folder, n) for n in names), key=os.path.getmtime, reverse=True
    )[:limit]
    out = []
    for path in paths:
        turns = load_local_turns(path)
        first_user = next((t["content"] for t in turns if t["role"] == "user"), "")
        if not turns or not first_user:
            continue
        out.append(
            {
                "session_id": os.path.splitext(os.path.basename(path))[0],
                "title": first_user.splitlines()[0][:80],
                "cwd": cwd,
                "turn_count": len(turns),
                "started_ts": turns[0].get("ts"),
                "last_ts": turns[-1].get("ts"),
            }
        )
    return out
