# Claude Agent Server

A minimal HTTP server that keeps a Claude CLI session alive and forwards
prompts to it over a persistent stdin/stdout pipe.

## Architecture

```
claude-agent/
  server.py          — FastAPI app, lifespan startup/shutdown
  claude_session.py  — persistent subprocess manager
  requirements.txt
```

**How it works**

1. On startup the server spawns `claude` as a background subprocess.
2. Each `POST /task` request writes the prompt to Claude's stdin and reads
   stdout until 2 seconds of silence (= end of response).
3. Requests are serialized with an `asyncio.Lock` — concurrent callers queue
   up automatically.
4. If Claude crashes it is restarted transparently before the next request.

## Prerequisites

- Python 3.11+
- [Claude CLI](https://docs.anthropic.com/claude/docs/claude-cli) installed and authenticated (`claude --version` should work)

## Setup

```bash
pip install -r requirements.txt
```

## Run

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

## Usage

### POST /task

```bash
curl -X POST http://localhost:8000/task \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is 2 + 2?"}'
```

```json
{"response": "4"}
```

## Tuning

| Setting | File | Default | When to change |
|---|---|---|---|
| `SILENCE_TIMEOUT` | `claude_session.py` | `2.0 s` | Raise if long responses get cut off |
| `timeout=120` | `claude_session.py` | `120 s` | Raise for very long tasks |

## Notes

- The server binds to `0.0.0.0` — reachable from any device on the same network.
- Find your local IP with `ipconfig` (Windows) and call `http://<your-ip>:8000/task`.
