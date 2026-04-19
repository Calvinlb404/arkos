"""
Task queue API router for submitting, querying, and updating tasks.
Stubs for persistence; schema defined in db/migrations/0001_create_tasks_table.sql
"""

import uuid
from datetime import datetime
from enum import StrEnum

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/tasks", tags=["tasks"])


class TaskStatus(StrEnum):
    """Task status values matching db schema."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskRequest(BaseModel):
    """Request model for creating a task."""
    user_id: str
    required_tools: list[str] | None = None
    context_payload: dict | None = None


class TaskResponse(BaseModel):
    """Response model for a task."""
    task_id: str
    user_id: str
    status: TaskStatus
    required_tools: list[str]
    context_payload: dict
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    """Response model for task list with metadata."""
    tasks: list[TaskResponse]
    total: int


class StatusUpdateRequest(BaseModel):
    """Request model for updating task status."""
    status: TaskStatus


# In-memory store for stubs (will be replaced by DB calls)
_tasks_store: dict[str, dict] = {}


@router.post("")
async def create_task(task_req: TaskRequest) -> TaskResponse:
    """
    POST /tasks
    Submit a new task to the queue.

    Returns: 200 with task_id and full task object
    """
    task_id = str(uuid.uuid4())
    now = datetime.utcnow()

    task = {
        "task_id": task_id,
        "user_id": task_req.user_id,
        "status": TaskStatus.PENDING,
        "required_tools": task_req.required_tools or [],
        "context_payload": task_req.context_payload or {},
        "created_at": now,
        "updated_at": now,
    }

    # Store in-memory stub
    _tasks_store[task_id] = task

    return TaskResponse(**task)


@router.get("")
async def list_tasks(user_id: str = Query(...)) -> TaskListResponse:
    """
    GET /tasks?user_id=<user_id>
    Query tasks for a specific user.

    Returns: 200 with list of tasks and total count
    """
    user_tasks = [
        TaskResponse(**task)
        for task in _tasks_store.values()
        if task["user_id"] == user_id
    ]

    return TaskListResponse(
        tasks=user_tasks,
        total=len(user_tasks),
    )


@router.patch("/{task_id}/status")
async def update_task_status(
    task_id: str,
    status_req: StatusUpdateRequest,
) -> TaskResponse:
    """
    PATCH /tasks/{task_id}/status
    Update the status of an existing task.

    Returns: 200 with updated task object
    Raises: 404 if task not found
    """
    if task_id not in _tasks_store:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    task = _tasks_store[task_id]
    task["status"] = status_req.status
    task["updated_at"] = datetime.utcnow()

    return TaskResponse(**task)
