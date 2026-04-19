"""
Planning state: buddy workshops a structured plan with the user.

The plan is NOT persisted to the DB from this state. It's returned to the
chat layer as structured_data so the frontend can render an inline plan card
with approve/decline buttons. On approve, the frontend calls POST /tasks
which mints a task row (status='running') and spawns a subagent via the
task runner. Pending Approvals on the desk is now reserved for mid-flight
checkpoints raised by running subagents, not initial plan approval.
"""

from __future__ import annotations

import os
import sys

from pydantic import BaseModel, Field

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from model_module.ArkModelNew import SystemMessage  # noqa: E402

from state_module.base_state import StateOutput  # noqa: E402
from state_module.state import State  # noqa: E402
from state_module.state_registry import register_state  # noqa: E402


class WorkshopOutput(BaseModel):
    """Structured plan the agent must produce before we persist a task."""

    title: str = Field(..., max_length=280, description="Short human title for the task")
    plan_steps: list[str] = Field(..., min_length=1, description="Concrete numbered steps buddy will take")
    required_tools: list[str] = Field(default_factory=list, description="Tool names buddy expects to call")
    needs_clarification: bool = Field(False, description="True if buddy needs more info before acting")
    clarifying_question: str | None = Field(None)


@register_state
class StatePlan(State):
    """`plan` state. Drafts a plan, persists it as a pending task, hands control back."""

    type = "plan"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        # Terminal so the agent loop returns to the user for approval via the dashboard
        self.is_terminal = True

    def check_transition_ready(self, context):
        return True

    async def run(self, context, agent=None):
        messages = context if isinstance(context, list) else context.get("messages", [])

        system = SystemMessage(
            content=(
                "You are buddy in planning mode.\n"
                "Workshop a concrete plan with the user BEFORE acting.\n"
                "Return JSON matching the provided schema.\n"
                "Plan steps must be numbered actions, each one sentence.\n"
                "If you truly lack information, set needs_clarification=true and include a single clarifying question.\n"
                "Otherwise provide at least 2 plan_steps."
            )
        )

        schema = {
            "type": "json_schema",
            "json_schema": {
                "name": "workshop_plan",
                "schema": WorkshopOutput.model_json_schema(),
            },
        }

        output = await agent.call_llm(context=[system] + messages, json_schema=schema)
        if not output or not output.content:
            return StateOutput(
                content="I couldn't draft a plan. Can you rephrase?",
                completion_signal="needs_input",
            )

        try:
            plan = WorkshopOutput.model_validate_json(output.content)
        except Exception as e:  # schema violation; surface the raw text
            return StateOutput(
                content=output.content,
                completion_signal="needs_input",
                error_detail=f"plan parse failed: {e}",
            )

        # If buddy still needs info, don't persist yet.
        if plan.needs_clarification and plan.clarifying_question:
            return StateOutput(
                content=plan.clarifying_question,
                completion_signal="needs_input",
            )

        plan_text = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(plan.plan_steps))

        # The plan is NOT written to the DB here. We stream it back to the
        # frontend as a human-readable body plus a sentinel JSON block the
        # frontend parses to render an inline plan card (approve/decline).
        # On approve, the frontend calls POST /tasks, which mints the task
        # row and spawns the runner.
        import json as _json

        payload = {
            "kind": "plan_proposal",
            "title": plan.title,
            "plan_steps": list(plan.plan_steps),
            "required_tools": list(plan.required_tools),
        }
        sentinel = "```ark-plan\n" + _json.dumps(payload) + "\n```"

        body = [
            "Here's the plan I put together. Approve it below to run it.",
            "",
            f"**{plan.title}**",
            plan_text,
            "",
            sentinel,
        ]
        return StateOutput(
            content="\n".join(body),
            completion_signal="needs_input",
            structured_data=payload,
        )
