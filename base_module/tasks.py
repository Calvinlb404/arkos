"""
Task + approval HTTP API. Persists to the `tasks`, `task_events`, and
`task_approvals` tables (see db/migrations/0001_*.sql and 0003_*.sql).

Lifecycle
---------
1. User chats with ARK. Buddy workshops a plan in chat (workshop_plan state).
2. Frontend renders the plan with approve/decline buttons inline.
3. On approve, frontend POSTs here with the plan payload. We insert a row with
   status='running', mint a session_id for the subagent's memory, and schedule
   `task_runner.spawn(task_id)` to execute it in the background.
4. The subagent loops through plan_steps. When it needs the human, it inserts
   a task_approvals row and flips the task to status='awaiting_approval'.
5. The desk's Pending Approvals panel queries /tasks/approvals. The user
   approves / declines / answers, which resolves the row; the subagent
   DB-polls, picks up the answer, and proceeds.
6. On completion (or failure/cancel) the task ends.

All endpoints are scoped to the caller's user_id from the Bearer token.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from base_module import task_runner
from base_module.jwt_utils import CurrentUser
from base_module.task_store import (
    list_events,
    list_pending_approvals,
    resolve_approval,
    set_task_status,
)
from config_module.loader import config

router = APIRouter(prefix="/tasks", tags=["tasks"])


# ---------- enums + schemas ----------
class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskCreate(BaseModel):
    title: str = Field(..., max_length=280)
    plan_steps: list[str] = Field(default_factory=list)
    plan: str | None = None  # optional pre-rendered "1. foo\n2. bar" blob
    required_tools: list[str] = Field(default_factory=list)
    context_payload: dict[str, Any] = Field(default_factory=dict)


class TaskResponse(BaseModel):
    task_id: str
    user_id: str
    status: TaskStatus
    title: str
    plan: str | None
    plan_steps: list[str]
    required_tools: list[str]
    context_payload: dict[str, Any]
    session_id: str | None
    agent_kind: str | None
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    tasks: list[TaskResponse]
    total: int


class StatusUpdateRequest(BaseModel):
    status: TaskStatus


class ApprovalResponseBody(BaseModel):
    approved: bool | None = None
    answer: str | None = None


class ApprovalCard(BaseModel):
    approval_id: str
    task_id: str
    task_title: str
    kind: str
    prompt: str
    context: dict[str, Any]
    created_at: datetime


class ApprovalListResponse(BaseModel):
    approvals: list[ApprovalCard]
    total: int


class TaskEvent(BaseModel):
    event_id: int
    kind: str
    content: str
    payload: dict[str, Any]
    created_at: datetime


class TaskEventsResponse(BaseModel):
    events: list[TaskEvent]
    next_after: int


# ---------- db helpers ----------
def _connect():
    return psycopg2.connect(config.get("database.url"))


def _row_to_response(row: dict[str, Any]) -> TaskResponse:
    payload = row["context_payload"] or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    return TaskResponse(
        task_id=str(row["task_id"]),
        user_id=str(row["user_id"]),
        status=TaskStatus(row["status"]),
        title=payload.get("title", ""),
        plan=payload.get("plan"),
        plan_steps=list(payload.get("plan_steps") or []),
        required_tools=list(row["required_tools"] or []),
        context_payload=payload,
        session_id=str(row["session_id"]) if row.get("session_id") else None,
        agent_kind=row.get("agent_kind"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _user_uuid(user_id_str: str) -> uuid.UUID:
    """Parse the JWT sub (user_id) into a UUID. Demo fallback tolerates non-uuid strings by hashing."""
    try:
        return uuid.UUID(user_id_str)
    except (ValueError, TypeError):
        return uuid.uuid5(uuid.NAMESPACE_URL, f"ark-legacy:{user_id_str}")


# ---------- create + list + get -------------------------------------------
@router.post("", response_model=TaskResponse)
async def create_task(body: TaskCreate, current: dict = CurrentUser) -> TaskResponse:
    """
    Create a task from an approved plan and start the subagent.

    Unlike the old flow, this endpoint does NOT create 'pending' rows. Plans
    are approved in chat now; by the time this is called, the user has already
    said 'run it'. So we insert with status='running' and schedule the runner.
    """
    user_uuid = _user_uuid(current["user_id"])

    plan_steps = list(body.plan_steps or [])
    if not plan_steps and body.plan:
        # parse "1. foo\n2. bar" blob
        plan_steps = [line.split(". ", 1)[-1].strip() for line in body.plan.splitlines() if line.strip()]

    if not plan_steps:
        raise HTTPException(400, "plan_steps required (or a multiline plan)")

    plan_text = body.plan or "\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan_steps))

    payload: dict[str, Any] = dict(body.context_payload or {})
    payload["title"] = body.title
    payload["plan"] = plan_text
    payload["plan_steps"] = plan_steps
    payload.setdefault("source", "chat")

    session_id = str(uuid.uuid4())

    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO tasks (user_id, status, required_tools, context_payload,
                                   session_id, agent_kind)
                VALUES (%s, 'running', %s, %s, %s, 'executor')
                RETURNING task_id, user_id, status, required_tools, context_payload,
                          session_id, agent_kind, created_at, updated_at
                """,
                (str(user_uuid), body.required_tools, json.dumps(payload), session_id),
            )
            row = cur.fetchone()
            conn.commit()
    finally:
        conn.close()

    resp = _row_to_response(row)

    # Fire and forget: the runner will log events + update status in the DB.
    try:
        task_runner.spawn(resp.task_id)
    except Exception as e:
        # If spawning fails, surface it but keep the row so the user can retry.
        from base_module.task_store import log_event, mark_task_failed

        log_event(resp.task_id, "error", f"spawn failed: {e}")
        mark_task_failed(resp.task_id, str(e))
        raise HTTPException(500, f"task row created but runner failed to start: {e}") from e

    return resp


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    status: Annotated[TaskStatus | None, Query()] = None,
    current: dict = CurrentUser,
) -> TaskListResponse:
    user_uuid = _user_uuid(current["user_id"])

    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if status is None:
                cur.execute(
                    """
                    SELECT task_id, user_id, status, required_tools, context_payload,
                           session_id, agent_kind, parent_task_id, created_at, updated_at
                    FROM tasks
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT 200
                    """,
                    (str(user_uuid),),
                )
            else:
                cur.execute(
                    """
                    SELECT task_id, user_id, status, required_tools, context_payload,
                           session_id, agent_kind, parent_task_id, created_at, updated_at
                    FROM tasks
                    WHERE user_id = %s AND status = %s
                    ORDER BY created_at DESC
                    LIMIT 200
                    """,
                    (str(user_uuid), status.value),
                )
            rows = cur.fetchall()
    finally:
        conn.close()

    items = [_row_to_response(r) for r in rows]
    return TaskListResponse(tasks=items, total=len(items))


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, current: dict = CurrentUser) -> TaskResponse:
    user_uuid = _user_uuid(current["user_id"])
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT task_id, user_id, status, required_tools, context_payload,
                       session_id, agent_kind, parent_task_id, created_at, updated_at
                FROM tasks
                WHERE task_id = %s AND user_id = %s
                """,
                (task_id, str(user_uuid)),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "task not found")
    return _row_to_response(row)


@router.get("/{task_id}/events", response_model=TaskEventsResponse)
async def task_events(
    task_id: str,
    after: int = Query(default=0, ge=0),
    current: dict = CurrentUser,
) -> TaskEventsResponse:
    """Return events for the task the caller owns. `after` lets the UI poll incrementally."""
    user_uuid = _user_uuid(current["user_id"])
    events = list_events(task_id, str(user_uuid), after_id=after)
    payload = [
        TaskEvent(
            event_id=int(e["event_id"]),
            kind=e["kind"],
            content=e["content"] or "",
            payload=(e["payload"] if isinstance(e["payload"], dict) else json.loads(e["payload"] or "{}")),
            created_at=e["created_at"],
        )
        for e in events
    ]
    next_after = payload[-1].event_id if payload else after
    return TaskEventsResponse(events=payload, next_after=next_after)


# ---------- status changes -------------------------------------------------
def _set_status(task_id: str, user_uuid: uuid.UUID, new_status: TaskStatus) -> dict[str, Any]:
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE tasks
                SET status = %s
                WHERE task_id = %s AND user_id = %s
                RETURNING task_id, user_id, status, required_tools, context_payload,
                          session_id, agent_kind, parent_task_id, created_at, updated_at
                """,
                (new_status.value, task_id, str(user_uuid)),
            )
            row = cur.fetchone()
            conn.commit()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "task not found")
    return row


