"""
StateHandler: loads a YAML state graph and manages state transitions.

Each agent passes its own package name (e.g. "state_module.agent_buddy") and
StateHandler auto-discovers every State subclass one level below that package
using pkgutil.iter_modules. No manual __init__.py import lists are needed, and
no global STATE_REGISTRY is consulted — each handler builds its own scoped type
map, so two agents can safely reuse the same type string (e.g. "user").

Usage
-----
    from state_module.core.state_handler import StateHandler
    from state_module.agent_buddy.routers import ROUTERS as BUDDY_ROUTERS

    flow = StateHandler(
        yaml_path="state_module/agent_buddy/graph.yaml",
        agent_pkg="state_module.agent_buddy",
        routers=BUDDY_ROUTERS,
    )
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Callable
from typing import Any

import yaml

from state_module.core.state import State


def _discover_states(agent_pkg: str) -> dict[str, type]:
    """
    Import every non-package module one level below *agent_pkg* and return a
    mapping of state-type-string -> State subclass.

    Scanning is intentionally shallow (one level only) so sub-packages are
    never touched accidentally.
    """
    pkg = importlib.import_module(agent_pkg)
    type_map: dict[str, type] = {}

    for _finder, mod_name, ispkg in pkgutil.iter_modules(pkg.__path__):
        if ispkg:
            continue  # one level only — skip sub-packages

        mod = importlib.import_module(f"{agent_pkg}.{mod_name}")

        for _attr, obj in inspect.getmembers(mod, inspect.isclass):
            if (
                issubclass(obj, State)
                and obj is not State
                and getattr(obj, "type", None)
                # Only register classes *defined* in this module, not imported ones.
                and obj.__module__ == mod.__name__
            ):
                type_map[obj.type] = obj

    return type_map


class StateHandler:
    """Loads a YAML state graph and manages state transitions."""

    def __init__(
        self,
        yaml_path: str,
        agent_pkg: str,
        routers: dict[str, Callable] | None = None,
    ):
        """
        Parameters
        ----------
        yaml_path:
            Absolute or relative path to the graph YAML.
        agent_pkg:
            Dotted Python package name for this agent's state package, e.g.
            ``"state_module.agent_buddy"``. All non-package modules one level
            below are imported and their State subclasses are registered in
            this handler's scoped type map.
        routers:
            Mapping of state-name -> router function for states that use a
            route signal to pick among multiple outgoing edges. States absent
            from this dict fall through to the single-edge deterministic path
            or choose_transition in agent.py.
        """
        self._state_types: dict[str, type] = _discover_states(agent_pkg)
        self._routers: dict[str, Callable] = routers or {}

        with open(yaml_path) as f:
            self.graph = yaml.safe_load(f)

        self.states: dict[str, State] = {}
        for name, config in self.graph.get("states", {}).items():
            state_type = config.get("type")
            if state_type not in self._state_types:
                raise ValueError(
                    f"Unknown state type {state_type!r} for state {name!r} "
                    f"in agent package {agent_pkg!r}. "
                    f"Discovered types: {sorted(self._state_types)}"
                )
            state_class = self._state_types[state_type]
            self.states[name] = state_class(name, config)

        self.initial_state_name: str = self.graph["initial"]

    def get_initial_state(self) -> State:
        """Return the state designated as 'initial' in the graph."""
        return self.states[self.initial_state_name]

    def get_transitions(self, current_state: State, context: Any) -> dict:
        """Return available transitions from the current state as target names and descriptions."""
        transition_targets = current_state.transition.get("next", [])
        transition_descs = [(t, getattr(self.states[t], "description", None)) for t in transition_targets]
        return {"td": transition_descs, "tt": transition_targets}

    def get_state(self, state_name: str) -> State:
        """Look up a state by name."""
        return self.states[state_name]

    def get_router(self, state: State) -> Callable | None:
        """Return the router function for a state, or None if it has no router.

        States without a router fall through to the single-edge deterministic
        path or choose_transition in agent.py.
        """
        return self._routers.get(state.name)
