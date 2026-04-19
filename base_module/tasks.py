"""
Task queue API. Persists to the `tasks` table (see db/migrations/0001_create_tasks_table.sql).

Lifecycle driven by the agent FSM:
    workshop_plan state writes a row with status=pending and a plan in context_payload.
    User approves:  PATCH /tasks/{id}/approve  -> status=running
    Agent runs:     status=running -> completed (or failed)
    User declines:  PATCH /tasks/{id}/decline  -> status=cancelled

All endpoints are scoped to the caller's user_id from the Bearer token.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from base_module.jwt_utils import CurrentUser
from config_module.loader import config


router = APIRouter(prefix="/tasks", tags=["tasks"])


# ---------- enums + schemas ----------
class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskCreate(BaseModel):
    title: str = Field(..., max_length=280)
    plan: str | None = None
    required_tools: list[str] = Field(default_factory=list)
    context_payload: dict[str, Any] = Field(default_factory=dict)


class TaskResponse(BaseModel):
    task_id: str
    user_id: str
    status: TaskStatus
    title: str
    plan: str | None
    required_tools: list[str]
    context_payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    tasks: list[TaskResponse]
    total: int


class StatusUpdateRequest(BaseModel):
    status: TaskStatus


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
        required_tools=list(row["required_tools"] or []),
        context_payload=payload,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _user_uuid(user_id_str: str) -> uuid.UUID:
    """Parse the JWT sub (user_id) into a UUID. Demo fallback tolerates non-uuid strings by hashing."""
    try:
        return uuid.UUID(user_id_str)
    except (ValueError, TypeError):
        # Deterministic fallback so X-User-ID-based legacy callers still hit a stable uuid
        return uuid.uuid5(uuid.NAMESPACE_URL, f"ark-legacy:{user_id_str}")


# ---------- endpoints ----------
@router.post("", response_model=TaskResponse)
async def create_task(body: TaskCreate, current: dict = CurrentUser) -> TaskResponse:
    """POST /tasks. Writes a task row owned by the authenticated user."""
    user_uuid = _user_uuid(current["user_id"])
    payload = dict(body.context_payload or {})
    payload.setdefault("title", body.title)
    if body.plan:
        payload["plan"] = body.plan

    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO tasks (user_id, status, required_tools, context_payload)
                VALUES (%s, 'pending', %s, %s)
                RETURNING task_id, user_id, status, required_tools, context_payload, created_at, updated_at
                """,
                (str(user_uuid), body.required_tools, json.dumps(payload)),
            )
            row = cur.fetchone()
            conn.commit()
    finally:
        conn.close()
    return _row_to_response(row)


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    status: TaskStatus | None = Query(default=None),
    current: dict = CurrentUser,
) -> TaskListResponse:
    """GET /tasks?status=<status>. Lists tasks for the authenticated user."""
    user_uuid = _user_uuid(current["user_id"])

    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if status is None:
                cur.execute(
                    """
                    SELECT task_id, user_id, status, required_tools, context_payload, created_at, updated_at
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
                    SELECT task_id, user_id, status, required_tools, context_payload, created_at, updated_at
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
                SELECT task_id, user_id, status, required_tools, context_payload, created_at, updated_at
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


def _set_status(task_id: str, user_uuid: uuid.UUID, new_status: TaskStatus) -> dict[str, Any]:
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE tasks
                SET status = %s
                WHERE task_id = %s AND user_id = %s
                RETURNING task_id, user_id, status, required_tools, context_payload, created_at, updated_at
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
async def update_task_status(
    task_id: str, body: StatusUpdateRequest, current: dict = CurrentUser
) -> TaskResponse:
    row = _set_status(task_id, _user_uuid(current["user_id"]), body.status)
    return _row_to_response(row)


@router.post("/{task_id}/approve", response_model=TaskResponse)
async def approve_task(task_id: str, current: dict = CurrentUser) -> TaskResponse:
    """Transition a pending (workshopped) task to running."""
    row = _set_status(task_id, _user_uuid(current["user_id"]), TaskStatus.RUNNING)
    return _row_to_response(row)


@router.post("/{task_id}/decline", response_model=TaskResponse)
async def decline_task(task_id: str, current: dict = CurrentUser) -> TaskResponse:
    """Transition a pending (workshopped) task to cancelled."""
    row = _set_status(task_id, _user_uuid(current["user_id"]), TaskStatus.CANCELLED)
    return _row_to_response(row)


# ---------- internal helper used by the workshop_plan FSM state ----------
def persist_workshopped_plan(
    *,
    user_id: str,
    title: str,
    plan: str,
    required_tools: list[str] | None = None,
    extra_context: dict[str, Any] | None = None,
) -> str:
    """Called from state_plan.py after the agent workshops a plan with the user."""
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
