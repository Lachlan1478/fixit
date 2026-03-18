"""Persistent per-agent conversation memory.

Files are stored as:
    memory/<agent_id>.json

Each file is a JSON array of {"role": "user"|"assistant", "content": "..."} dicts.
At most MAX_MESSAGES entries are kept (older ones are dropped).

Writes are atomic (write to .tmp then os.replace) to avoid corruption on crash.
A threading.Lock serializes concurrent file access for the same agent.
"""

import json
import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "memory")
MAX_MESSAGES = 20

# Per-agent locks to allow different agents to run concurrently without contention.
_agent_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


def _get_lock(agent_id: str) -> threading.Lock:
    with _registry_lock:
        if agent_id not in _agent_locks:
            _agent_locks[agent_id] = threading.Lock()
        return _agent_locks[agent_id]


def _safe_agent_id(agent_id: str) -> str:
    """Strip characters that could cause path traversal. Returns sanitized id."""
    sanitized = re.sub(r"[^\w\-]", "_", agent_id)
    if not sanitized:
        raise ValueError(f"agent_id {agent_id!r} is empty after sanitization")
    return sanitized


def _path(agent_id: str) -> str:
    return os.path.join(MEMORY_DIR, f"{_safe_agent_id(agent_id)}.json")


def _load_unlocked(agent_id: str) -> list[dict]:
    path = _path(agent_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.warning("memory_store: corrupt file for %r, resetting", agent_id)
            return []
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("memory_store: failed to read %r: %s", agent_id, exc)
        return []


def _save_unlocked(agent_id: str, messages: list[dict]) -> None:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    path = _path(agent_id)
    tmp_path = path + ".tmp"
    trimmed = messages[-MAX_MESSAGES:]
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(trimmed, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.error("memory_store: failed to write %r: %s", agent_id, exc)
        raise


def append_and_save(agent_id: str, user_prompt: str, assistant_reply: str) -> None:
    """Append a user/assistant turn to memory and persist it."""
    lock = _get_lock(agent_id)
    with lock:
        messages = _load_unlocked(agent_id)
        messages.append({"role": "user", "content": user_prompt})
        messages.append({"role": "assistant", "content": assistant_reply})
        _save_unlocked(agent_id, messages)


def reset(agent_id: str) -> None:
    """Delete memory for an agent. No-op if the agent has no memory."""
    lock = _get_lock(agent_id)
    with lock:
        path = _path(agent_id)
        if os.path.exists(path):
            os.remove(path)
            logger.info("memory_store: reset memory for %r", agent_id)


def build_context_prompt(agent_id: str, new_prompt: str) -> str:
    """Return the prompt enriched with conversation history (if any)."""
    lock = _get_lock(agent_id)
    with lock:
        messages = _load_unlocked(agent_id)

    if not messages:
        return new_prompt

    lines = ["Previous conversation (most recent last):"]
    for msg in messages:
        role_label = "User" if msg.get("role") == "user" else "Assistant"
        lines.append(f"[{role_label}]: {msg.get('content', '')}")
    lines.append("")
    lines.append(f"Current message: {new_prompt}")
    return "\n".join(lines)
