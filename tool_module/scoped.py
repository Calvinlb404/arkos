"""
ScopedToolManager: a thin wrapper around the process-wide SmitheryManager
(aka MCPToolManager) that restricts which tools a subagent can see and call.

It exists so a subagent can be spawned with only the tools its plan declared
it needs, without having to spin up a second manager or mutate the shared one.
"""

from __future__ import annotations

from typing import Any


class ScopedToolManager:
    def __init__(self, inner, allowed: list[str] | None = None):
        """
        inner : the real MCPToolManager / SmitheryManager
        allowed : list of tool names the subagent is allowed to use.
                  None or empty means "no restrictions" (inherits all tools).
        """
        self._inner = inner
        self._allowed = set(allowed or [])

    # ---- tool registry -----------------------------------------------------
    @property
    def _tool_registry(self) -> dict[str, str]:
        inner = self._inner._tool_registry  # name -> server
        if not self._allowed:
            return inner
        return {k: v for k, v in inner.items() if k in self._allowed}

    @property
    def clients(self):
        return self._inner.clients

    # ---- listing -----------------------------------------------------------
    async def list_all_tools(self) -> dict[str, dict[str, Any]]:
        servers = await self._inner.list_all_tools()
        if not self._allowed:
            return servers
        out: dict[str, dict[str, Any]] = {}
        for server, tools in servers.items():
            filtered = {name: spec for name, spec in tools.items() if name in self._allowed}
            if filtered:
                out[server] = filtered
        return out

    # ---- execution ---------------------------------------------------------
    async def call_tool(self, *, tool_name: str, arguments: dict, user_id: str | None = None):
        if self._allowed and tool_name not in self._allowed:
            raise PermissionError(
                f"tool {tool_name!r} is not in this subagent's allowed set: {sorted(self._allowed)}"
            )
        return await self._inner.call_tool(
            tool_name=tool_name,
            arguments=arguments,
            user_id=user_id,
        )

    # ---- misc passthroughs -------------------------------------------------
    async def get_user_service_status(self, *args, **kwargs):
        return await self._inner.get_user_service_status(*args, **kwargs)

    async def get_missing_services(self, *args, **kwargs):
        return await self._inner.get_missing_services(*args, **kwargs)
