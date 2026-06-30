"""
Planning state: buddy workshops a structured plan with the user.

The plan is NOT persisted to the DB from this state. It's returned to the
chat layer as structured_data so the frontend can render an inline plan card
with approve/decline buttons. On approve, the frontend calls POST /tasks
which mints a task row (status='running') and spawns a subagent via the
task runner. Pending Approvals on the desk is reserved for mid-flight
checkpoints raised by running subagents, not initial plan approval.
"""

from __future__ import annotations

import json as _json

from pydantic import BaseModel, Field

from model_module.ArkModelNew import SystemMessage
from model_module.llm_json import parse_llm_json
from state_module.core.base_state import StateOutput
from state_module.core.state import State
from state_module.core.state_registry import register_state


class WorkshopOutput(BaseModel):
    """Structured plan the agent must produce before we persist a task."""

    title: str = Field(..., max_length=280, description="Short human title for the task")
    plan_steps: list[str] = Field(..., min_length=1, description="Concrete numbered steps buddy will take")
    required_tools: list[str] = Field(default_factory=list, description="Tool names buddy expects to call")
    needs_clarification: bool = Field(False, description="True if buddy needs more info before acting")
    clarifying_question: str | None = Field(None)


@register_state
class StatePlan(State):
    """`plan` state. Drafts a plan, streams it back, hands control to the user."""

    type = "plan"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        # Terminal so the agent loop returns to the user for approval.
        self.is_terminal = True

    def check_transition_ready(self, context):
        return True

    async def run(self, context, agent=None):
        messages = context if isinstance(context, list) else context.get("messages", [])

        # The agent's root system prompt carries the live tool catalog. Without
        # it, the LLM doesn't know what capabilities exist and ends up writing
        # prose like "Opened https://example.com in the browser" instead of an
        # actionable step. The executor can't act on past-tense prose, so it
        # routes to ask_human and the task stalls in awaiting_approval.
        root_prompt = (getattr(agent, "system_prompt", None) or "").strip()

        plan_guidance = (
            "You are buddy in planning mode.\n"
            "Workshop a concrete, forward-looking plan with the user BEFORE acting.\n"
            "Return JSON matching the provided schema.\n"
            "\n"
            "Hard rules for plan_steps:\n"
            "  - Each step is something buddy WILL DO, not something that has "
            "happened. Use imperative voice: 'Open ...', 'Send ...', 'Fetch ...', "
            "'Retrieve ...'. NEVER write past tense like 'Opened ...' or 'Retrieved ...'.\n"
            "  - Describe WHAT to accomplish, not the exact internal tool/API/method "
            "or parameters. The executor picks the specific tool and the names may "
            "differ from what you expect.\n"
            "    WRONG: \"Use the 'list_emails' tool to retrieve the inbox\"\n"
            '    RIGHT: "Retrieve the latest 5 emails from the inbox"\n'
            "  - For web/browser tasks you MAY reference the browser as the capability "
            "(it is the only way to act on a URL), but still describe the action, not a "
            'made-up function signature. RIGHT: "Open https://example.com and return '
            'the page title."\n'
            "  - Do NOT pre-describe the result. Don't write a step that says "
            "'Title of the page is X' — that's the outcome, not the action.\n"
            "  - Do NOT split one action into multiple steps. One concrete action = one step.\n"
            "\n"
            "Set required_tools to the EXACT tool names from the catalog above that "
            "the executor will need -- this list scopes which tools it may call. For "
            "web/browser actions that tool is 'browser_task'. If you are unsure of the "
            "exact tool name for an external service, leave required_tools EMPTY: the "
            "executor then keeps access to all your connected tools and picks the right "
            "one itself. Never put a made-up capability word (like 'browser' or 'email') "
            "here -- a name that matches no real tool locks the executor out of every tool.\n"
            "\n"
            "If you truly lack information, set needs_clarification=true and include "
            "a single clarifying question. Otherwise provide 1 or more plan_steps.\n"
            "\n"
            "Example good plan for 'open example.com and tell me the title':\n"
            '  title: "Get example.com title"\n'
            '  plan_steps: ["Open https://example.com and return the page title."]\n'
            '  required_tools: ["browser_task"]\n'
        )
        if root_prompt:
            system = SystemMessage(content=root_prompt + "\n\n" + plan_guidance)
        else:
            system = SystemMessage(content=plan_guidance)

        schema = {
            "type": "json_schema",
            "json_schema": {
                "name": "workshop_plan",
                "schema": WorkshopOutput.model_json_schema(),
            },
        }

        output = await agent.call_llm(context=[system] + messages, json_schema=schema)
        plan = parse_llm_json(output.content if output else None, WorkshopOutput)

        if plan.needs_clarification and plan.clarifying_question:
            return StateOutput(
                content=plan.clarifying_question,
                completion_signal="needs_input",
            )

        # Guardrail: reject pseudo-plans where every step is just asking the user for info.
        _CLARIFY_TOKENS = (
            "ask the user",
            "ask user",
            "request the user",
            "request more",
            "request info",
            "request information",
            "prompt the user",
            "prompt for",
            "inquire",
            "clarify",
            "confirm with the user",
            "check with the user",
            "get the user",
            "obtain from the user",
            "gather from the user",
        )

        def _is_clarify_step(s: str) -> bool:
            t = (s or "").strip().lower().lstrip("0123456789. -")
            if not t:
                return False
            if t.startswith("ask "):
                return True
            return any(tok in t for tok in _CLARIFY_TOKENS)

        action_steps = [s for s in plan.plan_steps if not _is_clarify_step(s)]

        if plan.plan_steps and not action_steps:
            question_src = plan.plan_steps[0].lstrip("0123456789. -")
            for prefix in (
                "Ask the user to ",
                "Ask user to ",
                "Request the user to ",
                "Prompt the user to ",
                "Ask ",
                "Request ",
                "Prompt ",
            ):
                if question_src.lower().startswith(prefix.lower()):
                    question_src = question_src[len(prefix) :]
                    break
            question = question_src.strip().rstrip(".")
            if question and not question.endswith("?"):
                question = question[0].upper() + question[1:] + "?"
            return StateOutput(
                content=question or "Could you tell me more about what you want to do?",
                completion_signal="needs_input",
                structured_data={"route": "ask"},
            )

        payload = {
            "kind": "plan_proposal",
            "title": plan.title,
            "plan_steps": list(plan.plan_steps),
            "required_tools": list(plan.required_tools),
        }
        sentinel = "```ark-plan\n" + _json.dumps(payload) + "\n```"

        # One-line intro + the sentinel only. The title and steps live in the
        # ark-plan payload and are drawn by the frontend plan card; emitting them
        # as prose here too would render the plan twice in the chat.
        body = [
            "Here's the plan I put together. Approve it below to run it.",
            "",
            sentinel,
        ]
        return StateOutput(
            content="\n".join(body),
            completion_signal="needs_input",
            structured_data=payload,
        )
