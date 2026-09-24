"""Reading Claude Code's own session store."""

import json
import os

import local_sessions


def _write(folder, sid, entries):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, f"{sid}.jsonl"), "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def test_project_dir_slug_matches_claude_code():
    assert local_sessions.project_dir("/Users/gazmart/code/lifetracker").endswith(
        "/.claude/projects/-Users-gazmart-code-lifetracker"
    )


def test_list_and_load_local_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(local_sessions, "CLAUDE_PROJECTS", str(tmp_path))
    cwd = "/tmp/proj"
    folder = local_sessions.project_dir(cwd)
    _write(folder, "aaaa-1", [
        {"type": "queue-operation", "sessionId": "aaaa-1"},
        {"type": "user", "timestamp": "2026-09-23T12:00:00Z",
         "message": {"role": "user", "content": "<command-name>/model</command-name> set"}},
        {"type": "user", "timestamp": "2026-09-23T12:01:00Z",
         "message": {"role": "user", "content": "Fix the login bug\nplease"}},
        {"type": "assistant", "timestamp": "2026-09-23T12:02:00Z",
         "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Read"}, {"type": "text", "text": "Done."}]}},
        {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "subagent noise"}},
    ])
    _write(folder, "bbbb-2", [{"type": "queue-operation", "sessionId": "bbbb-2"}])  # empty chat
    os.utime(os.path.join(folder, "aaaa-1.jsonl"), (1_700_000_000, 1_700_000_000))

    sessions = local_sessions.list_local_sessions(cwd)
    assert [s["session_id"] for s in sessions] == ["aaaa-1"]
    assert sessions[0]["title"] == "Fix the login bug"
    assert sessions[0]["turn_count"] == 2

    turns = local_sessions.load_local_turns(os.path.join(folder, "aaaa-1.jsonl"))
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["content"] == "Fix the login bug\nplease"
    assert turns[-1]["content"] == "Done."
    assert local_sessions.list_local_sessions("/nowhere/else") == []
