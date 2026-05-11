# ARKOS — Coding Guidelines

This file is read automatically by Claude Code. When using ChatGPT or another AI
assistant, paste this file as your first message before asking for any code help.

---

## Project at a Glance

ARKOS is a Python async agent framework. FastAPI backend, PostgreSQL + mem0 memory,
MCP tool compatibility, YAML-based state graphs with Pydantic-enforced transitions.

| Module | What it owns |
|---|---|
| `agent_module/` | Agent loop, step(), step_stream(), choose_transition() |
| `state_module/` | State base class, state registry, state graph YAML, all state implementations |
| `tool_module/` | MCP client, tool registry, per-user auth (token_store) |
| `base_module/` | FastAPI app, API endpoints, auth router |
| `config_module/` | YAML config loader |
| `memory_module/` | Short-term + long-term memory (mem0 + PostgreSQL) |
| `model_module/` | LLM client wrapper (ArkModelLink) |
| `logging_module/` | Structured JSON lines logger (LogEvent, emit_log) |

**Stack:** Python 3.11+, FastAPI, PostgreSQL, Pydantic v2, asyncio, ruff, pytest.

---

## Architecture Contracts

These are permanent decisions for this codebase. Violating them silently breaks the
agent loop. Do not work around them — if a rule seems wrong for your use case, raise
it in review before merging.

### 1. State outputs are typed — never raw messages

Every `state.run()` returns a `StateOutput`. Never return a raw `AIMessage`.

```python
# WRONG
async def run(self, context, agent) -> AIMessage:
    return AIMessage(content="done")

# RIGHT
async def run(self, context, agent) -> StateOutput:
    return StateOutput(
        content="done",
        completion_signal="complete",
        structured_data={"key": value},
    )
```

`completion_signal` must be one of: `complete`, `incomplete`, `error`, `needs_input`.
`structured_data` holds anything the next state or the agent loop needs to read.

### 2. Transition gates are deterministic — never LLM calls

`check_transition_ready()` reads `completion_signal` only. No LLM call, no prompt,
no interpretation. A prompt-based gate hallucinates under context pressure.

```python
# WRONG
def check_transition_ready(self, context):
    return call_llm("is this state finished?")

# RIGHT
def check_transition_ready(self, last_output: StateOutput) -> bool:
    return last_output.completion_signal in ("complete", "error", "needs_input")
```

### 3. Every new state must be registered

- Decorate the class with `@register_state`
- Add to `state_module/state_graph.yaml` with `max_iterations` and `failure_state`
- `failure_state` must point to a real state name that exists in the graph

### 4. Per-user tool auth follows one pattern

If a tool needs per-user credentials, add it to `PER_USER_SERVICES` in
`tool_module/tool_call.py` and follow the existing pattern (Google Calendar is
the reference implementation). Do not write a separate auth system.

### 5. Structured logging only — no print()

Use `emit_log()` from `logging_module`. `print()` is for local debugging only
and must be removed before opening a PR.

---

## Adding a New State

1. Create `state_module/states/state_{name}.py`
2. Inherit from `State`, set `type = "{name}"`, decorate with `@register_state`
3. Implement `run()` — returns `StateOutput`, never `AIMessage`
4. Implement `check_transition_ready()` — reads `completion_signal`, no LLM call
5. Add to `state_module/state_graph.yaml`:

```yaml
my_state:
  description: "one sentence — what this state does and what it does not do"
  type: my_state
  max_iterations: 3
  failure_state: error_state
  transition:
    next: [next_state_name]
```

6. Add tests in `tests/test_state_module.py`

---

## Adding a New Tool (MCP)

1. Add server config to `tool_module/mcp_config.json`
2. If the tool needs per-user auth, add to `PER_USER_SERVICES` in `tool_call.py`
3. Follow `token_store.py` for credential storage — do not invent a new pattern
4. Add tests in `tests/test_tool_module.py`, mocking the MCP transport

---

## Code Style

Formatter: **ruff**. Run `ruff check .` before every commit. The CI rejects failures.

### Naming

| Thing | Convention | Example |
|---|---|---|
| Files | `snake_case.py` | `state_tool_call.py` |
| Classes | `PascalCase` | `ToolCallState` |
| Functions / methods | `snake_case` | `check_transition_ready` |
| Constants | `UPPER_SNAKE_CASE` | `MAX_RETRIES` |
| State files | `state_{type}.py` | `state_planning.py` |
| State classes | `State{Type}` | `StatePlanning` |
| Test files | `test_{module_name}.py` | `test_agent_module.py` |

### Type hints

Required on every function signature. No exceptions.

```python
# WRONG
async def run(self, context, agent):

# RIGHT
async def run(self, context: list, agent: Agent) -> StateOutput:
```

### Async

Every function that awaits must be `async def`. Never call `asyncio.run()` inside
application code — only at entrypoints. Never use `time.sleep()` in async code,
use `await asyncio.sleep()`.

### Pydantic

Use Pydantic models for structured data that crosses module boundaries.
Parse LLM output with `model_validate_json()`, not `json.loads()` + manual dict access.
Handle parse failures explicitly — never let a `ValidationError` propagate uncaught
to the agent loop.

---

## Comments

Comment the **why**, not the **what**. If the code is self-explanatory, skip the comment.

**Required:**

- Module docstring at the top of every new file — 3 lines max covering: what it does,
  its key constraint, and what it explicitly does NOT do
- Docstring on every public method where params, return value, or exceptions are non-obvious
- Inline comment on every LLM prompt string explaining what behavior it enforces

**Skip:**

- Simple variable assignments and obvious loops
- Getters, setters, and anything the function name already says clearly

**Never:**

