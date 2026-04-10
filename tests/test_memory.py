"""Tests for memory_module/memory.py: serialize, deserialize, and ROLE/CLASS mappings."""

from unittest.mock import MagicMock, patch

import pytest

from memory_module.memory import CLASS_TO_ROLE, ROLE_TO_CLASS
from model_module.ArkModelNew import (
    AIMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)

# --- Role/Class Mappings ---


class TestRoleMappings:
    def test_role_to_class_user(self):
        assert ROLE_TO_CLASS["user"] is UserMessage

    def test_role_to_class_assistant(self):
        assert ROLE_TO_CLASS["assistant"] is AIMessage

    def test_role_to_class_system(self):
        assert ROLE_TO_CLASS["system"] is SystemMessage

    def test_role_to_class_tool(self):
        assert ROLE_TO_CLASS["tool"] is ToolMessage

    def test_class_to_role_user(self):
        assert CLASS_TO_ROLE[UserMessage] == "user"

    def test_class_to_role_ai(self):
        assert CLASS_TO_ROLE[AIMessage] == "assistant"

    def test_class_to_role_system(self):
        assert CLASS_TO_ROLE[SystemMessage] == "system"

    def test_class_to_role_tool(self):
        assert CLASS_TO_ROLE[ToolMessage] == "tool"

    def test_mappings_are_inverses(self):
        for role, cls in ROLE_TO_CLASS.items():
            assert CLASS_TO_ROLE[cls] == role


# --- Serialize / Deserialize (standalone, no DB needed) ---


class TestSerializeDeserialize:
    """Test serialize/deserialize by calling the methods directly without a Memory instance."""

    def _serialize(self, message):
        """Call serialize logic directly (same as Memory.serialize)."""
        return message.model_dump_json()

    def _deserialize(self, message_str, role):
        """Call deserialize logic directly (same as Memory.deserialize)."""
        cls = ROLE_TO_CLASS.get(role)
        if cls is None:
            raise ValueError(f"Unknown role: {role}")
        return cls.model_validate_json(message_str)

    def test_serialize_user_message(self):
        msg = UserMessage(content="hello")
        json_str = self._serialize(msg)
        assert '"content":"hello"' in json_str or '"content": "hello"' in json_str

    def test_serialize_ai_message(self):
        msg = AIMessage(content="response")
        json_str = self._serialize(msg)
        assert "response" in json_str

    def test_serialize_system_message(self):
        msg = SystemMessage(content="be helpful")
        json_str = self._serialize(msg)
        assert "be helpful" in json_str

    def test_serialize_tool_message(self):
        msg = ToolMessage(content="tool result")
        json_str = self._serialize(msg)
        assert "tool result" in json_str

    def test_deserialize_user_message(self):
        msg = UserMessage(content="test")
        json_str = self._serialize(msg)
        restored = self._deserialize(json_str, "user")
        assert isinstance(restored, UserMessage)
        assert restored.content == "test"
        assert restored.role == "user"

    def test_deserialize_ai_message(self):
        msg = AIMessage(content="ai response")
        json_str = self._serialize(msg)
        restored = self._deserialize(json_str, "assistant")
        assert isinstance(restored, AIMessage)
        assert restored.content == "ai response"

    def test_deserialize_system_message(self):
        msg = SystemMessage(content="system msg")
        json_str = self._serialize(msg)
        restored = self._deserialize(json_str, "system")
        assert isinstance(restored, SystemMessage)

    def test_deserialize_tool_message(self):
        msg = ToolMessage(content="result", tool_calls={"name": "calc"})
        json_str = self._serialize(msg)
        restored = self._deserialize(json_str, "tool")
        assert isinstance(restored, ToolMessage)
        assert restored.tool_calls == {"name": "calc"}

    def test_deserialize_unknown_role_raises(self):
        with pytest.raises(ValueError, match="Unknown role"):
            self._deserialize('{"content":"x","role":"unknown"}', "unknown")

    def test_roundtrip_all_message_types(self):
        messages = [
            UserMessage(content="hello"),
            AIMessage(content="hi there"),
            SystemMessage(content="be concise"),
            ToolMessage(content="42", tool_calls={"name": "calc"}),
        ]
        roles = ["user", "assistant", "system", "tool"]

        for msg, role in zip(messages, roles, strict=False):
            json_str = self._serialize(msg)
            restored = self._deserialize(json_str, role)
            assert restored.content == msg.content
            assert restored.role == msg.role


# --- Memory class with mocked DB ---


class TestMemoryWithMockedDB:
    @pytest.fixture
    def memory_instance(self):
        """Create a Memory instance with mocked database pool and mem0."""
        with (
            patch("memory_module.memory._get_pool") as mock_pool,
            patch("memory_module.memory.Mem0Memory"),
        ):
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.cursor.return_value = mock_cursor
            mock_pool_instance = MagicMock()
            mock_pool_instance.getconn.return_value = mock_conn
            mock_pool.return_value = mock_pool_instance

            from memory_module.memory import Memory

            mem = Memory(
                user_id="test_user",
                session_id="test_session",
                db_url="postgresql://fake",
                use_long_term=False,
            )
            mem._pool = mock_pool_instance
            yield mem, mock_conn, mock_cursor

    def test_init(self, memory_instance):
        mem, _, _ = memory_instance
        assert mem.user_id == "test_user"
        assert mem.session_id == "test_session"

    def test_start_new_session(self, memory_instance):
        mem, _, _ = memory_instance
        old_session = mem.session_id
        new_session = mem.start_new_session()
        assert new_session != old_session
        assert mem.session_id == new_session

    def test_serialize(self, memory_instance):
        mem, _, _ = memory_instance
        msg = UserMessage(content="test")
        result = mem.serialize(msg)
        assert isinstance(result, str)
        assert "test" in result

    def test_deserialize(self, memory_instance):
        mem, _, _ = memory_instance
        msg = UserMessage(content="hello")
        json_str = mem.serialize(msg)
        restored = mem.deserialize(json_str, "user")
        assert isinstance(restored, UserMessage)
        assert restored.content == "hello"

    def test_add_memory_inserts_to_db(self, memory_instance):
        mem, mock_conn, mock_cursor = memory_instance
        msg = UserMessage(content="test message")
        result = mem.add_memory(msg)
        assert result is True
        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    def test_retrieve_short_memory(self, memory_instance):
        mem, mock_conn, mock_cursor = memory_instance
        # Simulate DB returning rows
        user_msg = UserMessage(content="hi")
        ai_msg = AIMessage(content="hello")
        mock_cursor.fetchall.return_value = [
            ("user", user_msg.model_dump_json()),
            ("assistant", ai_msg.model_dump_json()),
        ]

        result = mem.retrieve_short_memory(turns=5)
        assert len(result) == 2
        assert isinstance(result[0], UserMessage)
        assert isinstance(result[1], AIMessage)

    def test_retrieve_long_memory_disabled(self, memory_instance):
        mem, _, _ = memory_instance
        # use_long_term is False
        result = mem.retrieve_long_memory()
        assert isinstance(result, SystemMessage)
        assert result.content == ""
