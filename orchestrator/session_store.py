"""
session_store.py — In-memory pipeline session registry with SQLite persistence.

Sessions live in memory while active; completed/error sessions are persisted
to SQLite so they survive server restarts.
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_STORE: dict = {}  # session_id → PipelineSession (imported lazily to avoid circular)

_DB_PATH = os.path.join(os.path.dirname(__file__), "sessions.db")
_DB_TIMEOUT_S = 10

# Serialises SQLite writes across threads (persist runs via asyncio.to_thread).
_WRITE_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _init_db() -> None:
    with _WRITE_LOCK, sqlite3.connect(_DB_PATH, timeout=_DB_TIMEOUT_S) as conn:
        # WAL lets readers proceed while a write is in flight and keeps
        # short writes from blocking the event loop's to_thread workers.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_sessions (
                session_id TEXT PRIMARY KEY,
                problem TEXT,
                state TEXT,
                mode TEXT,
                convergence_output TEXT,
                spec TEXT,
                iteration_count INTEGER,
                build_outputs TEXT,
                reviewer_verdicts TEXT,
                notifications_sent TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        conn.commit()


_init_db()


def put(session) -> None:
    """Register session in memory."""
    _STORE[session.session_id] = session


def get(session_id: str):
    """Retrieve session from memory. Returns None if not found."""
    return _STORE.get(session_id)


def list_sessions() -> list[dict]:
    """Return summary of all in-memory sessions, newest first."""
    sessions = []
    for s in _STORE.values():
        sessions.append({
            "session_id": s.session_id,
            "problem": s.problem[:80] + "..." if len(s.problem) > 80 else s.problem,
            "state": s.state,
            "mode": s.mode,
            "iteration_count": s.iteration_count,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        })
    sessions.sort(key=lambda x: x["created_at"], reverse=True)
    return sessions


def persist(session) -> None:
    """
    Write session snapshot to SQLite (best effort).

    Blocking: call via `await asyncio.to_thread(persist, session)` from async code.
    """
    try:
        with _WRITE_LOCK, sqlite3.connect(_DB_PATH, timeout=_DB_TIMEOUT_S) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO pipeline_sessions
                (session_id, problem, state, mode, convergence_output, spec,
                 iteration_count, build_outputs, reviewer_verdicts,
                 notifications_sent, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                session.session_id,
                session.problem,
                session.state,
                session.mode,
                json.dumps(session.convergence_output),
                json.dumps(session.spec),
                session.iteration_count,
                json.dumps(session.build_outputs),
                json.dumps(session.reviewer_verdicts),
                json.dumps(session.notifications_sent),
                session.created_at,
                session.updated_at,
            ))
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to persist session %s: %s", session.session_id, exc)


def load_persisted(session_id: str) -> Optional[dict]:
    """Read one persisted session row back as a dict. Returns None if absent."""
    try:
        with sqlite3.connect(_DB_PATH, timeout=_DB_TIMEOUT_S) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM pipeline_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("Failed to load session %s: %s", session_id, exc)
        return None

    if row is None:
        return None
    data = dict(row)
    for key in ("convergence_output", "spec", "build_outputs",
                "reviewer_verdicts", "notifications_sent"):
        try:
            data[key] = json.loads(data[key]) if data[key] else None
        except (TypeError, json.JSONDecodeError) as exc:
            logger.warning("Bad JSON in column %s for session %s: %s", key, session_id, exc)
            data[key] = None
    return data
