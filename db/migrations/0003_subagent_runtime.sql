-- Migration: 0003_subagent_runtime
-- Adds the plumbing required for background subagent tasks:
--   * tasks.session_id / agent_kind / parent_task_id  (per-task memory row + lineage)
--   * task_status gains 'awaiting_approval'
--   * task_events     (append-only progress log)
--   * task_approvals  (human-in-the-loop checkpoints; binary OR free text)
--
-- Idempotent: safe to re-run.

BEGIN;

-- ---- extend task_status enum ----------------------------------------------
-- Postgres refuses to add enum values inside a transaction in some versions;
-- keep the enum extension in a DO block that's tolerant of existing members.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_enum e
        JOIN pg_type t ON t.oid = e.enumtypid
        WHERE t.typname = 'task_status'
          AND e.enumlabel = 'awaiting_approval'
    ) THEN
        ALTER TYPE task_status ADD VALUE 'awaiting_approval';
    END IF;
END$$;

-- ---- tasks columns --------------------------------------------------------
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS session_id      UUID;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS agent_kind      TEXT NOT NULL DEFAULT 'executor';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS parent_task_id  UUID REFERENCES tasks(task_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks (parent_task_id);

-- ---- task_events ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_events (
    event_id    BIGSERIAL       PRIMARY KEY,
    task_id     UUID            NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    kind        TEXT            NOT NULL,
    content     TEXT            NOT NULL DEFAULT '',
    payload     JSONB           NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_task_events_task_id   ON task_events (task_id, event_id);
CREATE INDEX IF NOT EXISTS idx_task_events_created   ON task_events (created_at);

-- ---- task_approvals -------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'approval_kind') THEN
        CREATE TYPE approval_kind AS ENUM ('binary', 'text');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'approval_status') THEN
        CREATE TYPE approval_status AS ENUM ('pending', 'approved', 'declined', 'answered', 'expired');
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS task_approvals (
    approval_id     UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         UUID             NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    user_id         UUID             NOT NULL,
    kind            approval_kind    NOT NULL,
    prompt          TEXT             NOT NULL,
    context         JSONB            NOT NULL DEFAULT '{}'::jsonb,
    status          approval_status  NOT NULL DEFAULT 'pending',
    response_bool   BOOLEAN,
    response_text   TEXT,
    created_at      TIMESTAMPTZ      NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_approvals_task        ON task_approvals (task_id);
CREATE INDEX IF NOT EXISTS idx_approvals_user        ON task_approvals (user_id);
CREATE INDEX IF NOT EXISTS idx_approvals_pending     ON task_approvals (user_id, status)
    WHERE status = 'pending';

COMMIT;
