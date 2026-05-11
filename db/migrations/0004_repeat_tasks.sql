-- Migration: 0004_repeat_tasks
-- Creates the repeat_tasks table for scheduled recurring agent tasks.
-- Idempotent: safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS repeat_tasks (
    id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID         NOT NULL,
    name             TEXT         NOT NULL,
    instructions     TEXT         NOT NULL,
    interval_seconds INTEGER      NOT NULL CHECK (interval_seconds > 0),
    enabled          BOOLEAN      NOT NULL DEFAULT true,
    last_run_at      TIMESTAMPTZ,
    next_run_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_repeat_tasks_user_id     ON repeat_tasks (user_id);
CREATE INDEX IF NOT EXISTS idx_repeat_tasks_next_run    ON repeat_tasks (next_run_at) WHERE enabled = true;

COMMIT;
