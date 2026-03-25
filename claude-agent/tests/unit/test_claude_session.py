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
