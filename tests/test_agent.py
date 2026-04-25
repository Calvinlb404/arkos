"""Tests for agent_module/agent.py: Agent class."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_module.agent import MAX_ITER, Agent
from model_module.ArkModelNew import AIMessage, SystemMessage, UserMessage


@pytest.fixture
def mock_deps():
    """Create mock dependencies for Agent."""
    flow = MagicMock()
    memory = MagicMock()
    llm = MagicMock()
    tool_manager = MagicMock()

    # Setup flow initial state
    initial_state = MagicMock()
    initial_state.name = "agent_reply"
    initial_state.is_terminal = False
    flow.get_initial_state.return_value = initial_state

    return flow, memory, llm, tool_manager


@pytest.fixture
def agent(mock_deps):
    """Create an Agent with mock dependencies."""
    flow, memory, llm, tool_manager = mock_deps
    return Agent(
        agent_id="test_agent",
        flow=flow,
        memory=memory,
        llm=llm,
        tool_manager=tool_manager,
    )


class TestAgentInit:
    def test_init(self, agent, mock_deps):
        flow, memory, llm, tool_manager = mock_deps
        assert agent.agent_id == "test_agent"
        assert agent.flow is flow
        assert agent.memory is memory
        assert agent.llm is llm
        assert agent.tool_manager is tool_manager
        assert agent.system_prompt is None
        assert agent.available_tools == {}

    def test_initial_state_set(self, agent):
        assert agent.current_state.name == "agent_reply"


class TestFillToolArgsClass:
    def test_creates_pydantic_model(self, agent):
        result = agent.fill_tool_args_class("search", {"query": "test"})
        dumped = result.model_dump()
        assert dumped["tool_name"] == "search"
        assert dumped["tool_args"] == {"query": "test"}

    def test_empty_args(self, agent):
        result = agent.fill_tool_args_class("simple_tool", {})
        dumped = result.model_dump()
        assert dumped["tool_name"] == "simple_tool"
        assert dumped["tool_args"] == {}

    def test_complex_args(self, agent):
        args = {"start_date": "2024-01-01", "end_date": "2024-12-31", "count": 10}
        result = agent.fill_tool_args_class("calendar_events", args)
        dumped = result.model_dump()
        assert dumped["tool_args"] == args


class TestCreateNextStateClass:
    def test_creates_enum_model(self, agent):
        options = [("agent_reply", "AI responds"), ("tool_use", "Use a tool")]
        model_class = agent.create_next_state_class(options)

        # Should be a Pydantic model
        schema = model_class.model_json_schema()
        assert "properties" in schema
        assert "next_state" in schema["properties"]

    def test_single_option(self, agent):
        options = [("wait_for_user", "Wait for user")]
        model_class = agent.create_next_state_class(options)
        schema = model_class.model_json_schema()
        assert "next_state" in schema["properties"]

    def test_model_validates_valid_state(self, agent):
        options = [("state_a", "State A"), ("state_b", "State B")]
        model_class = agent.create_next_state_class(options)
        instance = model_class(next_state="state_a")
        assert instance.next_state.value == "state_a"

    def test_model_rejects_invalid_state(self, agent):
        options = [("state_a", "State A"), ("state_b", "State B")]
        model_class = agent.create_next_state_class(options)
        with pytest.raises(ValueError):
            model_class(next_state="state_c")


class TestCreateToolOptionClass:
    @pytest.mark.asyncio
    async def test_creates_tool_enum(self, agent):
        agent.tool_manager.list_all_tools = AsyncMock(
            return_value={
                "brave": {"search": {"name": "search"}, "news": {"name": "news"}},
                "calc": {"calculate": {"name": "calculate"}},
            }
        )

        model_class = await agent.create_tool_option_class()
        schema = model_class.model_json_schema()
        assert "tool_name" in schema["properties"]

    @pytest.mark.asyncio
    async def test_validates_tool_name(self, agent):
        agent.tool_manager.list_all_tools = AsyncMock(return_value={"server": {"my_tool": {"name": "my_tool"}}})

        model_class = await agent.create_tool_option_class()
        instance = model_class(tool_name="my_tool")
        assert instance.tool_name.value == "my_tool"


class TestCallLLM:
    @pytest.mark.asyncio
    async def test_returns_ai_message(self, agent):
        agent.llm.generate_response = AsyncMock(return_value="hello world")

        result = await agent.call_llm(context=[UserMessage(content="hi")], json_schema=None)
        assert isinstance(result, AIMessage)
        assert result.content == "hello world"

    @pytest.mark.asyncio
    async def test_passes_schema(self, agent):
        agent.llm.generate_response = AsyncMock(return_value='{"key": "value"}')
        schema = {"type": "json_schema", "json_schema": {}}

        await agent.call_llm(context=[], json_schema=schema)
        agent.llm.generate_response.assert_called_once_with([], schema)


class TestAddContext:
    @pytest.mark.asyncio
    async def test_adds_messages_to_memory(self, agent):
        agent.memory.add_memory = AsyncMock()
        msgs = [UserMessage(content="hello"), AIMessage(content="hi")]
        await agent.add_context(msgs)
        assert agent.memory.add_memory.call_count == 2

    @pytest.mark.asyncio
    async def test_empty_list(self, agent):
        agent.memory.add_memory = AsyncMock()
        await agent.add_context([])
        agent.memory.add_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_on_non_list(self, agent):
        with pytest.raises(AssertionError):
            await agent.add_context("not a list")


class TestGetContext:
    @pytest.mark.asyncio
    async def test_short_term_only(self, agent):
        agent.memory.retrieve_short_memory = AsyncMock(return_value=[UserMessage(content="msg1")])
        agent.memory.retrieve_long_memory = AsyncMock()

        result = await agent.get_context(turns=5, include_long_term=False)
        assert len(result) == 1
        agent.memory.retrieve_long_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_with_long_term(self, agent):
        agent.memory.retrieve_short_memory = AsyncMock(return_value=[UserMessage(content="hi")])
        agent.memory.retrieve_long_memory = AsyncMock(
            return_value=SystemMessage(content="remembered: user likes blue")
        )

        result = await agent.get_context(turns=5, include_long_term=True)
        assert len(result) == 2
        assert isinstance(result[0], SystemMessage)

    @pytest.mark.asyncio
    async def test_empty_long_term_excluded(self, agent):
        agent.memory.retrieve_short_memory = AsyncMock(return_value=[UserMessage(content="hi")])
        agent.memory.retrieve_long_memory = AsyncMock(return_value=SystemMessage(content=""))

        result = await agent.get_context(turns=5, include_long_term=True)
        assert len(result) == 1  # Long-term excluded because empty


class TestChooseTransition:
    @pytest.mark.asyncio
    async def test_chooses_next_state(self, agent):
        agent.llm.generate_response = AsyncMock(return_value=json.dumps({"next_state": "tool_use"}))

        transitions = {
            "tt": ["tool_use", "wait_for_user"],
            "td": ["Use tools", "Wait for input"],
        }
        result = await agent.choose_transition(transitions, [UserMessage(content="search for something")])
        assert result == "tool_use"


class TestMaxIter:
    def test_max_iter_constant(self):
        assert MAX_ITER == 10
