"""
Persistence for computer tasks. As of migration 0007 these are rows in the
shared `tasks` table with agent_kind='computer' (events in task_events,
approvals in task_approvals) -- one task backbone, not a separate table.

This module keeps the create_computer_task/.../list_computer_events surface so
the runner and router are unchanged; it projects the computer-shaped fields
(prompt/summary/error/outputs/chat_session_id) in and out of context_payload.
All reads are user-scoped. Does NOT run the agent or make routing decisions.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg2
import psycopg2.extras

from base_module.task_store import (
    _user_uuid,
    list_events,
    log_event,
    mark_task_completed,
    mark_task_failed,
    set_task_status,
)
from config_module.loader import config

_AGENT_KIND = "computer"


def _connect():
    return psycopg2.connect(config.get("database.url"))


def _project(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a tasks row (agent_kind='computer') into the computer-task dict the
    router's _serialize_task expects."""
    ctx = row.get("context_payload") or {}
    if isinstance(ctx, str):
        ctx = json.loads(ctx)
    return {
        "task_id": row["task_id"],
        "user_id": row["user_id"],
        "chat_session_id": ctx.get("chat_session_id", ""),
        "prompt": ctx.get("prompt", ""),
        "status": row["status"],
        "summary": ctx.get("summary") or None,
        "error": ctx.get("error") or None,
        "outputs": ctx.get("outputs") or [],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---------- computer tasks (agent_kind='computer' rows in `tasks`) -----------

def create_computer_task(user_id: str, chat_session_id: str, prompt: str) -> str:
    """Insert a computer task row (status=pending) and return the task_id."""
    payload = {
        "title": prompt[:90],          # header for the unified list + approvals panel
        "prompt": prompt,
        "chat_session_id": chat_session_id,
        "source": "computer",
        "outputs": [],
        "summary": "",
        "error": "",
    }
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tasks (user_id, status, agent_kind, session_id, context_payload)
                VALUES (%s, 'pending', %s, %s, %s)
                RETURNING task_id
                """,
                (
                    str(_user_uuid(user_id)),
                    _AGENT_KIND,
                    str(uuid.uuid4()),
                    json.dumps(payload),
                ),
            )
            task_id = str(cur.fetchone()[0])
            conn.commit()
            return task_id
    finally:
        conn.close()


def set_computer_status(
    task_id: str,
    status: str,
    *,
    summary: str | None = None,
    error: str | None = None,
    outputs: list[str] | None = None,
) -> None:
    """Update a computer task's status. Terminal states route through the shared
    task_store helpers so summary/error/outputs land in context_payload."""
    if status == "completed":
        mark_task_completed(task_id, summary, outputs)
    elif status == "failed":
        mark_task_failed(task_id, error or "", outputs)
    else:
        set_task_status(task_id, status)


def get_computer_task(task_id: str, user_id: str) -> dict[str, Any] | None:
    """Return the computer task scoped to user_id (and agent_kind), or None.
    Self-scoped because task_store.get_task is not user-scoped."""
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT task_id, user_id, status, context_payload, created_at, updated_at
                FROM tasks
                WHERE task_id = %s AND user_id = %s AND agent_kind = %s
                """,
                (task_id, str(_user_uuid(user_id)), _AGENT_KIND),
            )
            row = cur.fetchone()
            return _project(dict(row)) if row else None
    finally:
        conn.close()


def list_computer_tasks(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT task_id, user_id, status, context_payload, created_at, updated_at
                FROM tasks
                WHERE user_id = %s AND agent_kind = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (str(_user_uuid(user_id)), _AGENT_KIND, limit),
            )
            return [_project(dict(r)) for r in cur.fetchall()]
    finally:
        conn.close()


# ---------- events (shared task_events) --------------------------------------

def log_computer_event(
    task_id: str,
    kind: str,
    content: str = "",
    payload: dict[str, Any] | None = None,
) -> int:
    """Append an event and return the event_id."""
    return log_event(task_id, kind, content, payload)


def list_computer_events(
    task_id: str,
    user_id: str,
    after_id: int = 0,
) -> list[dict[str, Any]]:
    """Events for a task, guarded by user ownership (via the shared join)."""
    return list_events(task_id, str(_user_uuid(user_id)), after_id)
