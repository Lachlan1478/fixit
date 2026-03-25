"""
Unit tests for rate_limit.py — no server, no browser, no subprocess.

Tests cover:
- is_rate_limit_message(): detection of 6 limit phrases
- extract_reset_time(): 7 time-extraction patterns + fallback
- RateLimitState: state machine methods + async wait
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import rate_limit as rl
from rate_limit import (
    RateLimitState,
    extract_reset_time,
    is_rate_limit_message,
)

# ── Detection tests ───────────────────────────────────────────────────────────

POSITIVE_PHRASES = [
    "Your usage limit has been reached",
    "Rate limit exceeded, please wait",
    "Too many requests, slow down",
    "Quota exceeded for this period",
    "claude.ai is currently unavailable",
    "Please try again later",
]

NEGATIVE_PHRASES = [
    "everything is fine",
    "error 500 internal server error",
    "authentication failed",
    "invalid API key",
]


@pytest.mark.parametrize("phrase", POSITIVE_PHRASES)
@pytest.mark.unit
def test_detection_positive(phrase):
    assert is_rate_limit_message(phrase) is True


@pytest.mark.parametrize("phrase", NEGATIVE_PHRASES)
@pytest.mark.unit
def test_detection_negative(phrase):
    assert is_rate_limit_message(phrase) is False


@pytest.mark.unit
def test_detection_case_insensitive():
    assert is_rate_limit_message("USAGE LIMIT REACHED") is True
    assert is_rate_limit_message("RATE LIMIT EXCEEDED") is True
    assert is_rate_limit_message("TOO MANY REQUESTS") is True


# ── extract_reset_time tests ──────────────────────────────────────────────────

@pytest.mark.unit
def test_extract_iso_timestamp():
    iso = "2099-06-15T12:30:00+00:00"
    result = extract_reset_time(f"Limit resets at {iso}")
    expected = datetime(2099, 6, 15, 12, 30, 0, tzinfo=timezone.utc)
    assert abs((result - expected).total_seconds()) < 2


@pytest.mark.unit
def test_extract_iso_timestamp_z_suffix():
    result = extract_reset_time("Reset: 2099-06-15T12:30:00Z")
    expected = datetime(2099, 6, 15, 12, 30, 0, tzinfo=timezone.utc)
    assert abs((result - expected).total_seconds()) < 2


@pytest.mark.unit
def test_extract_unix_epoch():
    # 2099-01-01 00:00:00 UTC in unix seconds
    epoch_s = 4070908800
    result = extract_reset_time(f"reset at {epoch_s} seconds")
    expected = datetime.fromtimestamp(epoch_s, tz=timezone.utc)
    assert abs((result - expected).total_seconds()) < 2


@pytest.mark.unit
def test_extract_unix_ms():
    epoch_s = 4070908800
    epoch_ms = epoch_s * 1000
    result = extract_reset_time(f"reset at {epoch_ms}")
    expected = datetime.fromtimestamp(epoch_s, tz=timezone.utc)
    assert abs((result - expected).total_seconds()) < 2


@pytest.mark.unit
def test_extract_hours_and_mins():
    before = datetime.now(timezone.utc)
    result = extract_reset_time("Try again in 2 hours and 30 minutes")
    expected = before + timedelta(hours=2, minutes=30)
    assert abs((result - expected).total_seconds()) < 5


@pytest.mark.unit
def test_extract_hours_only():
    before = datetime.now(timezone.utc)
    result = extract_reset_time("Available again in 1.5 hours")
    expected = before + timedelta(hours=1.5)
    assert abs((result - expected).total_seconds()) < 5


@pytest.mark.unit
def test_extract_minutes_only():
    before = datetime.now(timezone.utc)
    result = extract_reset_time("Please wait in 45 minutes")
    expected = before + timedelta(minutes=45)
    assert abs((result - expected).total_seconds()) < 5


@pytest.mark.unit
def test_extract_fallback():
    before = datetime.now(timezone.utc)
    result = extract_reset_time("You hit the limit. No timing info here.")
    expected = before + timedelta(hours=5)
    assert abs((result - expected).total_seconds()) < 10


# ── RateLimitState tests ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_state_initial():
    state = RateLimitState()
    assert state.is_limited is False
    assert state.reset_at is None


@pytest.mark.unit
def test_state_set_limited():
    state = RateLimitState()
    reset_at = datetime(2099, 1, 1, tzinfo=timezone.utc)
    state.set_limited(reset_at)
    assert state.is_limited is True
    assert state.reset_at == reset_at


@pytest.mark.unit
def test_state_set_limited_idempotent():
    state = RateLimitState()
    dt1 = datetime(2099, 1, 1, tzinfo=timezone.utc)
    dt2 = datetime(2099, 6, 1, tzinfo=timezone.utc)
    state.set_limited(dt1)
    state.set_limited(dt2)  # second call is a no-op
    assert state.reset_at == dt1   # first value is kept


@pytest.mark.unit
def test_state_clear():
    state = RateLimitState()
    state.set_limited(datetime(2099, 1, 1, tzinfo=timezone.utc))
    state.clear_limit()
    assert state.is_limited is False
    assert state.reset_at is None


@pytest.mark.unit
async def test_state_wait_until_clear():
    """Async: set_limited → spawn waiter → clear_limit → waiter unblocks."""
    state = RateLimitState()
    reset_at = datetime(2099, 1, 1, tzinfo=timezone.utc)
    state.set_limited(reset_at)

    unblocked = asyncio.Event()

    async def _waiter():
        await state.wait_until_clear()
        unblocked.set()

    task = asyncio.create_task(_waiter())
    await asyncio.sleep(0)          # let waiter reach the await
    assert not unblocked.is_set()   # still blocked

    state.clear_limit()
    await asyncio.wait_for(task, timeout=2.0)
    assert unblocked.is_set()


@pytest.mark.unit
def test_to_dict_not_limited():
    state = RateLimitState()
    d = state.to_dict()
    assert d == {"is_limited": False, "reset_at": None}


@pytest.mark.unit
def test_to_dict_limited():
    state = RateLimitState()
    reset_at = datetime(2099, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    state.set_limited(reset_at)
    d = state.to_dict()
    assert d["is_limited"] is True
    assert d["reset_at"] == reset_at.isoformat()
