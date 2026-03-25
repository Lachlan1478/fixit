"""
analytics.py — SQLite-backed analytics store for claude-agent.

Schema
------
  sessions         — one row per completed task (timing, cost, outcome)
  tool_calls       — one row per tool call within a session
  telemetry        — UI interaction events posted from the browser
  rate_limit_events — every rate-limit hit and its resolution

All writes are fire-and-forget (best-effort); failures are logged but never
propagated to callers so analytics never breaks the main flow.
"""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_DB_PATH: str | None = None


def init_db(logs_dir: str) -> None:
    """Create the analytics DB and all tables (idempotent)."""
    global _DB_PATH
    os.makedirs(logs_dir, exist_ok=True)
    _DB_PATH = os.path.join(logs_dir, "analytics.db")

    with _conn() as conn:
        conn.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous  = NORMAL;

        CREATE TABLE IF NOT EXISTS sessions (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                   TEXT    NOT NULL,
            agent_id             TEXT    NOT NULL,
            model                TEXT,
            is_resume            INTEGER,
            prompt_len           INTEGER,
            prompt_snippet       TEXT,
            total_ms             REAL,
            spawn_ms             REAL,
            inference_ms         REAL,
            tool_call_count      INTEGER,
            tools_used           TEXT,       -- JSON array of names
            num_turns            INTEGER,
            total_cost_usd       REAL,
            input_tokens         INTEGER,
            output_tokens        INTEGER,
            exit_code            INTEGER,
            had_stderr           INTEGER,
            outcome              TEXT        -- success | error | rate_limited
        );

        CREATE TABLE IF NOT EXISTS tool_calls (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_row_id  INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
            ts              TEXT,
            tool_name       TEXT,
            elapsed_ms      REAL,
            exec_ms         REAL,
            is_error        INTEGER,
            input_snippet   TEXT
        );

        CREATE TABLE IF NOT EXISTS telemetry (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT,
            server_ts   TEXT,
            session_key TEXT,
            agent_id    TEXT,
            event_type  TEXT,
            payload     TEXT    -- JSON blob
        );

        CREATE TABLE IF NOT EXISTS rate_limit_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT,
            agent_id        TEXT,
            reset_at        TEXT,
            actual_clear_ts TEXT,
            wait_secs       REAL
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_ts      ON sessions(ts);
        CREATE INDEX IF NOT EXISTS idx_sessions_agent   ON sessions(agent_id);
        CREATE INDEX IF NOT EXISTS idx_tool_calls_name  ON tool_calls(tool_name);
        CREATE INDEX IF NOT EXISTS idx_telemetry_event  ON telemetry(event_type);
        CREATE INDEX IF NOT EXISTS idx_telemetry_ts     ON telemetry(ts);
        """)
    logger.info("Analytics DB ready: %s", _DB_PATH)


# ── Connection helper ──────────────────────────────────────────────────────────

@contextmanager
def _conn():
    if not _DB_PATH:
        raise RuntimeError("analytics.init_db() not called")
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Write helpers ──────────────────────────────────────────────────────────────

def record_session(summary: dict, tool_call_list: list[dict]) -> None:
    """Persist a completed task summary + its tool calls."""
    if not _DB_PATH:
        return
    try:
        # Determine outcome
        exit_code = summary.get("exit_code", 0)
        had_rate_limit = any(
            t.get("name") == "rate_limited" for t in tool_call_list
        )
        if had_rate_limit:
            outcome = "rate_limited"
        elif exit_code != 0:
            outcome = "error"
        else:
            outcome = "success"

        tools_json = json.dumps(summary.get("tools_used", []))

        with _conn() as conn:
            cur = conn.execute(
                """INSERT INTO sessions
                   (ts, agent_id, model, is_resume, prompt_len, prompt_snippet,
                    total_ms, spawn_ms, inference_ms, tool_call_count, tools_used,
                    num_turns, total_cost_usd, input_tokens, output_tokens,
                    exit_code, had_stderr, outcome)
                   VALUES (?,?,?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?)""",
                (
                    summary.get("ts"),
                    summary.get("agent_id"),
                    summary.get("model"),
                    int(bool(summary.get("is_resume"))),
                    summary.get("prompt_len"),
                    summary.get("prompt_snippet"),
                    summary.get("total_ms"),
                    summary.get("spawn_ms"),
                    summary.get("inference_ms"),
                    summary.get("tool_call_count"),
                    tools_json,
                    summary.get("num_turns"),
                    summary.get("total_cost_usd"),
                    summary.get("input_tokens"),
                    summary.get("output_tokens"),
                    exit_code,
                    int(bool(summary.get("had_stderr"))),
                    outcome,
                ),
            )
            session_row_id = cur.lastrowid

            for tc in tool_call_list:
                conn.execute(
                    """INSERT INTO tool_calls
                       (session_row_id, ts, tool_name, elapsed_ms, exec_ms,
                        is_error, input_snippet)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        session_row_id,
                        tc.get("ts"),
                        tc.get("name"),
                        tc.get("elapsed_ms"),
                        tc.get("exec_ms"),
                        int(bool(tc.get("is_error"))),
                        tc.get("input_snippet", "")[:200],
                    ),
                )
    except Exception as exc:
        logger.error("analytics.record_session failed: %s", exc)


