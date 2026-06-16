# Feature Spec: Task Surfacing — Issues & Fixes

**Sources**

- Backend surfacing map: `base_module/tasks.py`, `base_module/task_store.py`, `computer_module/store.py`, `computer_module/runner.py`, `computer_module/computer_router.py`, `state_module/agent_executor/state_approval.py`, `db/migrations/0001,0003,0006,0007`
- Frontend map: `frontend/app.jsx`, `frontend/seed.jsx`, `frontend/views.jsx`, `frontend/components.jsx` (the **only** served UI — `arkos-webui` is not used)
- Companion specs: `HARNESS_SPEC.md`, `MEMORY_SPEC.md`, `ROLLBACK_SPEC.md`

**Status:** Not started | **Author:** | **Last updated:** 2026-06-14

---

# Problem

The database stores task/approval/event state correctly, but the UI surfaces it
brittlely — finished tasks disappear, approvals reappear after being resolved,
and some rows never surface at all. The cause is structural: task state lives in
**four sources of truth** (`tasks.status`, `task_events`, `task_approvals`,
`conversation_context`) with **no single stream the UI subscribes to**. The
frontend reconstructs the picture by polling several endpoints on a 6-second
timer, so any gap between "DB is correct" and "reconstructed view is correct
right now" shows as flakiness. Two concrete mechanisms produce the user-visible
"stores but doesn't surface":

1. **The UI never asks for finished work.** `refreshAll` polls only
   `running` and `awaiting_approval` (`app.jsx:111`). A task that completes is
   `completed`/`failed` — in neither bucket — so it drops out of the list on the
   next poll. The DONE state, and the computer-task badge count (`views.jsx:170`),
   are structurally unobservable.

2. **Two user-id keyspaces, read with the wrong one.** Tasks/approvals are scoped
   by `_user_uuid(sub)` → a derived **UUID** (`task_store.py:23`); Memory/sandbox/
   `conversation_context` use the **raw `sub`**. Frontend GET reads send
   **Bearer only**; POST writes send **Bearer + `X-User-ID`** (`seed.jsx`); and
   backend endpoints are **mixed** — some scope by the JWT `sub` (`CurrentUser`),
   others by the `X-User-ID` header (`app.py:238,299,344,371,505`). If
   `X-User-ID` ever differs from the JWT `sub`, a row is written under one
   identity and listed under another.

Secondary brittleness: optimistic approval removal that doesn't roll back on
failure (`app.jsx:166`), silent `ark-plan` fence parse failures (`seed.jsx:50`),
and a backend status machine with three writers and a status/event ordering
window.

**Success looks like:** a finished task stays visible with its DONE state; a row
created in the UI always appears in that same UI; approvals fail loud, not
silent; and task status is written by one path, atomically with its event.

---

# Technical Background

**The four sources of truth and who writes them:**
- `tasks.status` — written by `set_task_status`, `mark_task_completed/failed`
  (`task_store.py:81-137`) and the `set_computer_status` wrapper
  (`computer_module/store.py:94-109`). Three writers.
- `task_events` — append-only progress, written separately from status.
- `task_approvals` — created by `create_approval`, resolved by `resolve_approval`;
  polled every 2s by `state_approval.py` and `computer_module/runner.py`.
- `conversation_context` — the chat "Done." message, injected under the **raw
  sub** by `runner.py` Memory.add_memory.

**The keyspace boundary** (`task_store.py:23`): `_user_uuid()` converts the JWT
`sub` to a deterministic UUID. This is the single tasks/approvals boundary; the
sandbox and Memory keyspaces use the raw `sub`. The split is internally
consistent *only if every endpoint derives identity the same way* — which today
it does not (some use `CurrentUser`/JWT, some use the `X-User-ID` header).

**Unification status:** migrations `0006→0007` merged `computer_tasks` into the
`tasks` table (`agent_kind='computer'`), and events/approvals are now shared. The
projection `_project()` (`store.py:38`) reshapes a unified row back into the
computer-task shape by reading `context_payload`, which is only written on
terminal states — so mid-flight reads can see stale summary/outputs.

