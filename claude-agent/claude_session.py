"""
claude_session.py — Claude Code CLI streaming session.

Spawns `claude --print --output-format stream-json --verbose --dangerously-skip-permissions`
and yields simplified events for the phone UI. Uses --resume <session_id> to maintain
multi-turn conversation history per agent_id natively via Claude Code.
"""

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Maps agent_id → Claude Code session_id for --resume
_agent_sessions: dict[str, str] = {}

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


def _summarise_tool(name: str, inp: dict) -> str:
    """Return a short human-readable summary of a tool call."""
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


async def stream_task(prompt: str, agent_id: str = "default") -> AsyncIterator[dict]:
    """
    Run Claude Code non-interactively and yield UI-ready events:

      {"type": "tool",  "name": str, "summary": str, "input": dict}
      {"type": "text",  "content": str}
      {"type": "done",  "result": str}
      {"type": "error", "message": str}

    Conversation history is maintained automatically via --resume <session_id>.
    """
    session_id = _agent_sessions.get(agent_id)

    cmd = [
        "claude", "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    if session_id:
        cmd += ["--resume", session_id]

    logger.info("Spawning Claude: cwd=%s session=%s", WORKSPACE_ROOT, session_id)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKSPACE_ROOT,
        )
    except FileNotFoundError:
        yield {"type": "error", "message": "claude CLI not found — is Claude Code installed?"}
        return

    proc.stdin.write(prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    result_text = ""

    async for raw_line in proc.stdout:
        line = raw_line.decode(errors="replace").strip()
        if not line:
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")

        # Save session_id so follow-up prompts resume the same conversation
        if "session_id" in event and event["session_id"]:
            _agent_sessions[agent_id] = event["session_id"]

        if event_type == "assistant":
            content = event.get("message", {}).get("content", [])
            for block in content:
                btype = block.get("type")
                if btype == "tool_use":
                    name = block.get("name", "")
                    inp = block.get("input") or {}
                    yield {
                        "type": "tool",
                        "name": name,
                        "summary": _summarise_tool(name, inp),
                        "input": inp,
                    }
                elif btype == "text":
                    text = block.get("text", "").strip()
                    if text:
                        yield {"type": "text", "content": text}

        elif event_type == "result":
            result_text = event.get("result", "")
            if event.get("subtype") == "error" or event.get("is_error"):
                yield {"type": "error", "message": result_text}
            else:
                yield {"type": "done", "result": result_text}

    stderr_bytes = await proc.stderr.read()
    if stderr_bytes:
        logger.warning("Claude stderr: %s", stderr_bytes.decode(errors="replace").strip()[:500])

    await proc.wait()
    if proc.returncode != 0 and not result_text:
        err = stderr_bytes.decode(errors="replace").strip() if stderr_bytes else "unknown error"
        yield {"type": "error", "message": f"Claude exited with code {proc.returncode}: {err}"}


def reset_session(agent_id: str) -> None:
    """Clear stored session_id so the next prompt starts a fresh conversation."""
    _agent_sessions.pop(agent_id, None)
    logger.info("Session reset for agent_id=%r", agent_id)
