"""Status-bar data: meta / context / limits events and session stats."""

import json
import os

import claude_session as cs


def test_context_usage_and_limits_are_pure():
    assert cs.context_usage({"input_tokens": 10, "cache_creation_input_tokens": 17462, "cache_read_input_tokens": 500, "output_tokens": 8}) == {"used": 17972, "output": 8}
    info = {"unifiedWindows": {"five_hour": {"utilization": 0.29, "resetsAt": 1790275800}, "seven_day": {"utilization": 0.334, "resetsAt": 1790362800}}}
    assert cs.limits_from_info(info) == {
        "five_hour": {"used_percentage": 29, "resets_at": 1790275800},
        "seven_day": {"used_percentage": 33, "resets_at": 1790362800},
    }
    assert cs.limits_from_info({}) is None


def test_session_stats_sum_this_chat_and_today(monkeypatch):
    os.makedirs(cs.LOGS_DIR, exist_ok=True)
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).isoformat()
    rows = [
        {"ts": today, "agent_id": "a", "session_id": "s1", "total_cost_usd": 0.5, "model": "m1"},
        {"ts": today, "agent_id": "a", "session_id": "s1", "total_cost_usd": 0.25, "model": "m2"},
        {"ts": today, "agent_id": "b", "session_id": "s9", "total_cost_usd": 1.0, "model": "m1"},
        {"ts": "2020-01-01T00:00:00+00:00", "agent_id": "a", "session_id": "s0", "total_cost_usd": 9.0},
    ]
    with open(os.path.join(cs.LOGS_DIR, "sessions.jsonl"), "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    monkeypatch.setitem(cs._agent_sessions, "a", "s1")
    stats = cs.session_stats("a")
    assert stats["runs"] == 2 and stats["cost_usd"] == 0.75 and stats["model"] == "m2"
    assert stats["today_cost_usd"] == 1.75
