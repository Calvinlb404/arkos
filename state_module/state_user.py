import os
import sys

from state_module.base_state import StateOutput
from state_module.state import State
from state_module.state_registry import register_state

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@register_state
class StateUser(State):
    """Terminal state that waits for user input before transitioning."""

    type = "user"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.is_terminal = True  # Stop after this state

    def check_transition_ready(self, context):
        """Always ready to transition once user provides input."""
        return True

    async def run(self, context, agent=None):
        """Signals that control is being returned to the user."""
        return StateOutput(
            content="",
            completion_signal="needs_input",
        )
