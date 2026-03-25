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

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOGS_DIR = os.path.join(os.path.dirname(__file__), "logs")

# Maps agent_id → Claude Code session_id for --resume
_agent_sessions: dict[str, str] = {}

# Conversation history per agent_id
_conversation_history: dict[str, list[dict]] = {}

# Image extensions that trigger an inline image event in the UI
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

# System prompt injected into every Claude session so it can exploit the UI features
_SYSTEM_PROMPT = """\
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

- Root is the parent of claude-agent/.
- Use workspace-relative paths (e.g. claude-agent/static/screenshots/result.png).
"""

# Short name → full model ID
_MODELS: dict[str, str] = {
    "haiku":  "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-6",
}


def get_history(agent_id: str) -> list[dict]:
    return _conversation_history.get(agent_id, [])

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> float:
    return time.monotonic() * 1000


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

async def stream_task(prompt: str, agent_id: str = "default", model: str = "sonnet") -> AsyncIterator[dict]:
    """
    Run Claude Code non-interactively and yield UI-ready events:

      {"type": "tool",  "name": str, "summary": str, "input": dict}
      {"type": "text",  "content": str}
      {"type": "done",  "result": str}
      {"type": "error", "message": str}

    Conversation history is maintained automatically via --resume <session_id>.
    """
    session_id = _agent_sessions.get(agent_id)
    is_resume = session_id is not None
    task_start_ms = _now_ms()
    task_start_ts = _now_iso()

    model_id = _MODELS.get(model, _MODELS["sonnet"])
    cmd = [
        "claude", "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--model", model_id,
        "--system-prompt", _SYSTEM_PROMPT,
    ]
    if session_id:
        cmd += ["--resume", session_id]

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
            cwd=WORKSPACE_ROOT,
        )
    except FileNotFoundError:
        err_event = {"type": "error", "message": "claude CLI not found — is Claude Code installed?"}
        _write_jsonl("events.jsonl", {
            "ts": task_start_ts, "agent_id": agent_id, "event": "spawn_failed",
            "error": err_event["message"],
        })
        yield err_event
        return

    spawn_ms = _now_ms() - task_start_ms
    logger.info("Subprocess spawned in %.0f ms (PID %s)", spawn_ms, proc.pid)

    _write_jsonl("events.jsonl", {
        "ts": task_start_ts,
        "agent_id": agent_id,
        "event": "task_start",
        "session_id": session_id,
        "is_resume": is_resume,
        "prompt_snippet": prompt[:120],
        "prompt_len": len(prompt),
        "spawn_ms": round(spawn_ms, 1),
    })

    proc.stdin.write(prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    # ── Read stream ───────────────────────────────────────────────────────
    async for raw_line in proc.stdout:
        line = raw_line.decode(errors="replace").strip()
        if not line:
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            _write_jsonl("events.jsonl", {
                "ts": _now_iso(), "agent_id": agent_id,
                "event": "parse_error", "raw": line[:200],
            })
            continue

        raw_event_count += 1
        event_type = event.get("type")
        elapsed_ms = round(_now_ms() - task_start_ms, 1)

        # Save session_id for future --resume
        if "session_id" in event and event["session_id"]:
            new_sid = event["session_id"]
            if new_sid != session_id:
                _agent_sessions[agent_id] = new_sid
                session_id = new_sid

        # ── system init ──────────────────────────────────────────────────
        if event_type == "system":
            model = event.get("model", "")
            tools_available = len(event.get("tools", []))
            system_init_ms = elapsed_ms
            logger.info("Claude init | model=%s tools=%d", model, tools_available)
            _write_jsonl("events.jsonl", {
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

        # ── assistant turn ───────────────────────────────────────────────
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
                        "input_snippet": str(inp)[:200],
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
                        "input_snippet": str(inp)[:200],
                        "tool_use_id": tool_use_id,
                        "exec_ms": None,
                        "is_error": False,
                    })

                    logger.info("Tool call | %s | %s", name, summary)
                    _write_jsonl("events.jsonl", {
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
                        _write_jsonl("events.jsonl", {
                            "ts": _now_iso(), "agent_id": agent_id,
                            "event": "text_block",
                            "snippet": text[:120],
                            "length": len(text),
                            "elapsed_ms": elapsed_ms,
                        })
                        yield {"type": "text", "content": text}

            # Log token usage from assistant turn if present
            if msg_usage:
                _write_jsonl("events.jsonl", {
                    "ts": _now_iso(), "agent_id": agent_id,
                    "event": "turn_usage", "usage": msg_usage,
                    "elapsed_ms": elapsed_ms,
                })

        # ── user turn (tool results) ──────────────────────────────────────
        elif event_type == "user":
            content = event.get("message", {}).get("content", [])
            for block in content:
                if block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id", "")
                    is_error = block.get("is_error", False)
                    result_content = block.get("content", "")
                    result_snippet = str(result_content)[:120] if result_content else ""

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
                    _write_jsonl("events.jsonl", {
                        "ts": _now_iso(), "agent_id": agent_id,
                        "event": "tool_result",
                        "tool_use_id": tool_use_id,
                        "tool_name": tool_name,
                        "is_error": is_error,
                        "result_snippet": result_snippet,
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

        # ── rate limit ────────────────────────────────────────────────────
        elif event_type == "rate_limit_event":
            info = event.get("rate_limit_info", {})
            logger.info(
                "Rate limit | status=%s type=%s overage=%s",
                info.get("status"), info.get("rateLimitType"), info.get("overageStatus"),
            )
            _write_jsonl("events.jsonl", {
                "ts": _now_iso(), "agent_id": agent_id,
                "event": "rate_limit", "info": info,
                "elapsed_ms": elapsed_ms,
            })

        # ── final result ──────────────────────────────────────────────────
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

    # ── Post-stream ───────────────────────────────────────────────────────
    stderr_bytes = await proc.stderr.read()
    stderr_text = stderr_bytes.decode(errors="replace").strip() if stderr_bytes else ""
    if stderr_text:
        logger.warning("Claude stderr: %s", stderr_text[:500])
        _write_jsonl("events.jsonl", {
            "ts": _now_iso(), "agent_id": agent_id,
            "event": "stderr", "text": stderr_text[:500],
        })

    await proc.wait()
    exit_code = proc.returncode
    total_ms = round(_now_ms() - task_start_ms, 1)

    # Handle unexpected exit
    if exit_code != 0 and not result_text:
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

    # ── Write session summary ─────────────────────────────────────────────
    session_summary = {
        "ts": task_start_ts,
        "agent_id": agent_id,
        "session_id": session_id,
        "is_resume": is_resume,
        "prompt_snippet": prompt[:120],
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
        "result_snippet": result_text[:120],
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
    }
    _write_jsonl("sessions.jsonl", session_summary)

    # Persist to analytics DB (best-effort, non-blocking)
    analytics.record_session(session_summary, _tool_analytics)

    # Append to in-memory conversation history
    turns = _conversation_history.setdefault(agent_id, [])
    turns.append({"role": "user", "content": prompt, "ts": task_start_ts})
    if result_text:
        turns.append({"role": "assistant", "content": result_text, "ts": _now_iso(), "model": model_id})

    logger.info(
        "Task done | agent=%s total_ms=%.0f tools=%d turns=%s cost=$%.4f exit=%s",
        agent_id, total_ms, len(tool_calls), num_turns,
        total_cost_usd or 0, exit_code,
    )


def reset_session(agent_id: str) -> None:
    """Clear stored session_id and conversation history for a fresh start."""
    _agent_sessions.pop(agent_id, None)
    _conversation_history.pop(agent_id, None)
    logger.info("Session reset for agent_id=%r", agent_id)
    _write_jsonl("events.jsonl", {
        "ts": _now_iso(), "agent_id": agent_id, "event": "session_reset",
    })
