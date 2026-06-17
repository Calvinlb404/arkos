import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, Optional

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import BaseModel, Field

from model_module.errors import ModelError

logger = logging.getLogger(__name__)


# --- Custom Message Classes ---
# These classes define the structure for different types of messages
class Message(BaseModel):
    """Base class for all messages."""

    content: str
    role: str


class SystemMessage(Message):
    """Represents a message to the system"""

    role: str = "system"


class UserMessage(Message):
    """Represents a message from the user."""

    role: str = "user"


class ToolMessage(Message):
    """Represents a message from a tool call"""

    role: str = "tool"
    tool_calls: dict | None = None


class AIMessage(Message):
    """
    Represents a message from the AI.
    Can include tool calls if the AI decides to use tools.
    """

    role: str = "assistant"
    # content is now Optional[str] to handle cases where the AI's turn is solely a tool call.
    content: str | None = None

    tool_calls: dict | None = None


class ArkModelLink(BaseModel):
    """
    Chat model backed by either a local SGLang/TGI endpoint or the OpenAI API.

    Both speak the OpenAI-compatible REST protocol so the same client works for
    both. Set api_key="-" for local endpoints (no real auth needed); set it to
    the real OPENAI_API_KEY value for the OpenAI backend.
    """

    model_name: str = Field(default="tgi")
    base_url: str = Field(default="http://0.0.0.0:30000/v1")
    max_tokens: int = Field(default=1024)
    temperature: float = Field(default=0.7)
    api_key: str = Field(default="-")  # "-" = local placeholder; real key for OpenAI

    # Use a property or method to initialize the client asynchronously if needed,
    # or just create it in the async method, as AsyncOpenAI handles the session.

    # We'll use a property for a lazy, non-async instantiation of the client wrapper.
    # The actual network calls will be awaited inside the method.
    @property
    def client(self) -> AsyncOpenAI:
        """Returns the configured AsyncOpenAI client."""
        return AsyncOpenAI(
            base_url=self.base_url,
            api_key=self.api_key,
        )

    async def make_llm_call(self, messages: list[Message], json_schema: Optional, stream=False) -> dict[str, Any] | str:
        """
        Makes an ASYNCHRONOUS call to the OpenAI-compatible LLM endpoint.

        Args:
            messages: A list of custom Message objects representing the conversation history.
            json_schema: An optional schema to expose to the LLM.

        Returns:
            The content of the LLM's text response (str) or a detailed dict if streaming.
        """

        # Convert custom Message objects into the format expected by the OpenAI API.

        openai_messages_payload = []
        for msg in messages:
            if isinstance(msg, UserMessage):
                openai_messages_payload.append({"role": "user", "content": msg.content})

            elif isinstance(msg, SystemMessage):
                openai_messages_payload.append({"role": "system", "content": msg.content})

            elif isinstance(msg, ToolMessage):
                # Note: ToolMessage in OpenAI API usually requires 'tool_call_id'
                # and 'name' if it's a ToolMessage response, but this format
                # (role='tool', content=...) is often used for simple outputs.
                openai_messages_payload.append({"role": "tool", "content": msg.content})

            elif isinstance(msg, AIMessage):
                msg_dict = {"role": "assistant"}
                # Always include 'content' key for assistant messages.
                msg_dict["content"] = msg.content if msg.content is not None else ""
                openai_messages_payload.append(msg_dict)
            else:
                print(type(msg))
                print(msg)
                raise ValueError("Unsupported Message Type ArkModel.py")

        if stream:
            raise NotImplementedError("Streaming not yet implemented; use generate_stream.")

        try:
            chat_completion = await self.client.chat.completions.create(
                model=self.model_name,
                messages=openai_messages_payload,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                response_format=json_schema,
            )
            return chat_completion.choices[0].message.content

        except (APITimeoutError, APIConnectionError) as e:
            raise ModelError(f"Model request timed out or connection failed: {e}", retryable=True, cause=e) from e
        except RateLimitError as e:
            raise ModelError(f"Rate limit hit: {e}", retryable=True, cause=e) from e
        except InternalServerError as e:
            raise ModelError(f"Model server error: {e}", retryable=True, cause=e) from e
        except (BadRequestError, AuthenticationError, PermissionDeniedError) as e:
            raise ModelError(f"Model request rejected: {e}", retryable=False, cause=e) from e

    async def generate_response(self, messages: list[Message], json_schema) -> str:
        """
        Generate a response, retrying up to 3 times on transient failures.

        Raises ModelError on terminal failures (bad request, auth) and after
        exhausting retries on transient failures (timeout, rate limit, 5xx).
        Never returns an error string -- callers must handle ModelError.

        Args:
            messages: Conversation history to send.
            json_schema: Optional structured output schema.

        Returns:
            Raw model response content as a string.

        Raises:
            ModelError: If the call fails terminally or retries are exhausted.
        """
        delay = 1.0
        last_error: ModelError | None = None

        for attempt in range(3):
            try:
                return await self.make_llm_call(messages, json_schema=json_schema)
            except ModelError as e:
                if not e.retryable:
                    raise
                last_error = e
                logger.warning(
                    "model call failed (attempt %d/3), retrying in %.1fs: %s",
                    attempt + 1,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)
                delay *= 2

        raise last_error

    def _format_messages(self, messages: list[Message]) -> list[dict[str, str]]:
        """Convert Message objects to OpenAI format."""
        formatted = []
        for msg in messages:
            if isinstance(msg, (UserMessage, SystemMessage, ToolMessage)):
                formatted.append({"role": msg.role, "content": msg.content or ""})
            elif isinstance(msg, AIMessage):
                formatted.append({"role": "assistant", "content": msg.content or ""})
        return formatted

    async def generate_stream(self, messages: list[Message]) -> AsyncIterator[str]:
        """Stream tokens as they're generated."""
        openai_messages = self._format_messages(messages)

        try:
            stream = await self.client.chat.completions.create(
                model=self.model_name,
                messages=openai_messages,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                stream=True,
            )
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:
            logger.error("streaming failed: %s", e)
            raise
