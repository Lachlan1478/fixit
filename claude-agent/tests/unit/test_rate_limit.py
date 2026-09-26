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
    assert d == {"is_limited": False, "reset_at": None, "queued": 0, "usage": None}


@pytest.mark.unit
def test_to_dict_limited():
    state = RateLimitState()
    reset_at = datetime(2099, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    state.set_limited(reset_at)
    d = state.to_dict()
    assert d["is_limited"] is True
    assert d["reset_at"] == reset_at.isoformat()


# ── Queue tests ──────────────────────────────────────────────────────────────

def _entry(state, prompt="p", **kw):
    return state.enqueue(prompt, kw.get("agent_id", "a"), kw.get("model", "opus"), kw.get("mode", "auto"), kw.get("cwd", "/w/lifetracker"), front=kw.get("front", False))


def test_enqueue_order_front_and_remove():
    state = rl.get_state()
    a = _entry(state, "first")
    b = _entry(state, "second")
    c = _entry(state, "interrupted", front=True)
    assert [e["prompt"] for e in state.queue] == ["interrupted", "first", "second"]
    assert state.remove(a["id"]) is True
    assert state.remove("nope") is False
    assert [e["id"] for e in state.queue] == [c["id"], b["id"]]
    assert state.to_dict()["queued"] == 2


def test_queue_persists_and_reloads():
    state = rl.get_state()
    reset = datetime.now(timezone.utc) + timedelta(hours=2)
    state.set_limited(reset)
    _entry(state, "later")

    fresh = RateLimitState()
    assert fresh.load() is True
    assert fresh.is_limited and fresh.reset_at == reset
    assert [e["prompt"] for e in fresh.queue] == ["later"]


def test_load_ignores_empty_queue():
    state = rl.get_state()
    state.set_limited(datetime.now(timezone.utc) + timedelta(hours=2))
    fresh = RateLimitState()
    assert fresh.load() is False
    assert fresh.is_limited is False


async def test_drain_runs_in_order_emails_and_requeues_on_limit(monkeypatch):
    import claude_session as cs
    import notifications

    state = rl.get_state()
    _entry(state, "one", cwd="/w/lifetracker")
    _entry(state, "two", cwd="/w/fixit")
    runs, mails = [], []
    reset_again = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    async def fake_stream(prompt, agent_id, model, mode, cwd=None, source="phone"):
        runs.append((prompt, agent_id, model, mode, cwd, source))
        if prompt == "two" and len(runs) == 2:
            yield {"type": "rate_limited", "reset_at": reset_again, "message": "limit"}
            return
        yield {"type": "text", "content": "working"}
        yield {"type": "done", "result": f"did {prompt}"}

    async def fake_notify(message, subject=""):
        mails.append((subject, message))
        return True

    monkeypatch.setattr(cs, "stream_task", fake_stream)
    monkeypatch.setattr(notifications, "send_notification", fake_notify)

    await rl.drain_queue()

    assert [r[0] for r in runs] == ["one", "two"]
    assert runs[0][1:] == ("a", "opus", "auto", "/w/lifetracker", "queue")
    assert [e["prompt"] for e in state.queue] == ["two"]
    assert state.is_limited and state.reset_at.isoformat() == reset_again
    assert [m[0] for m in mails] == ["Claude resumed · lifetracker", "Claude finished · lifetracker", "Claude resumed · fixit"]
    assert "Prompt:\none" in mails[0][1] and "Result:\ndid one" in mails[1][1]

    state.clear_limit()
    await rl.drain_queue()
    assert state.queue == [] and [r[0] for r in runs] == ["one", "two", "two"]


async def test_handle_limit_hit_queues_front_and_notifies(monkeypatch):
    import notifications

    mails = []

    async def fake_notify(message, subject=""):
        mails.append((subject, message))
        return True

    async def never_run(reset_at):
        await asyncio.sleep(3600)

    monkeypatch.setattr(notifications, "send_notification", fake_notify)
    monkeypatch.setattr(rl, "watch_and_clear", never_run)
    state = rl.get_state()
    _entry(state, "already waiting")
    reset = datetime.now(timezone.utc) + timedelta(hours=3)

    entry = await rl.handle_limit_hit(reset, "broken off", "desk", "opus", "auto", "/w/lifetracker")

    assert state.is_limited and state.reset_at == reset
    assert [e["prompt"] for e in state.queue] == ["broken off", "already waiting"]
    assert entry["id"] == state.queue[0]["id"]
    assert mails[0][0] == "Claude limit hit · lifetracker" and "2 prompt(s) queued" in mails[0][1]
    assert state._watch_task is not None and not state._watch_task.done()
    state._watch_task.cancel()


# ── Usage-capped deferral ─────────────────────────────────────────────────────

def _window(used: int, resets_in: int = 3600) -> dict:
    return {"five_hour": {"used_percentage": used, "resets_at": int(datetime.now(timezone.utc).timestamp()) + resets_in}}


@pytest.mark.parametrize("entry,limits,expect_blocked", [
    ({"max_usage": None}, _window(99), False),
    ({"max_usage": 50}, _window(20), False),
    ({"max_usage": 50}, _window(50), True),
    ({"max_usage": 50}, _window(80), True),
    ({"max_usage": 50}, _window(80, resets_in=-60), False),   # window rolled over since the CLI last reported
    ({"max_usage": 50}, None, False),                         # no snapshot yet
])
def test_blocked_until(monkeypatch, entry, limits, expect_blocked):
    import claude_session as cs

    monkeypatch.setattr(cs, "_last_limits", limits)
    blocked = rl.blocked_until(entry)
    if expect_blocked:
        assert blocked == datetime.fromtimestamp(limits["five_hour"]["resets_at"], tz=timezone.utc)
    else:
        assert blocked is None


def test_blocked_until_holds_on_seven_day_window_too(monkeypatch):
    import claude_session as cs

    week = int(datetime.now(timezone.utc).timestamp()) + 5 * 86400
    monkeypatch.setattr(cs, "_last_limits", {**_window(10), "seven_day": {"used_percentage": 60, "resets_at": week}})
    assert rl.blocked_until({"max_usage": 50}) == datetime.fromtimestamp(week, tz=timezone.utc)
    monkeypatch.setattr(cs, "_last_limits", {**_window(60), "seven_day": {"used_percentage": 60, "resets_at": week}})
    assert rl.blocked_until({"max_usage": 50}) == datetime.fromtimestamp(week, tz=timezone.utc)  # later of the two resets


async def test_drain_skips_capped_entries_and_runs_the_rest(monkeypatch):
    import claude_session as cs

    ran = []

    async def _run(prompt, *a, **kw):
        ran.append(prompt)
        yield {"type": "done", "result": "ok"}

    monkeypatch.setattr(cs, "stream_task", _run)
    monkeypatch.setattr(cs, "_last_limits", _window(80))
    state = rl.get_state()
    state.enqueue("capped", "default", "opus", "auto", "/tmp/proj", max_usage=50)
    state.enqueue("free", "default", "opus", "auto", "/tmp/proj")

    wake_at = await rl.drain_queue()

    assert ran == ["free"]
    assert [e["prompt"] for e in state.queue] == ["capped"]
    assert wake_at == datetime.fromtimestamp(cs._last_limits["five_hour"]["resets_at"], tz=timezone.utc)


async def test_drain_runs_capped_entry_once_usage_drops(monkeypatch):
    import claude_session as cs

    ran = []

    async def _run(prompt, *a, **kw):
        ran.append(prompt)
        yield {"type": "done", "result": "ok"}

    monkeypatch.setattr(cs, "stream_task", _run)
    monkeypatch.setattr(cs, "_last_limits", _window(10))
    state = rl.get_state()
    state.enqueue("capped", "default", "opus", "auto", "/tmp/proj", max_usage=50)

    assert await rl.drain_queue() is None
    assert ran == ["capped"] and state.queue == []
