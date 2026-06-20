"""
Routers for the executor (subagent) graph.

Each router maps a StateOutput route signal to a concrete next-state name
as declared in executor/graph.yaml.
"""

from __future__ import annotations

from state_module.core.base_state import StateOutput


def executor_router(output: StateOutput) -> str:
    """
    Routes after state_executor runs.

    Signals
    -------
    tool   -> use_tool       (step can be handled by a tool call)
    ask    -> ask_human      (step needs human input or approval)
    done   -> executor_done  (no more plan steps)
    <none> -> executor_done  (safe default)
    """
    route = (output.structured_data or {}).get("route", "")
    if route == "tool":
        return "use_tool"
    if route == "ask":
        return "ask_human"
    return "executor_done"


def use_tool_router(output: StateOutput) -> str:
    """
    Routes after executor/state_tool runs.

    Always loops back to executor so it can pick the next plan step.
    The route signal is not checked; there is only one valid destination.
    """
    return "executor"


def ask_human_router(output: StateOutput) -> str:
    """
    Routes after state_approval (ask_human) runs.

    Signals
    -------
    continue -> executor      (approved or answered; carry on with the plan)
    done     -> executor_done (declined binary approval or task cancelled)
    <none>   -> executor_done (safe default)
    """
    route = (output.structured_data or {}).get("route", "")
    if route == "continue":
        return "executor"
    return "executor_done"


#: Maps state name (as in graph.yaml) -> router function.
ROUTERS: dict[str, callable] = {
    "executor": executor_router,
    "use_tool": use_tool_router,
    "ask_human": ask_human_router,
}
