-- Migration: 0005_user_sandboxes
-- One persistent e2b sandbox per user (the user's "computer"). We store the
-- e2b sandbox_id and resume by it via Sandbox.connect(); the filesystem
-- persists across pause/resume. Idempotent: safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS user_sandboxes (
    user_id        TEXT         PRIMARY KEY,
    e2b_sandbox_id TEXT         NOT NULL,
    status         TEXT         NOT NULL DEFAULT 'active',   -- active | paused
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_active_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);

COMMIT;
