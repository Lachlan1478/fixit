"""
server.py — Assembly × Claude Phone orchestrator FastAPI app.

Run with:
    uvicorn orchestrator.server:app --port 8002 --app-dir ..
    OR from the claude_phone root:
    uvicorn orchestrator.server:app --port 8002
"""

import asyncio
import json
import logging
import os
import sys

# Windows requires ProactorEventLoop to spawn subprocesses from async code
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from dotenv import load_dotenv
_HERE_ENV = os.path.dirname(os.path.abspath(__file__))
# Load orchestrator .env first, then Assembly's (for OPENAI_API_KEY)
load_dotenv()
load_dotenv(os.path.join(_HERE_ENV, "..", "..", "Assembly", ".env"))

# ── sys.path: make claude-agent and Assembly importable ────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_CLAUDE_AGENT_DIR = os.path.abspath(os.path.join(_HERE, "..", "claude-agent"))
_ASSEMBLY_DIR = os.path.abspath(os.path.join(_HERE, "..", "..", "Assembly"))

for _p in [_ASSEMBLY_DIR, _CLAUDE_AGENT_DIR]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
if _HERE in sys.path:
    sys.path.remove(_HERE)
sys.path.insert(0, _HERE)

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pipeline import (
    PipelineState,
    create_session,
)
import session_store as store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Assembly × Claude", version="1.0.0")

# Mount static files (unified UI)
_STATIC_DIR = os.path.join(_HERE, "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# Active pipelines: session_id → Pipeline
_pipelines: dict = {}


# ── Request models ─────────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    problem: str
    mode: str = "medium"
    auto_approve_spec: bool = False


class ApproveSpecRequest(BaseModel):
    spec: dict | None = None  # Optional edited spec


class HumanInputRequest(BaseModel):
    message: str


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.post("/pipeline/start")
async def start_pipeline(req: StartRequest):
    if not req.problem.strip():
        raise HTTPException(status_code=400, detail="problem must not be empty")
    if req.mode not in ("fast", "medium", "standard", "deep"):
        raise HTTPException(status_code=400, detail="mode must be fast|medium|standard|deep")

    session, pipeline = create_session(
        problem=req.problem,
        mode=req.mode,
        auto_approve_spec=req.auto_approve_spec,
    )
    _pipelines[session.session_id] = pipeline
    asyncio.create_task(pipeline.run())

    logger.info("Started pipeline %s mode=%s problem=%r", session.session_id, req.mode, req.problem[:60])
    return {"session_id": session.session_id}


@app.get("/pipeline/stream/{session_id}")
async def stream_pipeline(session_id: str):
    session = store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async def event_generator():
        # Send current state immediately on connect
        yield _sse({"type": "state_change", "old": "unknown", "new": session.state})

        terminal_states = {PipelineState.COMPLETE, PipelineState.ERROR}

        while session.state not in terminal_states:
            try:
                event = await asyncio.wait_for(
                    session.event_queue.get(),
                    timeout=25.0,
                )
                yield _sse(event)

                # Terminal: drain remaining events then stop
                if event.get("type") in ("pipeline_complete", "error"):
                    # Drain residual
                    while not session.event_queue.empty():
                        yield _sse(session.event_queue.get_nowait())
                    break

            except asyncio.TimeoutError:
                yield ": keepalive\n\n"  # SSE comment keeps connection alive

        # Always end with the final state
        yield _sse({"type": "state_change", "old": "unknown", "new": session.state})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/pipeline/{session_id}/approve_spec")
async def approve_spec(session_id: str, req: ApproveSpecRequest):
    session = store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.state != PipelineState.SPEC_REVIEW:
        raise HTTPException(
            status_code=400,
            detail=f"Session is in state '{session.state}', not spec_review",
        )
    pipeline = _pipelines.get(session_id)
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not running")

    pipeline.approve_spec(req.spec)
    return {"status": "approved", "session_id": session_id}


@app.post("/pipeline/{session_id}/human_input")
async def human_input(session_id: str, req: HumanInputRequest):
    session = store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.state != PipelineState.AWAITING_HUMAN:
        raise HTTPException(
            status_code=400,
            detail=f"Session is in state '{session.state}', not awaiting_human",
        )
    pipeline = _pipelines.get(session_id)
    if not pipeline:
        raise HTTPException(status_code=404, detail="Pipeline not running")

    pipeline.provide_human_input(req.message)
    return {"status": "received", "session_id": session_id}


@app.get("/pipeline/{session_id}")
async def get_session(session_id: str):
    session = store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.to_summary()


@app.get("/pipeline/sessions/list")
async def list_sessions():
    return store.list_sessions()


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"