def record_rate_limit(
    agent_id: str,
    reset_at: str | None,
    actual_clear_ts: str | None = None,
    wait_secs: float | None = None,
) -> None:
    """Record a rate-limit hit. Call again to update actual_clear_ts."""
    if not _DB_PATH:
        return
    try:
        ts = datetime.now(timezone.utc).isoformat()
        with _conn() as conn:
            conn.execute(
                """INSERT INTO rate_limit_events
                   (ts, agent_id, reset_at, actual_clear_ts, wait_secs)
                   VALUES (?,?,?,?,?)""",
                (ts, agent_id, reset_at, actual_clear_ts, wait_secs),
            )
    except Exception as exc:
        logger.error("analytics.record_rate_limit failed: %s", exc)


def record_telemetry(
    session_key: str,
    agent_id: str,
    event_type: str,
    payload: dict,
    client_ts: str | None = None,
) -> None:
    """Store a UI telemetry event."""
    if not _DB_PATH:
        return
    try:
        server_ts = datetime.now(timezone.utc).isoformat()
        with _conn() as conn:
            conn.execute(
                """INSERT INTO telemetry
                   (ts, server_ts, session_key, agent_id, event_type, payload)
                   VALUES (?,?,?,?,?,?)""",
                (
                    client_ts or server_ts,
                    server_ts,
                    session_key,
                    agent_id,
                    event_type,
                    json.dumps(payload),
                ),
            )
    except Exception as exc:
        logger.error("analytics.record_telemetry failed: %s", exc)


# ── Query helpers ──────────────────────────────────────────────────────────────

def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def query_summary() -> dict:
    """High-level aggregate stats for the dashboard summary cards."""
    if not _DB_PATH:
        return {}
    try:
        with _conn() as conn:
            sess = conn.execute("""
                SELECT
                    COUNT(*)                                    AS total_sessions,
                    COALESCE(SUM(total_cost_usd), 0)           AS total_cost_usd,
                    COALESCE(AVG(total_ms), 0)                 AS avg_total_ms,
                    COALESCE(AVG(tool_call_count), 0)          AS avg_tools_per_session,
                    SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END)      AS success_count,
                    SUM(CASE WHEN outcome='error' THEN 1 ELSE 0 END)        AS error_count,
                    SUM(CASE WHEN outcome='rate_limited' THEN 1 ELSE 0 END) AS rate_limited_count,
                    COALESCE(SUM(input_tokens), 0)             AS total_input_tokens,
                    COALESCE(SUM(output_tokens), 0)            AS total_output_tokens
                FROM sessions
            """).fetchone()

            tool_count = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
            tel_count  = conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
            rl_count   = conn.execute("SELECT COUNT(*) FROM rate_limit_events").fetchone()[0]

            # Sessions in last 7 days
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            recent = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE ts >= ?", (cutoff,)
            ).fetchone()[0]

        return {
            **dict(sess),
            "total_tool_calls": tool_count,
            "total_telemetry_events": tel_count,
            "total_rate_limit_events": rl_count,
            "sessions_last_7d": recent,
        }
    except Exception as exc:
        logger.error("analytics.query_summary failed: %s", exc)
        return {}


