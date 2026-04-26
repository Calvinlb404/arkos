"""Tests for base_module/tasks.py — pure helpers and Pydantic schemas.

The HTTP endpoints all hit a live Postgres (via _connect()) and require
JWT auth, so they're covered by integration tests on a real deployment
rather than unit-tested here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from base_module.tasks import (
    ApprovalCard,
    ApprovalListResponse,
    ApprovalResponseBody,
    StatusUpdateRequest,
    TaskCreate,
    TaskEvent,
    TaskEventsResponse,
    TaskListResponse,
    TaskResponse,
    TaskStatus,
    _row_to_response,
    _user_uuid,
)


class TestTaskStatus:
    def test_includes_all_lifecycle_states(self):
        assert TaskStatus.PENDING == "pending"
        assert TaskStatus.RUNNING == "running"
        assert TaskStatus.AWAITING_APPROVAL == "awaiting_approval"
        assert TaskStatus.COMPLETED == "completed"
        assert TaskStatus.FAILED == "failed"
        assert TaskStatus.CANCELLED == "cancelled"

    def test_is_str_enum_for_json_serialization(self):
        # StrEnum subclasses str, which lets Pydantic + json.dumps emit the
        # raw string value without an explicit serializer.
        assert isinstance(TaskStatus.RUNNING, str)
        assert TaskStatus.RUNNING == "running"


class TestUserUuid:
    def test_passes_through_real_uuid(self):
        u = uuid.uuid4()
        assert _user_uuid(str(u)) == u

    def test_hashes_legacy_string(self):
        # Non-UUID inputs (legacy header-based ids) should map deterministically
        # to the same UUID5 every time so DB rows for the same legacy user line up.
        a = _user_uuid("kshitij")
        b = _user_uuid("kshitij")
        assert a == b
        assert isinstance(a, uuid.UUID)

    def test_legacy_strings_map_to_distinct_uuids(self):
        assert _user_uuid("alice") != _user_uuid("bob")

    def test_handles_non_string_input(self):
        # The signature is typed as str but the try/except also covers None
        # via TypeError. Make sure that path produces a deterministic uuid.
        result = _user_uuid(None)  # type: ignore[arg-type]
        assert isinstance(result, uuid.UUID)


class TestRowToResponse:
    def _row(self, **overrides):
        base = {
            "task_id": uuid.uuid4(),
            "user_id": uuid.uuid4(),
            "status": "running",
            "required_tools": ["search", "calendar"],
            "context_payload": {"title": "do the thing", "plan_steps": ["a", "b"]},
            "session_id": uuid.uuid4(),
            "agent_kind": "executor",
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        base.update(overrides)
        return base

    def test_full_row_round_trips_to_response(self):
        row = self._row()
        resp = _row_to_response(row)
        assert isinstance(resp, TaskResponse)
        assert resp.title == "do the thing"
        assert resp.plan_steps == ["a", "b"]
        assert resp.required_tools == ["search", "calendar"]
        assert resp.status == TaskStatus.RUNNING
        assert resp.agent_kind == "executor"

    def test_handles_string_encoded_payload(self):
        # Postgres can return jsonb columns as already-decoded dicts OR as
        # strings depending on the driver/cursor configuration. Both must work.
        row = self._row(context_payload='{"title": "json string", "plan_steps": ["x"]}')
        resp = _row_to_response(row)
        assert resp.title == "json string"
        assert resp.plan_steps == ["x"]

    def test_handles_missing_session_id(self):
        row = self._row(session_id=None)
        resp = _row_to_response(row)
        assert resp.session_id is None

    def test_handles_empty_context_payload(self):
        row = self._row(context_payload=None)
        resp = _row_to_response(row)
        assert resp.title == ""
        assert resp.plan_steps == []
        assert resp.context_payload == {}

    def test_handles_missing_required_tools(self):
        row = self._row(required_tools=None)
        resp = _row_to_response(row)
        assert resp.required_tools == []


class TestTaskCreateSchema:
    def test_minimal_payload(self):
        tc = TaskCreate(title="t", plan_steps=["one step"])
        assert tc.title == "t"
        assert tc.plan_steps == ["one step"]
        assert tc.required_tools == []
        assert tc.context_payload == {}
        assert tc.plan is None

    def test_title_max_length_enforced(self):
        with pytest.raises(ValueError):
            TaskCreate(title="x" * 281, plan_steps=["a"])

    def test_full_payload(self):
        tc = TaskCreate(
            title="ship it",
            plan_steps=["pull main", "rebase", "push"],
            plan="1. pull main\n2. rebase\n3. push",
            required_tools=["git"],
            context_payload={"branch": "main"},
        )
        assert tc.required_tools == ["git"]
        assert tc.context_payload == {"branch": "main"}


class TestStatusUpdateRequest:
    def test_accepts_valid_status(self):
        s = StatusUpdateRequest(status=TaskStatus.COMPLETED)
        assert s.status == TaskStatus.COMPLETED

    def test_rejects_invalid_status(self):
        with pytest.raises(ValueError):
            StatusUpdateRequest(status="not-a-real-status")  # type: ignore[arg-type]


class TestApprovalSchemas:
    def test_approval_response_defaults(self):
        ar = ApprovalResponseBody()
        assert ar.approved is None
        assert ar.answer is None

    def test_approval_card_round_trips(self):
        card = ApprovalCard(
            approval_id="a1",
            task_id="t1",
            task_title="my task",
            kind="binary",
            prompt="approve?",
            context={"step": 2},
            created_at=datetime.now(UTC),
        )
        listed = ApprovalListResponse(approvals=[card], total=1)
        assert listed.total == 1
        assert listed.approvals[0].kind == "binary"


class TestEventSchemas:
    def test_task_event_minimal(self):
        ev = TaskEvent(event_id=1, kind="step_started", content="step 1", payload={}, created_at=datetime.now(UTC))
        assert ev.event_id == 1
        assert ev.kind == "step_started"

    def test_events_response_pagination_cursor(self):
        ev = TaskEvent(event_id=5, kind="info", content="x", payload={}, created_at=datetime.now(UTC))
        resp = TaskEventsResponse(events=[ev], next_after=5)
        assert resp.next_after == 5


class TestTaskListResponse:
    def test_empty_list(self):
        resp = TaskListResponse(tasks=[], total=0)
        assert resp.total == 0
        assert resp.tasks == []
