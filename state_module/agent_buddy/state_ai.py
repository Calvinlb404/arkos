"""
Chat/reasoning state for buddy.

Owns three responsibilities:
1. Respond conversationally when no external action is needed.
2. Workshop vague requests by asking clarifying questions.
3. Hand off to workshop_plan only when the user has clearly asked for a
   concrete multi-step action on an external system.

Routing is done by emitting a route signal in structured_data. The
agent_reply_router in buddy/routers.py maps signals to state names.
"""

from __future__ import annotations

try:
    from enum import StrEnum
except ImportError:  # Python < 3.11 (test environments only)
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]  # noqa: UP042
        pass


from pydantic import BaseModel, Field

from model_module.ArkModelNew import SystemMessage
from model_module.llm_json import parse_llm_json
from state_module.core.base_state import StateOutput
from state_module.core.state import State
from state_module.core.state_registry import register_state


class _Route(StrEnum):
    reply = "reply"        # stay in chat; answer in final
    ask = "ask"            # stay in chat; ask a clarifying question
    plan = "plan"          # hand off to workshop_plan (multi-step approval flow)
    computer = "computer"  # dispatch to the persistent computer (file/code/run tasks)


class ReasonedOutput(BaseModel):
    """Routing + user-facing reply. Chain-of-thought stays internal."""

    intent: str = Field(..., description="What buddy thinks the user is trying to do (internal)")
    approach: list[str] = Field(default_factory=list, description="Internal reasoning. NEVER shown to user.")
    route: _Route = Field(
        ...,
        description=(
            "reply = simple chat answer. "
            "ask = need a clarification from the user, include it in final. "
            "plan = the user has asked for a concrete multi-step action buddy should execute. "
            "Only choose plan when the user has explicitly asked buddy to DO something "
            "concrete involving external systems or tools and the goal is clear enough to "
            "write a plan for."
        ),
    )
    final: str = Field(..., description="The ONLY text shown to the user")


@register_state
class StateAI(State):
    type = "agent"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.is_terminal = False

    def check_transition_ready(self, context):
        return True

    async def run(self, context, agent):
        messages = context if isinstance(context, list) else context.get("messages", [])

        json_schema = {
            "type": "json_schema",
            "json_schema": {
                "name": "reasoned_output",
                "schema": ReasonedOutput.model_json_schema(),
            },
        }

        root_prompt = (getattr(agent, "system_prompt", None) or "").strip()

        chat_guidance = (
            "You are buddy, the ark chat agent.\n"
            "Read the user's latest message and pick a route:\n"
            "  - reply: answer them in chat. This is the default.\n"
            "  - ask:   ask ONE clarifying question if you need more info.\n"
            "  - plan:  ONLY when the user wants a multi-step action on an EXTERNAL SERVICE "
            "(calendar, linear, etc.) where approval is needed before acting.\n"
            "  - computer: when the user wants to write or run code, edit files, build "
            "something, do research that involves running commands, or any task that needs "
            "a real computer (filesystem + shell). The computer agent will handle it "
            "autonomously and message back when done.\n"
            "\n"
            "Workshop ideas in chat FIRST. Never jump to plan or computer just because the "
            "user mentioned a system. If they say 'check my linear tickets', answer in chat "
            "using tools you have. Use computer for genuine file/code/run work. "
            "If you truly cannot help, say so and ask (route=ask).\n"
            "\n"
            "Put your reasoning in `approach`. The user will NEVER see it. Only `final` "
            "is shown. Do not paraphrase your reasoning into the final message."
        )

        system_parts: list[str] = []
        if root_prompt:
            system_parts.append(root_prompt)
        system_parts.append(chat_guidance)

        system = SystemMessage(content="\n\n".join(system_parts))
        output = await agent.call_llm(context=[system] + messages, json_schema=json_schema)

        # parse_llm_json repairs common model failures then validates;
        # raises OutputValidationError (caught by _run_state for rerun/recovery).
        data = parse_llm_json(output.content if output else None, ReasonedOutput)

        user_text = (data.final or "").strip() or "(no content)"

        signal = "needs_input" if data.route == _Route.ask else "complete"
        if data.route == _Route.computer:
            signal = "complete"

        return StateOutput(
            content=user_text,
            completion_signal=signal,
            structured_data={"route": data.route.value},
        )
