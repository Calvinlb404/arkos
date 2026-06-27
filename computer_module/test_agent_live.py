"""
Live integration test: ComputerAgent writing + running real code in a real sandbox.
Run: python computer_module/test_agent_live.py
"""

import asyncio
import time

from dotenv import load_dotenv

load_dotenv(".env")

from computer_module.agent import ComputerAgent  # noqa: E402
from computer_module.sandbox import SandboxManager  # noqa: E402

USER = "live_test_agent_user"


def on_event(e):
    kind = e.get("kind", "?")
    detail = e.get("tool") or e.get("prompt", "")[:80] or e.get("summary", "")[:80] or e.get("reason", "")
    print(f"  [{kind}] {detail}")


async def main():
    sandbox = SandboxManager()
    agent = ComputerAgent(user_id=USER, sandbox=sandbox, emit=on_event)

    task = (
        "Create a Python script at /home/user/fib.py that prints the first 10 "
        "Fibonacci numbers. Then run it and show me the output. "
        "Verify the output looks correct before finishing."
    )

    print(f"TASK: {task}\n{'=' * 60}")
    t0 = time.time()
    result = await agent.run(task, step_cap=20)
    elapsed = time.time() - t0

    print(f"\n{'=' * 60}")
    print(f"STATUS:  {result['status']}")
    print(f"ELAPSED: {elapsed:.1f}s")
    print(f"OUTPUTS: {result['outputs']}")
    print(f"SUMMARY:\n{result['summary']}")

    # cleanup
    sbx = sandbox._live.get(USER)
    if sbx:
        sbx.kill()
        print("\n(sandbox killed)")


asyncio.run(main())
