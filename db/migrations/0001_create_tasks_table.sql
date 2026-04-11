-- Migration: 0001_create_tasks_table
-- Creates the task queue table for persisting and tracking agent tasks.

BEGIN;

CREATE TYPE task_status AS ENUM (
    'pending',
    'running',
    'completed',
    'failed',
    'cancelled'
);

CREATE TABLE tasks (
    task_id         UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID            NOT NULL,
    status          task_status     NOT NULL DEFAULT 'pending',
    required_tools  TEXT[]          NOT NULL DEFAULT '{}',
    context_payload JSONB           NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT now()
);

CREATE INDEX idx_tasks_user_id ON tasks (user_id);
CREATE INDEX idx_tasks_status  ON tasks (status);
CREATE INDEX idx_tasks_created_at ON tasks (created_at);

-- Keep updated_at current on every row modification.
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER tasks_set_updated_at
BEFORE UPDATE ON tasks
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMIT;
