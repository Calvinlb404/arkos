"""
Computer planning state for the buddy graph.

Entered when state_ai routes to 'computer'. Buddy workshops a concrete plan
for the computer task and returns it as an approvable plan card. NOTHING runs
on the computer here -- the plan is a proposal. Only when the user approves the
card does the frontend POST /computer/tasks, which dispatches the computer agent.

This mirrors the workshop_plan -> approval -> run flow used for external-service
actions: nothing happens without approval first.
"""

from __future__ import annotations

import json as _json

from pydantic import BaseModel, Field

from model_module.ArkModelNew import SystemMessage
from model_module.llm_json import parse_llm_json
from state_module.core.base_state import StateOutput
from state_module.core.state import State
from state_module.core.state_registry import register_state


class ComputerPlanOutput(BaseModel):
    """Plan for a computer task the user must approve before it runs."""

    title: str = Field(..., max_length=280, description="Short human title for the task")
    plan_steps: list[str] = Field(..., min_length=1, description="Concrete steps the computer agent will take")
    needs_clarification: bool = Field(False, description="True if buddy needs more info before planning")
    clarifying_question: str | None = Field(None)


@register_state
class StateComputerPlan(State):
    type = "computer_plan"

    def __init__(self, name: str, config: dict) -> None:
        super().__init__(name, config)
        # Terminal: hand back to the user for approval. The plan does NOT run.
        self.is_terminal = True

    def check_transition_ready(self, context) -> bool:
        return True

    async def run(self, context, agent=None) -> StateOutput:
        messages = context if isinstance(context, list) else []

        system = SystemMessage(
            content=(
                "You are buddy planning a COMPUTER task. The computer agent has its own "
                "Linux sandbox with a filesystem and shell, and will run autonomously ONCE "
                "THE USER APPROVES this plan.\n"
                "Draft a short, concrete plan of what it will do. Return JSON matching the "
                "schema. Each step is one sentence. If you genuinely lack key information, set "
                "needs_clarification=true with a single question.\n"
                "Do NOT run anything -- this is a proposal the user must approve first."
            )
        )

        schema = {
            "type": "json_schema",
            "json_schema": {"name": "computer_plan", "schema": ComputerPlanOutput.model_json_schema()},
        }

        output = await agent.call_llm(context=[system] + messages, json_schema=schema)
        plan = parse_llm_json(output.content if output else None, ComputerPlanOutput)

        if plan.needs_clarification and plan.clarifying_question:
            return StateOutput(
                content=plan.clarifying_question,
                completion_signal="needs_input",
                structured_data={"route": "ask"},
            )

        # The original user request becomes the agent's task prompt on approval.
        user_prompt = ""
        for m in reversed(messages):
            if getattr(m, "role", None) == "user" and (getattr(m, "content", "") or "").strip():
                user_prompt = m.content.strip()
                break

        plan_text = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(plan.plan_steps))

        # target=computer tells the frontend to POST /computer/tasks (not /tasks)
        # on approval, so the computer agent runs rather than the executor subagent.
        payload = {
            "kind": "plan_proposal",
            "target": "computer",
            "title": plan.title,
            "plan_steps": list(plan.plan_steps),
            "prompt": user_prompt or plan.title,
        }
        sentinel = "```ark-plan\n" + _json.dumps(payload) + "\n```"

        body = [
            "Here's what I'll do on your computer. Approve below to run it.",
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
