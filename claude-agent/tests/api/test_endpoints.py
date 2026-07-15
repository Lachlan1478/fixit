"""
API integration tests — all 8 GET + 2 POST routes.

Uses httpx.AsyncClient backed by the FastAPI app (no live server, no Claude).
stream_task is monkeypatched to an async generator for /task tests.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import claude_session as cs
import rate_limit as rl


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _make_stream(*events):
    """Return an async generator yielding the given event dicts."""
    for e in events:
        yield e


# ── Root redirect ─────────────────────────────────────────────────────────────

@pytest.mark.api
async def test_root_redirect(client):
    response = await client.get("/", follow_redirects=False)
    assert response.status_code in (307, 301, 302)
    assert response.headers["location"].endswith("index.html")


# ── /rate_limit_status ────────────────────────────────────────────────────────

@pytest.mark.api
async def test_rate_limit_status_clear(client):
    response = await client.get("/rate_limit_status")
    assert response.status_code == 200
    data = response.json()
    assert data["is_limited"] is False
    assert data["reset_at"] is None


@pytest.mark.api
async def test_rate_limit_status_limited(client):
    reset_dt = datetime(2099, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    rl.get_state().set_limited(reset_dt)

    response = await client.get("/rate_limit_status")
    assert response.status_code == 200
    data = response.json()
    assert data["is_limited"] is True
    assert data["reset_at"] is not None
    assert "2099" in data["reset_at"]


# ── /history/{agent_id} ───────────────────────────────────────────────────────

@pytest.mark.api
async def test_history_empty(client):
    response = await client.get("/history/some-fresh-agent")
    assert response.status_code == 200
    data = response.json()
    assert data == {"history": []}


@pytest.mark.api
async def test_history_with_turns(client):
    turns = [
        {"role": "user", "content": "hello", "ts": "2024-01-01T00:00:00+00:00"},
        {"role": "assistant", "content": "hi", "ts": "2024-01-01T00:00:01+00:00", "model": "sonnet"},
    ]
    cs._conversation_history["hist-test"] = turns

    response = await client.get("/history/hist-test")
    assert response.status_code == 200
    assert response.json()["history"] == turns


# ── /tree ─────────────────────────────────────────────────────────────────────

@pytest.mark.api
async def test_tree_structure(client):
    response = await client.get("/tree?depth=1")
    assert response.status_code == 200
    data = response.json()
    assert "name" in data
    assert data["type"] == "dir"
    assert "children" in data


@pytest.mark.api
async def test_tree_depth_clamped(client):
    # depth=99 should be clamped to 6, not error
    response = await client.get("/tree?depth=99")
    assert response.status_code == 200


# ── /files ────────────────────────────────────────────────────────────────────

@pytest.mark.api
async def test_files_root(client):
    response = await client.get("/files")
    assert response.status_code == 200
    data = response.json()
    assert "path" in data
    assert "dirs" in data
    assert "files" in data


# ── /repo ─────────────────────────────────────────────────────────────────────

@pytest.mark.api
async def test_repo_keys(client):
    response = await client.get("/repo")
    assert response.status_code == 200
    data = response.json()
    for key in ("status", "diff", "modified_files", "diff_file_count"):
        assert key in data


# ── /file ─────────────────────────────────────────────────────────────────────

@pytest.mark.api
async def test_file_not_found(client):
    response = await client.get("/file?path=nonexistent_file_xyz.txt")
    assert response.status_code == 200
    data = response.json()
    assert data["exists"] is False
    assert data["content"] is None


# ── /image ────────────────────────────────────────────────────────────────────

@pytest.mark.api
async def test_image_not_found(client):
    response = await client.get("/image?path=ghost.png")
    assert response.status_code == 404


@pytest.mark.api
async def test_image_bad_extension(client):
    response = await client.get("/image?path=claude-agent/server.py")
    assert response.status_code == 400


# ── POST /task ────────────────────────────────────────────────────────────────

@pytest.mark.api
async def test_task_empty_prompt(client):
    response = await client.post("/task", json={"prompt": "   "})
    assert response.status_code == 400


@pytest.mark.api
async def test_task_sse_stream(client, monkeypatch):
    """Monkeypatched stream_task → SSE content-type + events parsed."""
    async def _mock_stream(prompt, agent_id="default", model="sonnet", plan_mode=False):
        yield {"type": "status", "message": "Ready", "elapsed_ms": 50}
        yield {"type": "done", "result": "all done"}

    monkeypatch.setattr(cs, "stream_task", _mock_stream)

    response = await client.post("/task", json={"prompt": "hello"})
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]

    events = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))

    types = [e["type"] for e in events]
    assert "status" in types
    assert "done" in types


@pytest.mark.api
async def test_task_rate_limit_sets_state(client, monkeypatch):
    """When stream yields rate_limited, /rate_limit_status should reflect it."""
    from datetime import datetime, timezone, timedelta

    reset_dt = datetime.now(timezone.utc) + timedelta(hours=5)

    async def _mock_rl(prompt, agent_id="default", model="sonnet", plan_mode=False):
        yield {
            "type": "rate_limited",
            "reset_at": reset_dt.isoformat(),
            "message": "Usage limit reached",
        }

    monkeypatch.setattr(cs, "stream_task", _mock_rl)

    await client.post("/task", json={"prompt": "hello"})

    state = rl.get_state()
    assert state.is_limited is True
    assert state.reset_at is not None


# ── POST /reset_memory ────────────────────────────────────────────────────────

@pytest.mark.api
async def test_reset_memory(client):
    cs._conversation_history["my-bot"] = [{"role": "user", "content": "hi"}]
    cs._agent_sessions["my-bot"] = "sess-xyz"

    response = await client.post("/reset_memory", json={"agent_id": "my-bot"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "reset"

    assert cs.get_history("my-bot") == []
    assert "my-bot" not in cs._agent_sessions


@pytest.mark.api
async def test_reset_memory_empty_id(client):
    response = await client.post("/reset_memory", json={"agent_id": ""})
    assert response.status_code == 400
