"""
API integration tests — all 8 GET + 2 POST routes.

Uses httpx.AsyncClient backed by the FastAPI app (no live server, no Claude).
stream_task is monkeypatched to an async generator for /task tests.
"""

import asyncio
import json
import os
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
    assert data["history"] == []
    assert data["stats"]["runs"] == 0 and data["stats"]["cost_usd"] == 0


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
    async def _mock_stream(prompt, agent_id="default", model="sonnet", plan_mode=False, **kwargs):
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

    async def _mock_rl(prompt, agent_id, model, mode, cwd, source="phone"):
        yield {
            "type": "rate_limited",
            "reset_at": reset_dt.isoformat(),
            "message": "Usage limit reached",
        }

    monkeypatch.setattr(cs, "_stream_task_impl", _mock_rl)

    response = await client.post("/task", json={"prompt": "hello", "model": "opus"})
    event = json.loads(response.text.split("data: ", 1)[1].split("\n")[0])

    state = rl.get_state()
    assert state.is_limited is True
    assert state.reset_at is not None
    assert event["queued"] == 1 and event["queued_id"] == state.queue[0]["id"]
    assert state.queue[0]["prompt"] == "hello" and state.queue[0]["model"] == "opus"
    assert state.queue[0]["cwd"] == cs.SESSION_CWD
    state._watch_task.cancel()


