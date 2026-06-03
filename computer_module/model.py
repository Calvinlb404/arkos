"""
Tool-calling model client for the computer-agent.

Tries native OpenAI-style function calling first (tools=/tool_calls). If the
endpoint returns a 500 (e.g. SGLang launched without --tool-call-parser), falls
back to a constrained-JSON action format where the model emits one action per
turn as a JSON object. The agent loop is identical in both cases.

Works against SGLang now and a frontier OpenAI-compatible endpoint later.
Model is a config knob (computer_agent.llm).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI, InternalServerError

from config_module.loader import config
from model_module.errors import ModelError

logger = logging.getLogger(__name__)

# Action schema for the constrained-JSON fallback. The model emits one of these
# per turn instead of a tool_calls block.
_ACTION_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "action",
        "schema": {
            "type": "object",
            "properties": {
                "tool": {"type": "string", "description": "The tool to call, or 'finish' when done."},
                "args": {"type": "object", "description": "Arguments for the tool."},
                "reasoning": {"type": "string", "description": "One-line explanation of why."},
                "answer": {"type": "string", "description": "Final answer text (only when tool=finish)."},
            },
            "required": ["tool"],
        },
    },
}


class _AssistantMessage:
    """Normalised assistant message -- same shape whether native or fallback path."""

    def __init__(self, content: str | None, tool_calls: list[Any] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeToolCall:
    """Mimics openai.types.chat.ChatCompletionMessageToolCall for the fallback path."""

    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.type = "function"
        self.function = type("F", (), {"name": name, "arguments": arguments})()


class ToolCallingModel:
    """
    Calls an OpenAI-compatible endpoint with tool calling.

    On first call we auto-detect whether the server supports native tool-calling.
    If not, we switch permanently to the constrained-JSON fallback.
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
        self._native: bool | None = None  # None = not yet probed

    @property
    def _client(self) -> AsyncOpenAI:
        return AsyncOpenAI(base_url=self.base_url, api_key=self._api_key)

    async def call(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> _AssistantMessage:
        """
        One model turn. Returns an _AssistantMessage whose tool_calls list (possibly
        empty) drives the next iteration of the agent loop.

        Transparent fallback: if the server doesn't support native tool-calling, we
        switch to the constrained-JSON action path automatically.
        """
        # First call: probe native support
        if self._native is None:
            self._native = await self._probe_native(tools)
            logger.info(
                "computer-agent model %s: using %s tool-calling",
                self.model,
                "native" if self._native else "constrained-JSON fallback",
            )

        if self._native:
            return await self._call_native(messages, tools)
        return await self._call_fallback(messages, tools)

    def _extra(self) -> dict[str, Any]:
        """Extra body params: disable thinking mode for Qwen3 and similar models."""
        return {"chat_template_kwargs": {"enable_thinking": False}}

    async def _probe_native(self, tools: list[dict[str, Any]]) -> bool:
        """Return True if the server accepts tools= without a 500."""
        try:
            await self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "probe"}],
                tools=tools[:1],
                tool_choice="auto",
                max_tokens=4,
                extra_body=self._extra(),
            )
            return True
        except InternalServerError:
            return False
        except Exception:
            return False

    async def _call_native(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> _AssistantMessage:
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                extra_body=self._extra(),
            )
        except InternalServerError as e:
            raise ModelError(str(e), retryable=True, cause=e) from e

        msg = resp.choices[0].message
        return _AssistantMessage(content=msg.content, tool_calls=msg.tool_calls or [])

    async def _call_fallback(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> _AssistantMessage:
        """
        Constrained-JSON action format. We inject a tool-list into the system prompt
        addendum and ask the model to emit one JSON action per turn.
        """
        tool_names = [t["function"]["name"] for t in tools] + ["finish"]
        tool_list = "\n".join(
            f"- {t['function']['name']}: {t['function']['description']}"
            for t in tools
        ) + "\n- finish: call this when the task is done, with your answer in the 'answer' field."

        action_instruction = (
            "\n\n# Response format\n"
            "Respond ONLY with a JSON object -- no prose. One action per response:\n"
            '{"tool": "<tool_name>", "args": {...}, "reasoning": "<why>"}\n'
            "For the final answer: "
            '{"tool": "finish", "answer": "<your summary>", "reasoning": "<why done>"}\n\n'
            f"Available tools:\n{tool_list}\n"
            "Valid tool names: " + ", ".join(tool_names)
        )

        # Inject into system message (or prepend one).
        patched = list(messages)
        if patched and patched[0]["role"] == "system":
            patched[0] = {**patched[0], "content": patched[0]["content"] + action_instruction}
        else:
            patched.insert(0, {"role": "system", "content": action_instruction})

        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=patched,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                response_format={"type": "json_object"},
                extra_body=self._extra(),
            )
        except Exception as e:
            raise ModelError(str(e), retryable=True, cause=e) from e

        raw = resp.choices[0].message.content or "{}"
        try:
            action = json.loads(raw)
        except json.JSONDecodeError:
            # Bad JSON -- return as content so the loop feeds it back.
            return _AssistantMessage(content=raw)

        tool = action.get("tool", "finish")
        if tool == "finish" or tool not in tool_names:
            return _AssistantMessage(content=action.get("answer") or raw)

        # Synthesise a fake tool_call so the loop is identical to the native path.
        import uuid
        fake = _FakeToolCall(
            call_id=f"call_{uuid.uuid4().hex[:8]}",
            name=tool,
            arguments=json.dumps(action.get("args") or {}),
        )
        return _AssistantMessage(content=None, tool_calls=[fake])