**Decisive diagnostic** (splits Mechanism 1 from 2; run while a "missing" task is
on screen):
```sql
select task_id, status, agent_kind, user_id from tasks order by created_at desc limit 10;
```
Row present with `status=completed` → Mechanism 1. `user_id` not matching
`_user_uuid(your_sub)`, or two `user_id`s for "you" → Mechanism 2.

**Not in scope:** the `arkos-webui` Svelte UI (not served — ignore/delete).
Lower-priority backend hardening (SSE terminal-event race
`computer_router.py:146`, orphan double-execution `task_runner.py:250`) is
deferred to Open Questions — real but not the everyday flakiness.

---

# Proposed Approach

Fix the two "doesn't surface" mechanisms first (highest visible impact), then make
approvals fail loud, then collapse the backend status machine to one atomic
writer. Nothing here changes the state-graph design; the edits live in the
frontend fetch layer, the identity-resolution seam, and the status-write helpers.

What stays the same: the `tasks`/`task_events`/`task_approvals` schema, the router
state graph, and the SSE endpoint that already exists for computer tasks.

---

# Implementation Plan

## Task 1: Surface finished tasks in the list

**Problem:** The list polls only `running` + `awaiting_approval`, so a completed
or failed task vanishes on the next poll; DONE never shows and the computer badge
undercounts.

**Done when:**
- `refreshAll` also fetches `completed` and `failed` (bounded to a recent window,
  e.g. last N or last 24h) and merges them into the list.
- The task row renders its terminal state (`done`/`stop`) and persists across
  polls; the computer-task badge counts terminal states correctly.

**Touch point:** `frontend/app.jsx:111-138`, `frontend/seed.jsx` (`api.tasks`),
`frontend/views.jsx:62-73,170`.

**Priority:** P0 | **Effort:** ~0.5 day | **Blockers:** none

**Out of scope:** Replacing polling with SSE (Open Question 1).

**Acceptance test:** `test_completed_task_stays_visible` (below).

---

## Task 2: One identity source across reads and writes

**Problem:** Reads scope by JWT `sub`, some writes by the `X-User-ID` header; if
they differ, a row is stored under one user_id and listed under another
("stores but doesn't surface").

**Done when:**
- Every task/approval/computer endpoint derives identity from one source — the
  verified JWT `sub` via `CurrentUser`. The `X-User-ID` header is no longer used
  for scoping (removed from `app.py:238,299,344,371,505` and from the frontend
  POST headers, or explicitly ignored server-side).
- Reads and writes resolve to the same `user_id`; the diagnostic query shows a
  single `user_id` for one logged-in session.

**Touch point:** `base_module/app.py` (X-User-ID lines), `computer_module/
computer_router.py`, `frontend/seed.jsx` (auth headers), `task_store.py:23`
(`_user_uuid` stays the one boundary).

**Priority:** P0 | **Effort:** ~1 day | **Blockers:** none

**Out of scope:** Collapsing the UUID vs raw-sub keyspaces themselves (Memory
still uses raw sub — acceptable as long as identity *resolution* is single-source;
revisit in Open Question 2).

**Acceptance test:** `test_task_created_in_ui_appears_in_same_ui` (below).

---

## Task 3: Approvals fail loud, not silent

**Problem:** Optimistic approval removal isn't rolled back on failure (resolved in
UI, still `pending` in DB → reappears next session); a malformed `ark-plan` fence
is parsed away silently, leaving a plan with no approve control.

**Done when:**
- `respondApproval` throws on non-2xx; the approval is removed from the UI only
  after server confirmation (or restored if the call fails).
- `parsePlan` failure surfaces an inline "couldn't parse plan" affordance instead
  of silently dropping the block.

**Touch point:** `frontend/app.jsx:164-173,227-243`, `frontend/seed.jsx:50-59`
(`parsePlan`), `seed.jsx` (`respondApproval`).

**Priority:** P1 | **Effort:** ~1 day | **Blockers:** none

**Out of scope:** Backend approval lifecycle changes (Task 4).

**Acceptance test:** `test_failed_respond_restores_approval`,
`test_malformed_ark_plan_surfaces_error` (below).

---

## Task 4: One atomic status writer (backend)

**Problem:** Three writers touch `tasks.status` (`set_task_status`,
`set_computer_status`, `mark_task_completed/failed`), and status is written
separately from its event — a poll can see a status with no matching event, or a
terminal status with stale `context_payload`.

