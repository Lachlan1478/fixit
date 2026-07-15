"""
Tests for reviewer.py — reviewer CLI failures must degrade gracefully
(return an 'iterate' verdict dict) instead of crashing the pipeline.
"""

import reviewer


def _failing_stream_task(exc: Exception):
    """Return a stream_task stand-in whose async generator raises `exc`."""
    def _stream(**kwargs):
        async def _gen():
            raise exc
            yield  # pragma: no cover — makes this an async generator
        return _gen()
    return _stream


async def test_reviewer_cli_failure_returns_degraded_verdict(monkeypatch):
    reset_calls: list[str] = []
    monkeypatch.setattr(
        reviewer.cs, "stream_task", _failing_stream_task(RuntimeError("CLI exploded"))
    )
    monkeypatch.setattr(
        reviewer.cs, "reset_session", lambda agent_id: reset_calls.append(agent_id)
    )

    defn = reviewer._REVIEWER_DEFS[0]
    result = await reviewer._call_reviewer(defn, "report text", "sess1", 1)

    assert result["verdict"] == "iterate"
    assert result["persona"] == defn["name"]
    assert "CLI exploded" in result["reasoning"]
    assert result["follow_up"] == ""
    assert len(reset_calls) == 1  # session reset exactly once


async def test_review_manager_survives_all_reviewers_failing(monkeypatch):
    monkeypatch.setattr(
        reviewer.cs, "stream_task", _failing_stream_task(OSError("subprocess died"))
    )
    monkeypatch.setattr(reviewer.cs, "reset_session", lambda agent_id: None)

    manager = reviewer.ReviewerManager()
    result = await manager.review(
        spec={"product_name": "Widget"},
        iteration=1,
        events=[{"type": "done", "result": "built"}],
        session_id="sess1",
    )

    assert result["verdict"] == "iterate"
    assert len(result["verdicts"]) == len(reviewer._REVIEWER_DEFS)
    assert all(v["verdict"] == "iterate" for v in result["verdicts"])


async def test_reviewer_empty_output_returns_degraded_verdict(monkeypatch):
    def _empty_stream(**kwargs):
        async def _gen():
            yield {"type": "done", "result": ""}
        return _gen()

    monkeypatch.setattr(reviewer.cs, "stream_task", _empty_stream)
    monkeypatch.setattr(reviewer.cs, "reset_session", lambda agent_id: None)

    defn = reviewer._REVIEWER_DEFS[1]
    result = await reviewer._call_reviewer(defn, "report text", "sess1", 1)

    assert result["verdict"] == "iterate"
    assert result["reasoning"] == "Reviewer returned no output"
