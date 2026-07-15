"""
Shared test fixtures for the orchestrator test suite.

Adds orchestrator/ (and claude-agent/, which orchestrator modules import from)
to sys.path so pipeline, reviewer, and session_store are importable as
top-level modules — same pattern as claude-agent/tests/conftest.py.
"""

import os
import sys

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────

# orchestrator/ directory (parent of this tests/ dir)
_ORCH_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# claude-agent/ sibling dir — orchestrator modules import claude_session,
# rate_limit, and notifications from it.
_CLAUDE_AGENT_DIR = os.path.abspath(os.path.join(_ORCH_DIR, "..", "claude-agent"))

for _p in (_CLAUDE_AGENT_DIR, _ORCH_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── Notification mock (autouse) ────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_notifications(monkeypatch):
    """Prevent real network notifications during all tests."""
    import notifications

    async def _noop(msg: str) -> bool:
        return False

    monkeypatch.setattr(notifications, "send_notification", _noop)
    # Also patch the already-bound reference in pipeline.py if loaded
    try:
        import pipeline
        monkeypatch.setattr(pipeline, "send_notification", _noop)
    except ImportError:
        pass
