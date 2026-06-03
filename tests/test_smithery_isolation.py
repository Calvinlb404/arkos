"""Cross-user isolation tests for SmitheryManager tool routing (MULTIUSER Task 2)."""

import pytest

from tool_module.smithery import SmitheryManager


@pytest.fixture
def mgr():
    m = SmitheryManager({}, {"api_key": "x", "namespace": "test"})
    # Shared (no-auth) tool: same server for everyone.
    m._tool_registry = {"web_search": "brave"}
    # Per-user tools: A and B both connected a tool named "search" from different servers.
    m._user_tool_registry = {
        "userA": {"search": "serverA"},
        "userB": {"search": "serverB"},
    }
    return m


def test_tool_registry_is_user_scoped(mgr):
    # A's per-user tool resolves for A, and is invisible to B.
    assert mgr._resolve_server("search", "userA") == "serverA"
    assert mgr._resolve_server("search", "userB") == "serverB"


def test_colliding_tool_names_do_not_cross_users(mgr):
    # Same tool name, different servers per user -- each routes to their own,
    # regardless of which was registered last.
    assert mgr._resolve_server("search", "userA") == "serverA"
    assert mgr._resolve_server("search", "userB") == "serverB"
    # A user who connected nothing gets no per-user match (and no leak to another's).
    assert mgr._resolve_server("search", "userC") is None


def test_shared_tools_resolve_for_everyone(mgr):
    assert mgr._resolve_server("web_search", "userA") == "brave"
    assert mgr._resolve_server("web_search", "userB") == "brave"
    assert mgr._resolve_server("web_search", None) == "brave"


def test_unknown_tool_resolves_to_none(mgr):
    assert mgr._resolve_server("nonexistent", "userA") is None


@pytest.mark.asyncio
async def test_list_all_tools_excludes_other_users(mgr):
    mgr._shared_tools = {"brave": [{"name": "web_search"}]}
    mgr._user_tools = {
        "userA": {"serverA": [{"name": "search"}]},
        "userB": {"serverB": [{"name": "search"}]},
    }
    a_tools = await mgr.list_all_tools("userA")
    # A sees shared + their own server, never B's server.
    assert "brave" in a_tools
    assert "serverA" in a_tools
    assert "serverB" not in a_tools

    # No user_id -> shared only.
    none_tools = await mgr.list_all_tools(None)
    assert "brave" in none_tools
    assert "serverA" not in none_tools and "serverB" not in none_tools
