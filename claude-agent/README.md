# Claude Agent

A mobile-friendly web UI that lets you run Claude Code remotely — send prompts from your phone and watch every tool call stream live, exactly like using Claude Code in VS Code.

## How it works

```
Phone browser
    │  POST /task (prompt)
    ▼
FastAPI server  ──▶  claude --print --output-format stream-json
    │                    │
    │   SSE events ◀─────┘  (Write, Bash, Read, Glob… streamed live)
    ▼
Live feed in UI
```

Claude Code runs with full autonomy in the workspace directory. Every tool call streams to the phone UI in real time. Conversation history is maintained across prompts via `--resume <session_id>`.

## Structure

```
claude-agent/
  server.py          — FastAPI app (POST /task, GET /repo, GET /file, POST /reset_memory)
  claude_session.py  — Spawns claude CLI, streams events, manages session_ids per agent
  tools/
    filesystem.py    — read_file, write_file
    git.py           — git_status, git_diff, git_add, git_commit
    shell.py         — run_shell
    browser.py       — Playwright browser tools
  static/
    index.html       — Mobile UI (send prompt, live feed, repo view)
  requirements.txt
```

## Prerequisites

- Python 3.11+
- Claude Code CLI installed and authenticated (`claude --version` should work)

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

No API key needed — uses your Claude Pro subscription via the `claude` CLI.

## Run

```bash
uvicorn server:app --host 0.0.0.0 --port 8007 --app-dir claude-agent
```

Open `http://localhost:8007` (or your Tailscale address) from any device.

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/task` | POST `{prompt, agent_id}` | Run a prompt. Returns SSE stream of events. |
| `/reset_memory` | POST `{agent_id}` | Clear conversation history for an agent. |
| `/repo` | GET | Git status + diff for the workspace. |
| `/file` | GET `?path=` | Read a file from the workspace. |
