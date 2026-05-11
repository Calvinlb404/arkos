import os
import sys
from typing import Any, Callable

import yaml

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from state_module.state import State
from state_module.state_registry import STATE_REGISTRY, auto_register_states
from state_module.routers import ROUTERS

auto_register_states("state_module")


class StateHandler:
    """Loads a YAML state graph and manages state transitions."""

    def __init__(self, yaml_path: str):
        """Load the state graph from a YAML file and instantiate all states."""
        with open(yaml_path) as f:
            self.graph = yaml.safe_load(f)

        self.states = {}
        for name, config in self.graph.get("states", {}).items():
            state_type = config.get("type")
            if state_type not in STATE_REGISTRY:
                raise ValueError(f"Unknown state type: {state_type}")
            state_class = STATE_REGISTRY[state_type]
            self.states[name] = state_class(name, config)

        self.initial_state_name = self.graph["initial"]

    def get_initial_state(self) -> State:
        """Return the state designated as 'initial' in the graph."""
        return self.states[self.initial_state_name]

    def get_transitions(self, current_state: str, context: dict[str, Any]):
        """Return available transitions from the current state as target names and descriptions."""
        state = current_state
        transition_targets = state.transition.get("next", [])

        transition_descs = []
        for t in transition_targets:
            desc = getattr(self.states[t], "description", None)
            transition_descs.append((t, desc))

        return {"td": transition_descs, "tt": transition_targets}

    def get_state(self, state_name: str) -> State:
        """Look up a state by name."""
        return self.states[state_name]

    def get_router(self, state: State) -> Callable | None:
        """Return the router function for a state, or None if it has no router.

        States without a router fall through to the single-edge deterministic
        path or choose_transition in agent.py.
        """
        return ROUTERS.get(state.name)
