"""
HTTP endpoints for the per-user computer:
  - Computer task management (list, get, dispatch)
  - SSE progress stream + event polling (Task 8)
  - Filesystem viewer endpoints (Task 9)

All endpoints are scoped to the calling user via CurrentUser.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from base_module.jwt_utils import CurrentUser
from base_module.task_store import _user_uuid
from computer_module.sandbox import sandbox_manager
from computer_module.store import (
    create_computer_task,
    get_computer_task,
    list_computer_events,
    list_computer_tasks,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/computer", tags=["computer"])


# ---------- task dispatch + listing -----------------------------------------

class DispatchRequest(BaseModel):
    prompt: str


@router.post("/tasks")
async def dispatch_task(body: DispatchRequest, current: dict = CurrentUser):
    """
    Dispatch a computer task directly (bypasses the buddy routing path).
    Returns the task_id immediately; the task runs async and messages back on completion.
    """
    from computer_module.runner import spawn
    from memory_module.memory import Memory
    from config_module.loader import config

    # Normalise to UUID so conversation_context and tasks share the same key.
    user_id = str(_user_uuid(current["user_id"]))

    # Mint a fresh session_id so the completion message has somewhere to land.
    # When called from buddy, the session_id comes from the agent's memory.
    # When called directly (e.g. from a client), we create an ephemeral one.
    mem = Memory(user_id=user_id, session_id=None,
                 db_url=config.get("database.url"), use_long_term=False)
    chat_session_id = mem.session_id

    task_id = create_computer_task(user_id, chat_session_id, body.prompt)
    spawn(task_id=task_id, user_id=user_id,
          chat_session_id=chat_session_id, prompt=body.prompt)

    return JSONResponse({"task_id": task_id, "status": "pending",
                         "chat_session_id": chat_session_id})


@router.get("/tasks")
async def list_tasks(current: dict = CurrentUser):
    """List the calling user's computer tasks (most recent first)."""
    rows = list_computer_tasks(current["user_id"])
    return JSONResponse({"tasks": [_serialize_task(r) for r in rows]})


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, current: dict = CurrentUser):
    """Get one computer task (owner-scoped)."""
    task = get_computer_task(task_id, current["user_id"])
    if not task:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(_serialize_task(task))


def _serialize_task(row: dict) -> dict:
    return {
        "task_id": str(row["task_id"]),
        "user_id": row["user_id"],
        "prompt": row["prompt"][:200],
        "status": row["status"],
        "summary": row.get("summary"),
        "error": row.get("error"),
        "outputs": row.get("outputs") or [],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


# ---------- event polling (Task 8 fallback) ----------------------------------

@router.get("/tasks/{task_id}/events")
async def poll_events(task_id: str, after: int = 0, current: dict = CurrentUser):
    """
    Return new events for a task since `after` event_id.
    Polling fallback for clients that don't support SSE.
    """
    events = list_computer_events(task_id, current["user_id"], after_id=after)
    return JSONResponse({"events": [_serialize_event(e) for e in events]})


def _serialize_event(e: dict) -> dict:
    return {
        "event_id": e["event_id"],
        "kind": e["kind"],
        "content": e["content"],
        "payload": e.get("payload") or {},
        "created_at": e["created_at"].isoformat() if e.get("created_at") else None,
    }


# ---------- SSE stream (Task 8) ---------------------------------------------

@router.get("/tasks/{task_id}/stream")
async def stream_events(task_id: str, current: dict = CurrentUser):
    """
    Server-Sent Events stream of progress events for a computer task.
    Emits events as they are written; closes on completed/failed event.
    Owner-scoped: returns 403 if the task belongs to another user.
    """
    user_id = current["user_id"]

    # Verify ownership before opening the stream.
    task = get_computer_task(task_id, user_id)
    if not task:
        return JSONResponse({"error": "not found"}, status_code=404)

    async def generate():
        last_id = 0
        while True:
            try:
                events = await asyncio.to_thread(
                    list_computer_events, task_id, user_id, last_id
                )
            except Exception as e:
                yield f"data: {json.dumps({'kind': 'error', 'content': str(e)})}\n\n"
                return

            for e in events:
                last_id = e["event_id"]
                yield f"data: {json.dumps(_serialize_event(e))}\n\n"
                if e["kind"] in ("completed", "failed"):
                    return

            # Check if the task already reached a terminal state with no new events.
            try:
                current_task = await asyncio.to_thread(get_computer_task, task_id, user_id)
                if current_task and current_task["status"] in ("completed", "failed"):
                    return
            except Exception:
                pass

            await asyncio.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------- filesystem viewer (Task 9) --------------------------------------

@router.get("/files")
async def list_files(path: str = "/home/user", current: dict = CurrentUser):
    """
    List a directory in the user's persistent sandbox.
    Wakes the sandbox if paused (sub-second resume).
    """
    user_id = current["user_id"]
    try:
        entries = await sandbox_manager.list_dir(user_id, path)
        return JSONResponse({"path": path, "entries": entries})
    except Exception as e:
        logger.error("list_files failed for user %s path %s: %s", user_id, path, e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/file")
async def read_file(path: str, current: dict = CurrentUser):
    """
    Read a file from the user's sandbox. Content is size-capped to avoid
    flooding the response; the Computer tab should paginate large files.
    """
    user_id = current["user_id"]
    MAX_CHARS = 50_000
    try:
        content = await sandbox_manager.read_file(user_id, path)
        truncated = len(content) > MAX_CHARS
        return JSONResponse({
            "path": path,
            "content": content[:MAX_CHARS],
            "truncated": truncated,
            "size": len(content),
        })
    except Exception as e:
        logger.error("read_file failed for user %s path %s: %s", user_id, path, e)
        return JSONResponse({"error": str(e)}, status_code=500)
