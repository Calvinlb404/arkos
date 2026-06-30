-- Migration: 0006_computer_tasks
-- Per-user async computer tasks dispatched by buddy.
-- chat_session_id links back to the conversation so the runner can inject
-- the completion message directly into the user's chat on finish.
-- Idempotent: safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS computer_tasks (
    task_id         UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT         NOT NULL,
    chat_session_id TEXT         NOT NULL,
    prompt          TEXT         NOT NULL,
    status          TEXT         NOT NULL DEFAULT 'pending',  -- pending|running|completed|failed
    summary         TEXT,
    error           TEXT,
    outputs         JSONB        NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_computer_tasks_user
    ON computer_tasks (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS computer_task_events (
    event_id    BIGSERIAL    PRIMARY KEY,
    task_id     UUID         NOT NULL REFERENCES computer_tasks(task_id) ON DELETE CASCADE,
    kind        TEXT         NOT NULL,   -- shell|file|search|plan|ask|completed|failed|start
    content     TEXT         NOT NULL DEFAULT '',
    payload     JSONB        NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_computer_task_events_task
    ON computer_task_events (task_id, event_id ASC);

COMMIT;
