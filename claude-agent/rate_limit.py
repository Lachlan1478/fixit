"""
rate_limit.py — Claude usage-limit detection, state, and the prompt queue.

When the CLI returns a usage-limit error the server records the reset time,
queues the interrupted prompt, and arms a watcher that sleeps until the reset,
then runs every queued prompt server-side (emailing on each dispatch and
completion). Prompts can also be deferred with a ``max_usage`` cap: they only
run while the CLI's reported 5-hour and 7-day utilisation are both under that
percentage, otherwise they wait for the offending window to roll over. Queue + reset time persist to
QUEUE_FILE so they survive a restart.
"""

import asyncio
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

QUEUE_FILE = os.path.join(os.environ.get("AGENT_LOGS_DIR") or os.path.join(os.path.dirname(__file__), "logs"), "queue.json")

_LIMIT_PATTERNS = [
    r"usage.?limit",
    r"rate.?limit",
    r"too many requests",
    r"quota exceeded",
    r"claude\.ai.*unavailable",
    r"please try again later",
]

# Ordered: most specific / reliable first
_RESET_PATTERNS = [
    (r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)", "iso"),
    (r"\breset[^0-9]*(\d{10})\b", "unix"),
    (r"\breset[^0-9]*(\d{13})\b", "unix_ms"),
    (r"in\s+(\d+)\s*hours?\s+(?:and\s+)?(\d+)\s*minutes?", "hours_mins"),
    (r"in\s+(\d+(?:\.\d+)?)\s*hours?", "hours"),
    (r"in\s+(\d+)\s*minutes?", "minutes"),
]


def is_rate_limit_message(text: str) -> bool:
    """Return True if *text* contains a usage/rate-limit signal."""
    t = text.lower()
    return any(re.search(p, t) for p in _LIMIT_PATTERNS)


def extract_reset_time(text: str) -> datetime:
    """Parse when the limit resets from *text*; falls back to 5 hours from now."""
    now = datetime.now(timezone.utc)

    for pattern, kind in _RESET_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            continue
        try:
            if kind == "iso":
                val = m.group(1)
                if val.endswith("Z"):
                    val = val[:-1] + "+00:00"
                return datetime.fromisoformat(val)
            if kind == "unix":
                return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
            if kind == "unix_ms":
                return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc)
            if kind == "hours_mins":
                return now + timedelta(hours=int(m.group(1)), minutes=int(m.group(2)))
            if kind == "hours":
                return now + timedelta(hours=float(m.group(1)))
            if kind == "minutes":
                return now + timedelta(minutes=int(m.group(1)))
        except Exception as exc:
            logger.debug("Reset-time parse failed for %r: %s", m.group(0), exc)

    logger.info("Could not parse reset time from message; defaulting to +5 h")
    return now + timedelta(hours=5)


