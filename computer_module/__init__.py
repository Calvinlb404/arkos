"""
computer_module: the per-user persistent computer and the agent that operates it.

See arkos-inspo/specs/COMPUTER_SPEC.md and COMPUTER_AGENT_SPEC.md.

  spike_sandbox.py      Task 0: throwaway e2b proof (not imported by the app)
  sandbox.py            Task 4: SandboxManager, per-user e2b lifecycle
  prompt.py             Task A: system prompt builder (Claude Code scaffolding)
  tools.py              Task B: agent-computer tool schemas + dispatch
  model.py              Task E: ToolCallingModel, native tool-calling client
  agent.py              Task C: ComputerAgent loop
  store.py              Task 6: computer_tasks + events DB helpers
  runner.py             Tasks 6+7: async runner + chat-injection on completion
  computer_router.py    Tasks 8+9: HTTP endpoints (SSE stream, filesystem viewer)
"""
