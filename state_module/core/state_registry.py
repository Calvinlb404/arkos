"""
Global state type registry.

States self-register by decorating their class with @register_state.
Registration is triggered by importing the state's module — each agent
package's __init__.py does this explicitly for its own states.

The auto_register_states glob helper has been removed. Explicit imports
are preferred: they are faster, clearer, and don't rely on file naming
conventions.
"""

STATE_REGISTRY: dict[str, type] = {}


def register_state(cls):
    """Decorator that registers a State subclass in STATE_REGISTRY by its `type` attribute."""
    state_type = getattr(cls, "type", None)
    if not state_type:
        raise ValueError(f"State class {cls.__name__} must have a `type` attribute.")
    print(f"[registry] registered state type: {state_type!r} -> {cls.__name__}")
    STATE_REGISTRY[state_type] = cls
    return cls
