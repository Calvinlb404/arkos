-- Migration: 0007_unify_computer_tasks
-- Unify the computer task system into the executor `tasks` table.
-- Computer tasks now live in `tasks` with agent_kind = 'computer' (a slot the
-- table has had since 0003), their events in `task_events`, and their
-- human-in-the-loop in `task_approvals`. This makes one task backbone -- the
-- approvals JOIN now reaches computer tasks, and the frontend reads one list.
--
-- The old computer_tasks / computer_task_events tables are dropped. Existing
-- rows are dev/test data and are intentionally NOT migrated (the user_id was
-- TEXT vs the tasks UUID, and the data has no lasting value). The persistent
-- sandbox lifecycle (user_sandboxes) and the sandbox filesystems are untouched.
-- Idempotent: safe to re-run.

BEGIN;

-- events first (FK -> computer_tasks ON DELETE CASCADE)
DROP TABLE IF EXISTS computer_task_events;
DROP TABLE IF EXISTS computer_tasks;

COMMIT;
