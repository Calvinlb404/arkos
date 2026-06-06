"""Tests for tool_module/browser_actions.py — custom Tools() registration."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest


def _install_minimal_browser_use(monkeypatch, action_registry: list) -> Any:
    """Install a fake browser_use module exposing Tools and ActionResult.

    action_registry collects (description, handler) tuples on every
    @tools.action(...) decoration so tests can inspect what was registered.
    """
    fake = types.ModuleType("browser_use")

    class FakeActionResult:
        def __init__(self, extracted_content=None, include_in_memory=False):
            self.extracted_content = extracted_content
            self.include_in_memory = include_in_memory

    class FakeTools:
        def __init__(self):
            self.registered: list[tuple[str, Any]] = action_registry

        def action(self, description: str):
            def decorator(fn):
                self.registered.append((description, fn))
                return fn

            return decorator

    fake.Tools = FakeTools
    fake.ActionResult = FakeActionResult
    monkeypatch.setitem(sys.modules, "browser_use", fake)
    # Also remove any cached browser_actions so the lazy import picks up the fake.
    sys.modules.pop("tool_module.browser_actions", None)
    return fake


def test_build_arkos_tools_returns_none_without_browser_use(monkeypatch):
    monkeypatch.setitem(sys.modules, "browser_use", None)  # make the import fail
    sys.modules.pop("tool_module.browser_actions", None)
    # The import inside build_arkos_tools will fail; the function returns None.
    # We have to use a fresh import to avoid module caching.
    import importlib

    spec = importlib.util.find_spec("tool_module.browser_actions")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Force ImportError by deleting browser_use from sys.modules during call
    monkeypatch.delitem(sys.modules, "browser_use", raising=False)
    # Block re-import by inserting a finder that says no
    import builtins as _b

    real_import = _b.__import__

    def block(name, *a, **kw):
        if name == "browser_use":
            raise ImportError("blocked for test")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(_b, "__import__", block)
    assert mod.build_arkos_tools() is None


def test_build_arkos_tools_registers_dismiss_overlay(monkeypatch):
    registry: list = []
    _install_minimal_browser_use(monkeypatch, registry)

    from tool_module.browser_actions import build_arkos_tools

    tools = build_arkos_tools()
    assert tools is not None
    descriptions = [d for d, _ in registry]
    assert any("cookie" in d.lower() or "consent" in d.lower() for d in descriptions), (
        f"dismiss_overlay should mention cookie/consent in description; got {descriptions!r}"
    )
    handlers = [h for _, h in registry]
    assert any(h.__name__ == "dismiss_overlay" for h in handlers)


@pytest.mark.asyncio
async def test_dismiss_overlay_action_clicks_when_match(monkeypatch):
    registry: list = []
    _install_minimal_browser_use(monkeypatch, registry)

    from tool_module.browser_actions import build_arkos_tools

    tools = build_arkos_tools()
    assert tools is not None
    handler = next(h for _, h in registry if h.__name__ == "dismiss_overlay")

    # Build a fake browser_session whose CDP evaluate returns "I clicked something".
    class FakeCDPClient:
        class send:
            class Runtime:
                @staticmethod
                async def evaluate(params=None, session_id=None):
                    assert "TERMS" in params["expression"]  # arkos's heuristic is loaded
                    return {
                        "result": {
                            "value": {
                                "clicked": True,
                                "label": "accept all",
                                "score": 100,
                                "considered": 12,
                            }
                        }
                    }

    class FakeCDPSession:
        cdp_client = FakeCDPClient
        session_id = "S1"

    class FakeBrowserSession:
        async def get_or_create_cdp_session(self, target_id=None, focus=False):
            return FakeCDPSession()

    result = await handler(FakeBrowserSession())
    assert result.include_in_memory is True
    assert "accept all" in result.extracted_content
    assert "score=100" in result.extracted_content


@pytest.mark.asyncio
async def test_dismiss_overlay_action_no_match_returns_idempotent(monkeypatch):
    registry: list = []
    _install_minimal_browser_use(monkeypatch, registry)

    from tool_module.browser_actions import build_arkos_tools

    build_arkos_tools()
    handler = next(h for _, h in registry if h.__name__ == "dismiss_overlay")

    class FakeCDPClient:
        class send:
            class Runtime:
                @staticmethod
                async def evaluate(params=None, session_id=None):
                    return {"result": {"value": {"clicked": False, "considered": 0}}}

    class FakeCDPSession:
        cdp_client = FakeCDPClient
        session_id = "S1"

    class FakeBrowserSession:
        async def get_or_create_cdp_session(self, target_id=None, focus=False):
            return FakeCDPSession()

    result = await handler(FakeBrowserSession())
    assert result.include_in_memory is False
    assert "No consent/cookie overlay" in result.extracted_content


@pytest.mark.asyncio
async def test_dismiss_overlay_action_swallows_cdp_failures(monkeypatch):
    registry: list = []
    _install_minimal_browser_use(monkeypatch, registry)

    from tool_module.browser_actions import build_arkos_tools

    build_arkos_tools()
    handler = next(h for _, h in registry if h.__name__ == "dismiss_overlay")

    class BrokenBrowserSession:
        async def get_or_create_cdp_session(self, target_id=None, focus=False):
            raise RuntimeError("CDP socket dead")

    # Must NOT propagate the exception — the action returns a graceful result.
    result = await handler(BrokenBrowserSession())
    assert "page not ready" in result.extracted_content
