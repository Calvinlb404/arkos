"""
Low-level DB helpers shared by the FastAPI task endpoints, the background
TaskRunner, and executor states. Kept separate from tasks.py so subagent
states can import it without pulling the FastAPI router.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg2
import psycopg2.extras

from config_module.loader import config


def _connect():
    return psycopg2.connect(config.get("database.url"))


def _user_uuid(user_id_str: str) -> uuid.UUID:
    """Parse the JWT sub (user_id) into a UUID. Demo fallback tolerates non-uuid
    strings by hashing. This is the single user_id -> tasks DB boundary; the raw
    sub is what the sandbox/Memory keyspaces use, so convert only here."""
    try:
        return uuid.UUID(user_id_str)
    except (ValueError, TypeError):
        return uuid.uuid5(uuid.NAMESPACE_URL, f"ark-legacy:{user_id_str}")


# ---------- events -----------------------------------------------------------
def log_event(
    task_id: str,
    kind: str,
    content: str = "",
    payload: dict[str, Any] | None = None,
) -> int:
    """Append a row to task_events. Safe to call from any coroutine.
    Returns the new event_id (used by computer event polling)."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO task_events (task_id, kind, content, payload)
                VALUES (%s, %s, %s, %s)
                RETURNING event_id
                """,
                (task_id, kind, content, json.dumps(payload or {})),
            )
            event_id = cur.fetchone()[0]
            conn.commit()
            return int(event_id)
    finally:
        conn.close()


def list_events(task_id: str, user_id: str, after_id: int = 0) -> list[dict[str, Any]]:
    """Return events in chronological order. after_id lets the UI poll incrementally."""
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT e.event_id, e.kind, e.content, e.payload, e.created_at
                FROM task_events e
                JOIN tasks t ON t.task_id = e.task_id
                WHERE e.task_id = %s AND t.user_id = %s AND e.event_id > %s
                ORDER BY e.event_id ASC
                """,
                (task_id, user_id, after_id),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# ---------- task status ------------------------------------------------------
def update_task_status(
    task_id: str,
    status: str,
    *,
    summary: str | None = None,
    error: str | None = None,
    outputs: list[str] | None = None,
    event_kind: str | None = None,
    event_content: str = "",
    event_payload: dict[str, Any] | None = None,
) -> None:
    """Atomic: write status + context_payload + one event row in one transaction.

    This is the single correct writer for task status. Using set_task_status,
    mark_task_completed, or mark_task_failed directly is fine for simple cases
    but those write status and event separately — a poll can see the new status
    before its event exists. update_task_status closes that window.

    event_kind defaults to the status value when omitted.
    """
    ev_kind = event_kind or status
    outputs_json = json.dumps(outputs) if outputs is not None else None

    conn = _connect()
    try:
        with conn.cursor() as cur:
            if status == "completed":
                cur.execute(
                    """
                    UPDATE tasks
                    SET status = 'completed',
                        updated_at = now(),
                        context_payload = jsonb_set(
                            jsonb_set(context_payload, '{summary}', to_jsonb(%s::text), true),
                            '{outputs}', COALESCE(%s::jsonb, context_payload->'outputs', '[]'::jsonb), true
                        )
                    WHERE task_id = %s
                    """,
                    (summary or "", outputs_json, task_id),
                )
            elif status == "failed":
                cur.execute(
                    """
                    UPDATE tasks
                    SET status = 'failed',
                        updated_at = now(),
                        context_payload = jsonb_set(
                            jsonb_set(context_payload, '{error}', to_jsonb(%s::text), true),
                            '{outputs}', COALESCE(%s::jsonb, context_payload->'outputs', '[]'::jsonb), true
                        )
                    WHERE task_id = %s
                    """,
                    (error or "", outputs_json, task_id),
                )
            else:
                cur.execute(
                    "UPDATE tasks SET status = %s, updated_at = now() WHERE task_id = %s",
                    (status, task_id),
                )
            cur.execute(
                """
                INSERT INTO task_events (task_id, kind, content, payload)
                VALUES (%s, %s, %s, %s)
                """,
                (task_id, ev_kind, event_content, json.dumps(event_payload or {})),
            )
            conn.commit()
    finally:
        conn.close()


