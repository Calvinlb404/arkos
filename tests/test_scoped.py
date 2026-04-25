"""Tests for tool_module/scoped.py — ScopedToolManager filtering and access control."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tool_module.scoped import ScopedToolManager


def _make_inner_with_tools(servers: dict[str, dict[str, dict]]):
    """Build a fake MCPToolManager where list_all_tools returns the given map."""
    inner = MagicMock()
    inner.list_all_tools = AsyncMock(return_value=servers)
    inner.call_tool = AsyncMock(return_value={"ok": True})

    # Flatten servers -> _tool_registry (tool_name -> server_name)
    registry = {}
    for server_name, tools in servers.items():
        for tool_name in tools:
            registry[tool_name] = server_name
    inner._tool_registry = registry
    inner.clients = {srv: MagicMock() for srv in servers}
    return inner


@pytest.fixture
def inner():
    return _make_inner_with_tools(
        {
            "linear": {
                "create_issue": {"name": "create_issue"},
                "list_issues": {"name": "list_issues"},
            },
            "calendar": {
                "create_event": {"name": "create_event"},
            },
        }
    )


# ---------------------------------------------------------------------------
# Unrestricted (allowed=None or [])
# ---------------------------------------------------------------------------


class TestUnrestricted:
    def test_empty_allowed_list_means_no_restriction(self, inner):
        scoped = ScopedToolManager(inner, allowed=[])
        # All tools visible in registry
        assert scoped._tool_registry == inner._tool_registry

    def test_none_allowed_means_no_restriction(self, inner):
        scoped = ScopedToolManager(inner, allowed=None)
        assert scoped._tool_registry == inner._tool_registry

    @pytest.mark.asyncio
    async def test_list_all_tools_passthrough(self, inner):
        scoped = ScopedToolManager(inner, allowed=None)
        result = await scoped.list_all_tools()
        assert result == await inner.list_all_tools()

    @pytest.mark.asyncio
    async def test_call_tool_passthrough(self, inner):
        scoped = ScopedToolManager(inner, allowed=None)
        await scoped.call_tool(tool_name="create_issue", arguments={"title": "x"}, user_id="u")
        inner.call_tool.assert_called_once_with(
            tool_name="create_issue",
            arguments={"title": "x"},
            user_id="u",
        )


# ---------------------------------------------------------------------------
# Restricted (allowed={...})
# ---------------------------------------------------------------------------


class TestRestricted:
    def test_registry_filters_to_allowed(self, inner):
        scoped = ScopedToolManager(inner, allowed=["create_issue"])
        registry = scoped._tool_registry
        assert "create_issue" in registry
        assert "list_issues" not in registry
        assert "create_event" not in registry

    @pytest.mark.asyncio
    async def test_list_all_tools_drops_other_tools(self, inner):
        scoped = ScopedToolManager(inner, allowed=["create_issue"])
        result = await scoped.list_all_tools()
        assert "linear" in result
        assert "create_issue" in result["linear"]
        assert "list_issues" not in result["linear"]

    @pytest.mark.asyncio
    async def test_list_all_tools_drops_servers_with_no_allowed_tools(self, inner):
        # Only create_issue is allowed; calendar has nothing allowed, so it
        # should disappear from the listing entirely.
        scoped = ScopedToolManager(inner, allowed=["create_issue"])
        result = await scoped.list_all_tools()
        assert "calendar" not in result

    @pytest.mark.asyncio
    async def test_allowed_tool_call_proxies(self, inner):
        scoped = ScopedToolManager(inner, allowed=["create_issue"])
        await scoped.call_tool(tool_name="create_issue", arguments={}, user_id="u")
        inner.call_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_disallowed_tool_call_raises(self, inner):
        scoped = ScopedToolManager(inner, allowed=["create_issue"])
        with pytest.raises(PermissionError) as excinfo:
            await scoped.call_tool(tool_name="list_issues", arguments={}, user_id="u")
        assert "list_issues" in str(excinfo.value)
        assert "allowed set" in str(excinfo.value)
        # The inner manager must not be called.
        inner.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_tool_call_raises_when_restricted(self, inner):
        scoped = ScopedToolManager(inner, allowed=["create_issue"])
        with pytest.raises(PermissionError):
            await scoped.call_tool(tool_name="nonexistent_tool", arguments={}, user_id="u")


# ---------------------------------------------------------------------------
# Passthrough properties / helpers
# ---------------------------------------------------------------------------


class TestPassthroughs:
    def test_clients_property_proxies(self, inner):
        scoped = ScopedToolManager(inner, allowed=["create_issue"])
        assert scoped.clients is inner.clients

    @pytest.mark.asyncio
    async def test_get_user_service_status_proxies(self, inner):
        inner.get_user_service_status = AsyncMock(return_value={"linear": True})
        scoped = ScopedToolManager(inner, allowed=None)
        out = await scoped.get_user_service_status("user-1")
        assert out == {"linear": True}
        inner.get_user_service_status.assert_called_once_with("user-1")

    @pytest.mark.asyncio
    async def test_get_missing_services_proxies(self, inner):
        inner.get_missing_services = AsyncMock(return_value=["calendar"])
        scoped = ScopedToolManager(inner, allowed=None)
        out = await scoped.get_missing_services("user-1")
        assert out == ["calendar"]
        inner.get_missing_services.assert_called_once_with("user-1")
