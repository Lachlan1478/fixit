"""
Tests for session_store.py — persist/load round-trip against a tmp_path
database with WAL journal mode enabled.
"""

import sqlite3
from types import SimpleNamespace

import pytest

import session_store as store


def _make_session(session_id: str = "abc123") -> SimpleNamespace:
    """Minimal stand-in for PipelineSession with the persisted fields."""
    return SimpleNamespace(
        session_id=session_id,
        problem="test problem",
        state="complete",
        mode="medium",
        convergence_output={"product_name": "Widget"},
        spec={"product_name": "Widget", "mvp_bullets": ["a", "b"]},
        iteration_count=2,
        build_outputs=[{"iteration": 1, "verdict": "pass"}],
        reviewer_verdicts=[{"iteration": 1, "aggregate": "pass"}],
        notifications_sent=[{"moment": "pipeline_complete", "message": "done"}],
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:01:00+00:00",
    )


@pytest.fixture
def tmp_db(tmp_path, monkeypatch) -> str:
    """Point session_store at a fresh database under tmp_path."""
    db_path = str(tmp_path / "sessions_test.db")
    monkeypatch.setattr(store, "_DB_PATH", db_path)
    store._init_db()
    return db_path


def test_persist_load_round_trip(tmp_db):
    session = _make_session()
    store.persist(session)

    row = store.load_persisted("abc123")

    assert row is not None
    assert row["session_id"] == "abc123"
    assert row["problem"] == "test problem"
    assert row["state"] == "complete"
    assert row["iteration_count"] == 2
    assert row["spec"] == {"product_name": "Widget", "mvp_bullets": ["a", "b"]}
    assert row["reviewer_verdicts"] == [{"iteration": 1, "aggregate": "pass"}]


def test_persist_is_upsert(tmp_db):
    session = _make_session()
    store.persist(session)
    session.state = "error"
    session.iteration_count = 3
    store.persist(session)

    row = store.load_persisted("abc123")
    assert row["state"] == "error"
    assert row["iteration_count"] == 3

    with sqlite3.connect(tmp_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM pipeline_sessions").fetchone()[0]
    assert count == 1


def test_wal_mode_enabled(tmp_db):
    with sqlite3.connect(tmp_db) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_load_missing_session_returns_none(tmp_db):
    assert store.load_persisted("does-not-exist") is None
