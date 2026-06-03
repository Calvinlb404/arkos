"""
Tool-calling model client for the computer-agent.

Uses OpenAI-style native function calling (tools=/tool_calls). Works against any
OpenAI-compatible endpoint -- SGLang, vLLM, or a frontier API.

For Qwen3 on SGLang: launch with --tool-call-parser qwen25
For a frontier model: point computer_agent.llm at the frontier endpoint.

Does NOT mask errors or fall back silently. A 500 from the server means the
server is misconfigured; surface it so it gets fixed.
"""

from __future__ import annotations

import logging
from typing import Any

from openai import AsyncOpenAI

from config_module.loader import config
from model_module.errors import ModelError

logger = logging.getLogger(__name__)


class ToolCallingModel:
    """
    One model turn via native OpenAI function calling.

    Requires the endpoint to support tools=/tool_calls. For SGLang + Qwen3:
        --tool-call-parser qwen25

    Raises ModelError on transport/server failures so the agent loop can
    surface them rather than silently degrading.
    """

    def __init__(self) -> None:
        self.base_url: str = (
            config.get("computer_agent.llm.base_url") or config.get("llm.base_url") or ""
        )
        self.model: str = (
            config.get("computer_agent.llm.model_name")
            or config.get("llm.model_name")
            or "tgi"
        )
        self.max_tokens: int = int(config.get("computer_agent.llm.max_tokens") or 4096)
        self.temperature: float = float(config.get("computer_agent.llm.temperature") or 0.7)
        self._api_key: str = config.get("computer_agent.llm.api_key") or "-"

    @property
    def _client(self) -> AsyncOpenAI:
        return AsyncOpenAI(base_url=self.base_url, api_key=self._api_key)

    async def call(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        """
        One model turn. Returns the raw assistant message object from the SDK.
        .tool_calls is a list (possibly empty); .content is the final text when empty.

        Raises ModelError on any server/transport failure -- never swallows errors.
        """
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                # Disable Qwen3 thinking scratchpad so content is in the right field.
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
        except Exception as e:
            from openai import InternalServerError, APIConnectionError, APITimeoutError, RateLimitError
            retryable = isinstance(e, (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError))
            raise ModelError(
                f"model call failed ({type(e).__name__}): {e}",
                retryable=retryable,
                cause=e,
            ) from e

        return resp.choices[0].message