@router.patch("/{task_id}/status", response_model=TaskResponse)
async def update_task_status(task_id: str, body: StatusUpdateRequest, current: dict = CurrentUser) -> TaskResponse:
    row = _set_status(task_id, _user_uuid(current["user_id"]), body.status)
    return _row_to_response(row)


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(task_id: str, current: dict = CurrentUser) -> TaskResponse:
    """Cancel a running subagent. Also flips the DB status."""
    user_uuid = _user_uuid(current["user_id"])
    # Make sure the caller owns this task before cancelling.
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT task_id FROM tasks WHERE task_id = %s AND user_id = %s",
                (task_id, str(user_uuid)),
            )
            if not cur.fetchone():
                raise HTTPException(404, "task not found")
    finally:
        conn.close()

    task_runner.cancel(task_id)
    set_task_status(task_id, "cancelled")
    row = _set_status(task_id, user_uuid, TaskStatus.CANCELLED)
    return _row_to_response(row)


@router.delete("/{task_id}")
async def delete_task(task_id: str, current: dict = CurrentUser) -> dict:
    """Hard-delete a task (and its cascading events / approvals)."""
    user_uuid = _user_uuid(current["user_id"])
    # cancel the runner if it's alive
    task_runner.cancel(task_id)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM tasks WHERE task_id = %s AND user_id = %s",
                (task_id, str(user_uuid)),
            )
            deleted = cur.rowcount
            conn.commit()
    finally:
        conn.close()
    if not deleted:
        raise HTTPException(404, "task not found")
    return {"deleted": task_id}


