# agent.py

import logging
import os
import sys
import time
from collections.abc import Callable
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ValidationError, create_model

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from memory_module.memory import Memory
from model_module.errors import ModelError
from state_module.core.base_state import TerminalReason

# Assuming ArkModelLink.generate_response is actually ArkModelLink.agenerate_response
from model_module.ArkModelNew import AIMessage, ArkModelLink, SystemMessage
from state_module.core.base_state import StateOutput
from state_module.core.state_handler import StateHandler

logger = logging.getLogger(__name__)

MAX_ITER = 10


def parse_structured(content: str | None, model_class: type[BaseModel]) -> BaseModel | None:
    """
    Parse a JSON string from the model into a Pydantic model.

    Returns None on any parse or validation failure so callers can convert
    the failure to a typed outcome rather than letting an exception escape.
    """
    if not content:
        return None
    try:
        return model_class.model_validate_json(content)
    except (ValidationError, ValueError):
        logger.warning("structured output parse failed for %s", model_class.__name__)
        return None


class Agent:
    """Default agent class that orchestrates state transitions, LLM calls, and tool usage."""

    def __init__(
        self,
        agent_id: str,
        flow: StateHandler,
        memory: Memory,
        llm: ArkModelLink,
        tool_manager=None,
    ):
        """Initialize the agent with a state graph, memory backend, LLM, and optional tool manager."""
        self.agent_id = agent_id
        self.flow = flow
        self.memory = memory
        self.llm = llm
        self.tool_manager = tool_manager
        self.current_state = self.flow.get_initial_state()
        self.system_prompt = None

        self.startup_flag = True
        self.tools = []
        self.tool_names = []
        self.available_tools = {}
        self.current_user_id = None  # Set per-request for per-user tool auth
        self.last_state_output: StateOutput | None = None
        # Subagents override these. The executor graph uses them to iterate plan steps.
        self.task_id: str | None = None
        self.plan_steps: list[str] = []
        self.step_idx: int = 0
        self.max_iter: int = MAX_ITER
        self.terminal_reason: TerminalReason | None = None

    # def bind_tool(self, tool):
    #
    #    self.tool.append(tool)

    # def find_downloaded_tool(self, embedding):
    #    tool = Tool.pull_tool_from_registry(embedding)
    #    tool_name = tool.tool
    #    self.bind_tool(tool)
    #    self.tool_names.append(tool_name)

    def fill_tool_args_class(self, tool_name: str, tool_args: dict[str, Any]):
        """
        Returns a Pydantic object whose .model_dump() is:
          {"tool_name": <tool_name>, "tool_args": <tool_args>}
        """

        ToolCall = create_model(
            "ToolCall",
            tool_name=(str, Field(description="Tool name to execute")),
            tool_args=(
                dict[str, Any],
                Field(default_factory=dict, description="Tool args"),
            ),
        )

        return ToolCall(tool_name=tool_name, tool_args=tool_args)

    async def create_tool_option_class(self):
        """
        Returns a Pydantic model class with a single field 'tool_name',
        whose value must be one of the available tool IDs.
        """

        server_tool_map = await self.tool_manager.list_all_tools()

        enum_members = {}
        for server_name in server_tool_map:
            for tool_name in server_tool_map[server_name]:
                enum_members[tool_name] = tool_name

        ToolEnum = Enum("ToolEnum", enum_members)

        ToolOptionsModel = create_model(
            "ToolCall",
            tool_name=(
                ToolEnum,
                Field(description="The name of the tool to execute next"),
            ),
        )

        return ToolOptionsModel

    def create_next_state_class(self, options: list[tuple[str, str]]):
        """
        options: list of tuples (next_state, description of state)
        Returns a Pydantic model class with a single field 'next_state',
        whose value must be one of the provided state names.
        """

        # Dynamically build an Enum of allowed states
        enum_dict = {state: state for state, desc in options}

        # add desc into enum dict
        next_state_enum = Enum("NextStateEnum", enum_dict)

        # Build the model with a single constrained field
        next_state_model = create_model(
            "NextState",
            next_state=(
                next_state_enum,
                Field(..., description="The chosen next state"),
            ),
        )

        return next_state_model

    async def call_llm(self, context=None, json_schema=None):
        """
        Agent's interface with chat model
        input: messages (list), json_schema (json)

        output: AI Message
        """

        chat_model = self.llm

        llm_response = await chat_model.generate_response(context, json_schema)

        # else:
        #    messages = [SystemMessage(content=input)]
        #    llm_response = chat_model.generate_response(messages, json_schema)

        return AIMessage(content=llm_response)

    async def choose_transition(self, transitions_dict, messages):
        """
        Chooses subsequent transition in state graph
        """

        transition_tuples = list(zip(transitions_dict["tt"], transitions_dict["td"], strict=False))
        prompt = (
            f"given the context of the conversation and the following state options "
            f"{transition_tuples} output the most reasonable next state. "
            f"do not use tool result to determine the next state"
        )

        # creates pydantic class and a model dump
        NextStates = self.create_next_state_class(transition_tuples)
        json_schema = {
            "type": "json_schema",
            "json_schema": {
                "name": "class_options",
                "schema": NextStates.model_json_schema(),
            },
        }

        context_text = [SystemMessage(content=prompt)] + messages

        output = await self.call_llm(context=context_text, json_schema=json_schema)

        parsed = parse_structured(output.content, NextStates)
        if parsed is None:
            # Fall back to the first listed transition rather than crashing.
            logger.warning("choose_transition: could not parse model output, using first transition")
            return transitions_dict["tt"][0]

        return parsed.next_state.value

    async def _run_state(self, context: list) -> tuple[StateOutput | None, str | None]:
        """
        Run the current state once and classify the result.

        Returns (output, retry_signal) where:
        - output is a StateOutput on success or a terminal error.
        - retry_signal is "retry" when a transient ModelError should cause
          the loop to re-run the same state (bounded by max_iter).
        - (error_output, None) when the failure is permanent and should be
          routed to agent_reply with completion_signal="error".
        """
        try:
            return await self.current_state.run(context, self), None
        except ModelError as e:
            if e.retryable:
                logger.warning("transient model error in state %s: %s", self.current_state.name, e)
                return None, "retry"
            logger.error("terminal model error in state %s: %s", self.current_state.name, e)
            return StateOutput(
                content="I could not reach the model. Please try again.",
                completion_signal="error",
                error_detail=str(e),
                structured_data={"route": "ask"},
            ), None
        except Exception as e:
            logger.error("state %s raised unexpected error: %s", self.current_state.name, e)
            return StateOutput(
                content="An internal error occurred.",
                completion_signal="error",
                error_detail=str(e),
                structured_data={"route": "ask"},
            ), None

    def render_tool_result(self, tool_result: Any) -> str:
        """
        Convert a tool result into a string safe to place in the context window.

        This is a placeholder; Task 4 + 7 (context-aware budgeting) will replace
        the body with a structure-aware head+tail view sized to remaining context.
        Until then, the full stringified result is used -- same as the previous
        unbounded behaviour, but now routed through one place so the upgrade is
        a single edit.
        """
        return str(tool_result)

    async def add_context(self, messages):
        """
        processes incoming messages for memory module
        """

        assert isinstance(messages, list), "agent.py messages not a list"

        for message in messages:
            await self.memory.add_memory(message)

        return None

    async def get_context(self, turns=5, include_long_term=True):
        """
        Wrap long term and short term into context window.
        output: list of messages
        """
        short_term_mem = await self.memory.retrieve_short_memory(turns)

        if include_long_term:
            long_term_mem = await self.memory.retrieve_long_memory(context=short_term_mem)
            # Only include if it has content
            if long_term_mem and long_term_mem.content.strip():
                return [long_term_mem] + short_term_mem

        return short_term_mem

    async def step(self, messages, user_id: str = None):
        """
        Runs the agent until reaching a terminal state or completion.
        Returns the last AIMessage produced.

        Parameters
        ----------
        messages : list
            List of messages to process
        user_id : str, optional
            User ID for per-user tool authentication
        """
        step_start = time.time()

        # Set current user for per-user tool auth
        self.current_user_id = user_id

        t0 = time.time()
        await self.add_context(messages)
        print(f"[TIMING] add_context: {time.time() - t0:.3f}s")

        print("agent.py received message")

        self.last_state_output = None
        self.terminal_reason = None
        retry_count = 0

        logger.debug("step start: state=%s", self.current_state.name)

        while True:
            if retry_count > self.max_iter:
                logger.warning("max iterations (%d) reached", self.max_iter)
                self.terminal_reason = TerminalReason.max_steps
                break
            retry_count += 1

            context = await self.get_context()
            update, retry_signal = await self._run_state(context)

            if retry_signal == "retry":
                continue

            if update:
                assert isinstance(update, StateOutput), "State output was not a StateOutput instance"
                self.last_state_output = update
                if update.content:
                    await self.add_context([AIMessage(content=update.content)])

            if update and update.completion_signal == "error" and not self.current_state.is_terminal:
                # Route errors back to the reply state rather than crashing.
                self.current_state = self.flow.get_state("agent_reply")
                self.terminal_reason = TerminalReason.model_error
                break

            if self.current_state.is_terminal:
                self.terminal_reason = TerminalReason.completed
                break

            messages_list = await self.memory.retrieve_short_memory(5)
            if self.current_state.check_transition_ready(messages_list):
                transition_dict = self.flow.get_transitions(self.current_state, messages_list)
                transition_names = transition_dict["tt"]

                router = self.flow.get_router(self.current_state)
                if router and update:
                    # State has a registered router: resolve next state from
                    # the route signal in StateOutput.structured_data. No LLM call.
                    next_state_name = router(update)
                elif len(transition_names) == 1:
                    next_state_name = transition_names[0]
                else:
                    next_state_name = await self.choose_transition(transition_dict, messages_list)

                self.current_state = self.flow.get_state(next_state_name)
                logger.debug("transition -> %s", self.current_state.name)

            else:
                self.terminal_reason = TerminalReason.needs_input
                break

        logger.debug(
            "step done: reason=%s elapsed=%.3fs",
            self.terminal_reason,
            time.time() - step_start,
        )
        self.current_state = self.flow.get_initial_state()
        return self.last_state_output

    async def step_stream(self, messages, user_id: str = None):
        """
        Streaming version of step. Runs full state machine, streams output at state boundaries.

        Yields:
            str: Characters/chunks from each state's output
        """
        self.current_user_id = user_id
        self.terminal_reason = None
        await self.add_context(messages)

        retry_count = 0

        while True:
            if retry_count > self.max_iter:
                logger.warning("step_stream: max iterations (%d) reached", self.max_iter)
                self.terminal_reason = TerminalReason.max_steps
                yield "\n[Max iterations reached]"
                break
            retry_count += 1

            context = await self.get_context()
            update, retry_signal = await self._run_state(context)

            if retry_signal == "retry":
                continue

            if update:
                assert isinstance(update, StateOutput), "State output was not a StateOutput instance"
                self.last_state_output = update
                if update.content:
                    await self.add_context([AIMessage(content=update.content)])
                    for char in update.content:
                        yield char

            if update and update.completion_signal == "error" and not self.current_state.is_terminal:
                self.current_state = self.flow.get_state("agent_reply")
                self.terminal_reason = TerminalReason.model_error
                break

            if self.current_state.is_terminal:
                self.terminal_reason = TerminalReason.completed
                break

            messages_list = await self.memory.retrieve_short_memory(5)
            if self.current_state.check_transition_ready(messages_list):
                transition_dict = self.flow.get_transitions(self.current_state, messages_list)
                transition_names = transition_dict["tt"]

                router = self.flow.get_router(self.current_state)
                if router and update:
                    next_state_name = router(update)
                elif len(transition_names) == 1:
                    next_state_name = transition_names[0]
                else:
                    next_state_name = await self.choose_transition(transition_dict, messages_list)

                self.current_state = self.flow.get_state(next_state_name)

                if not self.current_state.is_terminal:
                    yield "\n\n"
            else:
                self.terminal_reason = TerminalReason.needs_input
                break

        self.current_state = self.flow.get_initial_state()


if __name__ == "__main__":
    pass
