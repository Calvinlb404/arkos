"""
Tool execution state for the executor (subagent) graph.

The executor decision state (state_executor.py) pre-selects the tool and
stores it in agent.pending_tool. This state reads that, executes the tool,
advances step_idx, then emits route="continue" so use_tool_router loops
back to the executor state for the next plan step.

This is distinct from buddy/state_tool.py, which is used by the chat agent
and selects a tool dynamically via LLM from full conversation context.
"""

from __future__ import annotations

from state_module.core.base_state import StateOutput
from state_module.core.state import State
from state_module.core.state_registry import register_state
from tool_module.tool_call import AuthRequiredError


@register_state
class StateExecutorTool(State):
    type = "executor_tool"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.is_terminal = False

    def check_transition_ready(self, context):
        return True

    async def _execute_tool(self, tool_call: dict, agent) -> str:
        return await agent.tool_manager.call_tool(
            tool_name=tool_call["tool_name"],
            arguments=tool_call["tool_args"],
            user_id=agent.current_user_id,
        )

    async def run(self, context, agent=None):
        # Local import: task_store is only available on the executor path.
        try:
            from base_module.task_store import log_event
        except Exception:
            log_event = None  # type: ignore

        pending = getattr(agent, "pending_tool", None) or {}
        tool_name = pending.get("tool_name", "")
        tool_args = pending.get("tool_args") or {}
        agent.pending_tool = None

        task_id = getattr(agent, "task_id", None)

        if task_id and log_event:
            log_event(task_id, "tool_call", tool_name, payload={"args": tool_args})

        try:
            tool_result = await self._execute_tool({"tool_name": tool_name, "tool_args": tool_args}, agent)
        except AuthRequiredError as e:
            service_label = e.service_info.get("name", e.service) if getattr(e, "service_info", None) else e.service
            link = e.setup_url or e.connect_url or ""
            body = (
                f"To complete this step I need access to **{service_label}**, "
                f"but it isn't connected. Setup link: {link}"
                if link
                else f"To complete this step I need access to **{service_label}**, but no setup URL was returned."
            )
            # Route back to ask_human so the user can unblock the subagent.
            agent.pending_ask = {"kind": "text", "prompt": body}
            return StateOutput(
                content=body,
                completion_signal="needs_input",
                structured_data={
                    "auth_required": True,
                    "service": e.service,
                    "setup_url": link or None,
                    "route": "ask",
                },
            )

        if task_id and log_event:
            log_event(task_id, "tool_result", str(tool_result)[:2000], payload={"tool_name": tool_name})

        # Advance past this plan step and signal the router to loop back to executor.
        agent.step_idx = getattr(agent, "step_idx", 0) + 1

        view = agent.render_tool_result(tool_result)
        return StateOutput(
            content=f"tool `{tool_name}` -> {view}",
            completion_signal="complete",
            structured_data={"route": "continue"},
        )
