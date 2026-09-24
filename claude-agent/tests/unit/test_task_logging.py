"""Every prompt and response through the agent lands in logs/, on every exit path."""

import asyncio
import json
import os

import claude_session as cs


def _read(name):
    path = os.path.join(cs.LOGS_DIR, name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def test_prompt_is_logged_before_spawn_and_spawn_failure_writes_a_session_row(monkeypatch):
    async def missing(*argv, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(cs.asyncio, "create_subprocess_exec", missing)

    async def run():
        return [e async for e in cs.stream_task("log me", "logtest", "haiku", "auto", "/tmp", source="dashboard")]

    events = asyncio.run(run())
    assert events[-1]["type"] == "error"
    requested = [e for e in _read("events.jsonl") if e["event"] == "task_requested"]
    assert requested and requested[0]["prompt"] == "log me" and requested[0]["source"] == "dashboard"
    assert requested[0]["mode"] == "auto" and requested[0]["cwd"] == "/tmp"
    rows = _read("sessions.jsonl")
    assert rows and rows[-1]["prompt"] == "log me" and rows[-1]["error"].startswith("claude CLI not found")
    assert rows[-1]["source"] == "dashboard" and rows[-1]["model"] == cs._MODELS["haiku"]


def test_completed_run_logs_prompt_response_model_and_source(monkeypatch):
    class FakeStdin:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

    class FakeStream:
        def __init__(self, lines):
            self.lines = list(lines)

        async def readline(self):
            return self.lines.pop(0) if self.lines else b""

        async def read(self, n=-1):
            return b""

    class FakeProc:
        pid = 1
        returncode = None

        def __init__(self):
            self.stdin = FakeStdin()
            self.stdout = FakeStream([
                json.dumps({"type": "system", "session_id": "s-1", "model": "claude-haiku", "tools": []}).encode() + b"\n",
                json.dumps({"type": "assistant", "session_id": "s-1", "message": {"content": [
                    {"type": "thinking", "thinking": "the user wants pong"},
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/tmp/x.py"}},
                ]}}).encode() + b"\n",
                json.dumps({"type": "user", "session_id": "s-1", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "line1\nline2"}]},
                ]}}).encode() + b"\n",
                json.dumps({"type": "assistant", "session_id": "s-1", "message": {"content": [{"type": "text", "text": "pong"}], "usage": {"input_tokens": 5, "cache_read_input_tokens": 100}}}).encode() + b"\n",
                json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization": 0.1, "resetsAt": 1}}}}).encode() + b"\n",
                json.dumps({"type": "result", "session_id": "s-1", "result": "pong", "num_turns": 1}).encode() + b"\n",
            ])
            self.stderr = FakeStream([])

        async def wait(self):
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = -9

    async def fake_exec(*argv, **kwargs):
        return FakeProc()

    monkeypatch.setattr(cs.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(cs.analytics, "record_session", lambda *a, **k: None)

    async def run():
        return [e async for e in cs.stream_task("ping", "logtest2", "fable", "plan", "/tmp", source="dashboard")]

    events = asyncio.run(run())
    assert any(e["type"] == "done" and e["result"] == "pong" for e in events)
    meta = next(e for e in events if e["type"] == "meta")
    assert meta["model"] == "claude-haiku" and meta["context_window"] == cs.CONTEXT_WINDOW
    assert next(e for e in events if e["type"] == "context")["used"] == 105
    assert next(e for e in events if e["type"] == "limits")["five_hour"]["used_percentage"] == 10
    assert next(e for e in events if e["type"] == "thinking")["content"] == "the user wants pong"
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["name"] == "Read" and result["content"] == "line1\nline2" and result["truncated"] is False
    row = _read("sessions.jsonl")[-1]
    assert (row["prompt"].endswith("Task: ping") or row["prompt"] == "ping")
    assert row["result"] == "pong" and row["assistant_text"] == "pong"
    assert row["source"] == "dashboard" and row["mode"] == "plan" and row["model"] == cs._MODELS["fable"]
    assert row["error"] is None
