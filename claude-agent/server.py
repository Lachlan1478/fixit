import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import memory_store
import middleware
import pending_store
from claude_session import ClaudeSession
from executor import WORKSPACE_ROOT, execute_approved_action
from tools.filesystem import read_file as _read_file
from tools.git import git_diff as _git_diff, git_status as _git_status

logging.basicConfig(level=logging.INFO)

session = ClaudeSession()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await session.start()
    yield
    await session.stop()


app = FastAPI(lifespan=lifespan)

app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")


class TaskRequest(BaseModel):
    prompt: str
    agent_id: str = "default"


class ApproveRequest(BaseModel):
    id: str
    approve: bool


class ResetMemoryRequest(BaseModel):
    agent_id: str


@app.post("/task")
async def run_task(request: TaskRequest):
    if not request.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    agent_id = request.agent_id.strip() or "default"
    enriched_prompt = memory_store.build_context_prompt(agent_id, request.prompt)

    try:
        result = await session.send_with_tools(enriched_prompt)
    except RuntimeError as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Session error: {exc}")

    memory_store.append_and_save(agent_id, request.prompt, result["answer"])

    return {
        "response": result["answer"],
        "pending_actions": result["pending"],
    }


@app.post("/reset_memory")
async def reset_memory(request: ResetMemoryRequest):
    agent_id = request.agent_id.strip()
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id cannot be empty")
    memory_store.reset(agent_id)
    return {"agent_id": agent_id, "status": "reset"}


@app.get("/pending")
async def get_pending():
    return {"pending_actions": pending_store.get_pending()}


@app.post("/approve")
async def approve_action(request: ApproveRequest):
    entry = pending_store.get_by_id(request.id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No pending action with id={request.id!r}")
    if entry["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Action is already {entry['status']!r} — cannot change",
        )

    action = entry["action"]

    if not request.approve:
        pending_store.update_status(request.id, "rejected")
        middleware.log_approval(request.id, action, "rejected", None)
        return {"id": request.id, "status": "rejected"}

    exec_result = await execute_approved_action(action)
    status = "approved" if exec_result["error"] is None else "failed"
    pending_store.update_status(request.id, status)
    middleware.log_approval(request.id, action, status, exec_result)

    return {
        "id": request.id,
        "status": status,
        "result": exec_result.get("result"),
        "error": exec_result.get("error"),
    }


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
