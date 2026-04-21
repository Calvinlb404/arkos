-- One-shot cleanup for legacy "ghost" task rows that were minted by the old
-- state_plan flow (status='pending' or 'running' with no subagent actually
-- executing them). Run this once after migration 0003 lands.
--
-- Safe to run multiple times: only flips rows that are still stuck in the
-- pre-rewrite states.

BEGIN;

-- 1. Cancel any row still stuck in the legacy 'pending' status. The new
--    architecture never creates 'pending' rows; every approved plan goes
--    straight to 'running' with a live asyncio task.
UPDATE tasks
SET status = 'cancelled',
    updated_at = NOW()
WHERE status = 'pending';

-- 2. Cancel rows stuck in 'running' that have zero task_events. A genuinely
--    running subagent logs a 'started' event within the first second, so
--    a running row with no events is an orphan left over from before the
--    sweep_orphans() hook existed.
UPDATE tasks
SET status = 'cancelled',
    updated_at = NOW()
WHERE status = 'running'
  AND task_id NOT IN (SELECT DISTINCT task_id FROM task_events);

-- 3. Report what got cleaned.
SELECT status, COUNT(*) AS row_count
FROM tasks
GROUP BY status
ORDER BY status;

COMMIT;
