"""
Routers for the buddy (chat) agent graph.

Each router function maps a StateOutput route signal to a concrete next-state
name as declared in buddy/graph.yaml. State files emit {"route": "<signal>"}
in structured_data; routers translate that signal to a state name so state
files never contain hardcoded destination names.
"""

from __future__ import annotations

from state_module.core.base_state import StateOutput


def agent_reply_router(output: StateOutput) -> str:
    """
    Routes after state_ai (agent_reply) runs.

    Signals
    -------
    plan     -> workshop_plan   (multi-step external-service action, needs approval)
    computer -> computer_plan   (computer task -- workshop a plan, then approve to run)
    ask      -> ask_user        (buddy needs clarification)
    reply    -> ask_user        (simple answer; hand back to user)
    <none>   -> ask_user        (safe default)
    """
    route = (output.structured_data or {}).get("route", "")
    if route == "plan":
        return "workshop_plan"
    if route == "computer":
        return "computer_plan"
    return "ask_user"


#: Maps state name (as in graph.yaml) -> router function.
ROUTERS: dict[str, callable] = {
    "agent_reply": agent_reply_router,
}
