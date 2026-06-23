"""
Terminal state for the executor graph.

Writes a short summary event and returns. TaskRunner flips tasks.status to
'completed' after step() returns; this state's job is just to produce the
final user-visible summary message.
"""

from __future__ import annotations

from base_module.task_store import log_event
from model_module.ArkModelNew import SystemMessage
from state_module.core.base_state import StateOutput
from state_module.core.state import State
from state_module.core.state_registry import register_state


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
        plan_steps = getattr(agent, "plan_steps", []) or []
        step_idx = getattr(agent, "step_idx", 0)

        try:
            system = SystemMessage(
                content=(
                    "You are summarising the results of a completed task for the user.\n"
                    "CRITICAL: Only report actions that are confirmed by an actual tool_result "
                    "in the conversation above. Do NOT infer, assume, or describe actions that "
                    "do not have a corresponding tool_result confirming they succeeded.\n"
                    "If a plan step has no tool_result proving it ran, say 'Step N was not completed' "
                    "rather than describing a fabricated outcome.\n"
                    "Include actual data returned by tools (IDs, names, times). "
                    "Do not say 'the task is complete' — present only what the tools confirmed. "
                    "Keep it under 200 words."
                )
            )
            output = await agent.call_llm(context=list(context) + [system], json_schema=None)
            summary = (output.content or "").strip() if output else ""
        except Exception as e:
            summary = ""
            if task_id:
                log_event(task_id, "error", f"summary LLM call failed: {e}")

        # Deterministic success gate (architecture contract #2 -- no LLM here):
        # step_idx only advances past a step on a non-error 'advance' decision, so
        # reaching the end means every step genuinely ran. Stopping short means the
        # executor gave up (e.g. no usable tool), which is a failure, not success.
        all_steps_done = step_idx >= len(plan_steps)

        if not summary:
            summary = (
                f"Finished all {len(plan_steps)} plan steps."
                if all_steps_done
                else f"Stopped at step {step_idx + 1} of {len(plan_steps)}."
            )

        if task_id:
            log_event(task_id, "done", summary, payload={"step_idx": step_idx, "total": len(plan_steps)})

        return StateOutput(
            content=summary,
            completion_signal="complete" if all_steps_done else "incomplete",
            structured_data={"summary": summary, "all_steps_done": all_steps_done},
        )
