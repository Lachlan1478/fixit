"""
rate_limit.py — Claude Pro usage-limit detection and state management.

When the Claude CLI returns a usage-limit error this module:
  1. Parses the reset time from the error text (falls back to +5 h).
  2. Stores the rate-limit state so the server can expose it via /rate_limit_status.
  3. Fires a background asyncio task that sleeps until the reset time,
     clears the state, and triggers a notification.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Detection patterns ──────────────────────────────────────────────────────

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
    # Full ISO-8601 timestamp
    (r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)", "iso"),
    # Unix epoch (seconds)
    (r"\breset[^0-9]*(\d{10})\b", "unix"),
    # Unix epoch (milliseconds)
    (r"\breset[^0-9]*(\d{13})\b", "unix_ms"),
    # "in X hours and Y minutes"
    (r"in\s+(\d+)\s*hours?\s+(?:and\s+)?(\d+)\s*minutes?", "hours_mins"),
    # "in X hours"
    (r"in\s+(\d+(?:\.\d+)?)\s*hours?", "hours"),
    # "in X minutes"
    (r"in\s+(\d+)\s*minutes?", "minutes"),
]


def is_rate_limit_message(text: str) -> bool:
    """Return True if *text* contains a usage/rate-limit signal."""
    t = text.lower()
    return any(re.search(p, t) for p in _LIMIT_PATTERNS)


def extract_reset_time(text: str) -> datetime:
    """
    Try to parse when the limit resets from *text*.
    Falls back to 5 hours from now if nothing useful is found.
    """
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


# ── Global state ────────────────────────────────────────────────────────────

class RateLimitState:
    def __init__(self) -> None:
        self.is_limited: bool = False
        self.reset_at: Optional[datetime] = None
        self._cleared: asyncio.Event = asyncio.Event()
        self._cleared.set()          # not limited initially
        self._watch_task: Optional[asyncio.Task] = None

    def set_limited(self, reset_at: datetime) -> None:
        if self.is_limited:
            return  # already tracking — don't restart
        self.is_limited = True
        self.reset_at = reset_at
        self._cleared.clear()
        logger.info("Rate limit active; resets at %s", reset_at.isoformat())

    def clear_limit(self) -> None:
        self.is_limited = False
        self.reset_at = None
        self._cleared.set()
        logger.info("Rate limit cleared")

    async def wait_until_clear(self) -> None:
        await self._cleared.wait()

    def to_dict(self) -> dict:
        return {
            "is_limited": self.is_limited,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
        }


_state = RateLimitState()


def get_state() -> RateLimitState:
    return _state


# ── Background watcher ──────────────────────────────────────────────────────

async def watch_and_clear(reset_at: datetime) -> None:
    """
    Sleep until *reset_at*, then clear the rate-limit flag and send a
    notification through every configured channel.
    """
    from notifications import send_notification  # late import — avoids circular

    now = datetime.now(timezone.utc)
    wait_secs = max(30.0, (reset_at - now).total_seconds())
    logger.info("Rate-limit watcher sleeping %.0f s until %s", wait_secs, reset_at.isoformat())

    await asyncio.sleep(wait_secs)

    _state.clear_limit()

    msg = (
        f"✅ Claude usage limit has reset!\n"
        f"You can resume your Claude Agent session now.\n"
        f"Reset at: {reset_at.strftime('%Y-%m-%d %H:%M UTC')}"
    )
    await send_notification(msg)