def query_sessions(limit: int = 50, offset: int = 0) -> list[dict]:
    """Recent sessions, most-recent first."""
    if not _DB_PATH:
        return []
    try:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT id, ts, agent_id, model, outcome, total_ms,
                          tool_call_count, total_cost_usd, num_turns,
                          prompt_snippet, had_stderr, exit_code
                   FROM sessions ORDER BY ts DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return _rows_to_dicts(rows)
    except Exception as exc:
        logger.error("analytics.query_sessions failed: %s", exc)
        return []


def query_tools(limit: int = 30) -> list[dict]:
    """Tool usage frequency + error rate + median exec_ms."""
    if not _DB_PATH:
        return []
    try:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT
                       tool_name,
                       COUNT(*)                                   AS call_count,
                       SUM(is_error)                              AS error_count,
                       ROUND(AVG(exec_ms), 1)                    AS avg_exec_ms,
                       ROUND(AVG(elapsed_ms), 1)                 AS avg_elapsed_ms
                   FROM tool_calls
                   GROUP BY tool_name
                   ORDER BY call_count DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return _rows_to_dicts(rows)
    except Exception as exc:
        logger.error("analytics.query_tools failed: %s", exc)
        return []


def query_perf() -> dict:
    """Timing distribution for session total_ms."""
    if not _DB_PATH:
        return {}
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT total_ms FROM sessions WHERE total_ms IS NOT NULL ORDER BY total_ms"
            ).fetchall()
        if not rows:
            return {}
        vals = [r[0] for r in rows]
        n = len(vals)

        def pct(p):
            idx = max(0, min(n - 1, int(p / 100 * n)))
            return round(vals[idx], 1)

        return {
            "count": n,
            "p25_ms": pct(25),
            "p50_ms": pct(50),
            "p75_ms": pct(75),
            "p90_ms": pct(90),
            "p99_ms": pct(99),
            "min_ms": round(vals[0], 1),
            "max_ms": round(vals[-1], 1),
            "mean_ms": round(sum(vals) / n, 1),
        }
    except Exception as exc:
        logger.error("analytics.query_perf failed: %s", exc)
        return {}


def query_cost(days: int = 30) -> list[dict]:
    """Daily cost + token usage for the last N days."""
    if not _DB_PATH:
        return []
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with _conn() as conn:
            rows = conn.execute(
                """SELECT
                       SUBSTR(ts, 1, 10)              AS day,
                       COUNT(*)                        AS sessions,
                       ROUND(SUM(total_cost_usd), 6)  AS cost_usd,
                       SUM(input_tokens)               AS input_tokens,
                       SUM(output_tokens)              AS output_tokens
                   FROM sessions
                   WHERE ts >= ? AND total_cost_usd IS NOT NULL
                   GROUP BY day
                   ORDER BY day""",
                (cutoff,),
            ).fetchall()
        return _rows_to_dicts(rows)
    except Exception as exc:
        logger.error("analytics.query_cost failed: %s", exc)
        return []


def query_telemetry_summary() -> list[dict]:
    """UI event counts grouped by event_type."""
    if not _DB_PATH:
        return []
    try:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT event_type, COUNT(*) AS count
                   FROM telemetry
                   GROUP BY event_type
                   ORDER BY count DESC""",
            ).fetchall()
        return _rows_to_dicts(rows)
    except Exception as exc:
        logger.error("analytics.query_telemetry_summary failed: %s", exc)
        return []


def query_rate_limit_events(limit: int = 20) -> list[dict]:
    """Recent rate-limit events."""
    if not _DB_PATH:
        return []
    try:
        with _conn() as conn:
            rows = conn.execute(
                """SELECT * FROM rate_limit_events ORDER BY ts DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return _rows_to_dicts(rows)
    except Exception as exc:
        logger.error("analytics.query_rate_limit_events failed: %s", exc)
        return []
