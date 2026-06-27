"""
Tool execution state for the buddy (chat) agent.

This state is entered only from the chat graph, after a user-approved plan
has already run and the agent needs to call a tool. It selects a tool by
asking the LLM to choose from available options, then executes it.

This is distinct from executor/state_tool.py, which is used by the subagent
and receives a pre-selected tool via agent.pending_tool.
"""

from __future__ import annotations

import logging

from agent_module.agent import parse_structured
from model_module.ArkModelNew import SystemMessage
from state_module.core.base_state import StateOutput
from state_module.core.state import State
from state_module.core.state_registry import register_state
from tool_module.tool_call import AuthRequiredError

logger = logging.getLogger(__name__)


@register_state
class StateTool(State):
    type = "tool"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.is_terminal = False

    def check_transition_ready(self, context):
        return True

    async def _choose_tool(self, context, agent) -> dict:
        """
        Ask the LLM to pick a tool from the available set and fill its args.

        Raises ValueError if the model output cannot be parsed or names a tool
        that is not in the registry; run() catches this and returns an error outcome.
        """
        prompt = "based on the above user request, choose the tool which best satisfies the users request"
        instructions = context + [SystemMessage(content=prompt)]

        tool_option_class = await agent.create_tool_option_class()
        json_schema = {
            "type": "json_schema",
            "json_schema": {
                "name": "tool_choice",
                "schema": tool_option_class.model_json_schema(),
            },
        }

        output = await agent.call_llm(instructions, json_schema)
        parsed_choice = parse_structured(output.content, tool_option_class)
        if parsed_choice is None:
            raise ValueError("could not parse tool choice from model output")

        tool_name = parsed_choice.tool_name.value

        # Resolve scoped to THIS user (shared + their per-user tools only).
        server_name = agent.tool_manager._resolve_server(tool_name, agent.current_user_id)
        if not server_name:
            raise ValueError(f"model chose unknown tool: {tool_name!r}")

        all_tools = await agent.tool_manager.list_all_tools(agent.current_user_id)
        tool_spec = all_tools[server_name][tool_name]

        tool_args_schema = {
            "type": "json_schema",
            "json_schema": {
                "name": "tool_args",
                "schema": tool_spec.get("inputSchema", {}),
            },
        }

        args_prompt = f"Fill in the arguments for the tool '{tool_name}' based on the user's request."
        args_context = context + [SystemMessage(content=args_prompt)]
        args_output = await agent.call_llm(args_context, tool_args_schema)

        # Args schema is dynamic — repair then parse; fall back to {} on failure.
        import json as _json

        try:
            from json_repair import repair_json  # type: ignore[import]

            raw_args = repair_json(args_output.content or "{}", return_objects=False)
        except Exception:
            raw_args = args_output.content or "{}"
        try:
            tool_args = _json.loads(raw_args)
        except (ValueError, TypeError):
            logger.warning("could not parse tool args for %s, using empty args", tool_name)
            tool_args = {}

        return {"tool_name": tool_name, "tool_args": tool_args}

    async def _execute_tool(self, tool_call: dict, agent) -> str:
        return await agent.tool_manager.call_tool(
            tool_name=tool_call["tool_name"],
            arguments=tool_call["tool_args"],
            user_id=agent.current_user_id,
        )

    async def run(self, context, agent=None):
        try:
            tool_arg_dict = await self._choose_tool(context=context, agent=agent)
            tool_result = await self._execute_tool(tool_call=tool_arg_dict, agent=agent)

            view = agent.render_tool_result(tool_result)
            return StateOutput(
                content=view,
                completion_signal="complete",
                structured_data={"route": "continue"},
            )

        except ValueError as e:
            logger.warning("tool selection failed: %s", e)
            return StateOutput(
                content="I could not select an appropriate tool for that request.",
                completion_signal="error",
                error_detail=str(e),
                structured_data={"route": "ask"},
            )

        except AuthRequiredError as e:
            service_label = e.service_info.get("name", e.service) if getattr(e, "service_info", None) else e.service
            link = e.setup_url or e.connect_url or ""
            if link:
                body = (
                    f"To do that I need access to **{service_label}**. "
                    f"Open this link to connect it via Smithery, then ask me again.\n\n"
                    f"[connect {service_label.lower()}]({link})"
                )
            else:
                body = (
                    f"To do that I need access to **{service_label}**, "
                    f"but Smithery didn't return a setup URL. "
                    f"Check the server's config (it may need an API key)."
                )
            return StateOutput(
                content=body,
                completion_signal="needs_input",
                structured_data={
                    "auth_required": True,
                    "service": e.service,
                    "setup_url": link or None,
                    "state": getattr(e, "state", "auth_required"),
                },
            )