class RateLimitState:
    def __init__(self) -> None:
        self.is_limited: bool = False
        self.reset_at: Optional[datetime] = None
        self.queue: list[dict] = []
        self.running: Optional[str] = None  # id of the entry _run_entry is streaming
        self._cleared: asyncio.Event = asyncio.Event()
        self._cleared.set()
        self._watch_task: Optional[asyncio.Task] = None
        self._watch_at: Optional[datetime] = None

    def set_limited(self, reset_at: datetime) -> None:
        if self.is_limited:
            return
        self.is_limited = True
        self.reset_at = reset_at
        self._cleared.clear()
        self.save()
        logger.info("Rate limit active; resets at %s", reset_at.isoformat())

    def clear_limit(self) -> None:
        self.is_limited = False
        self.reset_at = None
        self._cleared.set()
        self.save()
        logger.info("Rate limit cleared")

    async def wait_until_clear(self) -> None:
        await self._cleared.wait()

    def enqueue(self, prompt: str, agent_id: str, model: str, cwd: str, front: bool = False, max_usage: Optional[int] = None) -> dict:
        """Queue a prompt; `front` for an interrupted run, `max_usage` to hold it until 5h usage is under that %."""
        entry = {
            "id": secrets.token_hex(4), "prompt": prompt, "agent_id": agent_id,
            "model": model, "cwd": cwd, "max_usage": max_usage,
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
        self.queue.insert(0 if front else len(self.queue), entry)
        self.save()
        return entry

    def remove(self, entry_id: str) -> bool:
        before = len(self.queue)
        self.queue = [e for e in self.queue if e["id"] != entry_id]
        self.save()
        return len(self.queue) < before

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
            with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                json.dump({"reset_at": self.reset_at.isoformat() if self.reset_at else None, "queue": self.queue, "limits": current_limits() or None}, f, indent=1)
        except OSError as exc:
            logger.error("Could not persist queue: %s", exc)

    def load(self) -> bool:
        """Restore queue + limit from disk; True if anything is queued."""
        try:
            with open(QUEUE_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        self.queue = list(data.get("queue") or [])
        if data.get("limits"):  # so capped entries are still held after a restart, before the CLI reports again
            import claude_session as cs
            cs._last_limits = cs._last_limits or data["limits"]
        if self.queue and data.get("reset_at"):
            self.is_limited = True
            self.reset_at = datetime.fromisoformat(data["reset_at"])
            self._cleared.clear()
        if self.queue:
            logger.info("Restored %d queued prompt(s); limited=%s", len(self.queue), self.is_limited)
        return bool(self.queue)

    def to_dict(self) -> dict:
        return {
            "is_limited": self.is_limited,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "queued": len(self.queue),
            "running": self.running,
            "usage": current_usage(),
            "limits": current_limits(),
        }


_state = RateLimitState()
_drain_lock = asyncio.Lock()
_kick_tasks: set[asyncio.Task] = set()


def get_state() -> RateLimitState:
    return _state


def current_limits() -> dict:
    """The CLI's last-reported windows, five_hour / seven_day -> {used_percentage, resets_at}; empty before any run."""
    import claude_session as cs

    return cs._last_limits or {}


def current_usage() -> Optional[dict]:
    """The 5h window alone, for the UI countdown."""
    return current_limits().get("five_hour")


def blocked_until(entry: dict) -> Optional[datetime]:
    """When a usage-capped entry may next be tried, or None if it can run now."""
    cap = entry.get("max_usage")
    if cap is None:
        return None
    now = datetime.now(timezone.utc)
    blocked = None
    for window in current_limits().values():
        resets = window.get("resets_at")
        reset_dt = datetime.fromtimestamp(int(resets), tz=timezone.utc) if resets else None
        if (reset_dt and reset_dt <= now) or window.get("used_percentage", 0) < cap:  # rolled over since the CLI last reported, or under cap
            continue
        until = reset_dt or now + timedelta(minutes=30)
        blocked = max(blocked, until) if blocked else until
    return blocked


def arm_watcher(at: Optional[datetime] = None) -> None:
    """Sleep until *at* (default: the limit reset) then drain; an earlier *at* replaces a sleeping watcher."""
    at = at or _state.reset_at or datetime.now(timezone.utc)
    task = _state._watch_task
    if task and not task.done():
        if _drain_lock.locked() or (_state._watch_at and at >= _state._watch_at):
            return
        task.cancel()
    _state._watch_at = at
    _state._watch_task = asyncio.create_task(watch_and_clear(at))


def kick() -> None:
    """Try the queue now; whatever is still held back re-arms the watcher."""
    if _state.is_limited:
        arm_watcher()
        return

    async def _run() -> None:
        wake_at = await drain_queue()
        if wake_at:
            arm_watcher(wake_at)

    task = asyncio.create_task(_run())
    _kick_tasks.add(task)
    task.add_done_callback(_kick_tasks.discard)


def resume() -> None:
    """After a restart: restore the queue and pick up where the watcher left off."""
    if _state.load():
        kick()


def _fmt(dt: datetime) -> str:
    return dt.astimezone().strftime("%a %H:%M %Z")


def _project(entry: dict) -> str:
    return os.path.basename(entry["cwd"].rstrip(os.sep)) or entry["cwd"]


async def handle_limit_hit(reset_at: datetime, prompt: str, agent_id: str, model: str, cwd: str) -> dict:
    """Record a limit hit mid-run: queue the prompt first, arm the watcher, notify."""
    from notifications import send_notification

    _state.set_limited(reset_at)
    entry = _state.enqueue(prompt, agent_id, model, cwd, front=True)
    arm_watcher()
    await send_notification(
        f"Claude usage limit hit while working on {_project(entry)}.\n"
        f"{len(_state.queue)} prompt(s) queued — resuming automatically at {_fmt(reset_at)}.\n\n"
        f"Prompt:\n{prompt}",
        subject=f"Claude limit hit · {_project(entry)}",
    )
    return entry


async def watch_and_clear(wake_at: datetime) -> None:
    """Sleep until *wake_at*, then drain; loop while the limit or a usage cap still holds prompts back."""
    from notifications import send_notification

    while True:
        wait_secs = max(30.0, (wake_at - datetime.now(timezone.utc)).total_seconds())
        logger.info("Queue watcher sleeping %.0f s until %s", wait_secs, wake_at.isoformat())
        await asyncio.sleep(wait_secs)
        if _state.is_limited:
            _state.clear_limit()
            if not _state.queue:
                await send_notification("Claude usage limit has reset — you can resume now.", subject="Claude limit reset")
                return
        wake_at = await drain_queue()
        if wake_at is None:
            return
        _state._watch_at = wake_at


async def drain_queue() -> Optional[datetime]:
    """Run runnable queued prompts in order; return when to try again, or None if nothing is held back."""
    async with _drain_lock:
        skipped: set[str] = set()
        wake_at: Optional[datetime] = None
        while not _state.is_limited:
            entry = next((e for e in _state.queue if e["id"] not in skipped), None)
            if entry is None:
                break
            blocked = blocked_until(entry)
            if blocked:
                skipped.add(entry["id"])
                wake_at = min(wake_at, blocked) if wake_at else blocked
                continue
            await _run_entry(entry)
        return _state.reset_at if _state.is_limited else wake_at


async def _run_entry(entry: dict) -> None:
    import claude_session as cs
    from notifications import send_notification

    project = _project(entry)
    cap = entry.get("max_usage")
    used = " / ".join(f"{k.replace('_', ' ')} {w.get('used_percentage')}%" for k, w in current_limits().items())
    why = f"Usage ({used}) is under your {cap}% cap" if cap is not None else "Usage limit reset"
    header = f"Project: {project}\nAgent: {entry['agent_id']}\nModel: {entry['model']}\n\nPrompt:\n{entry['prompt']}"
    await send_notification(f"{why} — Claude is now running your queued prompt.\n\n{header}", subject=f"Claude resumed · {project}")
    result = ""
    _state.running = entry["id"]
    try:
        async for event in cs.stream_task(entry["prompt"], entry["agent_id"], entry["model"], "auto", cwd=entry["cwd"], source="queue"):
            if event.get("type") == "rate_limited":
                _state.set_limited(datetime.fromisoformat(event["reset_at"]))
                logger.info("Queued prompt %s hit the limit; retrying at %s", entry["id"], _state.reset_at)
                return
            if event.get("type") == "done":
                result = event.get("result") or ""
            elif event.get("type") == "error":
                result = f"Error: {event.get('message')}"
    finally:
        _state.running = None
    _state.remove(entry["id"])
    await send_notification(f"{header}\n\nResult:\n{result}", subject=f"Claude finished · {project}")
