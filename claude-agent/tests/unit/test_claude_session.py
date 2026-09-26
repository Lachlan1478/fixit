"""
Unit tests for claude_session.py — pure Python, no subprocess, no server.

Tests cover:
- _summarise_tool(): human-readable summaries for all tool types
- get_history(): per-agent conversation history
- reset_session(): clearing history + session_id
"""

import pytest

import claude_session as cs
from claude_session import _summarise_tool, get_history, reset_session


# ── _summarise_tool tests ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_summarise_write():
    result = _summarise_tool("Write", {"file_path": "foo.py"})
    assert result == "Writing foo.py"


@pytest.mark.unit
def test_summarise_edit():
    result = _summarise_tool("Edit", {"file_path": "bar.ts"})
    assert result == "Editing bar.ts"


@pytest.mark.unit
def test_summarise_multiedit():
    result = _summarise_tool("MultiEdit", {"file_path": "baz.js"})
    assert result == "Editing baz.js"


@pytest.mark.unit
def test_summarise_read():
    result = _summarise_tool("Read", {"file_path": "README.md"})
    assert result == "Reading README.md"


@pytest.mark.unit
def test_summarise_bash():
    result = _summarise_tool("Bash", {"command": "npm install"})
    assert result == "Running: npm install"


@pytest.mark.unit
def test_summarise_bash_truncates():
    long_cmd = "x" * 200
    result = _summarise_tool("Bash", {"command": long_cmd})
    assert result == "Running: " + "x" * 80


@pytest.mark.unit
def test_summarise_glob():
    result = _summarise_tool("Glob", {"pattern": "**/*.ts"})
    assert result == "Searching: **/*.ts"


@pytest.mark.unit
def test_summarise_grep():
    result = _summarise_tool("Grep", {"pattern": "TODO"})
    assert result == "Searching: TODO"


@pytest.mark.unit
def test_summarise_webfetch():
    result = _summarise_tool("WebFetch", {"url": "https://example.com/docs"})
    assert result == "Fetching: https://example.com/docs"


@pytest.mark.unit
def test_summarise_websearch():
    result = _summarise_tool("WebSearch", {"query": "pytest asyncio fixtures"})
    assert result == "Searching web: pytest asyncio fixtures"


@pytest.mark.unit
def test_summarise_ls():
    result = _summarise_tool("LS", {"path": "/tmp"})
    assert result == "Listing"


@pytest.mark.unit
def test_summarise_unknown():
    result = _summarise_tool("MyCustomTool", {"foo": "bar"})
    assert result == "MyCustomTool"


# ── get_history tests ─────────────────────────────────────────────────────────

@pytest.mark.unit
def test_get_history_empty():
    result = get_history("fresh-agent-xyz")
    assert result == []


@pytest.mark.unit
def test_get_history_returns_turns():
    turns = [
        {"role": "user", "content": "hello", "ts": "2024-01-01T00:00:00+00:00"},
        {"role": "assistant", "content": "hi", "ts": "2024-01-01T00:00:01+00:00", "model": "sonnet"},
    ]
    cs._conversation_history["test-agent-abc"] = turns
    result = get_history("test-agent-abc")
    assert result == turns


# ── reset_session tests ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_reset_clears_history():
    cs._conversation_history["my-agent"] = [
        {"role": "user", "content": "do something", "ts": "2024-01-01T00:00:00+00:00"},
    ]
    cs._agent_sessions["my-agent"] = "sess-abc123"

    reset_session("my-agent")

    assert get_history("my-agent") == []
    assert "my-agent" not in cs._agent_sessions


@pytest.mark.unit
def test_reset_unknown_agent():
    # Should silently succeed even if the agent never existed
    reset_session("nonexistent-agent-id-never-used")


@pytest.mark.unit
def test_reset_only_affects_target_agent():
    cs._conversation_history["agent-a"] = [{"role": "user", "content": "a"}]
    cs._conversation_history["agent-b"] = [{"role": "user", "content": "b"}]

    reset_session("agent-a")

    assert get_history("agent-a") == []
    assert get_history("agent-b") == [{"role": "user", "content": "b"}]


