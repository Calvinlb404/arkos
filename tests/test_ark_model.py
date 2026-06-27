"""Tests for ArkModelLink in model_module/ArkModelNew.py."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import AuthenticationError, InternalServerError

from model_module.ArkModelNew import (
    AIMessage,
    ArkModelLink,
    Message,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from model_module.errors import ModelError


class TestArkModelLinkInit:
    def test_default_values(self):
        model = ArkModelLink()
        assert model.model_name == "tgi"
        assert model.base_url == "http://0.0.0.0:30000/v1"
        assert model.max_tokens == 1024
        assert model.temperature == 0.7

    def test_custom_values(self):
        model = ArkModelLink(
            model_name="custom",
            base_url="http://localhost:8080/v1",
            max_tokens=2048,
            temperature=0.5,
        )
        assert model.model_name == "custom"
        assert model.base_url == "http://localhost:8080/v1"
        assert model.max_tokens == 2048
        assert model.temperature == 0.5

    def test_client_property_returns_async_openai(self):
        model = ArkModelLink()
        client = model.client
        from openai import AsyncOpenAI

        assert isinstance(client, AsyncOpenAI)


class TestFormatMessages:
    def test_format_user_message(self):
        model = ArkModelLink()
        result = model._format_messages([UserMessage(content="hello")])
        assert result == [{"role": "user", "content": "hello"}]

    def test_format_system_message(self):
        model = ArkModelLink()
        result = model._format_messages([SystemMessage(content="be helpful")])
        assert result == [{"role": "system", "content": "be helpful"}]

    def test_format_ai_message(self):
        model = ArkModelLink()
        result = model._format_messages([AIMessage(content="I can help")])
        assert result == [{"role": "assistant", "content": "I can help"}]

    def test_format_ai_message_none_content(self):
        model = ArkModelLink()
        result = model._format_messages([AIMessage(content=None)])
        assert result == [{"role": "assistant", "content": ""}]

    def test_format_tool_message(self):
        model = ArkModelLink()
        result = model._format_messages([ToolMessage(content="result")])
        assert result == [{"role": "tool", "content": "result"}]

    def test_format_mixed_messages(self):
        model = ArkModelLink()
        msgs = [
            SystemMessage(content="system"),
            UserMessage(content="hi"),
            AIMessage(content="hello"),
            ToolMessage(content="data"),
        ]
        result = model._format_messages(msgs)
        assert len(result) == 4
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "user"
        assert result[2]["role"] == "assistant"
        assert result[3]["role"] == "tool"

    def test_format_empty_list(self):
        model = ArkModelLink()
        result = model._format_messages([])
        assert result == []


class TestMakeLLMCall:
    @pytest.mark.asyncio
    async def test_successful_call(self):
        model = ArkModelLink()

        mock_choice = MagicMock()
        mock_choice.message.content = "test response"
        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)

        with patch.object(
            ArkModelLink,
            "client",
            new_callable=lambda: property(lambda self: mock_client),
        ):
            result = await model.make_llm_call([UserMessage(content="hello")], json_schema=None)
            assert result == "test response"

    @pytest.mark.asyncio
    async def test_call_with_various_message_types(self):
        model = ArkModelLink()

        mock_choice = MagicMock()
        mock_choice.message.content = "response"
        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)

        with patch.object(
            ArkModelLink,
            "client",
            new_callable=lambda: property(lambda self: mock_client),
        ):
            msgs = [
                SystemMessage(content="sys"),
                UserMessage(content="hi"),
                AIMessage(content="hello"),
                ToolMessage(content="data"),
            ]
            result = await model.make_llm_call(msgs, json_schema=None)
            assert result == "response"

            # Verify the payload sent to the API
            call_args = mock_client.chat.completions.create.call_args
            sent_messages = call_args.kwargs["messages"]
            assert len(sent_messages) == 4
            assert sent_messages[0]["role"] == "system"
            assert sent_messages[1]["role"] == "user"
            assert sent_messages[2]["role"] == "assistant"
            assert sent_messages[3]["role"] == "tool"

    @pytest.mark.asyncio
    async def test_unsupported_message_type_raises(self):
        model = ArkModelLink()

        mock_client = MagicMock()
        with (
            patch.object(
                ArkModelLink,
                "client",
                new_callable=lambda: property(lambda self: mock_client),
            ),
            pytest.raises(ValueError, match="Unsupported Message Type"),
        ):
            await model.make_llm_call([Message(content="raw", role="custom")], json_schema=None)

    @pytest.mark.asyncio
    async def test_stream_raises_not_implemented(self):
        model = ArkModelLink()
        with pytest.raises(NotImplementedError):
            await model.make_llm_call([UserMessage(content="hello")], json_schema=None, stream=True)

    @pytest.mark.asyncio
    async def test_retryable_error_raises_model_error(self):
        model = ArkModelLink()

        # Simulate a server error (retryable) from the API.
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.headers = {}
        server_err = InternalServerError("server down", response=mock_response, body=None)

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=server_err)

        with patch.object(
            ArkModelLink,
            "client",
            new_callable=lambda: property(lambda self: mock_client),
        ):
            with pytest.raises(ModelError) as exc_info:
                await model.make_llm_call([UserMessage(content="hello")], json_schema=None)
            assert exc_info.value.retryable is True

    @pytest.mark.asyncio
    async def test_terminal_error_raises_model_error(self):
        model = ArkModelLink()

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.headers = {}
        auth_err = AuthenticationError("bad key", response=mock_response, body=None)

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=auth_err)

        with patch.object(
            ArkModelLink,
            "client",
            new_callable=lambda: property(lambda self: mock_client),
        ):
            with pytest.raises(ModelError) as exc_info:
                await model.make_llm_call([UserMessage(content="hello")], json_schema=None)
            assert exc_info.value.retryable is False


class TestGenerateResponse:
    @pytest.mark.asyncio
    async def test_generate_response_delegates_to_make_llm_call(self):
        model = ArkModelLink()

        with patch.object(ArkModelLink, "make_llm_call", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = "generated text"
            msgs = [UserMessage(content="hello")]
            result = await model.generate_response(msgs, json_schema=None)
            assert result == "generated text"
            mock_call.assert_called_once_with(msgs, json_schema=None)

    @pytest.mark.asyncio
    async def test_retries_transient_errors_then_raises(self):
        model = ArkModelLink()
        err = ModelError("timeout", retryable=True)

        with patch.object(ArkModelLink, "make_llm_call", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = err
            with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(ModelError):
                await model.generate_response([UserMessage(content="hi")], json_schema=None)
            assert mock_call.call_count == 3

    @pytest.mark.asyncio
    async def test_does_not_retry_terminal_errors(self):
        model = ArkModelLink()
        err = ModelError("auth failed", retryable=False)

        with patch.object(ArkModelLink, "make_llm_call", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = err
            with pytest.raises(ModelError):
                await model.generate_response([UserMessage(content="hi")], json_schema=None)
            assert mock_call.call_count == 1

    @pytest.mark.asyncio
    async def test_succeeds_on_retry_after_transient(self):
        model = ArkModelLink()
        err = ModelError("timeout", retryable=True)

        with patch.object(ArkModelLink, "make_llm_call", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = [err, "recovered response"]
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await model.generate_response([UserMessage(content="hi")], json_schema=None)
            assert result == "recovered response"
            assert mock_call.call_count == 2


class TestGenerateStream:
    @pytest.mark.asyncio
    async def test_generate_stream_yields_tokens(self):
        model = ArkModelLink()

        # Create mock streaming chunks
        chunk1 = MagicMock()
        chunk1.choices = [MagicMock()]
        chunk1.choices[0].delta.content = "Hello"

        chunk2 = MagicMock()
        chunk2.choices = [MagicMock()]
        chunk2.choices[0].delta.content = " world"

        async def mock_stream():
            for chunk in [chunk1, chunk2]:
                yield chunk

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_stream())

        with patch.object(
            ArkModelLink,
            "client",
            new_callable=lambda: property(lambda self: mock_client),
        ):
            tokens = []
            async for token in model.generate_stream([UserMessage(content="hi")]):
                tokens.append(token)
            assert tokens == ["Hello", " world"]

    @pytest.mark.asyncio
    async def test_generate_stream_skips_empty_chunks(self):
        model = ArkModelLink()

        chunk1 = MagicMock()
        chunk1.choices = [MagicMock()]
        chunk1.choices[0].delta.content = "data"

        empty_chunk = MagicMock()
        empty_chunk.choices = [MagicMock()]
        empty_chunk.choices[0].delta.content = None

        async def mock_stream():
            for chunk in [chunk1, empty_chunk]:
                yield chunk

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_stream())

        with patch.object(
            ArkModelLink,
            "client",
            new_callable=lambda: property(lambda self: mock_client),
        ):
            tokens = []
            async for token in model.generate_stream([UserMessage(content="hi")]):
                tokens.append(token)
            assert tokens == ["data"]

    @pytest.mark.asyncio
    async def test_generate_stream_error_raises(self):
        model = ArkModelLink()

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("stream failed"))

        with (
            patch.object(
                ArkModelLink,
                "client",
                new_callable=lambda: property(lambda self: mock_client),
            ),
            pytest.raises(Exception, match="stream failed"),
        ):
            async for _ in model.generate_stream([UserMessage(content="hi")]):
                pass
