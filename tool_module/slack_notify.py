"""
Sends proactive Slack DMs from background task processes.

Uses the bot token directly — does NOT go through Smithery/MCP, which
requires an active agent session that background tasks don't have.
"""

from __future__ import annotations

import logging

import aiohttp
import psycopg2
import psycopg2.extras

from config_module.loader import config

logger = logging.getLogger(__name__)


def _get_slack_user_id(user_id: str) -> str | None:
    conn = psycopg2.connect(config.get("database.url"))
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT slack_user_id FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return row["slack_user_id"] if row else None
    finally:
        conn.close()


async def send_dm(user_id: str, text: str) -> None:
    """
    Send a Slack DM to the user associated with the given ARKOS user_id.

    Args:
        user_id: ARKOS UUID — looked up against the users table for slack_user_id.
        text: Message body sent to the user's Slack DM channel.

    Silently no-ops if the user hasn't linked their Slack account.
    """
    slack_uid = _get_slack_user_id(user_id)
    if not slack_uid:
        return
    token = config.get("mcp_servers.slack.headers.SLACK_BOT_TOKEN")
    if not token:
        logger.warning("send_dm: SLACK_BOT_TOKEN not configured, skipping")
        return
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://slack.com/api/conversations.open",
            headers=headers,
            json={"users": slack_uid},
        ) as r:
            dm = await r.json()
        if not dm.get("ok"):
            logger.warning("send_dm: conversations.open failed: %s", dm.get("error"))
            return
        async with session.post(
            "https://slack.com/api/chat.postMessage",
            headers=headers,
            json={"channel": dm["channel"]["id"], "text": text},
        ) as r:
            body = await r.json()
        if not body.get("ok"):
            logger.warning("send_dm: chat.postMessage failed: %s", body.get("error"))
