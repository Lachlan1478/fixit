import asyncio
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import analytics
import claude_session as cs
import rate_limit as rl
from notifications import send_notification

# Browsable/containment root for the file Explorer and file endpoints.
# Defaults to the repo root (parent of claude-agent); override with AGENT_WORKSPACE
# to expose a wider tree (e.g. the whole code directory).
WORKSPACE_ROOT = os.path.abspath(os.environ.get("AGENT_WORKSPACE") or os.path.join(os.path.dirname(__file__), ".."))
from tools.filesystem import read_file as _read_file
from tools.git import git_diff as _git_diff, git_status as _git_status

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


_LOGS_DIR = os.environ.get("AGENT_LOGS_DIR") or os.path.join(os.path.dirname(__file__), "logs")


@asynccontextmanager
async def lifespan(app: FastAPI):
    analytics.init_db(_LOGS_DIR)
    cs.hydrate_state()  # restore each agent's latest chat from disk (survives restarts)
    if not os.environ.get("AGENT_API_KEY"):
        logger.warning(
            "AGENT_API_KEY is not set — API endpoints are unauthenticated "
            "(personal-tool default). Set AGENT_API_KEY to require a bearer token."
        )
    yield


app = FastAPI(lifespan=lifespan)


# ── Auth (opt-in bearer token) ────────────────────────────────────────────────

async def require_token(request: Request) -> None:
    """
    Opt-in bearer-token auth. Enforced only when AGENT_API_KEY is set (non-empty).

    Accepts either an `Authorization: Bearer <token>` header or a `?token=`
    query parameter. Comparison is constant-time via secrets.compare_digest.
    """
    api_key = os.environ.get("AGENT_API_KEY", "")
    if not api_key:
        return  # personal-tool default: no auth configured

    supplied = ""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        supplied = auth_header[len("bearer "):].strip()
    if not supplied:
        supplied = request.query_params.get("token", "")

    if not supplied or not secrets.compare_digest(supplied.encode(), api_key.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing API token")


_PROTECTED = [Depends(require_token)]

app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)


@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")


KEEPALIVE_SECONDS = 15.0


async def _with_keepalive(stream, every: float):
    """Yield the stream's events, and None whenever `every` seconds pass without one."""
    it = stream.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(it.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=every)
            if not done:
                yield None
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield event
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
        await it.aclose()


class TaskRequest(BaseModel):
    prompt: str
    agent_id: str = "default"
    model: str = "opus"  # haiku | sonnet | opus | fable
    mode: str = "auto"   # auto | acceptEdits | plan
    plan_mode: bool = False  # legacy alias; when true, forces mode="plan"
    cwd: str | None = None   # workspace-relative folder Claude runs in (default AGENT_HOME)
    source: str = "phone"    # phone | dashboard — recorded with every prompt/response


class ResetMemoryRequest(BaseModel):
    agent_id: str


@app.post("/task", dependencies=_PROTECTED)
async def run_task(request: TaskRequest):
    """Stream Claude's work as SSE. Each event is 'data: <json>\\n\\n'."""
    if not request.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    agent_id = request.agent_id.strip() or "default"
    mode = "plan" if request.plan_mode else request.mode
    task_kwargs = {"source": (request.source or "phone")[:32]}
    if request.cwd:
        cwd = _resolve_workspace_path(request.cwd, outside_status=403)
        if not os.path.isdir(cwd):
            raise HTTPException(status_code=404, detail="cwd is not a directory")
        task_kwargs["cwd"] = cwd

    async def event_stream():
        t0 = time.monotonic()
        event_count = 0
        logger.info("Request | agent=%s mode=%s prompt=%r", agent_id, mode, request.prompt[:60])
        try:
            async for event in _with_keepalive(cs.stream_task(request.prompt, agent_id, request.model, mode, **task_kwargs), KEEPALIVE_SECONDS):
                if event is None:
                    # SSE comment: keeps proxies (Node's fetch drops a body silent for 5 min) and
                    # browsers from giving up while Claude is inside a long tool call.
                    yield ": keepalive\n\n"
                    continue
                event_count += 1
                if event.get("type") == "rate_limited":
                    reset_at_str = event.get("reset_at")
                    try:
                        reset_at = datetime.fromisoformat(reset_at_str)
                    except Exception:
                        reset_at = datetime.now(timezone.utc)

                    state = rl.get_state()
                    state.set_limited(reset_at)

                    # Launch background watcher (idempotent — skips if already running)
                    if state._watch_task is None or state._watch_task.done():
                        state._watch_task = asyncio.create_task(rl.watch_and_clear(reset_at))

                    # Notify immediately that the limit was hit
                    asyncio.create_task(send_notification(
                        f"⚠️ Claude usage limit hit!\n"
                        f"Will automatically resume at: {reset_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
                        f"You'll be notified when the limit resets."
                    ))

                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            logger.error("stream_task error: %s", exc)
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            elapsed = round((time.monotonic() - t0) * 1000)
            logger.info("Request done | agent=%s events=%d elapsed_ms=%d", agent_id, event_count, elapsed)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/logs/tasks", dependencies=_PROTECTED)
