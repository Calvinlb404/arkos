"""HTTP routes for the live browser screencast viewer.

A single SSE endpoint that streams the current user's screencast events
out of the in-process frame broker. Events:
  {"type": "started"}                  a new session began
  {"type": "frame", "jpeg_b64": "..."} one screencast frame
  {"type": "ended"}                    the active session finished
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from config_module.loader import config
from tool_module.browser_stream import broker

router = APIRouter()

# How often to wake and check whether the client has disconnected while we
# wait for the next broker event. Without this poll the handler can sit
# forever in `subscribe()`'s queue.get() after a quiet client goes away.
_DISCONNECT_POLL_SECONDS = 1.0


def _resolve_user_id(request: Request) -> str:
    return (
        request.headers.get("X-User-ID") or request.query_params.get("user_id") or config.get("memory.fallback_user_id")
    )


@router.get("/v1/browser/stream")
async def browser_stream(request: Request) -> StreamingResponse:
    user_id = _resolve_user_id(request)

    return StreamingResponse(_event_stream(user_id, request), media_type="text/event-stream")


async def _event_stream(user_id: str, request: Request):
    sub = broker.subscribe(user_id).__aiter__()
    while True:
        try:
            event = await asyncio.wait_for(sub.__anext__(), timeout=_DISCONNECT_POLL_SECONDS)
        except TimeoutError:
            if await request.is_disconnected():
                break
            continue
        except StopAsyncIteration:
            break
        if await request.is_disconnected():
            break
        yield f"data: {json.dumps(event)}\n\n"
