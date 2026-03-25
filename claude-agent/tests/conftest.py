"""
Shared test fixtures and helpers for the claude-agent test suite.

Adds claude-agent/ to sys.path so server, claude_session, rate_limit,
and notifications are importable as top-level modules.
"""

import json
import os
import sys
import threading
import time

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────

# claude-agent/ directory (parent of this tests/ dir)
_AGENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

# Workspace root is the parent of claude-agent/
WORKSPACE_ROOT = os.path.abspath(os.path.join(_AGENT_DIR, ".."))


# ── Rate-limit state reset (autouse) ──────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_rate_limit_state():
    """
    Reset global rate-limit state before (and after) every test.

    Creates a fresh asyncio.Event each time to avoid stale-loop issues:
    asyncio.Event() is bound to the running loop when first awaited (Python 3.10+),
    so creating a new instance prevents cross-test loop contamination.
    """
    import asyncio
    import rate_limit as rl

    state = rl.get_state()
    state._watch_task = None
    state.is_limited = False
    state.reset_at = None
    state._cleared = asyncio.Event()   # fresh event — avoids stale-loop binding
    state._cleared.set()               # mark as "not limited"

    yield

    state.is_limited = False
    state.reset_at = None
    state._watch_task = None
    state._cleared = asyncio.Event()
    state._cleared.set()


# ── Claude-session history reset (autouse) ────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_claude_session():
    """Clear in-memory session + history between tests."""
    import claude_session as cs

    cs._agent_sessions.clear()
    cs._conversation_history.clear()
    yield
    cs._agent_sessions.clear()
    cs._conversation_history.clear()


# ── Notification mock (autouse) ────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_notifications(monkeypatch):
    """Prevent real network notifications during all tests."""
    import notifications

    async def _noop(msg: str) -> bool:
        return False

    monkeypatch.setattr(notifications, "send_notification", _noop)
    # Also patch the already-bound reference in server.py if loaded
    try:
        import server
        monkeypatch.setattr(server, "send_notification", _noop)
    except ImportError:
        pass


# ── In-process API client ──────────────────────────────────────────────────────

@pytest.fixture
async def client():
    """httpx AsyncClient backed by the FastAPI app (no live server)."""
    import httpx
    from server import app

    async with httpx.AsyncClient(app=app, base_url="http://test") as c:
        yield c


# ── Live uvicorn server for Playwright UI tests ────────────────────────────────

_LIVE_SERVER_PORT = 8791
_live_server_instance = None


@pytest.fixture(scope="session")
def live_server_url():
    """Start uvicorn in a daemon thread; return its base URL."""
    global _live_server_instance

    if _live_server_instance is not None:
        yield f"http://127.0.0.1:{_LIVE_SERVER_PORT}"
        return

    import uvicorn
    from server import app as _app

    config = uvicorn.Config(
        _app,
        host="127.0.0.1",
        port=_LIVE_SERVER_PORT,
        log_level="error",
    )
    srv = uvicorn.Server(config)
    _live_server_instance = srv

    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    time.sleep(1.5)   # wait for startup

    yield f"http://127.0.0.1:{_LIVE_SERVER_PORT}"

    srv.should_exit = True


# ── SSE wire-format helper ────────────────────────────────────────────────────

def _make_sse_body(events: list[dict]) -> str:
    """Format a list of dicts as SSE wire format."""
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


# ── Playwright route helpers (fixtures) ──────────────────────────────────────

@pytest.fixture
def route_task_sse():
    """
    Returns a helper that intercepts POST /task and replies with fake SSE events.

    Usage::

        def test_foo(ui_page, route_task_sse):
            route_task_sse(ui_page, [{"type": "done", "result": "ok"}])
            ui_page.locator("#prompt").fill("hello")
            ui_page.locator("#send-btn").click()
    """
    def _impl(page, events: list[dict]) -> None:
        body = _make_sse_body(events)

        def _handle(route):
            route.fulfill(
                status=200,
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
                body=body,
            )

        page.route("**/task", _handle)

    return _impl


@pytest.fixture
def route_json_endpoint():
    """
    Returns a helper that intercepts a URL glob and returns a JSON payload.

    Usage::

        route_json_endpoint(page, "**/rate_limit_status", {"is_limited": False})
    """
    def _impl(page, url_glob: str, payload: dict, status: int = 200) -> None:
        def _handle(route):
            route.fulfill(
                status=status,
                content_type="application/json",
                body=json.dumps(payload),
            )

        page.route(url_glob, _handle)

    return _impl


@pytest.fixture
def route_rate_limit_status():
    """
    Returns a helper that intercepts /rate_limit_status with multiple sequential responses.
    The last response is repeated when the list is exhausted.
    """
    def _impl(page, responses: list[dict]) -> None:
        calls = [0]

        def _handle(route):
            idx = min(calls[0], len(responses) - 1)
            calls[0] += 1
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(responses[idx]),
            )

        page.route("**/rate_limit_status", _handle)

    return _impl


# ── Shared Playwright page fixture (with default API mocks) ────────────────────

@pytest.fixture
def ui_page(page, live_server_url):
    """
    A Playwright page loaded with the Claude Agent UI.

    Pre-mocked endpoints (override per-test as needed):
      /history/* → {history: []}
      /repo       → minimal stub
      /tree       → single-dir stub
      /rate_limit_status → {is_limited: false, reset_at: null}
    """
    _stub_tree = json.dumps({
        "name": "workspace",
        "path": "",
        "type": "dir",
        "children": [{"name": "claude-agent", "path": "claude-agent", "type": "dir", "children": []}],
    })
    _stub_repo = json.dumps({
        "status": "## master...origin/master\n",
        "diff": "",
        "modified_files": [],
        "diff_file_count": 0,
    })
    _stub_rl = json.dumps({"is_limited": False, "reset_at": None})
    _stub_hist = json.dumps({"history": []})

    page.route("**/history/**", lambda r: r.fulfill(status=200, content_type="application/json", body=_stub_hist))
    page.route("**/repo", lambda r: r.fulfill(status=200, content_type="application/json", body=_stub_repo))
    page.route("**/tree**", lambda r: r.fulfill(status=200, content_type="application/json", body=_stub_tree))
    page.route("**/rate_limit_status", lambda r: r.fulfill(status=200, content_type="application/json", body=_stub_rl))

    page.goto(live_server_url)
    page.wait_for_load_state("networkidle")
    return page