async def logs_tasks(limit: int = 50, source: str | None = None):
    """Recent prompt/response records from logs/sessions.jsonl, newest first."""
    def read():
        path = os.path.join(cs.LOGS_DIR, "sessions.jsonl")
        rows = []
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if source and e.get("source") != source:
                        continue
                    rows.append({k: e.get(k) for k in (
                        "ts", "agent_id", "session_id", "source", "model", "mode", "cwd",
                        "prompt", "result", "assistant_text", "error", "exit_code",
                        "tool_call_count", "total_ms", "total_cost_usd")})
        except OSError:
            return []
        return rows[::-1][: max(1, min(limit, 500))]
    return {"tasks": await asyncio.to_thread(read)}


@app.get("/rate_limit_status")
async def rate_limit_status():
    """Return current rate-limit state so the UI can show a countdown."""
    return rl.get_state().to_dict()


@app.get("/history/{agent_id}", dependencies=_PROTECTED)
async def get_history(agent_id: str):
    return {"history": cs.get_history(agent_id), "stats": await asyncio.to_thread(cs.session_stats, agent_id)}


@app.get("/sessions", dependencies=_PROTECTED)
async def list_sessions():
    """Past chats (from sessions.jsonl), newest first — powers the picker."""
    return {"sessions": await asyncio.to_thread(cs.list_conversations)}


class OpenSessionRequest(BaseModel):
    session_id: str
    agent_id: str = "default"


class OpenLocalSessionRequest(OpenSessionRequest):
    cwd: str = ""  # workspace-relative folder the session was created in


@app.post("/sessions/open", dependencies=_PROTECTED)
async def open_session(req: OpenSessionRequest):
    """Reopen a past chat in an agent tab: restore its turns and set it as the
    --resume target so the next prompt continues it."""
    turns = await asyncio.to_thread(cs.open_conversation, req.agent_id, req.session_id)
    if turns is None:
        raise HTTPException(status_code=404, detail="Unknown session_id")
    return {"history": turns}


@app.get("/sessions/local", dependencies=_PROTECTED)
async def list_local_sessions(cwd: str = "", limit: int = 30):
    """Claude Code's own sessions for a workspace folder (VS Code / terminal chats), newest first."""
    import local_sessions

    abs_cwd = _resolve_workspace_path(cwd, outside_status=403) if cwd else cs.SESSION_CWD
    return {"sessions": await asyncio.to_thread(local_sessions.list_local_sessions, abs_cwd, max(1, min(limit, 100)))}


@app.post("/sessions/open-local", dependencies=_PROTECTED)
async def open_local_session(req: OpenLocalSessionRequest):
    """Point an agent tab at a Claude Code session from that folder's store."""
    abs_cwd = _resolve_workspace_path(req.cwd, outside_status=403) if req.cwd else cs.SESSION_CWD
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", req.session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id")
    turns = await asyncio.to_thread(cs.open_local_session, req.agent_id or "default", req.session_id, abs_cwd)
    if turns is None:
        raise HTTPException(status_code=404, detail="Unknown local session")
    return {"history": turns, "cwd": abs_cwd}


_TREE_SKIP = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".next",
    "dist", "build", ".cache", ".pytest_cache", "*.egg-info",
}


