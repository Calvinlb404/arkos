"""
computer_module: the per-user persistent computer and the agent that operates it.

See arkos-inspo/specs/COMPUTER_SPEC.md. Built bottom-up:
  sandbox.py  -- per-user e2b sandbox lifecycle (the computer)
  agent.py    -- the agent loop over the sandbox (the worker, local model for now)
  store.py    -- computer_tasks persistence
  runner.py   -- async dispatch + completion notification
"""