@pytest.mark.api
async def test_task_while_limited_is_queued_not_run(client, monkeypatch):
    called = []

    async def _never(*a, **kw):
        called.append(1)
        yield {"type": "done", "result": "x"}

    monkeypatch.setattr(cs, "stream_task", _never)
    reset_dt = datetime(2099, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    rl.get_state().set_limited(reset_dt)

    response = await client.post("/task", json={"prompt": "later", "cwd": "claude-agent"})
    body = response.json()

    assert response.status_code == 200 and body["queued"] is True and body["position"] == 1
    assert body["reset_at"] == reset_dt.isoformat()
    assert called == []
    assert rl.get_state().queue[0]["cwd"].endswith("claude-agent")

    listing = (await client.get("/queue")).json()
    assert listing["queued"] == 1 and listing["queue"][0]["id"] == body["id"]
    assert (await client.delete(f"/queue/{body['id']}")).status_code == 200
    assert (await client.delete(f"/queue/{body['id']}")).status_code == 404
    assert (await client.get("/rate_limit_status")).json()["queued"] == 0


@pytest.mark.api
async def test_task_cwd_resolved_inside_workspace(client, monkeypatch):
    seen = {}

    async def _mock(prompt, agent_id="default", model="sonnet", mode="auto", cwd=None, **kwargs):
        seen["cwd"] = cwd
        yield {"type": "done", "result": "ok"}

    monkeypatch.setattr(cs, "stream_task", _mock)
    import server

    response = await client.post("/task", json={"prompt": "hi", "cwd": "claude-agent"})
    assert response.status_code == 200
    assert seen["cwd"] == os.path.join(os.path.realpath(server.WORKSPACE_ROOT), "claude-agent")

    outside = await client.post("/task", json={"prompt": "hi", "cwd": "../.."})
    assert outside.status_code == 403
    missing = await client.post("/task", json={"prompt": "hi", "cwd": "no-such-dir"})
    assert missing.status_code == 404


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


@pytest.mark.api
async def test_local_sessions_endpoints(client, monkeypatch, tmp_path):
    import local_sessions

    monkeypatch.setattr(local_sessions, "CLAUDE_PROJECTS", str(tmp_path))
    import server

    cwd = os.path.join(os.path.realpath(server.WORKSPACE_ROOT), "claude-agent")
    folder = local_sessions.project_dir(cwd)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "0123abcd-0000-0000-0000-000000000000.jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "user", "timestamp": "t", "message": {"role": "user", "content": "hello there"}}) + "\n")

    listed = await client.get("/sessions/local", params={"cwd": "claude-agent"})
    assert listed.status_code == 200
    assert listed.json()["sessions"][0]["title"] == "hello there"

    opened = await client.post("/sessions/open-local", json={"agent_id": "loc", "session_id": "0123abcd-0000-0000-0000-000000000000", "cwd": "claude-agent"})
    assert opened.status_code == 200
    assert opened.json()["history"][0]["content"] == "hello there"
    assert cs._agent_sessions["loc"] == "0123abcd-0000-0000-0000-000000000000"
    assert cs._agent_session_cwd["loc"] == cwd

    assert (await client.post("/sessions/open-local", json={"session_id": "../../etc", "cwd": "claude-agent"})).status_code == 400
    assert (await client.post("/sessions/open-local", json={"session_id": "deadbeef-0000-0000-0000-000000000000", "cwd": "claude-agent"})).status_code == 404
    assert (await client.get("/sessions/local", params={"cwd": "../.."})).status_code == 403
    cs.reset_session("loc")


@pytest.mark.api
async def test_logs_tasks_endpoint_returns_recent_rows_newest_first(client):
    os.makedirs(cs.LOGS_DIR, exist_ok=True)
    with open(os.path.join(cs.LOGS_DIR, "sessions.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "1", "agent_id": "a", "source": "phone", "prompt": "one", "result": "r1"}) + "\n")
        fh.write(json.dumps({"ts": "2", "agent_id": "a", "source": "dashboard", "prompt": "two", "result": "r2"}) + "\n")
    resp = await client.get("/logs/tasks")
    assert resp.status_code == 200
    assert [t["prompt"] for t in resp.json()["tasks"]] == ["two", "one"]
    only = await client.get("/logs/tasks", params={"source": "dashboard"})
    assert [t["prompt"] for t in only.json()["tasks"]] == ["two"]


@pytest.mark.api
async def test_task_stream_sends_keepalives_during_silence(client, monkeypatch):
    import server

    monkeypatch.setattr(server, "KEEPALIVE_SECONDS", 0.05)

    async def _slow(prompt, agent_id="default", model="sonnet", mode="auto", **kwargs):
        yield {"type": "status", "message": "Ready"}
        await asyncio.sleep(0.2)
        yield {"type": "done", "result": "ok"}

    monkeypatch.setattr(cs, "stream_task", _slow)
    response = await client.post("/task", json={"prompt": "hello"})
    assert response.status_code == 200
    assert response.text.count(": keepalive") >= 2
    assert '"type": "done"' in response.text


def _usage(used: int) -> dict:
    return {"five_hour": {"used_percentage": used, "resets_at": 4102444800}}


@pytest.mark.api
async def test_task_defer_runs_when_usage_under_cap(client, monkeypatch):
    ran = []

    async def _run(prompt, agent_id="default", model="sonnet", mode="auto", **kw):
        ran.append(prompt)
        yield {"type": "done", "result": "ok"}

    monkeypatch.setattr(cs, "stream_task", _run)
    monkeypatch.setattr(cs, "_last_limits", _usage(20))

    body = (await client.post("/task", json={"prompt": "later", "defer": True, "max_usage": 50})).json()
    assert body["queued"] is True and body["is_limited"] is False and body["max_usage"] == 50
    await asyncio.gather(*rl._kick_tasks)

    assert ran == ["later"] and rl.get_state().queue == []


@pytest.mark.api
async def test_task_defer_held_while_usage_over_cap(client, monkeypatch):
    called = []

    async def _never(*a, **kw):
        called.append(1)
        yield {"type": "done", "result": "x"}

    monkeypatch.setattr(cs, "stream_task", _never)
    monkeypatch.setattr(cs, "_last_limits", _usage(80))

    body = (await client.post("/task", json={"prompt": "later", "defer": True, "max_usage": 50})).json()
    await asyncio.gather(*rl._kick_tasks)
    state = rl.get_state()

    assert called == [] and state.queue[0]["max_usage"] == 50 and state.is_limited is False
    assert state._watch_task is not None and not state._watch_task.done()
    listing = (await client.get("/queue")).json()
    assert listing["usage"]["used_percentage"] == 80 and listing["queue"][0]["id"] == body["id"]
    assert (await client.get("/rate_limit_status")).json()["queued"] == 1
    state._watch_task.cancel()


@pytest.mark.api
async def test_task_max_usage_validated(client):
    assert (await client.post("/task", json={"prompt": "x", "defer": True, "max_usage": 150})).status_code == 422


@pytest.mark.api
async def test_task_live_replays_and_follows_then_204_when_idle(client, monkeypatch):
    live = cs.LiveRun()
    live.add({"type": "status", "message": "a"})
    live.add({"type": "text", "content": "b"})
    monkeypatch.setitem(cs._live_runs, "reattach", live)

    async def finish_soon():
        await asyncio.sleep(0.05)
        live.add({"type": "done", "result": "c"})
        live.finish()

    asyncio.ensure_future(finish_soon())
    resp = await client.get("/task/live/reattach", params={"since": 1})
    assert resp.status_code == 200
    types = [json.loads(l[6:])["type"] for l in resp.text.splitlines() if l.startswith("data: ")]
    assert types == ["text", "done"]
    assert (await client.get("/task/live/nobody-here")).status_code == 204


@pytest.mark.api
async def test_stop_and_config(client, monkeypatch):
    import server

    assert (await client.post("/task/stop/nobody")).json() == {"stopped": False}
    monkeypatch.setattr(cs, "stop_run", lambda agent_id: agent_id == "busy")
    assert (await client.post("/task/stop/busy")).json() == {"stopped": True}
    assert (await client.get("/config")).json() == {"session_cwd": cs.SESSION_CWD, "workspace": server.WORKSPACE_ROOT}


@pytest.mark.api
async def test_keepalive_lets_a_repeated_cancel_propagate():
    import server

    closed = []

    async def slow():
        try:
            await asyncio.sleep(10)
            yield {"type": "never"}
        finally:
            closed.append(1)
            await asyncio.sleep(0.05)

    async def consume():
        async for _ in server._with_keepalive(slow(), 5):
            pass

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.1)
    assert task.cancelled() and closed == [1]