def _build_tree(abs_path: str, rel_path: str, depth: int) -> dict:
    name = os.path.basename(abs_path) or "workspace"
    node: dict = {"name": name, "path": rel_path, "type": "dir", "children": []}
    if depth == 0:
        return node
    try:
        entries = sorted(os.listdir(abs_path), key=lambda e: (not os.path.isdir(os.path.join(abs_path, e)), e.lower()))
    except PermissionError:
        return node
    for entry in entries:
        if entry.startswith(".") or entry in _TREE_SKIP:
            continue
        child_abs = os.path.join(abs_path, entry)
        child_rel = (rel_path + "/" + entry).lstrip("/")
        if os.path.isdir(child_abs):
            node["children"].append(_build_tree(child_abs, child_rel, depth - 1))
        else:
            node["children"].append({
                "name": entry,
                "path": child_rel,
                "type": "file",
                "size": os.path.getsize(child_abs),
            })
    return node


@app.get("/tree", dependencies=_PROTECTED)
async def get_tree(depth: int = 4):
    depth = max(1, min(depth, 6))
    return _build_tree(WORKSPACE_ROOT, "", depth)


@app.get("/files", dependencies=_PROTECTED)
async def list_files(path: str = ""):
    if path:
        abs_path = os.path.normpath(os.path.join(WORKSPACE_ROOT, path))
    else:
        abs_path = WORKSPACE_ROOT
    if not (abs_path.startswith(WORKSPACE_ROOT + os.sep) or abs_path == WORKSPACE_ROOT):
        raise HTTPException(status_code=400, detail="Path outside workspace")
    if not os.path.isdir(abs_path):
        raise HTTPException(status_code=404, detail="Not a directory")

    dirs, files = [], []
    for entry in sorted(os.listdir(abs_path), key=str.lower):
        if entry.startswith('.'):
            continue
        entry_abs = os.path.join(abs_path, entry)
        rel = os.path.relpath(entry_abs, WORKSPACE_ROOT).replace("\\", "/")
        if os.path.isdir(entry_abs):
            dirs.append({"name": entry, "path": rel})
        else:
            files.append({"name": entry, "path": rel, "size": os.path.getsize(entry_abs)})

    rel_cur = os.path.relpath(abs_path, WORKSPACE_ROOT).replace("\\", "/")
    return {"path": "" if rel_cur == "." else rel_cur, "dirs": dirs, "files": files}


@app.post("/reset_memory", dependencies=_PROTECTED)
async def reset_memory(request: ResetMemoryRequest):
    agent_id = request.agent_id.strip()
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id cannot be empty")
    cs.reset_session(agent_id)
    return {"agent_id": agent_id, "status": "reset"}


@app.get("/repo", dependencies=_PROTECTED)
async def get_repo():
    status_out = await _git_status(WORKSPACE_ROOT)
    diff_out = await _git_diff(WORKSPACE_ROOT)

    modified = []
    for line in status_out.splitlines():
        if line and not line.startswith("##"):
            fname = line[3:].strip() if len(line) > 3 else line.strip()
            if " -> " in fname:
                fname = fname.split(" -> ")[-1].strip()
            if fname:
                modified.append(fname)

    diff_file_count = sum(1 for ln in diff_out.splitlines() if ln.startswith("diff --git"))

    return {
        "status": status_out,
        "diff": diff_out,
        "modified_files": modified,
        "diff_file_count": diff_file_count,
    }


# ── Workspace path resolution + sensitive-file blocklist ─────────────────────

_BLOCKED_FILE_EXTS = {".db"}


def _resolve_workspace_path(path: str, outside_status: int = 403) -> str:
    """
    Resolve a user-supplied path and enforce workspace containment.

    Uses os.path.realpath on both sides (resolves symlinks / 8.3 names) and a
    case-insensitive commonpath comparison (Windows). Raises HTTPException with
    `outside_status` if the resolved path escapes WORKSPACE_ROOT.
    """
    if "\x00" in path:
        raise HTTPException(status_code=400, detail="Invalid path")
    candidate = path if os.path.isabs(path) else os.path.join(WORKSPACE_ROOT, path)
    abs_path = os.path.realpath(candidate)
    root = os.path.realpath(WORKSPACE_ROOT)
    try:
        inside = os.path.commonpath(
            [os.path.normcase(root), os.path.normcase(abs_path)]
        ) == os.path.normcase(root)
    except ValueError:  # e.g. different drives on Windows
        inside = False
    if not inside:
        raise HTTPException(status_code=outside_status, detail="Path outside workspace")
    return abs_path


