import json
import logging
import os
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import claude_session as cs

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
from tools.filesystem import read_file as _read_file
from tools.git import git_diff as _git_diff, git_status as _git_status

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(lifespan=lifespan)

app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)


@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")


class TaskRequest(BaseModel):
    prompt: str
    agent_id: str = "default"


class ResetMemoryRequest(BaseModel):
    agent_id: str


@app.post("/task")
async def run_task(request: TaskRequest):
    """Stream Claude's work as SSE. Each event is 'data: <json>\\n\\n'."""
    if not request.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    agent_id = request.agent_id.strip() or "default"

    async def event_stream():
        t0 = time.monotonic()
        event_count = 0
        logger.info("Request | agent=%s prompt=%r", agent_id, request.prompt[:60])
        try:
            async for event in cs.stream_task(request.prompt, agent_id):
                event_count += 1
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            logger.error("stream_task error: %s", exc)
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            elapsed = round((time.monotonic() - t0) * 1000)
            logger.info("Request done | agent=%s events=%d elapsed_ms=%d", agent_id, event_count, elapsed)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/reset_memory")
async def reset_memory(request: ResetMemoryRequest):
    agent_id = request.agent_id.strip()
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id cannot be empty")
    cs.reset_session(agent_id)
    return {"agent_id": agent_id, "status": "reset"}


@app.get("/repo")
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


@app.get("/file")
async def get_file(path: str):
    if os.path.isabs(path):
        abs_path = os.path.normpath(path)
    else:
        abs_path = os.path.normpath(os.path.join(WORKSPACE_ROOT, path))
    if not (abs_path.startswith(WORKSPACE_ROOT + os.sep) or abs_path == WORKSPACE_ROOT):
        raise HTTPException(status_code=400, detail="Path outside workspace")
    if not os.path.exists(abs_path) or not os.path.isfile(abs_path):
        return {"content": None, "exists": False}
    content = await _read_file(abs_path)
    return {"content": content, "exists": True}
