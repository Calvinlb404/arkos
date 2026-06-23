"""
Async background runner for computer tasks.

Drives ComputerAgent.run(), emits progress events to computer_task_events,
and on completion injects the result message into the user's chat session
via Memory.add_memory() so it appears in the conversation they came from.

Completion notification flow (Task 7):
  1. set_computer_status(completed | failed)
  2. log_computer_event(kind=completed|failed, result)
  3. Memory.add_memory(AIMessage(result)) -> chat_session_id  <- the message back to the user
  4. sandbox.pause(user_id)
"""

from __future__ import annotations

import asyncio
import logging

from base_module.task_store import update_task_status
from computer_module.agent import ComputerAgent
from computer_module.sandbox import sandbox_manager
from computer_module.store import (
    log_computer_event,
    set_computer_status,
)

logger = logging.getLogger(__name__)


async def run_computer_task(
    task_id: str,
    user_id: str,
    chat_session_id: str,
    prompt: str,
    tool_manager=None,
) -> None:
    """
    Drive one computer task end-to-end. Called as an asyncio task.
    Never raises -- all errors are caught and written to the task row.
    """
    set_computer_status(task_id, "running")
    log_computer_event(task_id, "start", f"task started: {prompt[:120]}")

    def emit(event: dict) -> None:
        kind = event.get("kind", "info")
        content = event.get("tool") or event.get("summary", "")[:200] or event.get("reason", "")
        payload = {k: v for k, v in event.items() if k not in ("kind",)}
        log_computer_event(task_id, kind, content, payload)

    async def ask(prompt: str) -> str:
        """
        Create an approval row, surface it in the Pending Approvals tray, and
        poll until the user responds. Now that computer tasks live in `tasks`,
        the approval JOINs cleanly and the existing tray works for them too.
        The approval's user_id must be the UUID (the tasks/approvals keyspace),
        while the sandbox/Memory keyspaces keep the raw sub.
        """
        from base_module.task_store import _user_uuid, create_approval, get_approval
        approval_id = create_approval(task_id, str(_user_uuid(user_id)), "text", prompt)
        emit({"kind": "ask", "prompt": prompt, "approval_id": approval_id})
        set_computer_status(task_id, "awaiting_approval")  # render as "waiting on you"
        poll_interval = 2.0
        timeout = 86400  # 24h
        elapsed = 0.0
        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            row = get_approval(approval_id)
            if row and row["status"] not in ("pending",):
                set_computer_status(task_id, "running")
                return row.get("response_text") or ("approved" if row.get("response_bool") else "declined")
        set_computer_status(task_id, "running")
        return "(ask timed out)"

    agent = ComputerAgent(
        user_id=user_id,
        sandbox=sandbox_manager,
        tool_manager=tool_manager,
        emit=emit,
        ask=ask,
    )

    try:
        result = await agent.run(prompt)
    except Exception as e:
        logger.error("computer task %s crashed: %s", task_id, e)
        result = {"status": "failed", "summary": str(e), "outputs": []}

    status = result["status"]
    summary = result.get("summary", "")
    outputs = result.get("outputs", [])

    # Write status + event atomically so a poll can never see the new status
    # without its corresponding event (ISSUES.md Task 4).
    if status == "completed":
        update_task_status(
            task_id, "completed",
            summary=summary, outputs=outputs,
            event_kind="completed", event_content=summary[:500],
            event_payload={"outputs": outputs},
        )
    else:
        update_task_status(
            task_id, "failed",
            error=summary, outputs=outputs,
            event_kind="failed", event_content=summary[:500],
            event_payload={"outputs": outputs},
        )

    # 3. Inject the result into the user's chat session so it appears in the conversation.
    await _inject_chat_message(user_id, chat_session_id, status, summary, outputs)

    # 4. Pause the sandbox (stop compute cost while idle)
    try:
        await sandbox_manager.pause(user_id)
    except Exception as e:
        logger.warning("could not pause sandbox for user %s: %s", user_id, e)


async def _inject_chat_message(
    user_id: str,
    chat_session_id: str,
    status: str,
    summary: str,
    outputs: list[str],
) -> None:
    """
    Write the result as an AIMessage into conversation_context for chat_session_id.

    This is what makes the 'done' message appear in the user's chat without them
    needing to poll. The Memory class writes to the same conversation_context table
    the chat endpoint reads from.
    """
    try:
        from config_module.loader import config
        from memory_module.memory import Memory
        from model_module.ArkModelNew import AIMessage

        if status == "completed":
            parts = ["Done."]
            if summary:
                parts.append(summary)
            if outputs:
                file_list = ", ".join(f"`{p}`" for p in outputs[:5])
                parts.append(f"Files: {file_list}")
            message_text = " ".join(parts)
        else:
            message_text = (
                f"The computer task didn't complete. {summary}"
                if summary
                else "The computer task failed. Let me know if you'd like to try again."
            )

        mem = Memory(
            user_id=user_id,
            session_id=chat_session_id,
            db_url=config.get("database.url"),
            use_long_term=False,
        )
        await mem.add_memory(AIMessage(content=message_text))
        logger.info("injected completion message for user %s session %s", user_id, chat_session_id)

    except Exception as e:
        logger.error(
            "failed to inject completion message for user %s session %s: %s",
            user_id, chat_session_id, e,
        )


# Strong references to in-flight tasks. asyncio only holds weak refs to tasks,
# so without this a fire-and-forget task can be garbage-collected mid-run (a
# silent hang). Tasks remove themselves on completion.
_INFLIGHT: set = set()


def spawn(
    task_id: str,
    user_id: str,
    chat_session_id: str,
    prompt: str,
    tool_manager=None,
) -> None:
    """
    Fire-and-forget: schedule run_computer_task on the running event loop.
    Returns immediately so the HTTP handler can respond to the user.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.get_event_loop()
    t = loop.create_task(
        run_computer_task(task_id, user_id, chat_session_id, prompt, tool_manager),
        name=f"computer_task_{task_id[:8]}",
    )
    _INFLIGHT.add(t)
    t.add_done_callback(_INFLIGHT.discard)
