"""
Executor decision state. Runs inside a subagent (TaskRunner) and walks
plan_steps one at a time. For each step it decides whether the next action
is a tool call or a human question, then emits a route signal so the
executor_router in routers.py can pick the right next state.

Never re-plans. If a step can't be handled by the available tools, it routes
to ask_human instead of silently deviating.
"""

from __future__ import annotations

from enum import Enum, StrEnum

from pydantic import BaseModel, Field, create_model

from base_module.task_store import log_event
from model_module.ArkModelNew import SystemMessage
from model_module.llm_json import parse_llm_json
from state_module.core.base_state import StateOutput
from state_module.core.state import State
from state_module.core.state_registry import register_state

_TOOL_ERROR_SIGNALS = (
    '"ok": false',
    '"ok":false',
    "mcp error",
    '"error":',
    "channel_not_found",
    "not_found",
    "invalid_type",
    "unrecognized_keys",
    "validation error",
    "input validation error",
)


def _last_tool_was_error(context: list) -> bool:
    """Return True if the most recent tool result in context indicates failure.

    Scans backwards for the first AIMessage that looks like a tool result
    (content starts with 'tool ') and checks for error signals in its body.
    This is the programmatic gate on the advance action — the model's prompt
    instruction alone is not reliable enough on a 7B model that has seen
    a human approval and treats it as confirmation.
    """
    for msg in reversed(context):
        content = (getattr(msg, "content", "") or "").strip()
        if content.startswith("tool ") and " -> " in content:
            result = content.split(" -> ", 1)[-1].lower()
            return any(sig in result for sig in _TOOL_ERROR_SIGNALS)
    return False


class _ActionKind(StrEnum):
    tool = "tool"
    ask = "ask"
    advance = "advance"  # step is complete; move to the next one


class _AskKind(StrEnum):
    binary = "binary"
    text = "text"


class ExecutorDecision(BaseModel):
    """Structured choice for the current plan step.

    tool_args is intentionally absent — args are filled in a separate
    schema-constrained call using the selected tool's inputSchema so the
    model cannot hallucinate parameter names from training data.
    """

    action: _ActionKind = Field(
        ...,
        description="tool if the step can be performed by calling a tool, ask if human input is required",
    )
    reason: str = Field(..., description="One-sentence justification")

    # populated when action == tool
    tool_name: str | None = Field(None, description="Exact tool name from the tool list")

    # populated when action == ask
    ask_kind: _AskKind | None = Field(None, description="binary (approve/deny) or text (free-form answer)")
    ask_prompt: str | None = Field(None, description="Exact question to present to the user")


