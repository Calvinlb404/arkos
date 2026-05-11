"""
Graph-level router functions.

Each function maps a StateOutput's `route` signal to a concrete next-state
name.  Routers live here — not inside state classes — so states stay focused
on execution and the graph topology stays in one place.

Pattern
-------
  - States emit  structured_data={"route": "<signal>"}
  - Routers read that signal and return a state-name string
  - agent.py calls the router after a state runs, before falling through to
    choose_transition

Adding a new routed state
-------------------------
  1. Have the state emit a "route" key in structured_data.
  2. Write a router function here that maps route values to state names.
  3. Register it in ROUTERS below.
"""

from __future__ import annotations

from state_module.base_state import StateOutput


# ---------------------------------------------------------------------------
# Chat graph routers
# ---------------------------------------------------------------------------

def agent_reply_router(output: StateOutput) -> str:
    """
    Routes after state_ai (agent_reply) runs.

    Signals
    -------
    plan   -> workshop_plan  (user asked buddy to DO something concrete)
    ask    -> ask_user       (buddy needs clarification)
    reply  -> ask_user       (simple answer; hand back to user)
    <none> -> ask_user       (safe default)
    """
    route = (output.structured_data or {}).get("route", "")
    if route == "plan":
        return "workshop_plan"
    return "ask_user"


# ---------------------------------------------------------------------------
# Executor graph routers
# ---------------------------------------------------------------------------

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
    Routes after state_tool runs inside the executor graph.

    Signals
    -------
    continue -> executor     (tool succeeded; advance to next step)
    <none>   -> executor     (same; always loop back in executor graph)

    Note: the chat graph's use_tool state has only one transition (agent_reply)
    so it never hits this router — single-edge states are handled deterministically
    in agent.py before routers are consulted.
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


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: Maps state *name* (as declared in the YAML graphs) to its router function.
#: States not listed here are handled by the single-edge deterministic path
#: or by choose_transition in agent.py.
ROUTERS: dict[str, callable] = {
    # chat graph
    "agent_reply": agent_reply_router,
    # executor graph
    "executor":    executor_router,
    "use_tool":    use_tool_router,
    "ask_human":   ask_human_router,
}
