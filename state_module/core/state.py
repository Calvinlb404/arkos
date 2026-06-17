from typing import Any


class State:
    """Base class for all states in the state graph.

    Instances are shared across every concurrent request and user: one State
    object lives in the StateHandler and is reused by every agent that points
    its current_state at it. They MUST therefore stay immutable at runtime --
    all per-request data belongs on the Agent (pending_tool, plan_steps,
    step_idx, ...), never on self. To enforce this, StateHandler calls
    `_freeze()` after construction; any attribute write afterwards raises,
    turning an accidental cross-request bleed into an immediate error (Fix 4).
    """

    def __init__(self, name: str, config: dict[str, Any]):
        """Initialize a state with its name and configuration from the YAML graph."""
        self.name = name
        self.is_terminal: bool = False
        self.transition = config.get("transition", {})

    def _freeze(self) -> None:
        """Lock the instance so no attribute can be set at runtime."""
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_frozen", False):
            raise AttributeError(
                f"{type(self).__name__} is frozen: cannot set {name!r} at runtime. "
                "State objects are shared across users and requests; store per-request "
                "data on the Agent, not on the state."
            )
        object.__setattr__(self, name, value)

    def check_transition_ready(self, context: dict[str, Any]) -> bool:
        """Subclasses must override. Return True when the state is ready to transition."""
        raise NotImplementedError

    def run(self, context: dict[str, Any]) -> dict[str, Any] | None:
        """Subclasses must override. Execute the state's logic and return a StateOutput."""
        raise NotImplementedError