- Commented-out code in a PR — delete it or keep it in a branch
- `# TODO` in a PR — file a Linear issue instead

### Docstring format

```python
async def fetch_items(user_id: str, limit: int = 20) -> list[dict]:
    """
    Retrieve items for a user from the external service.

    Args:
        user_id: Used to load credentials from user_credentials table.
        limit: Max items to return. Keep low — large payloads exceed context window.

    Returns:
        List of dicts with keys: id, title, created_at.

    Raises:
        AuthRequiredError: If no valid token exists for this user.
    """
```

---

## Testing

**One test file per module.** Lives in `tests/`, named `test_{module_name}.py`.
Run with `pytest`. Async tests use `@pytest.mark.asyncio`.

### What to test

- The `completion_signal` value and required `structured_data` keys from `state.run()`
- Every API endpoint: happy path + one meaningful error case
- Every Pydantic model: invalid input raises `ValidationError`
- Auth flows: token missing, token expired, token valid
- Any function with branching logic: one test per branch

### What not to test

- LLM output quality — non-deterministic, always mock the LLM call
- Private methods (`_prefixed`) — test through the public interface
- Third-party library internals (Pydantic, FastAPI routing, MCP transport)
- Live external APIs — mock them without exception

### Test structure

```python
# tests/test_state_module.py

import pytest
from unittest.mock import AsyncMock, MagicMock
from state_module.states.state_my_feature import MyFeatureState
from state_module.base_state import StateOutput


class TestMyFeatureState:
    @pytest.fixture
    def state(self):
        config = {"transition": {"next": ["next_state"]}, "max_iterations": 3}
        return MyFeatureState("my_feature", config)

    @pytest.fixture
    def mock_agent(self):
        agent = MagicMock()
        agent.call_llm = AsyncMock(return_value=MagicMock(
            content='{"key": "value"}'
        ))
        return agent

    @pytest.mark.asyncio
    async def test_returns_complete_on_success(self, state, mock_agent):
        result = await state.run(context=[], agent=mock_agent)
        assert isinstance(result, StateOutput)
        assert result.completion_signal == "complete"
        assert "key" in result.structured_data

    @pytest.mark.asyncio
    async def test_returns_error_on_llm_failure(self, state, mock_agent):
        mock_agent.call_llm = AsyncMock(side_effect=Exception("timeout"))
        result = await state.run(context=[], agent=mock_agent)
        assert result.completion_signal == "error"
        assert result.error_detail is not None
```

**Max 5 tests per state or function.** If you need more, the function does too much — split it.

---

## PR Conventions

### Title

```
[module] verb: short description
```

Keep the description under 60 characters. Module is the primary module being changed.

| Verb | Use when |
|---|---|
| `add` | New file, new feature, new capability |
| `fix` | Correcting broken behavior |
| `refactor` | No behavior change, code quality improvement |
| `remove` | Deleting dead code or a deprecated feature |
| `update` | Changing existing behavior intentionally |
| `test` | Tests only, zero production code change |

```
[agent] fix: replace prompt-based transition gate with signal check
[state] add: planning state as new initial state in graph
[tool] add: per-user OAuth via token_store for new MCP service
[config] update: add max_iterations field to all state YAML configs
[logging] add: structured LogEvent schema and JSON lines writer
[base] fix: task queue status not updating on needs_input signal
```

### Description

```markdown
## What
One sentence.

## Why
One sentence. Link the Linear issue: LINEAR-123

## How
- Key decision or approach (max 4 bullets)
- If you need more than 4, the PR is too large

## Test
What you ran to verify this works.

## Checklist
- [ ] ruff check passes with zero warnings
- [ ] tests/test_{module}.py updated or confirmed unchanged
- [ ] No hardcoded credentials or .env files committed
- [ ] No print() statements left in production paths
- [ ] If adding a state: registered in state_graph.yaml with max_iterations and failure_state
- [ ] If adding a tool: follows token_store.py auth pattern
```

### Size and scope

- Target under 300 lines changed. Hard limit 500 — split if over.
- One concern per PR. A refactor and a feature are two PRs.
- Do not mix agent loop changes with state logic changes in the same PR.

---

## Hard No's

These are rejected in review without discussion.

```python
# Returning raw AIMessage from state.run()
return AIMessage(content="done")

# LLM call inside check_transition_ready()
def check_transition_ready(self, context):
    return self.llm.call("are we done?")

# Bare except — always catch a specific exception
try:
    result = await do_thing()
except:
    pass

# Hardcoded credentials
API_TOKEN = "sk-abc123..."

# Blocking sleep in async code
time.sleep(2)

# Commented-out code in a PR
# old_result = await deprecated_call()

# print() in production paths
print(f"[debug] state ran: {self.name}")

# Functions over 40 lines — extract the logic
# More than 3 levels of nesting — extract to a function
```

---

## Quick Reference — Paste Into Any AI Assistant

Use this block when starting a session with ChatGPT, Gemini, or any tool that
doesn't auto-read this file:

```
This is ARKOS, a Python async agent framework. Rules before writing any code:

1. state.run() returns StateOutput — never AIMessage.
   completion_signal: complete | incomplete | error | needs_input

2. check_transition_ready() reads completion_signal only — never calls the LLM.

3. New states need @register_state and a state_graph.yaml entry with
   max_iterations and failure_state.

4. Per-user tool auth follows token_store.py — do not create a new pattern.

5. Tests: one file per module in tests/, max 5 per function, mock all external
   APIs and all LLM calls.

6. PR titles: [module] verb: description (under 60 chars). ruff must pass.

7. Type hints on every function. async def for anything that awaits.

8. Comment the why, not the what. No commented-out code. No print() in PRs.
```
