"""Tests for base_module/browser_routes.py — /v1/browser/stream SSE endpoint.

The SSE generator is exercised directly rather than through httpx, because
httpx's ASGITransport buffers streamed responses until the response
generator returns — useless for testing a long-lived SSE stream. Driving
the generator's __anext__ in the test loop gives us deterministic delivery.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from base_module import browser_routes
from tool_module import browser_stream as bs_module


class _FakeRequest:
    """Minimal stand-in for starlette.Request used by the SSE generator."""

    def __init__(self, headers=None, query=None, disconnected: bool = False):
        self.headers = headers or {}
        self.query_params = query or {}
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected


@pytest.fixture
def fresh_broker(monkeypatch):
    broker = bs_module.BrowserStreamBroker()
    monkeypatch.setattr(bs_module, "broker", broker)
    monkeypatch.setattr(browser_routes, "broker", broker)
    return broker


def _parse_sse(chunk: str) -> dict:
    """Parse a single 'data: {...}\\n\\n' frame into a dict."""
    assert chunk.startswith("data: ")
    assert chunk.endswith("\n\n")
    return json.loads(chunk[len("data: ") : -2])


@pytest.mark.asyncio
async def test_event_stream_forwards_broker_events(fresh_broker):
    req = _FakeRequest(headers={"X-User-ID": "alice"})
    gen = browser_routes._event_stream("alice", req)

    fresh_broker.start_session("alice")
    fresh_broker.push_frame("alice", "ZZZZ")
    fresh_broker.end_session("alice")

    out = [_parse_sse(await asyncio.wait_for(gen.__anext__(), timeout=1.0)) for _ in range(3)]
    assert out == [
        {"type": "started"},
        {"type": "frame", "jpeg_b64": "ZZZZ"},
        {"type": "ended"},
    ]
    await gen.aclose()


@pytest.mark.asyncio
async def test_event_stream_isolates_users(fresh_broker):
    """Bob's stream must not see Alice's events."""
    req_bob = _FakeRequest(headers={"X-User-ID": "bob"})
    gen = browser_routes._event_stream("bob", req_bob)

    fresh_broker.start_session("alice")
    fresh_broker.push_frame("alice", "A1")
    fresh_broker.start_session("bob")
    fresh_broker.push_frame("bob", "B1")

    out = [_parse_sse(await asyncio.wait_for(gen.__anext__(), timeout=1.0)) for _ in range(2)]
    assert out == [
        {"type": "started"},
        {"type": "frame", "jpeg_b64": "B1"},
    ]
    await gen.aclose()


@pytest.mark.asyncio
async def test_event_stream_exits_on_disconnect(fresh_broker, monkeypatch):
    """When the client disconnects, the generator must exit during the poll."""
    # Shrink the poll so the test doesn't wait a full second.
    monkeypatch.setattr(browser_routes, "_DISCONNECT_POLL_SECONDS", 0.01)

    req = _FakeRequest(headers={"X-User-ID": "ghost"}, disconnected=True)
    gen = browser_routes._event_stream("ghost", req)

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(gen.__anext__(), timeout=1.0)


def test_resolve_user_id_prefers_header():
    req = _FakeRequest(headers={"X-User-ID": "from-header"}, query={"user_id": "from-query"})
    assert browser_routes._resolve_user_id(req) == "from-header"


def test_resolve_user_id_falls_back_to_query():
    req = _FakeRequest(headers={}, query=SimpleNamespace())
    # query_params behaves like a dict for `.get`; SimpleNamespace lacks it,
    # so use a plain dict to exercise the fallthrough to query then config.
    req.query_params = {"user_id": "from-query"}
    assert browser_routes._resolve_user_id(req) == "from-query"
