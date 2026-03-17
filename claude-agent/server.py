import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from claude_session import ClaudeSession

logging.basicConfig(level=logging.INFO)

session = ClaudeSession()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await session.start()
    yield
    await session.stop()


app = FastAPI(lifespan=lifespan)


class TaskRequest(BaseModel):
    prompt: str


@app.post("/task")
async def run_task(request: TaskRequest):
    if not request.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    try:
        response = await session.send_with_tools(request.prompt)
        return {"response": response}
    except RuntimeError as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Session error: {exc}")
