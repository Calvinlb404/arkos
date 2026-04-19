"""
Tool call entrypoint for ArkOS.

All MCP traffic now flows through Smithery Connect. This module keeps the
historical names (`MCPToolManager`, `AuthRequiredError`) so callers elsewhere
in the codebase don't need to change; the real implementation lives in
`tool_module.smithery`.
"""

from __future__ import annotations

from .smithery import AuthRequiredError, SmitheryError, SmitheryManager


class MCPToolManager(SmitheryManager):
    """Back-compat alias. Everything is Smithery under the hood.

    Old signature was MCPToolManager(config, token_store=None). Callers must
    now pass a `smithery_config` dict (api_key, namespace). The `token_store`
    kwarg is accepted and ignored so old callers don't explode.
    """

    def __init__(
        self,
        config: dict | None,
        smithery_config: dict | None = None,
        token_store=None,  # deprecated, ignored
    ):
        if smithery_config is None:
            raise ValueError(
                "MCPToolManager now requires `smithery_config` with an api_key. "
                "Pass config.get('smithery') from config.yaml."
            )
        super().__init__(config or {}, smithery_config)


__all__ = ["AuthRequiredError", "MCPToolManager", "SmitheryError"]
