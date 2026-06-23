"""Tests for tool_module/slack_notify.py — DB lookup and send_dm branches.

Mocks psycopg2 and aiohttp — no live DB or Slack API calls.
Does not test message content quality, only control flow.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from tool_module.slack_notify import _get_slack_user_id, send_dm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(json_data: dict) -> MagicMock:
    """Async context manager mock for a single aiohttp response."""
    r = MagicMock()
    r.json = AsyncMock(return_value=json_data)
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _make_session(*responses: MagicMock) -> MagicMock:
    """Async context manager mock for aiohttp.ClientSession.

    Each positional arg is the response returned by successive session.post() calls.
    """
    it = iter(responses)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(side_effect=lambda *a, **kw: next(it))
    return session


# ---------------------------------------------------------------------------
# _get_slack_user_id
# ---------------------------------------------------------------------------


class TestGetSlackUserId:
    def _mock_conn(self, row: dict | None) -> MagicMock:
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = row
        conn.cursor.return_value = cur
        return conn

    def test_returns_slack_id_when_row_exists(self):
        conn = self._mock_conn({"slack_user_id": "U012AB3CD"})
        with patch("tool_module.slack_notify.psycopg2.connect", return_value=conn):
            result = _get_slack_user_id("user-1")
        assert result == "U012AB3CD"

    def test_returns_none_when_no_row(self):
        conn = self._mock_conn(None)
        with patch("tool_module.slack_notify.psycopg2.connect", return_value=conn):
            result = _get_slack_user_id("user-1")
        assert result is None

    def test_returns_none_when_slack_id_is_null(self):
        conn = self._mock_conn({"slack_user_id": None})
        with patch("tool_module.slack_notify.psycopg2.connect", return_value=conn):
            result = _get_slack_user_id("user-1")
        assert result is None


# ---------------------------------------------------------------------------
# send_dm
# ---------------------------------------------------------------------------


class TestSendDm:
    def test_noop_when_user_has_no_slack_id(self):
        with patch("tool_module.slack_notify._get_slack_user_id", return_value=None):
            with patch("tool_module.slack_notify.aiohttp.ClientSession") as mock_cls:
                asyncio.run(send_dm("user-1", "hello"))
                mock_cls.assert_not_called()

    def test_noop_when_bot_token_not_configured(self):
        with patch("tool_module.slack_notify._get_slack_user_id", return_value="U012AB3CD"):
            with patch("tool_module.slack_notify.config") as mock_cfg:
                mock_cfg.get.return_value = None
                with patch("tool_module.slack_notify.aiohttp.ClientSession") as mock_cls:
                    asyncio.run(send_dm("user-1", "hello"))
                    mock_cls.assert_not_called()

    def test_sends_dm_calls_both_slack_endpoints(self):
        open_resp = _make_response({"ok": True, "channel": {"id": "DM123"}})
        post_resp = _make_response({"ok": True})
        session = _make_session(open_resp, post_resp)

        with patch("tool_module.slack_notify._get_slack_user_id", return_value="U012AB3CD"):
            with patch("tool_module.slack_notify.config") as mock_cfg:
                mock_cfg.get.return_value = "xoxb-test-token"
                with patch("tool_module.slack_notify.aiohttp.ClientSession", return_value=session):
                    asyncio.run(send_dm("user-1", "task done"))

        assert session.post.call_count == 2
        urls = [c.args[0] for c in session.post.call_args_list]
        assert any("conversations.open" in u for u in urls)
        assert any("chat.postMessage" in u for u in urls)

    def test_logs_warning_and_returns_when_conversations_open_fails(self, caplog):
        open_resp = _make_response({"ok": False, "error": "not_in_channel"})
        session = _make_session(open_resp)

        with patch("tool_module.slack_notify._get_slack_user_id", return_value="U012AB3CD"):
            with patch("tool_module.slack_notify.config") as mock_cfg:
                mock_cfg.get.return_value = "xoxb-test-token"
                with patch("tool_module.slack_notify.aiohttp.ClientSession", return_value=session):
                    with caplog.at_level(logging.WARNING, logger="tool_module.slack_notify"):
                        asyncio.run(send_dm("user-1", "hello"))

        assert session.post.call_count == 1  # chat.postMessage was never called
        assert "conversations.open failed" in caplog.text

    def test_logs_warning_when_post_message_fails(self, caplog):
        open_resp = _make_response({"ok": True, "channel": {"id": "DM123"}})
        post_resp = _make_response({"ok": False, "error": "channel_not_found"})
        session = _make_session(open_resp, post_resp)

        with patch("tool_module.slack_notify._get_slack_user_id", return_value="U012AB3CD"):
            with patch("tool_module.slack_notify.config") as mock_cfg:
                mock_cfg.get.return_value = "xoxb-test-token"
                with patch("tool_module.slack_notify.aiohttp.ClientSession", return_value=session):
                    with caplog.at_level(logging.WARNING, logger="tool_module.slack_notify"):
                        asyncio.run(send_dm("user-1", "hello"))

        assert "chat.postMessage failed" in caplog.text
