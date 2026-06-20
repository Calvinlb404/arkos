"""Tests for SmitheryManager's local (non-Smithery) tool registry."""

from __future__ import annotations

import pytest

from tool_module.smithery import SmitheryManager


def _make_manager() -> SmitheryManager:
    # Bare-minimum config; we only exercise the local-tool path which
    # never touches the Smithery REST client.
    return SmitheryManager(servers={}, smithery_config={"api_key": "test", "namespace": "test"})


@pytest.mark.asyncio
async def test_register_local_tool_appears_in_list_all_tools():
    mgr = _make_manager()

    async def handler(args, user_id):
        return {"echo": args}

    mgr.register_local_tool(
        name="my_local",
        description="a local tool",
        input_schema={"type": "object"},
        handler=handler,
    )

    tools = await mgr.list_all_tools()
    assert "local" in tools
    assert "my_local" in tools["local"]
    assert tools["local"]["my_local"]["description"] == "a local tool"


@pytest.mark.asyncio
async def test_call_tool_routes_to_local_handler():
    mgr = _make_manager()
    captured = {}

    async def handler(args, user_id):
        captured["args"] = args
        captured["user_id"] = user_id
        return "ok"

    mgr.register_local_tool(
        name="my_local",
        description="x",
        input_schema={"type": "object"},
        handler=handler,
    )

    result = await mgr.call_tool("my_local", {"a": 1}, user_id="u1")

    assert result == "ok"
    assert captured == {"args": {"a": 1}, "user_id": "u1"}