**Done when:**
- A single `update_task_status(task_id, status, *, summary=None, error=None,
  outputs=None, event=None)` writes status, `context_payload`, and the
  corresponding `task_events` row in **one transaction**.
- Both the executor and computer paths route through it; the
  `set_computer_status` wrapper either delegates to it or is removed.

**Touch point:** `base_module/task_store.py:81-137`, `computer_module/store.py:
94-109`, emit sites in `computer_module/runner.py` and
`state_module/agent_executor/state_approval.py`.

**Priority:** P1 | **Effort:** ~1.5 days | **Blockers:** none

**Out of scope:** SSE terminal-event race and orphan resume (Open Question 3).

**Acceptance test:** `test_status_and_event_are_atomic`,
`test_single_status_writer` (below).

---

# Tests

## Test 1: test_completed_task_stays_visible

**What it verifies:** A task transitioned to `completed` is still returned by the
list the UI fetches and renders with a terminal state across two consecutive
polls.

**Why this matters:** Disappearing finished tasks is the #1 visible symptom; the
test pins that completion is observable, not just stored.

---

## Test 2: test_task_created_in_ui_appears_in_same_ui

**What it verifies:** A task created through the dispatch path (POST) is returned
by the list path (GET) for the same authenticated session — i.e. write-identity
and read-identity resolve to the same `user_id`.

**Why this matters:** This is the "stores but doesn't surface" identity bug; the
test guarantees one identity end-to-end.

---

## Test 3: test_failed_respond_restores_approval

**What it verifies:** When the approval-respond call fails, the approval is
restored in the UI state (not left optimistically removed) and remains
resolvable.

**Why this matters:** Silent optimistic removal produces zombie approvals that
reappear; the test pins fail-loud behavior.

---

## Test 4: test_malformed_ark_plan_surfaces_error

**What it verifies:** A buddy reply with a malformed `ark-plan` fence renders a
visible parse-error affordance rather than silently dropping the block.

**Why this matters:** A plan with no approve button looks like the system did
nothing; surfacing the failure is the difference between "broken" and "told me
why."

---

## Test 5: test_status_and_event_are_atomic

**What it verifies:** A status transition and its corresponding event are written
in one transaction — a reader never observes the new status without its event (or
vice versa).

**Why this matters:** The status/event ordering window is a core source of
"flaky for a second" surfacing; atomicity closes it.

---

## Test 6: test_single_status_writer

**What it verifies:** Both executor and computer terminal transitions go through
the one `update_task_status` helper, which sets status + `context_payload`
together (summary/outputs never null on a terminal row).

**Why this matters:** Multiple writers + payload-only-on-terminal causes stale or
inconsistent rows; one writer makes the terminal row self-consistent.

---

# Open Questions

1. Replace the 6s task polling with the SSE pattern already used for computer
   tasks (`/computer/tasks/{id}/stream`), extended to all tasks? Removes the
   poll-window flakiness entirely but is a larger change. *Leaning: do Tasks 1–4
   first; SSE as a fast-follow once identity and the writer are clean.*
2. Should the UUID vs raw-sub keyspaces be unified (make Memory use the UUID too),
   or is single-source *resolution* (Task 2) enough? Unifying is cleaner but
   touches the memory layer (see `MEMORY_SPEC.md`).
3. Backend hardening pass for the SSE terminal-event race (`computer_router.py:
   146`) and orphan double-execution on restart (`task_runner.py:250`) — real but
   not everyday flakiness; bundle separately?
4. Should the finished-task list be time-bounded (last 24h / last N) to avoid
   unbounded growth once completed tasks are fetched (Task 1)?

---

# Implementation Notes

*Add entries here as work lands.*

- `arkos-webui` (Svelte) is **not served** — only `/frontend` is. It has no auth
  and no task handling; ignore it in all surfacing work, or delete it to remove
  the temptation to "use both UIs."
- Run the decisive diagnostic query (Technical Background) before starting: it
  tells you whether the first missing task you see is Mechanism 1 (Task 1) or
  Mechanism 2 (Task 2), so you fix the one that's actually biting first.