def _reject_sensitive_path(abs_path: str) -> None:
    """403 for dotfile/dot-directory components (.env, .git, …) and .db files."""
    rel = os.path.relpath(abs_path, os.path.realpath(WORKSPACE_ROOT))
    components = rel.replace("\\", "/").split("/")
    if any(part.startswith(".") and part != "." for part in components):
        raise HTTPException(status_code=403, detail="Access to this path is forbidden")
    if os.path.splitext(abs_path)[1].lower() in _BLOCKED_FILE_EXTS:
        raise HTTPException(status_code=403, detail="Access to this file type is forbidden")


_IMAGE_MIMETYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}


@app.get("/image", dependencies=_PROTECTED)
async def get_image(path: str):
    """Serve an image file from within the workspace."""
    abs_path = _resolve_workspace_path(path, outside_status=403)
    _reject_sensitive_path(abs_path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")
    ext = os.path.splitext(abs_path)[1].lower()
    if ext not in _IMAGE_MIMETYPES:
        raise HTTPException(status_code=400, detail="Not an image file")
    return FileResponse(abs_path, media_type=_IMAGE_MIMETYPES[ext])


@app.get("/serve", dependencies=_PROTECTED)
async def serve_raw(path: str):
    """Serve a workspace file with correct Content-Type (for iframe preview)."""
    abs_path = _resolve_workspace_path(path, outside_status=403)
    _reject_sensitive_path(abs_path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(abs_path)


@app.get("/analytics")
async def analytics_redirect():
    return RedirectResponse(url="/static/analytics.html")


class TelemetryEvent(BaseModel):
    session_key: str
    agent_id: str = "default"
    event_type: str
    payload: dict = {}
    ts: str | None = None


@app.post("/telemetry", dependencies=_PROTECTED)
async def post_telemetry(event: TelemetryEvent):
    await asyncio.to_thread(
        analytics.record_telemetry,
        session_key=event.session_key,
        agent_id=event.agent_id,
        event_type=event.event_type,
        payload=event.payload,
        client_ts=event.ts,
    )
    return {"ok": True}


@app.get("/analytics/summary", dependencies=_PROTECTED)
async def analytics_summary():
    return await asyncio.to_thread(analytics.query_summary)


@app.get("/analytics/sessions", dependencies=_PROTECTED)
async def analytics_sessions(limit: int = 50, offset: int = 0):
    return {"sessions": await asyncio.to_thread(analytics.query_sessions, limit=limit, offset=offset)}


@app.get("/analytics/tools", dependencies=_PROTECTED)
async def analytics_tools(limit: int = 30):
    return {"tools": await asyncio.to_thread(analytics.query_tools, limit=limit)}


@app.get("/analytics/perf", dependencies=_PROTECTED)
async def analytics_perf():
    return await asyncio.to_thread(analytics.query_perf)


@app.get("/analytics/cost", dependencies=_PROTECTED)
async def analytics_cost(days: int = 30):
    return {"days": days, "data": await asyncio.to_thread(analytics.query_cost, days=days)}


@app.get("/analytics/telemetry/summary", dependencies=_PROTECTED)
async def analytics_telemetry_summary():
    return {"events": await asyncio.to_thread(analytics.query_telemetry_summary)}


@app.get("/analytics/rate_limits", dependencies=_PROTECTED)
async def analytics_rate_limits(limit: int = 20):
    return {"events": await asyncio.to_thread(analytics.query_rate_limit_events, limit=limit)}


@app.get("/file", dependencies=_PROTECTED)
async def get_file(path: str):
    abs_path = _resolve_workspace_path(path, outside_status=400)
    _reject_sensitive_path(abs_path)
    if not os.path.exists(abs_path) or not os.path.isfile(abs_path):
        return {"content": None, "exists": False}
    content = await _read_file(abs_path)
    return {"content": content, "exists": True}
