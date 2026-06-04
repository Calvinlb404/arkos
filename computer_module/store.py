"""
Persistence helpers for computer_tasks and computer_task_events.

All reads are scoped by user_id so one user cannot access another's tasks.
Mirrors the style of base_module/task_store.py.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg2
import psycopg2.extras

from config_module.loader import config


def _connect():
    return psycopg2.connect(config.get("database.url"))


# ---------- computer_tasks --------------------------------------------------

def create_computer_task(
    user_id: str,
    chat_session_id: str,
    prompt: str,
) -> str:
    """Insert a new task row (status=pending) and return the task_id."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO computer_tasks (user_id, chat_session_id, prompt)
                VALUES (%s, %s, %s)
                RETURNING task_id
                """,
                (user_id, chat_session_id, prompt),
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
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE computer_tasks
                SET status = %s,
                    summary = COALESCE(%s, summary),
                    error   = COALESCE(%s, error),
                    outputs = COALESCE(%s::jsonb, outputs),
                    updated_at = now()
                WHERE task_id = %s
                """,
                (
                    status,
                    summary,
                    error,
                    json.dumps(outputs) if outputs is not None else None,
                    task_id,
                ),
            )
            conn.commit()
    finally:
        conn.close()


def get_computer_task(task_id: str, user_id: str) -> dict[str, Any] | None:
    """Return the task row scoped to user_id, or None if not found / wrong user."""
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT task_id, user_id, chat_session_id, prompt,
                       status, summary, error, outputs, created_at, updated_at
                FROM computer_tasks
                WHERE task_id = %s AND user_id = %s
                """,
                (task_id, user_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def list_computer_tasks(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT task_id, user_id, chat_session_id, prompt,
                       status, summary, error, outputs, created_at, updated_at
                FROM computer_tasks
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (user_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# ---------- computer_task_events --------------------------------------------

def log_computer_event(
    task_id: str,
    kind: str,
    content: str = "",
    payload: dict[str, Any] | None = None,
) -> int:
    """Append an event row and return the event_id."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO computer_task_events (task_id, kind, content, payload)
                VALUES (%s, %s, %s, %s)
                RETURNING event_id
                """,
                (task_id, kind, content, json.dumps(payload or {})),
            )
            event_id = cur.fetchone()[0]
            conn.commit()
            return event_id
    finally:
        conn.close()


def list_computer_events(
    task_id: str,
    user_id: str,
    after_id: int = 0,
) -> list[dict[str, Any]]:
    """Events for a task, guarded by user ownership."""
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT e.event_id, e.kind, e.content, e.payload, e.created_at
                FROM computer_task_events e
                JOIN computer_tasks t ON t.task_id = e.task_id
                WHERE e.task_id = %s AND t.user_id = %s AND e.event_id > %s
                ORDER BY e.event_id ASC
                """,
                (task_id, user_id, after_id),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
