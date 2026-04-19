"""Tests for task queue API endpoints (base_module/tasks.py)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from base_module.tasks import _tasks_store, router


@pytest.fixture
def client():
    """Create test client with tasks router."""
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def clear_tasks():
    """Clear in-memory task store before each test."""
    _tasks_store.clear()
    yield
    _tasks_store.clear()


class TestCreateTask:
    def test_post_returns_200_with_task_id(self, client):
        """POST /tasks returns 200 with task_id"""
        payload = {
            "user_id": "test-user",
            "required_tools": ["tool1", "tool2"],
            "context_payload": {"key": "value"}
        }
        response = client.post("/tasks", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert "task_id" in data
        assert data["user_id"] == "test-user"
        assert data["status"] == "pending"
        assert data["required_tools"] == ["tool1", "tool2"]
        assert data["context_payload"] == {"key": "value"}

    def test_post_minimal_payload(self, client):
        """POST /tasks with minimal payload"""
        payload = {"user_id": "user123"}
        response = client.post("/tasks", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["required_tools"] == []
        assert data["context_payload"] == {}


class TestListTasks:
    def test_get_returns_list(self, client):
        """GET /tasks?user_id= returns list"""
        # Create a task first
        client.post("/tasks", json={"user_id": "user-a"})
        client.post("/tasks", json={"user_id": "user-a"})
        client.post("/tasks", json={"user_id": "user-b"})

        # List tasks for user-a
        response = client.get("/tasks?user_id=user-a")

        assert response.status_code == 200
        data = response.json()
        assert "tasks" in data
        assert "total" in data
        assert data["total"] == 2
        assert len(data["tasks"]) == 2
        assert all(t["user_id"] == "user-a" for t in data["tasks"])

    def test_get_empty_list(self, client):
        """GET /tasks returns empty list for user with no tasks"""
        response = client.get("/tasks?user_id=nonexistent")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["tasks"] == []

    def test_get_requires_user_id(self, client):
        """GET /tasks without user_id param returns 422"""
        response = client.get("/tasks")
        assert response.status_code == 422


class TestUpdateTaskStatus:
    def test_patch_updates_status(self, client):
        """PATCH /tasks/{id}/status updates status"""
        # Create a task
        create_resp = client.post("/tasks", json={"user_id": "user1"})
        task_id = create_resp.json()["task_id"]

        # Update status
        payload = {"status": "running"}
        response = client.patch(f"/tasks/{task_id}/status", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"
        assert data["task_id"] == task_id

    def test_patch_multiple_status_transitions(self, client):
        """PATCH can update status multiple times"""
        create_resp = client.post("/tasks", json={"user_id": "user1"})
        task_id = create_resp.json()["task_id"]

        # pending -> running
        client.patch(f"/tasks/{task_id}/status", json={"status": "running"})

        # running -> completed
        response = client.patch(f"/tasks/{task_id}/status", json={"status": "completed"})
        assert response.json()["status"] == "completed"

    def test_patch_nonexistent_task_returns_404(self, client):
        """PATCH nonexistent task returns 404"""
        payload = {"status": "completed"}
        response = client.patch("/tasks/nonexistent-id/status", json=payload)

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_patch_accepts_all_statuses(self, client):
        """PATCH accepts all valid TaskStatus values"""
        create_resp = client.post("/tasks", json={"user_id": "user1"})
        task_id = create_resp.json()["task_id"]

        for status in ["pending", "running", "completed", "failed", "cancelled"]:
            response = client.patch(
                f"/tasks/{task_id}/status",
                json={"status": status}
            )
            assert response.status_code == 200
            assert response.json()["status"] == status


class TestTaskTimestamps:
    def test_created_at_set_on_creation(self, client):
        """Task has created_at when created"""
        response = client.post("/tasks", json={"user_id": "user1"})
        task = response.json()
        assert "created_at" in task
        assert task["created_at"] is not None

    def test_updated_at_changes_on_status_update(self, client):
        """Task updated_at changes when updated"""
        create_resp = client.post("/tasks", json={"user_id": "user1"})
        task_id = create_resp.json()["task_id"]
        created_at = create_resp.json()["created_at"]

        # Update status
        import time
        time.sleep(0.1)  # Ensure time difference
        update_resp = client.patch(
            f"/tasks/{task_id}/status",
            json={"status": "running"}
        )
        updated_at = update_resp.json()["updated_at"]

        assert updated_at != created_at
