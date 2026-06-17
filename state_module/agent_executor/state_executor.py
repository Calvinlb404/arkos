"""
Executor decision state. Runs inside a subagent (TaskRunner) and walks
plan_steps one at a time. For each step it decides whether the next action
is a tool call or a human question, then emits a route signal so the
executor_router in routers.py can pick the right next state.

Never re-plans. If a step can't be handled by the available tools, it routes
to ask_human instead of silently deviating.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from base_module.task_store import log_event
from model_module.ArkModelNew import SystemMessage
from model_module.llm_json import parse_llm_json
from state_module.core.base_state import StateOutput
from state_module.core.state import State
from state_module.core.state_registry import register_state


class _ActionKind(StrEnum):
    tool = "tool"
    ask = "ask"
    advance = "advance"   # step is complete; move to the next one


class _AskKind(StrEnum):
    binary = "binary"
    text = "text"


class ExecutorDecision(BaseModel):
    """Structured choice for the current plan step."""

    action: _ActionKind = Field(
        ...,
        description="tool if the step can be performed by calling a tool, ask if human input is required",
    )
    reason: str = Field(..., description="One-sentence justification")

    # populated when action == tool
    tool_name: str | None = Field(None, description="Exact tool name from the tool list")
    tool_args: dict | None = Field(None, description="Arguments dict for the tool")

    # populated when action == ask
    ask_kind: _AskKind | None = Field(None, description="binary (approve/deny) or text (free-form answer)")
    ask_prompt: str | None = Field(None, description="Exact question to present to the user")


@register_state
class StateExecutor(State):
    """Subagent decision state. Picks the next step's action and emits a route signal."""

    type = "executor"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.is_terminal = False

    def check_transition_ready(self, context):
        return True

    async def run(self, context, agent=None):
        plan_steps: list[str] = getattr(agent, "plan_steps", []) or []
        step_idx: int = getattr(agent, "step_idx", 0)
        task_id: str | None = getattr(agent, "task_id", None)

        if step_idx >= len(plan_steps):
            return StateOutput(
                content="",
                completion_signal="complete",
                structured_data={"route": "done"},
            )

        current_step = plan_steps[step_idx]

        tool_lines: list[str] = []
        tool_names: list[str] = []
        if agent.tool_manager is not None:
            try:
                servers = await agent.tool_manager.list_all_tools(agent.current_user_id)
                for _server, tools in servers.items():
                    for tname, tspec in tools.items():
                        tool_names.append(tname)
                        desc = (
                            tspec.get("description", "")
                            if isinstance(tspec, dict)
                            else getattr(tspec, "description", "")
                        ) or ""
                        tool_lines.append(f"- {tname}: {desc[:160]}")
            except Exception as e:
                if task_id:
                    log_event(task_id, "error", f"could not list tools: {e}")

        tools_block = "\n".join(tool_lines) if tool_lines else "(no tools available)"

        system = SystemMessage(
            content=(
                "You are a subagent executing a plan one step at a time.\n"
                "You are NOT allowed to re-plan or skip steps.\n\n"
                "Check the conversation above for tool results already obtained for this step.\n"
                "  - If the current step is fully complete based on those results → action=advance\n"
                "  - If more tool calls are still needed to complete this step → action=tool\n"
                "  - If you cannot complete the step with available tools → action=ask\n\n"
                "A step often needs MULTIPLE tool calls. For example, creating a calendar event "
                "may require listing calendars first to get the ID, then calling create_event. "
                "In that case: first call list_calendars, then on the next decision call create_event, "
                "then advance once the event is confirmed created.\n\n"
                "NEVER advance a step unless a tool_result in the conversation confirms it succeeded.\n"
                "Never invent tool names. Pick from the list below.\n\n"
                f"Available tools:\n{tools_block}\n\n"
                f"Current plan step ({step_idx + 1}/{len(plan_steps)}): {current_step}\n"
            )
        )

        schema = {
            "type": "json_schema",
            "json_schema": {
                "name": "executor_decision",
                "schema": ExecutorDecision.model_json_schema(),
            },
        }

        # Pass full context so the executor can see prior tool results for this step.
        output = await agent.call_llm(context=[system] + list(context), json_schema=schema)
        # parse_llm_json raises OutputValidationError on failure; _run_state
        # catches it and produces an error StateOutput rather than crashing.
        decision = parse_llm_json(output.content if output else None, ExecutorDecision)

        if task_id:
            log_event(
                task_id,
                "step_started",
                current_step,
                payload={"step_idx": step_idx, "decision": decision.model_dump(mode="json")},
            )

        if decision.action == _ActionKind.advance:
            # Step confirmed complete by tool results — advance to the next step.
            agent.step_idx = step_idx + 1
            if task_id:
                log_event(task_id, "step_complete", current_step, payload={"step_idx": step_idx})
            return StateOutput(
                content="",
                completion_signal="complete",
                structured_data={"route": "continue"},
            )

        if decision.action == _ActionKind.tool:
            if not decision.tool_name or (tool_names and decision.tool_name not in tool_names):
                agent.pending_ask = {
                    "kind": "text",
                    "prompt": (
                        f"I need to do: {current_step}\nBut I don't have a tool that matches. How should I handle this?"
                    ),
                }
                if task_id:
                    log_event(
                        task_id,
                        "fallback_ask",
                        "invalid tool name from LLM",
                        payload={"llm_choice": decision.tool_name},
                    )
                return StateOutput(
                    content="",
                    completion_signal="incomplete",
                    structured_data={"route": "ask"},
                )

            agent.pending_tool = {
                "tool_name": decision.tool_name,
                "tool_args": decision.tool_args or {},
            }
            return StateOutput(
                content=f"calling tool `{decision.tool_name}` for step {step_idx + 1}",
                completion_signal="incomplete",
                structured_data={"route": "tool"},
            )

        # action == ask
        agent.pending_ask = {
            "kind": (decision.ask_kind or _AskKind.text).value,
            "prompt": decision.ask_prompt or current_step,
        }
        return StateOutput(
            content="",
            completion_signal="incomplete",
            structured_data={"route": "ask"},
        )