# ---------- approvals ------------------------------------------------------
@router.get("/approvals/pending", response_model=ApprovalListResponse)
async def approvals_pending(current: dict = CurrentUser) -> ApprovalListResponse:
    """List open approval requests for the caller. Powers the desk panel."""
    user_uuid = _user_uuid(current["user_id"])
    rows = list_pending_approvals(str(user_uuid))
    cards: list[ApprovalCard] = []
    for r in rows:
        ctx = r["task_context"] or {}
        if isinstance(ctx, str):
            ctx = json.loads(ctx)
        cards.append(
            ApprovalCard(
                approval_id=str(r["approval_id"]),
                task_id=str(r["task_id"]),
                task_title=(ctx.get("title") if isinstance(ctx, dict) else "") or "(task)",
                kind=r["kind"],
                prompt=r["prompt"],
                context=r["context"] if isinstance(r["context"], dict) else json.loads(r["context"] or "{}"),
                created_at=r["created_at"],
            )
        )
    return ApprovalListResponse(approvals=cards, total=len(cards))


@router.post("/approvals/{approval_id}/respond")
async def respond_to_approval(
    approval_id: str,
    body: ApprovalResponseBody,
    current: dict = CurrentUser,
) -> dict:
    """User answer for an approval row. Subagent picks it up on next poll."""
    user_uuid = _user_uuid(current["user_id"])
    updated = resolve_approval(
        approval_id,
        str(user_uuid),
        approved=body.approved,
        answer=body.answer,
    )
    if not updated:
        raise HTTPException(404, "approval not found or already resolved")
    return {
        "approval_id": str(updated["approval_id"]),
        "status": updated["status"],
        "task_id": str(updated["task_id"]),
    }


# ---------- legacy plan-stage approve/decline (kept for back-compat) -------
@router.post("/{task_id}/approve", response_model=TaskResponse)
async def approve_task_legacy(task_id: str, current: dict = CurrentUser) -> TaskResponse:
    """
    Legacy: old flow used to create tasks as 'pending' and approve here.
    New flow creates tasks as 'running' directly from chat. This endpoint
    is kept so anything still relying on it works.
    """
    row = _set_status(task_id, _user_uuid(current["user_id"]), TaskStatus.RUNNING)
    task_runner.spawn(task_id)
    return _row_to_response(row)


@router.post("/{task_id}/decline", response_model=TaskResponse)
async def decline_task_legacy(task_id: str, current: dict = CurrentUser) -> TaskResponse:
    row = _set_status(task_id, _user_uuid(current["user_id"]), TaskStatus.CANCELLED)
    return _row_to_response(row)


# ---------- legacy in-FSM helper (now unused) ------------------------------
def persist_workshopped_plan(
    *,
    user_id: str,
    title: str,
    plan: str,
    required_tools: list[str] | None = None,
    extra_context: dict[str, Any] | None = None,
) -> str:
    """
    Kept for back-compat but not called by the new workshop_plan state.
    Inserts as status='pending' (legacy) so a separate approve call is needed.
    """
    user_uuid = _user_uuid(user_id)
    payload: dict[str, Any] = dict(extra_context or {})
    payload["title"] = title
    payload["plan"] = plan

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tasks (user_id, status, required_tools, context_payload)
                VALUES (%s, 'pending', %s, %s)
                RETURNING task_id
                """,
                (str(user_uuid), required_tools or [], json.dumps(payload)),
            )
            task_id = cur.fetchone()[0]
            conn.commit()
    finally:
        conn.close()
    return str(task_id)
