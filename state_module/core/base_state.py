from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class TerminalReason(StrEnum):
    """
    Why the agent loop stopped.

    Returned alongside the last StateOutput so callers (app.py, task_runner.py)
    can react correctly rather than guessing from content.
    """

    completed = "completed"
    max_steps = "max_steps"
    model_error = "model_error"
    needs_input = "needs_input"


class StateOutput(BaseModel):
    """Structured return type for all state run() methods."""

    content: str = Field(description="The text content produced by the state.")
    completion_signal: Literal["complete", "incomplete", "error", "needs_input"] = Field(
        description="Indicates the outcome of the state's execution."
    )
    structured_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional structured payload produced by the state.",
    )
    error_detail: str | None = Field(
        default=None,
        description="Error message when completion_signal is 'error'.",
    )
