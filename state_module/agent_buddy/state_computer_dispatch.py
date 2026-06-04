"""
Computer dispatch state for the buddy graph.

Entered when state_ai routes to 'computer' -- the user asked for something
that needs the persistent computer (file work, running code, multi-step
computer tasks). Creates a computer_tasks row, spawns the async runner, and
returns a holding message so buddy's turn ends immediately.

The runner will inject the result back into the conversation when done.
"""

from __future__ import annotations

import logging

from computer_module.runner import spawn
from computer_module.store import create_computer_task
from state_module.core.base_state import StateOutput
from state_module.core.state import State

logger = logging.getLogger(__name__)


class StateComputerDispatch(State):
    type = "computer_dispatch"

    def __init__(self, name: str, config: dict) -> None:
        super().__init__(name, config)
        self.is_terminal = True  # hand control back to user immediately

    def check_transition_ready(self, context) -> bool:
        return True

    async def run(self, context, agent=None) -> StateOutput:
        # Extract the last user message as the task prompt.
        messages = context if isinstance(context, list) else []
        prompt = ""
        for m in reversed(messages):
            role = getattr(m, "role", None)
            content = getattr(m, "content", None) or ""
            if role == "user" and content.strip():
                prompt = content.strip()
                break

        if not prompt:
            return StateOutput(
                content="I'm not sure what computer task to run. Could you describe it?",
                completion_signal="needs_input",
                structured_data={"route": "ask"},
            )

        user_id = getattr(agent, "current_user_id", None) or "unknown"
        chat_session_id = agent.memory.session_id

        try:
            task_id = create_computer_task(
                user_id=user_id,
                chat_session_id=chat_session_id,
                prompt=prompt,
            )
            spawn(
                task_id=task_id,
                user_id=user_id,
                chat_session_id=chat_session_id,
                prompt=prompt,
                tool_manager=getattr(agent, "tool_manager", None),
            )
            logger.info("dispatched computer task %s for user %s", task_id, user_id)
        except Exception as e:
            logger.error("failed to dispatch computer task for user %s: %s", user_id, e)
            return StateOutput(
                content="I couldn't start the computer task. Please try again.",
                completion_signal="error",
                error_detail=str(e),
                structured_data={"route": "ask"},
            )

        return StateOutput(
            content="On it. I'll let you know here when it's done.",
            completion_signal="complete",
            structured_data={"route": "ask", "task_id": task_id},
        )
