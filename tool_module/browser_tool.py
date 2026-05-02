"""
Browser automation tool: hand a natural-language task to a sandboxed Chromium
session managed by Browserless and return the final string result.

User isolation is delegated to Browserless: each call gets a fresh session
from the pool, torn down on completion or timeout. This module only owns the
glue between Arkos's tool registry and the `browser-use` Agent.

Configuration (env):
  BROWSERLESS_URL    CDP WebSocket URL, e.g. ws://browserless:3000 (required)
  SGLANG_URL         base URL of the in-cluster SGLang Qwen server
                     (default http://sglang:30000); the tool talks to its
                     OpenAI-compatible /v1 endpoint
  BROWSER_USE_MODEL  model name to send to SGLang (default "tgi", which the
                     SGLang launcher accepts as an alias for whatever model
                     is loaded)
  OPENAI_API_KEY     forwarded as the bearer token; SGLang ignores it but the
                     OpenAI client requires something. Defaults to "sk-dummy".
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class BrowserToolError(RuntimeError):
    """Raised when the browser automation tool cannot run a task."""


async def run_browser_task(user_id: str, task: str) -> str:
    """Run a single browser task in an isolated Browserless session.

    Returns the agent's final string result. Raises BrowserToolError if
    Browserless is unreachable or the configured CDP endpoint is missing.
    """
    cdp_url = os.environ.get("BROWSERLESS_URL")
    if not cdp_url:
        raise BrowserToolError("BROWSERLESS_URL is not set; cannot reach the sandboxed browser pool")

    # Lazy imports so the rest of arkos can boot without browser-use installed.
    try:
        from browser_use import Agent, Browser, BrowserConfig
        from langchain_openai import ChatOpenAI
    except ImportError as e:
        raise BrowserToolError(f"browser-use is not installed in this environment: {e}") from e

    sglang_base = os.environ.get("SGLANG_URL", "http://sglang:30000").rstrip("/")
    llm_base_url = f"{sglang_base}/v1"
    llm_model = os.environ.get("BROWSER_USE_MODEL", "tgi")
    llm_api_key = os.environ.get("OPENAI_API_KEY", "sk-dummy")

    logger.info(
        "browser_tool: user=%s cdp=%s llm=%s model=%s",
        user_id,
        cdp_url,
        llm_base_url,
        llm_model,
    )
    browser = Browser(config=BrowserConfig(cdp_url=cdp_url))
    agent = Agent(
        task=task,
        llm=ChatOpenAI(model=llm_model, base_url=llm_base_url, api_key=llm_api_key),
        browser=browser,
    )

    try:
        history = await agent.run()
    except Exception as e:
        raise BrowserToolError(f"browser task failed: {e}") from e
    finally:
        try:
            await browser.close()
        except Exception:
            logger.exception("browser_tool: error closing browser session")

    final = history.final_result() if hasattr(history, "final_result") else str(history)
    return final or ""


async def _handler(arguments: dict[str, Any], user_id: str | None) -> dict[str, Any]:
    task = arguments.get("task")
    if not task or not isinstance(task, str):
        raise BrowserToolError("browser_task requires a non-empty 'task' string argument")
    result = await run_browser_task(user_id or "anonymous", task)
    return {"content": [{"type": "text", "text": result}]}


def register_browser_tool(tool_manager: Any) -> None:
    """Register `browser_task` as a local tool on the shared tool manager.

    Safe to call when tool_manager is None (e.g. Smithery disabled in dev) — it
    becomes a no-op so app startup still succeeds.
    """
    if tool_manager is None:
        logger.info("browser_tool: tool_manager is None; skipping registration")
        return
    tool_manager.register_local_tool(
        name="browser_task",
        description=(
            "Run a natural-language browser automation task in a sandboxed "
            "Chromium session. Pass a 'task' string describing what the "
            "browser should do; receive the final result text."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Natural-language description of the browser task",
                },
            },
            "required": ["task"],
        },
        handler=_handler,
    )
    logger.info("browser_tool: registered browser_task on tool_manager")
