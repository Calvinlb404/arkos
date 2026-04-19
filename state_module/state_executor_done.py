"""
Terminal state for the executor graph. Writes a short summary event and
returns. TaskRunner is the one that flips tasks.status to 'completed'
after step() returns, so this state's job is just to produce the final
user-visible message.
"""

from __future__ import annotations

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from base_module.task_store import log_event  # noqa: E402
from state_module.base_state import StateOutput  # noqa: E402
from state_module.state import State  # noqa: E402
from state_module.state_registry import register_state  # noqa: E402


@register_state
class StateExecutorDone(State):
    type = "executor_done"

    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.is_terminal = True

    def check_transition_ready(self, context):
        return True

    async def run(self, context, agent=None):
        task_id = getattr(agent, "task_id", None)
        # TaskRunner reads this string as the final summary
        plan_steps = getattr(agent, "plan_steps", []) or []
        step_idx = getattr(agent, "step_idx", 0)

        if step_idx >= len(plan_steps):
            summary = f"Finished all {len(plan_steps)} plan steps."
        else:
            summary = f"Stopped at step {step_idx + 1} of {len(plan_steps)}."

        if task_id:
            log_event(task_id, "done", summary, payload={"step_idx": step_idx, "total": len(plan_steps)})

        return StateOutput(
            content=summary,
            completion_signal="complete",
            structured_data={"summary": summary},
        )
