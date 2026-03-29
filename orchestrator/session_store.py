"""
session_store.py — In-memory pipeline session registry with SQLite persistence.

Sessions live in memory while active; completed/error sessions are persisted
to SQLite so they survive server restarts.
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_STORE: dict = {}  # session_id → PipelineSession (imported lazily to avoid circular)

_DB_PATH = os.path.join(os.path.dirname(__file__), "sessions.db")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _init_db() -> None:
    with sqlite3.connect(_DB_PATH) as conn:
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
    """Write session snapshot to SQLite (non-blocking; best effort)."""
    try:
        with sqlite3.connect(_DB_PATH) as conn:
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
