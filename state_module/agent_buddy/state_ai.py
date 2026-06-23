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
            "plan = the user has asked for a concrete action buddy should execute. "
            "Choose plan when the user has clearly asked buddy to DO something concrete "
            "on an external system where the target is specific. This INCLUDES single-step "
            "actions like browsing to a URL, opening a webpage, searching for a specific "
            "query, fetching content from the web, looking something up online, AND "
            "multi-step actions like create/send/schedule/update. "
            "If the request names a specific URL, page, query, or recipient, prefer plan."
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
            "  - reply: answer them in chat. This is the default for conversational turns.\n"
            "  - ask:   ask ONE clarifying question if the request is ambiguous.\n"
            "  - plan:  the user wants a concrete action on an EXTERNAL SERVICE where the "
            "target is specific. This covers BOTH multi-step orchestration "
            "(calendar, linear, slack: create/send/schedule/update) AND single-step "
            "web/browser actions (go to a URL, open a page, search for a specific query, "
            "fetch a page, look up specific content online, use the browser to do X). "
            "These workshop a plan and need approval before acting.\n"
            "  - computer: the user wants to write or run code, edit files, build "
            "something, or do research that involves running commands -- any task that needs "
            "a real computer (filesystem + shell). The computer agent handles it "
            "autonomously and messages back when done.\n"
            "\n"
            "Examples that route to plan:\n"
            "  - 'open example.com and tell me the title' (specific URL + concrete action)\n"
            "  - 'use the browser to search for python tutorials on duckduckgo'\n"
            "  - 'go to https://news.ycombinator.com and summarize the top story'\n"
            "  - 'send a slack message to #eng saying the deploy is done'\n"
            "Examples that route to computer:\n"
            "  - 'write a script that renames these files'\n"
            "  - 'build a small flask app and run it'\n"
            "Examples that route to ask:\n"
            "  - 'check my linear tickets' (no specific ticket or query)\n"
            "  - 'what's on my calendar' (no time range)\n"
            "\n"
            "Workshop ideas in chat FIRST. Do NOT jump to plan or computer just because the "
            "user mentioned a system -- route there only when the target is specific. But do "
            "NOT downgrade a clear browser request to ask just because the action is a single "
            "step: single-step browser tasks with a named URL/query are still plan. Use "
            "computer for genuine file/code/run work. If you truly cannot help, ask "
            "(route=ask).\n"
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
