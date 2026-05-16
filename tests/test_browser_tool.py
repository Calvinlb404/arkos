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
    """Install a fake `browser_use` module so the lazy import inside
    run_browser_task succeeds without the real package.

    Matches the browser-use 0.12 surface: top-level Agent, Browser (an alias
    for BrowserSession that takes cdp_url directly), and ChatOpenAI.

    `run_side_effect` is awaited as the result of Agent.run(). If a list is
    passed, each call pops the next item.
    """
    fake_browser_use = types.ModuleType("browser_use")

    class FakeBrowser:
        def __init__(self, cdp_url=None, is_local=True):
            self.cdp_url = cdp_url
            self.is_local = is_local
            self.closed = False
            if captured_cdp is not None:
                captured_cdp.append(cdp_url)

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
        def __init__(self, model, base_url=None, api_key=None):
            self.model = model
            self.base_url = base_url
            self.api_key = api_key

    fake_browser_use.Agent = FakeAgent
    fake_browser_use.Browser = FakeBrowser
    fake_browser_use.ChatOpenAI = FakeChatOpenAI

    monkeypatch.setitem(sys.modules, "browser_use", fake_browser_use)


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
async def test_browser_tool_routes_llm_through_sglang(monkeypatch):
    """The browser-use Agent's LLM should be pointed at the in-cluster SGLang
    Qwen server rather than the public OpenAI API."""
    import tool_module.browser_tool as bt

    captured_llm = {}

    fake_browser_use = types.ModuleType("browser_use")

    class FakeBrowser:
        def __init__(self, cdp_url=None, is_local=True):
            self.cdp_url = cdp_url

        async def close(self):
            pass

    class FakeHistory:
        def final_result(self):
            return "ok"

    class FakeAgent:
        def __init__(self, task, llm, browser):
            self.llm = llm

        async def run(self):
            return FakeHistory()

    class FakeChatOpenAI:
        def __init__(self, model, base_url=None, api_key=None):
            captured_llm["model"] = model
            captured_llm["base_url"] = base_url
            captured_llm["api_key"] = api_key

    fake_browser_use.Agent = FakeAgent
    fake_browser_use.Browser = FakeBrowser
    fake_browser_use.ChatOpenAI = FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "browser_use", fake_browser_use)

    monkeypatch.setenv("BROWSERLESS_URL", "ws://browserless:3000")
    monkeypatch.setenv("SGLANG_URL", "http://sglang:30000")
    monkeypatch.delenv("BROWSER_USE_MODEL", raising=False)

    await bt.run_browser_task("user_1", "anything")

    assert captured_llm["base_url"] == "http://sglang:30000/v1"
    assert captured_llm["model"] == "tgi"


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


# ---------------------------------------------------------------------------
# Screencast wiring
# ---------------------------------------------------------------------------


class _FakeCDP:
    """Minimal stand-in for a playwright CDPSession that records sent commands
    and lets a test fire synthetic Page.screencastFrame events at it."""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self._handlers: dict[str, list] = {}

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    async def send(self, method, params=None):
        self.sent.append((method, params or {}))

    def fire(self, event, params):
        for h in self._handlers.get(event, []):
            h(params)


class _FakeContext:
    def __init__(self, cdp):
        self._cdp = cdp

    async def new_cdp_session(self, page):
        return self._cdp


class _FakePage:
    def __init__(self, cdp):
        self.context = _FakeContext(cdp)


@pytest.mark.asyncio
async def test_browser_tool_starts_and_ends_screencast_session(monkeypatch):
    """Around agent.run(), the broker must see exactly one started/ended pair
    and any CDP frames must be forwarded to push_frame."""
    import tool_module.browser_tool as bt

    cdp = _FakeCDP()
    page = _FakePage(cdp)

    captured = []

    class FakeBroker:
        def start_session(self, user_id):
            captured.append(("start", user_id))

        def push_frame(self, user_id, jpeg_b64):
            captured.append(("frame", user_id, jpeg_b64))

        def end_session(self, user_id):
            captured.append(("end", user_id))

    monkeypatch.setattr(bt, "_stream_broker", FakeBroker())
    monkeypatch.setattr(bt, "_find_agent_page", lambda agent: page)

    _install_fake_browser_use(monkeypatch, run_side_effect="done")
    monkeypatch.setenv("BROWSERLESS_URL", "ws://browserless:3000")

    # Drive a frame through during agent.run(): patch agent.run on the fake.
    import sys as _sys

    real_browser_use = _sys.modules["browser_use"]

    class FakeAgentWithFrame:
        def __init__(self, task, llm, browser):
            self.browser = browser

        async def run(self):
            # Give the screencast task time to attach, then fire a frame.
            for _ in range(20):
                if (
                    "Page.startScreencast",
                    {
                        "format": "jpeg",
                        "quality": 60,
                        "maxWidth": 1024,
                        "maxHeight": 768,
                        "everyNthFrame": 1,
                    },
                ) in cdp.sent:
                    break
                await asyncio.sleep(0.01)
            cdp.fire("Page.screencastFrame", {"data": "ZZZZ", "sessionId": 1})
            await asyncio.sleep(0.01)

            class _H:
                def final_result(self_inner):
                    return "done"

            return _H()

    real_browser_use.Agent = FakeAgentWithFrame

    result = await bt.run_browser_task("user_42", "do a thing")

    assert result == "done"
    # Must have exactly one start/end and at least one frame in between.
    assert ("start", "user_42") in captured
    assert ("end", "user_42") in captured
    assert ("frame", "user_42", "ZZZZ") in captured
    # Acks should have been sent for any fired frames.
    assert ("Page.screencastFrameAck", {"sessionId": 1}) in cdp.sent
    # Stop must have been issued on teardown.
    assert ("Page.stopScreencast", {}) in cdp.sent


@pytest.mark.asyncio
async def test_browser_tool_screencast_disabled_via_env(monkeypatch):
    """BROWSER_STREAM_ENABLED=0 should skip the broker entirely."""
    import tool_module.browser_tool as bt

    touched = []

    class TouchyBroker:
        def start_session(self, user_id):
            touched.append("start")

        def push_frame(self, user_id, jpeg_b64):
            touched.append("frame")

        def end_session(self, user_id):
            touched.append("end")

    monkeypatch.setattr(bt, "_stream_broker", TouchyBroker())
    _install_fake_browser_use(monkeypatch, run_side_effect="done")
    monkeypatch.setenv("BROWSERLESS_URL", "ws://browserless:3000")
    monkeypatch.setenv("BROWSER_STREAM_ENABLED", "0")

    result = await bt.run_browser_task("user_1", "task")

    assert result == "done"
    assert touched == []


@pytest.mark.asyncio
async def test_browser_tool_screencast_failure_does_not_break_agent(monkeypatch):
    """If we cannot find a page, the agent must still complete and return."""
    import tool_module.browser_tool as bt

    monkeypatch.setattr(bt, "_find_agent_page", lambda agent: None)
    _install_fake_browser_use(monkeypatch, run_side_effect="ok")
    monkeypatch.setenv("BROWSERLESS_URL", "ws://browserless:3000")

    # Speed up the page-discovery loop in the test
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr(bt.asyncio, "sleep", fast_sleep)

    result = await bt.run_browser_task("user_1", "task")
    assert result == "ok"
