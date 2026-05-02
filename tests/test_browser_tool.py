"""Tests for tool_module/browser_tool.py — browser automation tool.

The actual browser-use Agent is mocked. These tests verify the wiring:
config plumbing, error surfacing, isolation between concurrent users, and
correct registration on the tool manager.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_fake_browser_use(monkeypatch, run_side_effect=None, captured_cdp=None):
    """Install a fake `browser_use` and `langchain_openai` module so the lazy
    import inside run_browser_task succeeds without the real packages.

    `run_side_effect` is awaited as the result of Agent.run(). If a list is
    passed, each call pops the next item.
    """
    fake_browser_use = types.ModuleType("browser_use")
    fake_langchain = types.ModuleType("langchain_openai")

    class FakeBrowserConfig:
        def __init__(self, cdp_url=None):
            self.cdp_url = cdp_url
            if captured_cdp is not None:
                captured_cdp.append(cdp_url)

    class FakeBrowser:
        def __init__(self, config):
            self.config = config
            self.closed = False

        async def close(self):
            self.closed = True

    class FakeHistory:
        def __init__(self, value):
            self._value = value

        def final_result(self):
            return self._value

    class FakeAgent:
        def __init__(self, task, llm, browser):
            self.task = task
            self.llm = llm
            self.browser = browser

        async def run(self):
            value = run_side_effect.pop(0) if isinstance(run_side_effect, list) else run_side_effect
            if isinstance(value, Exception):
                raise value
            return FakeHistory(value)

    class FakeChatOpenAI:
        def __init__(self, model):
            self.model = model

    fake_browser_use.Agent = FakeAgent
    fake_browser_use.Browser = FakeBrowser
    fake_browser_use.BrowserConfig = FakeBrowserConfig
    fake_langchain.ChatOpenAI = FakeChatOpenAI

    monkeypatch.setitem(sys.modules, "browser_use", fake_browser_use)
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_langchain)


# ---------------------------------------------------------------------------
# Test 1: tool returns a result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_tool_returns_result(monkeypatch):
    from tool_module.browser_tool import run_browser_task

    captured = []
    _install_fake_browser_use(monkeypatch, run_side_effect="Example Domain", captured_cdp=captured)
    monkeypatch.setenv("BROWSERLESS_URL", "ws://browserless:3000")

    result = await run_browser_task("user_1", "go to example.com and return the title")

    assert result == "Example Domain"
    assert captured == ["ws://browserless:3000"]


# ---------------------------------------------------------------------------
# Test 2: user isolation (concurrent calls, separate sessions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_tool_user_isolation(monkeypatch):
    from tool_module.browser_tool import run_browser_task

    _install_fake_browser_use(monkeypatch, run_side_effect=["A", "B"])
    monkeypatch.setenv("BROWSERLESS_URL", "ws://browserless:3000")

    result_a, result_b = await asyncio.gather(
        run_browser_task("user_a", "task one"),
        run_browser_task("user_b", "task two"),
    )

    # Each call returns its own result; one user's result does not bleed into
    # the other's. (Fake Agent pops from a shared list, so order doesn't matter
    # as long as the set is correct.)
    assert {result_a, result_b} == {"A", "B"}


# ---------------------------------------------------------------------------
# Test 3: missing/unreachable browserless surfaces a clear error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_tool_missing_url_raises(monkeypatch):
    from tool_module.browser_tool import BrowserToolError, run_browser_task

    monkeypatch.delenv("BROWSERLESS_URL", raising=False)
    with pytest.raises(BrowserToolError, match="BROWSERLESS_URL"):
        await run_browser_task("user_1", "anything")


@pytest.mark.asyncio
async def test_browser_tool_agent_failure_surfaces(monkeypatch):
    from tool_module.browser_tool import BrowserToolError, run_browser_task

    _install_fake_browser_use(
        monkeypatch,
        run_side_effect=ConnectionRefusedError("CDP endpoint unreachable"),
    )
    monkeypatch.setenv("BROWSERLESS_URL", "ws://nope:3000")

    with pytest.raises(BrowserToolError, match="CDP endpoint unreachable"):
        await run_browser_task("user_1", "anything")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_browser_tool_calls_register_local_tool():
    from tool_module.browser_tool import register_browser_tool

    tm = MagicMock()
    tm.register_local_tool = MagicMock()

    register_browser_tool(tm)

    tm.register_local_tool.assert_called_once()
    kwargs = tm.register_local_tool.call_args.kwargs
    assert kwargs["name"] == "browser_task"
    assert "task" in kwargs["input_schema"]["properties"]
    assert kwargs["input_schema"]["required"] == ["task"]


def test_register_browser_tool_noop_on_none():
    from tool_module.browser_tool import register_browser_tool

    register_browser_tool(None)  # must not raise


@pytest.mark.asyncio
async def test_handler_routes_through_run_browser_task(monkeypatch):
    """The MCP-shaped handler unpacks `task` from arguments and returns content."""
    import tool_module.browser_tool as bt

    monkeypatch.setattr(bt, "run_browser_task", AsyncMock(return_value="hello"))
    out = await bt._handler({"task": "say hi"}, "user_1")
    assert out == {"content": [{"type": "text", "text": "hello"}]}
    bt.run_browser_task.assert_awaited_once_with("user_1", "say hi")


@pytest.mark.asyncio
async def test_handler_rejects_missing_task():
    from tool_module.browser_tool import BrowserToolError, _handler

    with pytest.raises(BrowserToolError, match="task"):
        await _handler({}, "user_1")