def set_task_status(task_id: str, status: str) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET status = %s, updated_at = now() WHERE task_id = %s",
                (status, task_id),
            )
            conn.commit()
    finally:
        conn.close()


def mark_task_completed(task_id: str, summary: str | None = None, outputs: list[str] | None = None) -> None:
    """Mark completed; write summary (and optional output paths, for computer
    tasks) into context_payload."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tasks
                SET status = 'completed',
                    updated_at = now(),
                    context_payload = jsonb_set(
                        jsonb_set(context_payload, '{summary}', to_jsonb(%s::text), true),
                        '{outputs}', COALESCE(%s::jsonb, context_payload->'outputs', '[]'::jsonb), true
                    )
                WHERE task_id = %s
                """,
                (summary or "", json.dumps(outputs) if outputs is not None else None, task_id),
            )
            conn.commit()
    finally:
        conn.close()


def mark_task_failed(task_id: str, error: str, outputs: list[str] | None = None) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tasks
                SET status = 'failed',
                    updated_at = now(),
                    context_payload = jsonb_set(
                        jsonb_set(context_payload, '{error}', to_jsonb(%s::text), true),
                        '{outputs}', COALESCE(%s::jsonb, context_payload->'outputs', '[]'::jsonb), true
                    )
                WHERE task_id = %s
                """,
                (error, json.dumps(outputs) if outputs is not None else None, task_id),
            )
            conn.commit()
    finally:
        conn.close()


def get_task(task_id: str) -> dict[str, Any] | None:
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT task_id, user_id, status, required_tools, context_payload,
                       session_id, agent_kind, parent_task_id, created_at, updated_at
                FROM tasks
                WHERE task_id = %s
                """,
                (task_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


# ---------- approvals --------------------------------------------------------
def create_approval(
    task_id: str,
    user_id: str,
    kind: str,
    prompt: str,
    context: dict[str, Any] | None = None,
) -> str:
    """Insert a task_approvals row and return the approval_id."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO task_approvals (task_id, user_id, kind, prompt, context)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING approval_id
                """,
                (task_id, user_id, kind, prompt, json.dumps(context or {})),
            )
            row = cur.fetchone()
            conn.commit()
            return str(row[0])
    finally:
        conn.close()


def get_approval(approval_id: str) -> dict[str, Any] | None:
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT approval_id, task_id, user_id, kind, prompt, context,
                       status, response_bool, response_text, created_at, resolved_at
                FROM task_approvals
                WHERE approval_id = %s
                """,
                (approval_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def resolve_approval(
    approval_id: str,
    user_id: str,
    *,
    approved: bool | None = None,
    answer: str | None = None,
) -> dict[str, Any] | None:
    """
    Called by the HTTP endpoint when the user responds. The subagent's
    state_approval loop polls get_approval() and picks up the new status.
    """
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT kind, status FROM task_approvals WHERE approval_id = %s AND user_id = %s",
                (approval_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            if row["status"] != "pending":
                return None

            if row["kind"] == "binary":
                new_status = "approved" if approved else "declined"
                cur.execute(
                    """
                    UPDATE task_approvals
                    SET status = %s, response_bool = %s, resolved_at = now()
                    WHERE approval_id = %s AND user_id = %s
                    RETURNING approval_id, task_id, kind, status, response_bool, response_text
                    """,
                    (new_status, bool(approved), approval_id, user_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE task_approvals
                    SET status = 'answered', response_text = %s, resolved_at = now()
                    WHERE approval_id = %s AND user_id = %s
                    RETURNING approval_id, task_id, kind, status, response_bool, response_text
                    """,
                    (answer or "", approval_id, user_id),
                )
            conn.commit()
            updated = cur.fetchone()
            return dict(updated) if updated else None
    finally:
        conn.close()


def list_pending_approvals(user_id: str) -> list[dict[str, Any]]:
    """All pending approvals for a user, with the parent task title joined in."""
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT a.approval_id, a.task_id, a.kind, a.prompt, a.context,
                       a.created_at,
                       t.context_payload AS task_context
                FROM task_approvals a
                JOIN tasks t ON t.task_id = a.task_id
                WHERE a.user_id = %s AND a.status = 'pending'
                ORDER BY a.created_at ASC
                """,
                (user_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
