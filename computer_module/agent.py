"""
ComputerAgent: the loop that drives the user's persistent sandbox.

Prompts and tool-interface discipline borrowed as paradigms from Claude Code
(see COMPUTER_AGENT_SPEC.md). Model is a config knob (computer_agent.llm);
everything else is ours.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from computer_module.model import ToolCallingModel
from computer_module.prompt import build_system_prompt
from computer_module.sandbox import SandboxManager
from computer_module.tools import TOOL_SCHEMAS, ToolContext, dispatch, tool_inventory

logger = logging.getLogger(__name__)

_DEFAULT_STEP_CAP = 50


class ComputerAgent:
    """
    Drives the user's persistent sandbox end-to-end using native tool-calling.

    Args:
        user_id:      The verified user whose sandbox is targeted.
        sandbox:      The SandboxManager singleton (already built + verified).
        tool_manager: The Smithery tool manager for user-scoped MCP calls (optional).
        model:        ToolCallingModel instance (defaults to a fresh one from config).
        emit:         Progress event callback -> feeds the SSE stream and activity view.
        ask:          Structured-ask callback -> routes through the approval tray.
    """

    def __init__(
        self,
        user_id: str,
        sandbox: SandboxManager,
        tool_manager=None,
        model: ToolCallingModel | None = None,
        emit: Callable[[dict[str, Any]], None] | None = None,
        ask: Callable[[str], Any] | None = None,
    ) -> None:
        self.user_id = user_id
        self.sandbox = sandbox
        self.tool_manager = tool_manager
        self.model = model or ToolCallingModel()
        self._emit = emit or (lambda e: None)
        self._ask = ask

    def _emit_event(self, event: dict[str, Any]) -> None:
        try:
            self._emit(event)
        except Exception as e:
            logger.warning("emit failed: %s", e)

    async def _build_mcp_schemas(self) -> list[dict[str, Any]]:
        """Fetch the user's connected MCP tools and convert to OpenAI tool schemas."""
        if not self.tool_manager:
            return []
        try:
            servers = await self.tool_manager.list_all_tools(self.user_id)
            schemas = []
            for _server, tools in servers.items():
                for tool_name, spec in tools.items():
                    schemas.append({
                        "type": "function",
                        "function": {
                            "name": f"mcp_{tool_name}",
                            "description": spec.get("description", tool_name),
                            "parameters": spec.get("inputSchema", {"type": "object", "properties": {}}),
                        },
                    })
            return schemas
        except Exception as e:
            logger.warning("could not load MCP tools for user %s: %s", self.user_id, e)
            return []

    async def _call_mcp(self, tool_name: str, args: dict[str, Any]) -> str:
        """Execute a user-scoped MCP tool (name has the mcp_ prefix stripped)."""
        bare = tool_name.removeprefix("mcp_")
        try:
            result = await self.tool_manager.call_tool(bare, args, user_id=self.user_id)
            return json.dumps(result) if not isinstance(result, str) else result
        except Exception as e:
            return f"ERROR calling MCP tool {bare}: {e}"

    async def run(self, prompt: str, *, step_cap: int = _DEFAULT_STEP_CAP) -> dict[str, Any]:
        """
        Run the agent until the task is complete, the step cap is hit, or an
        unrecoverable error occurs.

        Returns:
            {
                "status": "completed" | "failed" | "step_cap_reached",
                "summary": str,      # the agent's final message to the user
                "outputs": list[str] # file paths produced (from write_file/edit_file events)
            }
        """
        sbx_handle = await self.sandbox.get_or_create(self.user_id)
        cwd = "/home/user"

        # Build tool list: our sandbox tools + user's MCP tools.
        mcp_schemas = await self._build_mcp_schemas()
        all_tool_schemas = TOOL_SCHEMAS + mcp_schemas

        inventory = tool_inventory()
        if mcp_schemas:
            mcp_names = ", ".join(s["function"]["name"] for s in mcp_schemas)
            inventory += f"\n- MCP services: {mcp_names}"

        system = build_system_prompt(
            cwd=cwd,
            username=self.user_id,
            date=datetime.now().strftime("%A %B %d %Y"),
            tool_inventory=inventory,
        )

        ctx = ToolContext(
            user_id=self.user_id,
            sandbox=self.sandbox,
            emit=self._emit_event,
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

        outputs: list[str] = []

        self._emit_event({"kind": "start", "prompt": prompt[:200]})

        for step in range(step_cap):
            try:
                assistant_msg = await self.model.call(messages, all_tool_schemas)
            except Exception as e:
                logger.error("model call failed at step %d: %s", step, e)
                return {
                    "status": "failed",
                    "summary": f"I could not reach the model: {e}",
                    "outputs": outputs,
                }

            # No tool calls -- the model has finished.
            if not assistant_msg.tool_calls:
                summary = assistant_msg.content or "(no output)"
                self._emit_event({"kind": "completed", "summary": summary[:500]})
                return {"status": "completed", "summary": summary, "outputs": outputs}

            # Append the assistant turn and execute all tool calls.
            msg_dict: dict[str, Any] = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in assistant_msg.tool_calls
                ],
            }
            if assistant_msg.content:
                msg_dict["content"] = assistant_msg.content
            messages.append(msg_dict)

            tool_results = []
            for tc in assistant_msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                if name == "ask_user":
                    result = await self._handle_ask(args.get("prompt", ""))
                elif name.startswith("mcp_"):
                    self._emit_event({"kind": "mcp", "tool": name, "args": args})
                    result = await self._call_mcp(name, args)
                else:
                    result = await dispatch(name, args, ctx)

                if name in ("write_file", "edit_file") and "path" in args:
                    if args["path"] not in outputs:
                        outputs.append(args["path"])

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(result),
                })

            messages.extend(tool_results)

        # Step cap reached -- the agent did not finish cleanly.
        self._emit_event({"kind": "failed", "reason": "step_cap_reached"})
        return {
            "status": "step_cap_reached",
            "summary": f"Task did not complete within {step_cap} steps.",
            "outputs": outputs,
        }

    async def _handle_ask(self, prompt: str) -> str:
        """Route a structured ask through the approval tray and wait for an answer."""
        if not self._ask:
            return "(ask not configured -- proceeding without user input)"
        try:
            self._emit_event({"kind": "ask", "prompt": prompt})
            result = await asyncio.wait_for(self._ask(prompt), timeout=86400)
            return str(result) if result is not None else "(no answer)"
        except asyncio.TimeoutError:
            return "(ask timed out -- proceeding without user input)"
        except Exception as e:
            return f"(ask failed: {e})"
