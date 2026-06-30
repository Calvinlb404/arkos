"""Tests for state module: State, StateUser, StateAI, StateTool, state_registry, StateHandler."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from model_module.ArkModelNew import AIMessage, SystemMessage
from model_module.errors import OutputValidationError
from state_module.agent_buddy.state_ai import ReasonedOutput, StateAI
from state_module.agent_buddy.state_tool import StateTool
from state_module.agent_buddy.state_user import StateUser
from state_module.agent_executor.routers import use_tool_router
from state_module.agent_executor.state_tool import StateExecutorTool
from state_module.core.base_state import StateOutput
from state_module.core.state import State
from state_module.core.state_registry import STATE_REGISTRY, register_state

# --- State base class ---


class TestState:
    def test_init(self):
        config = {"transition": {"next": ["state_b"]}}
        state = State("test_state", config)
        assert state.name == "test_state"
        assert state.is_terminal is False
        assert state.transition == {"next": ["state_b"]}

    def test_init_empty_transition(self):
        state = State("s", {})
        assert state.transition == {}

    def test_check_transition_ready_raises(self):
        state = State("s", {})
        with pytest.raises(NotImplementedError):
            state.check_transition_ready({})

    def test_run_raises(self):
        state = State("s", {})
        with pytest.raises(NotImplementedError):
            state.run({})


# --- StateUser ---


class TestStateUser:
    def test_init_is_terminal(self):
        su = StateUser("wait_for_user", {"transition": {}})
        assert su.is_terminal is True
        assert su.name == "wait_for_user"

    def test_type_attribute(self):
        assert StateUser.type == "user"

    def test_check_transition_ready_always_true(self):
        su = StateUser("u", {})
        assert su.check_transition_ready({}) is True
        assert su.check_transition_ready({"anything": "here"}) is True

    @pytest.mark.asyncio
    async def test_run_returns_none(self):
        su = StateUser("u", {})
        result = await su.run({})
        assert result is not None
        assert result.completion_signal == "needs_input"
        assert result.content == ""


# --- ReasonedOutput ---


class TestReasonedOutput:
    def test_valid_output(self):
        data = ReasonedOutput(
            intent="answer question",
            approach=["think", "respond"],
            route="reply",
            final="The answer is 42.",
        )
        assert data.intent == "answer question"
        assert len(data.approach) == 2
        assert data.final == "The answer is 42."
        # _Route is a StrEnum so the enum and the string are equal
        assert data.route == "reply"

    def test_route_ask_for_clarification(self):
        data = ReasonedOutput(
            intent="unclear request",
            approach=["analyze"],
            route="ask",
            final="What do you mean?",
        )
        assert data.route == "ask"
        assert data.final == "What do you mean?"

    def test_route_plan_for_action(self):
        data = ReasonedOutput(
            intent="schedule meeting",
            approach=["call calendar tool"],
            route="plan",
            final="On it.",
        )
        assert data.route == "plan"

    def test_invalid_route_rejected(self):
        with pytest.raises(ValueError):
            ReasonedOutput(intent="x", route="bogus", final="y")

    def test_json_schema_generation(self):
        schema = ReasonedOutput.model_json_schema()
        assert "properties" in schema
        assert "intent" in schema["properties"]
        assert "final" in schema["properties"]
        assert "route" in schema["properties"]


# --- StateAI ---


class TestStateAI:
    def test_init(self):
        sa = StateAI("reasoning", {"transition": {"next": ["user"]}})
        assert sa.name == "reasoning"
        assert sa.is_terminal is False

    def test_type_attribute(self):
        assert StateAI.type == "agent"

    def test_check_transition_ready(self):
        sa = StateAI("r", {})
        assert sa.check_transition_ready({}) is True

    @pytest.mark.asyncio
    async def test_run_with_valid_json(self):
        sa = StateAI("reasoning", {})
        reasoned = ReasonedOutput(
            intent="help user",
            approach=["step 1", "step 2"],
            route="reply",
            final="Here is your answer.",
        )
        mock_agent = MagicMock()
        mock_agent.system_prompt = ""
        mock_agent.call_llm = AsyncMock(return_value=AIMessage(content=reasoned.model_dump_json()))

        result = await sa.run(
            [SystemMessage(content="system"), AIMessage(content="hi")],
            mock_agent,
        )

        assert isinstance(result, StateOutput)
        # The new state_ai surfaces ONLY `final` to the user. Approach steps
        # stay internal (chain-of-thought hiding).
        assert result.content == "Here is your answer."
        assert "step 1" not in result.content
        # Router pattern: state emits a route signal, not a next_state name.
        assert result.structured_data["route"] == "reply"

    @pytest.mark.asyncio
    async def test_run_with_invalid_json_raises_for_rerun(self):
        sa = StateAI("reasoning", {})
        mock_agent = MagicMock()
        mock_agent.system_prompt = ""
        mock_agent.call_llm = AsyncMock(return_value=AIMessage(content="not valid json at all"))

        # Raises so _run_state can rerun with the error fed back; raw content
        # is quarantined in .raw and never surfaces to the user.
        with pytest.raises(OutputValidationError) as exc_info:
            await sa.run([], mock_agent)
        assert exc_info.value.raw == "not valid json at all"
        assert exc_info.value.detail != "not valid json at all"

    @pytest.mark.asyncio
    async def test_run_with_none_content_raises_for_rerun(self):
        sa = StateAI("reasoning", {})
        mock_agent = MagicMock()
        mock_agent.system_prompt = ""
        mock_agent.call_llm = AsyncMock(return_value=AIMessage(content=None))

        with pytest.raises(OutputValidationError):
            await sa.run([], mock_agent)

    @pytest.mark.asyncio
    async def test_run_routes_plan_to_workshop(self):
        sa = StateAI("reasoning", {})
        reasoned = ReasonedOutput(
            intent="schedule something",
            approach=["use calendar tool"],
            route="plan",
            final="Putting together a plan.",
        )
        mock_agent = MagicMock()
        mock_agent.system_prompt = ""
        mock_agent.call_llm = AsyncMock(return_value=AIMessage(content=reasoned.model_dump_json()))

        result = await sa.run([], mock_agent)
        # Router pattern: plan route emits signal "plan"; buddy/routers.py maps it to workshop_plan.
        assert result.structured_data["route"] == "plan"
        assert result.completion_signal == "complete"

    @pytest.mark.asyncio
    async def test_run_with_clarification(self):
        sa = StateAI("reasoning", {})
        reasoned = ReasonedOutput(
            intent="unclear",
            approach=["analyze"],
            route="ask",
            final="Could you clarify?",
        )
        mock_agent = MagicMock()
        mock_agent.system_prompt = ""
        mock_agent.call_llm = AsyncMock(return_value=AIMessage(content=reasoned.model_dump_json()))

        result = await sa.run([], mock_agent)
        assert "Could you clarify?" in result.content
        # Router pattern: ask route emits signal "ask".
        assert result.structured_data["route"] == "ask"
        assert result.completion_signal == "needs_input"


# --- StateTool ---


class TestStateTool:
    def test_init(self):
        st = StateTool("tool_use", {})
        assert st.is_terminal is False
        assert st.name == "tool_use"

    def test_type_attribute(self):
        assert StateTool.type == "tool"

    def test_check_transition_ready(self):
        st = StateTool("t", {})
        assert st.check_transition_ready({}) is True

    @pytest.mark.asyncio
    async def test_choose_tool(self):
        st = StateTool("t", {})

        mock_tool_class = MagicMock()
        mock_tool_class.model_json_schema.return_value = {
            "type": "object",
            "properties": {"tool_name": {"type": "string"}},
        }
        # parse_structured calls model_validate_json on the class; set up a
        # return value whose .tool_name.value resolves to the expected string.
        parsed_mock = MagicMock()
        parsed_mock.tool_name.value = "search"
        mock_tool_class.model_validate_json.return_value = parsed_mock

        mock_agent = MagicMock()
        mock_agent.create_tool_option_class = AsyncMock(return_value=mock_tool_class)
        mock_agent.call_llm = AsyncMock(
            side_effect=[
                AIMessage(content=json.dumps({"tool_name": "search"})),
                AIMessage(content=json.dumps({"query": "test"})),
            ]
        )
        mock_agent.current_user_id = "u1"
        mock_agent.tool_manager = MagicMock()
        # Tool resolution is now user-scoped via _resolve_server (shared + per-user).
        mock_agent.tool_manager._resolve_server = MagicMock(return_value="brave-search")
        mock_agent.tool_manager.list_all_tools = AsyncMock(
            return_value={
                "brave-search": {
                    "search": {
                        "name": "search",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    }
                }
            }
        )

        result = await st._choose_tool([SystemMessage(content="find something")], mock_agent)
        assert result["tool_name"] == "search"
        assert result["tool_args"] == {"query": "test"}

    @pytest.mark.asyncio
    async def test_execute_tool(self):
        st = StateTool("t", {})
        mock_agent = MagicMock()
        mock_agent.current_user_id = "user1"
        mock_agent.tool_manager = MagicMock()
        mock_agent.tool_manager.call_tool = AsyncMock(return_value={"result": "ok"})

        result = await st._execute_tool({"tool_name": "search", "tool_args": {"q": "test"}}, mock_agent)
        assert result == {"result": "ok"}
        mock_agent.tool_manager.call_tool.assert_called_once_with(
            tool_name="search", arguments={"q": "test"}, user_id="user1"
        )

    @pytest.mark.asyncio
    async def test_run_returns_tool_output(self):
        st = StateTool("t", {})
        mock_agent = MagicMock()
        mock_agent.current_user_id = "user1"
        mock_agent.render_tool_result.return_value = "42"

        with (
            patch.object(st, "_choose_tool", new_callable=AsyncMock) as mock_choose,
            patch.object(st, "_execute_tool", new_callable=AsyncMock) as mock_exec,
        ):
            mock_choose.return_value = {"tool_name": "calc", "tool_args": {"x": 1}}
            mock_exec.return_value = "42"

            result = await st.run([], mock_agent)
            assert isinstance(result, StateOutput)
            assert "42" in result.content
            assert result.completion_signal == "complete"

    @pytest.mark.asyncio
    async def test_run_no_dead_stash_in_structured_data(self):
        """tool_result must not appear in structured_data; nothing reads it."""
        st = StateTool("t", {})
        mock_agent = MagicMock()
        mock_agent.current_user_id = "user1"
        mock_agent.render_tool_result.return_value = "rendered"

        with (
            patch.object(st, "_choose_tool", new_callable=AsyncMock) as mock_choose,
            patch.object(st, "_execute_tool", new_callable=AsyncMock) as mock_exec,
        ):
            mock_choose.return_value = {"tool_name": "list_events", "tool_args": {}}
            mock_exec.return_value = "x" * 5000

            result = await st.run([], mock_agent)
            assert "tool_result" not in result.structured_data


# --- state_registry ---


class TestStateRegistry:
    def test_register_state_adds_to_registry(self):
        # StateUser and StateAI already registered on import
        assert "user" in STATE_REGISTRY
        assert "agent" in STATE_REGISTRY
        assert "tool" in STATE_REGISTRY

    def test_register_state_returns_class(self):
        @register_state
        class DummyState(State):
            type = "dummy_test"

            def check_transition_ready(self, ctx):
                return True

            def run(self, ctx):
                return None

        assert STATE_REGISTRY["dummy_test"] is DummyState
        # Cleanup
        del STATE_REGISTRY["dummy_test"]

    def test_register_state_missing_type_raises(self):
        with pytest.raises((ValueError, AttributeError)):

            @register_state
            class BadState:
                pass


# --- executor StateExecutorTool ---


class TestStateExecutorTool:
    @pytest.mark.asyncio
    async def test_tool_error_asks_human(self):
        """A non-auth tool failure routes to ask_human instead of killing the task."""
        st = StateExecutorTool("use_tool", {})
        mock_agent = MagicMock()
        mock_agent.task_id = None
        mock_agent.current_user_id = "u1"
        mock_agent.step_idx = 0
        mock_agent.pending_tool = {"tool_name": "search", "tool_args": {}}
        mock_agent.tool_manager.call_tool = AsyncMock(side_effect=RuntimeError("network down"))

        result = await st.run([], mock_agent)

        assert result.completion_signal == "error"
        assert result.structured_data["route"] == "ask"
        assert mock_agent.pending_ask["kind"] == "text"
        assert "search" in mock_agent.pending_ask["prompt"]
        # The failed step is not advanced past; the human decides next.
        assert mock_agent.step_idx == 0


# --- executor use_tool_router ---


class TestUseToolRouter:
    def test_ask_signal_routes_to_ask_human(self):
        out = StateOutput(
            content="boom",
            completion_signal="error",
            structured_data={"route": "ask"},
        )
        assert use_tool_router(out) == "ask_human"

    def test_other_signals_route_to_executor(self):
        out = StateOutput(
            content="ok",
            completion_signal="complete",
            structured_data={"route": "continue"},
        )
        assert use_tool_router(out) == "executor"


# --- StateHandler ---


class TestStateHandler:
    @pytest.fixture
    def state_graph_yaml(self, tmp_path):
        graph = {
            "initial": "agent_reply",
            "states": {
                "agent_reply": {
                    "type": "agent",
                    "transition": {"next": ["wait_for_user"]},
                },
                "wait_for_user": {
                    "type": "user",
                    "transition": {"next": ["agent_reply"]},
                },
            },
        }
        f = tmp_path / "graph.yaml"
        f.write_text(yaml.dump(graph))
        return str(f)

    _BUDDY_PKG = "state_module.agent_buddy"

    def test_init_loads_states(self, state_graph_yaml):
        from state_module.core.state_handler import StateHandler

        handler = StateHandler(state_graph_yaml, agent_pkg=self._BUDDY_PKG)
        assert "agent_reply" in handler.states
        assert "wait_for_user" in handler.states

    def test_get_initial_state(self, state_graph_yaml):
        from state_module.core.state_handler import StateHandler

        handler = StateHandler(state_graph_yaml, agent_pkg=self._BUDDY_PKG)
        initial = handler.get_initial_state()
        assert initial.name == "agent_reply"
        assert isinstance(initial, StateAI)

    def test_get_state(self, state_graph_yaml):
        from state_module.core.state_handler import StateHandler

        handler = StateHandler(state_graph_yaml, agent_pkg=self._BUDDY_PKG)
        user_state = handler.get_state("wait_for_user")
        assert isinstance(user_state, StateUser)
        assert user_state.is_terminal is True

    def test_unknown_state_type_raises(self, tmp_path):
        from state_module.core.state_handler import StateHandler

        graph = {
            "initial": "bad",
            "states": {"bad": {"type": "nonexistent_type"}},
        }
        f = tmp_path / "bad_graph.yaml"
        f.write_text(yaml.dump(graph))
        with pytest.raises(ValueError, match="Unknown state type"):
            StateHandler(str(f), agent_pkg=self._BUDDY_PKG)
