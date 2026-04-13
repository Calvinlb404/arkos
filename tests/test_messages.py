"""Tests for Message Pydantic models in model_module/ArkModelNew.py."""

from model_module.ArkModelNew import (
    AIMessage,
    Message,
    SystemMessage,
    ToolMessage,
    UserMessage,
)


class TestMessage:
    def test_create_message(self):
        msg = Message(content="hello", role="user")
        assert msg.content == "hello"
        assert msg.role == "user"

    def test_message_serialization(self):
        msg = Message(content="test", role="system")
        data = msg.model_dump()
        assert data == {"content": "test", "role": "system"}

    def test_message_json_roundtrip(self):
        msg = Message(content="test", role="user")
        json_str = msg.model_dump_json()
        restored = Message.model_validate_json(json_str)
        assert restored.content == msg.content
        assert restored.role == msg.role


class TestSystemMessage:
    def test_default_role(self):
        msg = SystemMessage(content="you are helpful")
        assert msg.role == "system"

    def test_override_role_ignored(self):
        msg = SystemMessage(content="test", role="system")
        assert msg.role == "system"

    def test_serialization(self):
        msg = SystemMessage(content="be concise")
        data = msg.model_dump()
        assert data["role"] == "system"
        assert data["content"] == "be concise"


class TestUserMessage:
    def test_default_role(self):
        msg = UserMessage(content="what is 2+2?")
        assert msg.role == "user"

    def test_serialization(self):
        msg = UserMessage(content="hello")
        data = msg.model_dump()
        assert data["role"] == "user"
        assert data["content"] == "hello"

    def test_json_roundtrip(self):
        msg = UserMessage(content="test message")
        json_str = msg.model_dump_json()
        restored = UserMessage.model_validate_json(json_str)
        assert restored.content == "test message"
        assert restored.role == "user"


class TestToolMessage:
    def test_default_role(self):
        msg = ToolMessage(content="tool result")
        assert msg.role == "tool"

    def test_tool_calls_none_by_default(self):
        msg = ToolMessage(content="result")
        assert msg.tool_calls is None

    def test_tool_calls_with_data(self):
        calls = {"name": "search", "args": {"q": "test"}}
        msg = ToolMessage(content="result", tool_calls=calls)
        assert msg.tool_calls == calls

    def test_serialization_with_tool_calls(self):
        calls = {"name": "calc", "args": {"x": 1}}
        msg = ToolMessage(content="42", tool_calls=calls)
        data = msg.model_dump()
        assert data["role"] == "tool"
        assert data["tool_calls"] == calls


class TestAIMessage:
    def test_default_role(self):
        msg = AIMessage(content="I can help with that")
        assert msg.role == "assistant"

    def test_content_optional(self):
        msg = AIMessage()
        assert msg.content is None
        assert msg.role == "assistant"

    def test_tool_calls_none_by_default(self):
        msg = AIMessage(content="hello")
        assert msg.tool_calls is None

    def test_tool_calls_with_data(self):
        calls = {"name": "search", "args": {}}
        msg = AIMessage(content=None, tool_calls=calls)
        assert msg.tool_calls == calls
        assert msg.content is None

    def test_serialization(self):
        msg = AIMessage(content="response text")
        data = msg.model_dump()
        assert data["role"] == "assistant"
        assert data["content"] == "response text"

    def test_json_roundtrip(self):
        msg = AIMessage(content="hello", tool_calls={"name": "test"})
        json_str = msg.model_dump_json()
        restored = AIMessage.model_validate_json(json_str)
        assert restored.content == "hello"
        assert restored.tool_calls == {"name": "test"}


class TestMessageInheritance:
    def test_all_subclass_message(self):
        assert issubclass(SystemMessage, Message)
        assert issubclass(UserMessage, Message)
        assert issubclass(ToolMessage, Message)
        assert issubclass(AIMessage, Message)

    def test_isinstance_checks(self):
        msgs = [
            SystemMessage(content="sys"),
            UserMessage(content="usr"),
            ToolMessage(content="tool"),
            AIMessage(content="ai"),
        ]
        for m in msgs:
            assert isinstance(m, Message)