# ── _get_agent_lock tests ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_agent_lock_is_stable_per_agent():
    lock_a = cs._get_agent_lock("agent-a")
    assert cs._get_agent_lock("agent-a") is lock_a


@pytest.mark.unit
def test_agent_lock_differs_between_agents():
    assert cs._get_agent_lock("agent-a") is not cs._get_agent_lock("agent-b")


# ── _idle_timeout_s tests ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_idle_timeout_default(monkeypatch):
    monkeypatch.delenv("CLAUDE_IDLE_TIMEOUT_S", raising=False)
    assert cs._idle_timeout_s() == cs._DEFAULT_IDLE_TIMEOUT_S


@pytest.mark.unit
def test_idle_timeout_from_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_IDLE_TIMEOUT_S", "42.5")
    assert cs._idle_timeout_s() == 42.5


@pytest.mark.unit
def test_idle_timeout_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("CLAUDE_IDLE_TIMEOUT_S", "not-a-number")
    assert cs._idle_timeout_s() == cs._DEFAULT_IDLE_TIMEOUT_S


# ── system prompt tests ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_system_prompt_pins_absolute_workspace_root():
    """The system prompt must state the absolute workspace root so Claude does
    not guess a similarly-named sibling directory (regression: files were
    written to ...\claude-phone instead of ...\claude_phone)."""
    assert cs.WORKSPACE_ROOT in cs._SYSTEM_PROMPT
    # No unrendered f-string placeholders left behind.
    assert "{" not in cs._SYSTEM_PROMPT and "}" not in cs._SYSTEM_PROMPT


def test_resume_is_dropped_when_cwd_changes(monkeypatch):
    """A session created in one folder is never resumed from another (the CLI
    keys sessions by project directory)."""
    import asyncio

    cs._agent_sessions["cwd-test"] = "sid-1"
    cs._agent_session_cwd["cwd-test"] = "/tmp/folder-a"
    captured = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        raise FileNotFoundError  # stop before a real spawn

    monkeypatch.setattr(cs.asyncio, "create_subprocess_exec", fake_exec)

    async def run():
        return [e async for e in cs._stream_task_impl("hi", "cwd-test", "haiku", "auto", "/tmp/folder-b")]

    asyncio.run(run())
    assert "--resume" not in captured["argv"]
    assert "/tmp/folder-b" in " ".join(captured["argv"])

    async def run_same():
        return [e async for e in cs._stream_task_impl("hi", "cwd-test", "haiku", "auto", "/tmp/folder-a")]

    asyncio.run(run_same())
    assert "--resume" in captured["argv"] and "sid-1" in captured["argv"]
    cs.reset_session("cwd-test")
    assert "cwd-test" not in cs._agent_session_cwd


def test_accept_edits_keeps_bash_for_settings_allow_rules(monkeypatch):
    """acceptEdits leaves Bash available so settings allow rules can apply."""
    import asyncio

    captured = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        raise FileNotFoundError

    monkeypatch.setattr(cs.asyncio, "create_subprocess_exec", fake_exec)

    async def run():
        return [e async for e in cs._stream_task_impl("hi", "ae-test", "haiku", "acceptEdits", "/tmp")]

    asyncio.run(run())
    argv = captured["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--disallowedTools" not in argv


def test_stream_task_keeps_running_after_consumer_disconnects(monkeypatch):
    import asyncio

    finished = {"done": False}

    async def fake_impl(prompt, agent_id, model, mode, cwd, source="phone"):
        yield {"type": "status", "message": "Ready"}
        await asyncio.sleep(0.05)
        finished["done"] = True
        yield {"type": "done", "result": "ok"}

    monkeypatch.setattr(cs, "_stream_task_impl", fake_impl)

    async def run():
        gen = cs.stream_task("hi", "detach-test", "haiku", "auto")
        first = await gen.__anext__()
        await gen.aclose()  # the browser went away after the first event
        assert first["type"] == "status"
        await asyncio.sleep(0.2)
        return finished["done"]

    assert asyncio.run(run()) is True