@register_state
class StateExecutor(State):
    """Subagent decision state. Picks the next step's action and emits a route signal."""

    type = "executor"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.is_terminal = False

    def check_transition_ready(self, context):
        return True

    async def run(self, context, agent=None):
        plan_steps: list[str] = getattr(agent, "plan_steps", []) or []
        step_idx: int = getattr(agent, "step_idx", 0)
        task_id: str | None = getattr(agent, "task_id", None)

        if step_idx >= len(plan_steps):
            return StateOutput(
                content="",
                completion_signal="complete",
                structured_data={"route": "done"},
            )

        current_step = plan_steps[step_idx]

        tool_lines: list[str] = []
        tool_names: list[str] = []
        tool_specs: dict[str, dict] = {}  # tool_name -> full spec (for arg schema)
        if agent.tool_manager is not None:
            try:
                servers = await agent.tool_manager.list_all_tools(agent.current_user_id)
                for _server, tools in servers.items():
                    for tname, tspec in tools.items():
                        tool_names.append(tname)
                        spec = tspec if isinstance(tspec, dict) else {}
                        tool_specs[tname] = spec
                        desc = spec.get("description") or getattr(tspec, "description", "") or ""
                        desc_short = desc.strip().splitlines()[0][:120] if desc.strip() else ""

                        # Include required + optional param names so the model
                        # doesn't guess field names from training-data knowledge
                        # of the raw API (e.g. guessing end.dateTime vs end_time).
                        schema = spec.get("inputSchema") or spec.get("input_schema") or {}
                        props = schema.get("properties") or {}
                        required = set(schema.get("required") or [])
                        if props:
                            # Required params: name* + one-line description so the
                            # model sees the exact format expected, not a guess.
                            req_lines = []
                            for k in props:
                                if k not in required:
                                    continue
                                pdesc = (props[k].get("description") or "").strip()
                                pdesc_short = pdesc.splitlines()[0][:100] if pdesc else ""
                                req_lines.append(f"{k}* — {pdesc_short}" if pdesc_short else f"{k}*")
                            # Optional params: names only (keep prompt compact)
                            opt_names = [k for k in props if k not in required]
                            req_str = ", ".join(req_lines)
                            opt_str = ", ".join(opt_names)
                            params_str = req_str + (f" | optional: {opt_str}" if opt_str else "")
                            tool_lines.append(f"- {tname}: {desc_short}")
                            if params_str:
                                tool_lines.append(f"    params: {params_str}")
                        else:
                            tool_lines.append(f"- {tname}: {desc_short}")
            except Exception as e:
                if task_id:
                    log_event(task_id, "error", f"could not list tools: {e}")

        tools_block = "\n".join(tool_lines) if tool_lines else "(no tools available)"

        system = SystemMessage(
            content=(
                "You are a subagent executing a plan one step at a time.\n"
                "You are NOT allowed to re-plan or skip steps.\n\n"
                "Check the conversation above for tool results already obtained for this step.\n"
                "  - If the current step is fully complete based on those results → action=advance\n"
                "  - If more tool calls are still needed to complete this step → action=tool\n"
                "  - If you cannot complete the step with available tools → action=ask\n\n"
                "IMPORTANT: Plan steps describe INTENT, not tool names. The step may mention a tool "
                "name like 'list_emails' but the actual tool available may be called 'fetch_emails'. "
                "Judge completion by whether the step's GOAL was fully achieved, not by whether a "
                "specific tool name was called. If a tool_result satisfies the step's intent and "
                "quantity (e.g. step asks for 5 emails and you have 5 emails in the results), advance. "
                "Do NOT advance if the result is partial (e.g. step asks for 5 but only 1 was returned "
                "and a nextPageToken exists — call the tool again with the token or a higher limit).\n\n"
                "A step often needs MULTIPLE tool calls: when one call provides info needed for the "
                "next (list calendars → create event), or when a paginated result must be continued "
                "(first page → next page). Use page_token or equivalent to fetch additional pages.\n\n"
                "NEVER choose advance if the most recent tool_result shows an error (e.g. ok=false, "
                "MCP error, channel_not_found, not_found, validation error). On an error: choose tool "
                "to retry with corrected arguments, or ask if you cannot fix it.\n"
                "A human saying 'yes' or 'approved' does NOT mean the step succeeded — only a "
                "successful tool_result (ok=true or data returned with no error field) confirms success.\n"
                "If a tool returns 'not_found' for a channel, user, or resource ID: use a "
                "list/find/search tool first to discover the correct identifier, then retry.\n"
                "Never invent tool names or parameter names. Use ONLY the tools and params listed below.\n"
                "Parameters marked with * are required. Use exact parameter names as shown.\n\n"
                f"Available tools:\n{tools_block}\n\n"
                f"Current plan step ({step_idx + 1}/{len(plan_steps)}): {current_step}\n"
            )
        )

        # Build a constrained decision schema with tool_name as an enum of the
        # actual available tools. This prevents the model from hallucinating a
        # tool name — it can only pick from the real tool list. Without this,
        # the model guesses names like 'fetch_articles' or 'web_search' from
        # training data, which triggers fallback_ask.
        if tool_names:
            ToolEnum = Enum("ToolEnum", {t: t for t in tool_names})
            ConstrainedDecision = create_model(
                "ExecutorDecision",
                action=(_ActionKind, Field(...)),
                reason=(str, Field(...)),
                tool_name=(ToolEnum | None, Field(None)),
                ask_kind=(_AskKind | None, Field(None)),
                ask_prompt=(str | None, Field(None)),
            )
        else:
            ConstrainedDecision = ExecutorDecision

        schema = {
            "type": "json_schema",
            "json_schema": {
                "name": "executor_decision",
                "schema": ConstrainedDecision.model_json_schema(),
            },
        }

        # Pass full context so the executor can see prior tool results for this step.
        output = await agent.call_llm(context=[system] + list(context), json_schema=schema)
        # parse_llm_json raises OutputValidationError on failure; _run_state
        # catches it and produces an error StateOutput rather than crashing.
        raw_decision = parse_llm_json(output.content if output else None, ConstrainedDecision)
        # Normalise to base ExecutorDecision so the rest of the code is type-stable.
        decision = ExecutorDecision(
            action=_ActionKind(
                raw_decision.action.value if hasattr(raw_decision.action, "value") else raw_decision.action
            ),
            reason=raw_decision.reason,
            tool_name=raw_decision.tool_name.value
            if raw_decision.tool_name and hasattr(raw_decision.tool_name, "value")
            else raw_decision.tool_name,
            ask_kind=raw_decision.ask_kind,
            ask_prompt=raw_decision.ask_prompt,
        )

        if task_id:
            log_event(
                task_id,
                "step_started",
                current_step,
                payload={"step_idx": step_idx, "decision": decision.model_dump(mode="json")},
            )

        if decision.action == _ActionKind.advance:
            # Programmatic guard: the model's prompt instruction ("never advance
            # on an error") is not reliable on a 7B model that sees a human
            # approval and treats it as step confirmation. Block advance if the
            # most recent tool result in context signals failure, and force a
            # retry instead.
            if _last_tool_was_error(context):
                if task_id:
                    log_event(
                        task_id,
                        "advance_blocked",
                        "blocked advance: last tool result was an error",
                        payload={"step_idx": step_idx},
                    )
                agent.pending_ask = {
                    "kind": "text",
                    "prompt": (
                        f"The last tool call failed for step {step_idx + 1}: {current_step}\n"
                        "The error is shown above. How should I fix this and retry?"
                    ),
                }
                return StateOutput(
                    content="",
                    completion_signal="incomplete",
                    structured_data={"route": "ask"},
                )

            agent.step_idx = step_idx + 1
            if task_id:
                log_event(task_id, "step_complete", current_step, payload={"step_idx": step_idx})
            next_route = "done" if agent.step_idx >= len(plan_steps) else "continue"
            return StateOutput(
                content="",
                completion_signal="complete",
                structured_data={"route": next_route},
            )

        if decision.action == _ActionKind.tool:
            if not decision.tool_name or (tool_names and decision.tool_name not in tool_names):
                agent.pending_ask = {
                    "kind": "text",
                    "prompt": (
                        f"I need to do: {current_step}\nBut I don't have a tool that matches. How should I handle this?"
                    ),
                }
                if task_id:
                    log_event(
                        task_id,
                        "fallback_ask",
                        "invalid tool name from LLM",
                        payload={"llm_choice": decision.tool_name},
                    )
                return StateOutput(
                    content="",
                    completion_signal="incomplete",
                    structured_data={"route": "ask"},
                )

            # Second constrained call: fill tool args using the tool's own
            # inputSchema so the model cannot emit param names that don't exist
            # in the schema (e.g. end_datetime when the tool only has
            # event_duration_hour/event_duration_minutes).
            tool_name = decision.tool_name
            tool_args: dict = {}
            input_schema = (tool_specs.get(tool_name) or {}).get("inputSchema") or {}
            if input_schema and input_schema.get("properties"):
                args_system = SystemMessage(
                    content=(
                        f"Fill in the arguments for the tool '{tool_name}' to accomplish:\n"
                        f"{current_step}\n\n"
                        "Use ONLY the parameter names defined in the JSON schema. "
                        "Do not add any other keys. Use exact formats shown in descriptions."
                    )
                )
                args_schema = {
                    "type": "json_schema",
                    "json_schema": {"name": "tool_args", "schema": input_schema},
                }
                try:
                    args_output = await agent.call_llm(context=[args_system] + list(context), json_schema=args_schema)
                    import json as _json

                    try:
                        from json_repair import repair_json

                        raw = repair_json(args_output.content or "{}", return_objects=False)
                    except Exception:
                        raw = args_output.content or "{}"
                    tool_args = _json.loads(raw) if raw else {}
                except Exception as e:
                    if task_id:
                        log_event(task_id, "error", f"arg-fill call failed for {tool_name}: {e}")
                    tool_args = {}

            agent.pending_tool = {"tool_name": tool_name, "tool_args": tool_args}
            return StateOutput(
                content=f"calling tool `{tool_name}` for step {step_idx + 1}",
                completion_signal="incomplete",
                structured_data={"route": "tool"},
            )

        # action == ask
        agent.pending_ask = {
            "kind": (decision.ask_kind or _AskKind.text).value,
            "prompt": decision.ask_prompt or current_step,
        }
        return StateOutput(
            content="",
            completion_signal="incomplete",
            structured_data={"route": "ask"},
        )
