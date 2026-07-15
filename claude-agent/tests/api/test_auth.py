"""
Auth dependency tests — opt-in bearer-token auth via AGENT_API_KEY.

When AGENT_API_KEY is unset  → all endpoints open (personal-tool default).
When AGENT_API_KEY is set    → protected endpoints require
                               `Authorization: Bearer <token>` or `?token=`.
"""

import pytest

_KEY = "test-secret-token"


# ── No key configured → everything open ───────────────────────────────────────

@pytest.mark.api
async def test_no_key_allows_get(client, monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    response = await client.get("/tree?depth=1")
    assert response.status_code == 200


@pytest.mark.api
async def test_no_key_allows_post(client, monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    response = await client.post("/reset_memory", json={"agent_id": "auth-test"})
    assert response.status_code == 200


# ── Key configured → token required ───────────────────────────────────────────

@pytest.mark.api
async def test_key_set_rejects_missing_token(client, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", _KEY)
    response = await client.get("/tree?depth=1")
    assert response.status_code == 401


@pytest.mark.api
async def test_key_set_rejects_wrong_bearer(client, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", _KEY)
    response = await client.get("/tree?depth=1", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


@pytest.mark.api
async def test_key_set_accepts_bearer_header(client, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", _KEY)
    response = await client.get("/tree?depth=1", headers={"Authorization": f"Bearer {_KEY}"})
    assert response.status_code == 200


@pytest.mark.api
async def test_key_set_accepts_query_token(client, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", _KEY)
    response = await client.get(f"/tree?depth=1&token={_KEY}")
    assert response.status_code == 200


@pytest.mark.api
async def test_key_set_protects_task(client, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", _KEY)
    response = await client.post("/task", json={"prompt": "hello"})
    assert response.status_code == 401


@pytest.mark.api
@pytest.mark.parametrize("path", [
    "/file?path=README.md",
    "/serve?path=README.md",
    "/image?path=x.png",
    "/repo",
    "/files",
    "/history/some-agent",
    "/analytics/summary",
])
async def test_key_set_protects_data_routes(client, monkeypatch, path):
    monkeypatch.setenv("AGENT_API_KEY", _KEY)
    response = await client.get(path)
    assert response.status_code == 401


@pytest.mark.api
async def test_key_set_root_redirect_stays_public(client, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", _KEY)
    response = await client.get("/", follow_redirects=False)
    assert response.status_code in (301, 302, 307)
