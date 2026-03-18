"""Persistent store for actions awaiting human/external approval."""

import json
import logging
import os
import uuid

logger = logging.getLogger(__name__)

_STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_actions.json")


def _load() -> list[dict]:
    if not os.path.exists(_STORE_PATH):
        return []
    try:
        with open(_STORE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error("pending_store: failed to load %s: %s", _STORE_PATH, e)
        return []


def _save(entries: list[dict]) -> None:
    try:
        with open(_STORE_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
    except OSError as e:
        logger.error("pending_store: failed to save %s: %s", _STORE_PATH, e)


def add_pending(action: dict) -> str:
    """Add an action with status 'pending'. Returns its UUID."""
    entries = _load()
    entry_id = str(uuid.uuid4())
    entries.append({"id": entry_id, "action": action, "status": "pending"})
    _save(entries)
    return entry_id


def get_all() -> list[dict]:
    return _load()


def get_pending() -> list[dict]:
    return [e for e in _load() if e["status"] == "pending"]


def get_by_id(entry_id: str) -> dict | None:
    for e in _load():
        if e["id"] == entry_id:
            return e
    return None


def update_status(entry_id: str, status: str) -> bool:
    """Set status on entry. Returns True if found."""
    entries = _load()
    for e in entries:
        if e["id"] == entry_id:
            e["status"] = status
            _save(entries)
            return True
    return False
