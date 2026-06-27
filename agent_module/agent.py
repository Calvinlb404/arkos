# agent.py

import logging
import os
import sys
import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ValidationError, create_model

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from memory_module.memory import Memory

# Assuming ArkModelLink.generate_response is actually ArkModelLink.agenerate_response
from model_module.ArkModelNew import AIMessage, ArkModelLink, SystemMessage
from model_module.errors import ModelError
from state_module.core.base_state import StateOutput, TerminalReason
from state_module.core.state_handler import StateHandler

logger = logging.getLogger(__name__)

MAX_ITER = 10

# Tokens kept below the hard limit to absorb tiktoken/Qwen mismatch (~10-30%)
# and leave headroom for system-prompt framing added per request.
_CONTEXT_SAFETY_MARGIN = 2048

# tiktoken approximates tokens; Qwen tokenizes differently. Apply a fudge
# factor so we err on the side of under-filling the window.
_TIKTOKEN_FUDGE = 1.15

try:
    import tiktoken as _tiktoken

    _ENCODER = _tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENCODER = None


def _count_tokens(text: str) -> int:
    """Approximate token count using tiktoken cl100k_base + fudge factor."""
    if _ENCODER is None:
        # Rough fallback: 1 token per 4 chars.
        return int(len(text) / 4 * _TIKTOKEN_FUDGE)
    return int(len(_ENCODER.encode(text)) * _TIKTOKEN_FUDGE)


def _render_for_context(value: Any, budget_chars: int) -> str:
    """
    Produce a context-safe string from a tool result.

    If the full string fits in budget_chars, returns it unchanged. Otherwise
    returns a head+tail view with a truncation marker so the model sees the
    structure of the result without blowing the context window.
    """
    text = str(value)
    if len(text) <= budget_chars:
        return text
    if budget_chars <= 0:
        return "[result omitted: context window is full]"
    # Keep equal portions from head and tail so the model sees start and end.
    half = budget_chars // 2
    omitted = len(text) - (half * 2)
    return f"{text[:half]}\n... [{omitted} chars omitted] ...\n{text[-half:]}"


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
        self.context_tokens: int = 0

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

        server_tool_map = await self.tool_manager.list_all_tools(self.current_user_id)

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

    def tool_result_budget(self) -> int:
        """
        Remaining character budget for a tool result to enter the context window.

        Computes: context_window - current_context_tokens - output_reserve - safety_margin.
        Characters rather than tokens because the caller works in strings; the
        fudge factor in _count_tokens already over-estimates tokens, so treating
        characters as tokens here is conservative enough.
        """
        from config_module.loader import config as _cfg

        context_window: int = _cfg.get("llm.context_window") or _cfg.get("llm.max_tokens") or 8192
        output_reserve: int = _cfg.get("llm.max_tokens") or 0
        # If context_window was not explicitly set, treat max_tokens as the full
        # budget (no separate output reserve to subtract).
        if _cfg.get("llm.context_window") is None:
            output_reserve = 0
        remaining_tokens = context_window - self.context_tokens - output_reserve - _CONTEXT_SAFETY_MARGIN
        return max(0, remaining_tokens)

    def render_tool_result(self, tool_result: Any) -> str:
        """
        Convert a tool result into a string safe to place in the context window.

        Uses the context-aware budget so a large result never pushes the prompt
        past the model's limit. The full result is still available to callers
        via structured_data if needed (for code consumers -- see HARNESS_SPEC
        Task 4 and ENVIRONMENT_SPEC for model-side retrieval).
        """
        budget = self.tool_result_budget()
        return _render_for_context(tool_result, budget_chars=budget)

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

        Also updates self.context_tokens with an approximate token count of the
        assembled context so callers (render_tool_result, MEMORY_SPEC Task 2) can
        budget additions without re-counting.

        Returns:
            list of messages
        """
        short_term_mem = await self.memory.retrieve_short_memory(turns)

        if include_long_term:
            long_term_mem = await self.memory.retrieve_long_memory(context=short_term_mem)
            if long_term_mem and long_term_mem.content.strip():
                ctx = [long_term_mem] + short_term_mem
            else:
                ctx = short_term_mem
        else:
            ctx = short_term_mem

        self.context_tokens = sum(_count_tokens(m.content or "") for m in ctx)
        return ctx

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
                # Route errors to the reply state when it exists (buddy graph).
                # The executor graph has no "agent_reply" — fall through to
                # terminal so the task is marked model_error instead of crashing.
                if "agent_reply" in self.flow.states:
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

        # Map the active state to a short activity label so the UI can show a
        # live status line instead of an opaque "…". Buddy chats and plans; it
        # does NOT call tools (that's the subagent), so there is no "using a
        # tool" label here -- tool activity is surfaced via the task event log.
        _STATUS = {
            "agent": "thinking",
            "plan": "drafting a plan",
            "computer_plan": "drafting a plan",
            "user": "waiting for you",
        }

        def _status_for(state):
            return _STATUS.get(getattr(state, "type", "")) or _STATUS.get(getattr(state, "name", "")) or "working"

        while True:
            if retry_count > self.max_iter:
                logger.warning("step_stream: max iterations (%d) reached", self.max_iter)
                self.terminal_reason = TerminalReason.max_steps
                yield {"type": "content", "text": "\n[Max iterations reached]"}
                break
            retry_count += 1

            # Announce what buddy is about to do; shows during slow model calls.
            yield {"type": "status", "label": _status_for(self.current_state)}

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
                        yield {"type": "content", "text": char}

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
                    yield {"type": "content", "text": "\n\n"}
            else:
                self.terminal_reason = TerminalReason.needs_input
                break

        self.current_state = self.flow.get_initial_state()


if __name__ == "__main__":
    pass
