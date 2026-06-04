"""
Unit tests for computer_module. All tests mock the sandbox, model, and DB
so they run without e2b or a live LLM.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from computer_module.prompt import build_system_prompt
from computer_module.tools import ToolContext, _dispatch


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------

class TestBuildSystemPrompt:
    def test_includes_env_context(self):
        p = build_system_prompt("/work", "alice", "Monday", "- run_command: run")
        assert "/work" in p
        assert "alice" in p
        assert "Monday" in p

    def test_includes_tool_inventory(self):
        p = build_system_prompt("/work", "alice", "Monday", "- run_command: run a command")
        assert "run_command" in p

    def test_includes_verify_discipline(self):
        p = build_system_prompt("/work", "alice", "Monday", "")
        assert "VERIFY" in p or "verify" in p.lower()

    def test_includes_read_before_edit(self):
        p = build_system_prompt("/work", "alice", "Monday", "")
        assert "edit" in p.lower()
        assert "read" in p.lower()


# ---------------------------------------------------------------------------
# tools -- dispatch
# ---------------------------------------------------------------------------

def _make_ctx(user_id="u1") -> ToolContext:
    sbx = MagicMock()
    sbx.exec = AsyncMock(return_value={"stdout": "hi\n", "stderr": "", "exit_code": 0})
    sbx.read_file = AsyncMock(return_value="line1\nline2\nline3")
    sbx.write_file = AsyncMock()
    sbx.list_dir = AsyncMock(return_value=[
        {"name": "foo.py", "is_dir": False, "size": 10},
    ])
    return ToolContext(user_id=user_id, sandbox=sbx, emit=lambda e: None)


class TestDispatchRunCommand:
    @pytest.mark.asyncio
    async def test_returns_stdout(self):
        ctx = _make_ctx()
        result = await _dispatch("run_command", {"command": "echo hi"}, ctx)
        assert "hi" in result
        assert "(exit 0)" in result

    @pytest.mark.asyncio
    async def test_includes_stderr(self):
        ctx = _make_ctx()
        ctx.sandbox.exec = AsyncMock(return_value={"stdout": "", "stderr": "err", "exit_code": 1})
        result = await _dispatch("run_command", {"command": "bad"}, ctx)
        assert "err" in result


class TestDispatchReadFile:
    @pytest.mark.asyncio
    async def test_returns_numbered_lines(self):
        ctx = _make_ctx()
        result = await _dispatch("read_file", {"path": "/f.py"}, ctx)
        assert "1\t" in result
        assert "/f.py" in ctx.read_files

    @pytest.mark.asyncio
    async def test_offset_and_limit(self):
        ctx = _make_ctx()
        ctx.sandbox.read_file = AsyncMock(return_value="a\nb\nc\nd\ne")
        result = await _dispatch("read_file", {"path": "/f.py", "offset": 2, "limit": 2}, ctx)
        # Should return lines 2-3 (b, c)
        assert "b" in result
        assert "c" in result

    @pytest.mark.asyncio
    async def test_marks_file_as_read(self):
        ctx = _make_ctx()
        await _dispatch("read_file", {"path": "/f.py"}, ctx)
        assert "/f.py" in ctx.read_files


class TestDispatchEditFile:
    @pytest.mark.asyncio
    async def test_requires_prior_read(self):
        ctx = _make_ctx()
        # No prior read of this path.
        result = await _dispatch("edit_file", {"path": "/f.py", "old_string": "x", "new_string": "y"}, ctx)
        assert "read" in result.lower()
        ctx.sandbox.write_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_nonunique_old_string(self):
        ctx = _make_ctx()
        ctx.read_files.add("/f.py")
        ctx.sandbox.read_file = AsyncMock(return_value="foo\nfoo\n")
        result = await _dispatch("edit_file", {"path": "/f.py", "old_string": "foo", "new_string": "bar"}, ctx)
        assert "unique" in result.lower() or "not unique" in result.lower()
        ctx.sandbox.write_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_succeeds_after_read_unique(self):
        ctx = _make_ctx()
        ctx.read_files.add("/f.py")
        ctx.sandbox.read_file = AsyncMock(return_value="hello world")
        result = await _dispatch("edit_file", {"path": "/f.py", "old_string": "world", "new_string": "earth"}, ctx)
        assert "Edited" in result
        ctx.sandbox.write_file.assert_called_once()
        written_content = ctx.sandbox.write_file.call_args[0][2]
        assert written_content == "hello earth"

    @pytest.mark.asyncio
    async def test_replace_all_replaces_every_occurrence(self):
        ctx = _make_ctx()
        ctx.read_files.add("/f.py")
        ctx.sandbox.read_file = AsyncMock(return_value="foo foo foo")
        result = await _dispatch("edit_file",
            {"path": "/f.py", "old_string": "foo", "new_string": "bar", "replace_all": True}, ctx)
        written = ctx.sandbox.write_file.call_args[0][2]
        assert written == "bar bar bar"
        assert "Edited" in result

    @pytest.mark.asyncio
    async def test_old_string_not_found(self):
        ctx = _make_ctx()
        ctx.read_files.add("/f.py")
        ctx.sandbox.read_file = AsyncMock(return_value="hello world")
        result = await _dispatch("edit_file", {"path": "/f.py", "old_string": "xyz", "new_string": "abc"}, ctx)
        assert "not found" in result.lower()
        ctx.sandbox.write_file.assert_not_called()


class TestDispatchWriteFile:
    @pytest.mark.asyncio
    async def test_writes_and_marks_read(self):
        ctx = _make_ctx()
        result = await _dispatch("write_file", {"path": "/n.py", "content": "hello"}, ctx)
        assert "Wrote" in result
        ctx.sandbox.write_file.assert_called_once_with("u1", "/n.py", "hello")
        assert "/n.py" in ctx.read_files


class TestDispatchListDir:
    @pytest.mark.asyncio
    async def test_returns_entries(self):
        ctx = _make_ctx()
        result = await _dispatch("list_dir", {}, ctx)
        assert "foo.py" in result


class TestDispatchGrep:
    @pytest.mark.asyncio
    async def test_calls_grep_command(self):
        ctx = _make_ctx()
        ctx.sandbox.exec = AsyncMock(return_value={
            "stdout": "/f.py:1: hello world", "stderr": "", "exit_code": 0})
        result = await _dispatch("grep", {"pattern": "hello"}, ctx)
        assert "/f.py" in result
        cmd = ctx.sandbox.exec.call_args[0][1]
        assert "grep" in cmd
        assert "hello" in cmd

    @pytest.mark.asyncio
    async def test_no_matches(self):
        ctx = _make_ctx()
        ctx.sandbox.exec = AsyncMock(return_value={"stdout": "", "stderr": "", "exit_code": 1})
        result = await _dispatch("grep", {"pattern": "xyz"}, ctx)
        assert "no matches" in result.lower()


class TestDispatchTodoWrite:
    @pytest.mark.asyncio
    async def test_updates_todos(self):
        ctx = _make_ctx()
        items = [{"step": "write file", "status": "done"}, {"step": "run it", "status": "pending"}]
        result = await _dispatch("todo_write", {"items": items}, ctx)
        assert "write file" in result
        assert ctx.todos == items


class TestDispatchErrorHandling:
    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_string(self):
        ctx = _make_ctx()
        from computer_module.tools import dispatch
        result = await dispatch("nonexistent_tool", {}, ctx)
        assert "ERROR" in result or "unknown" in result.lower()

    @pytest.mark.asyncio
    async def test_sandbox_error_returns_error_string(self):
        ctx = _make_ctx()
        ctx.sandbox.exec = AsyncMock(side_effect=Exception("sandbox exploded"))
        from computer_module.tools import dispatch
        result = await dispatch("run_command", {"command": "echo hi"}, ctx)
        assert "ERROR" in result


# ---------------------------------------------------------------------------
# agent -- loop behaviour
# ---------------------------------------------------------------------------

def _make_agent():
    from computer_module.agent import ComputerAgent
    sandbox = MagicMock()
    sandbox.get_or_create = AsyncMock(return_value=MagicMock(sandbox_id="sbx1"))
    sandbox.pause = AsyncMock()
    agent = ComputerAgent(user_id="u1", sandbox=sandbox, tool_manager=None)
    return agent


class TestComputerAgentLoop:
    @pytest.mark.asyncio
    async def test_finishes_when_model_returns_no_tool_calls(self):
        agent = _make_agent()
        final_msg = MagicMock()
        final_msg.tool_calls = []
        final_msg.content = "All done."
        agent.model = MagicMock()
        agent.model.call = AsyncMock(return_value=final_msg)
        result = await agent.run("do something")
        assert result["status"] == "completed"
        assert result["summary"] == "All done."

    @pytest.mark.asyncio
    async def test_step_cap_returns_step_cap_reached(self):
        agent = _make_agent()
        # Model always returns a tool call so it never finishes naturally.
        tc = MagicMock()
        tc.id = "call_1"
        tc.function.name = "run_command"
        tc.function.arguments = '{"command":"echo hi"}'
        tool_call_msg = MagicMock()
        tool_call_msg.tool_calls = [tc]
        tool_call_msg.content = None
        agent.model = MagicMock()
        agent.model.call = AsyncMock(return_value=tool_call_msg)
        # Mock the sandbox exec so tool calls complete quickly
        agent.sandbox.exec = AsyncMock(return_value={"stdout": "hi", "stderr": "", "exit_code": 0})
        result = await agent.run("loop forever", step_cap=3)
        assert result["status"] == "step_cap_reached"
        assert agent.model.call.call_count == 3

    @pytest.mark.asyncio
    async def test_model_error_returns_failed(self):
        from model_module.errors import ModelError
        agent = _make_agent()
        agent.model = MagicMock()
        agent.model.call = AsyncMock(side_effect=ModelError("down", retryable=False))
        result = await agent.run("do something")
        assert result["status"] == "failed"
        assert "model" in result["summary"].lower()

    @pytest.mark.asyncio
    async def test_tracks_file_outputs(self):
        agent = _make_agent()
        # Step 0: write_file tool call
        tc = MagicMock()
        tc.id = "call_w"
        tc.function.name = "write_file"
        tc.function.arguments = '{"path":"/home/user/out.py","content":"x"}'
        write_msg = MagicMock(); write_msg.tool_calls = [tc]; write_msg.content = None
        # Step 1: finish
        done_msg = MagicMock(); done_msg.tool_calls = []; done_msg.content = "Done."
        agent.model = MagicMock()
        agent.model.call = AsyncMock(side_effect=[write_msg, done_msg])
        agent.sandbox.write_file = AsyncMock()
        result = await agent.run("write a file")
        assert "/home/user/out.py" in result["outputs"]

    @pytest.mark.asyncio
    async def test_emits_events(self):
        agent = _make_agent()
        events = []
        agent._emit = lambda e: events.append(e)
        tc = MagicMock()
        tc.id = "c1"; tc.function.name = "run_command"
        tc.function.arguments = '{"command":"echo hi"}'
        tool_msg = MagicMock(); tool_msg.tool_calls = [tc]; tool_msg.content = None
        done_msg = MagicMock(); done_msg.tool_calls = []; done_msg.content = "done"
        agent.model = MagicMock()
        agent.model.call = AsyncMock(side_effect=[tool_msg, done_msg])
        agent.sandbox.exec = AsyncMock(return_value={"stdout": "hi", "stderr": "", "exit_code": 0})
        await agent.run("run something")
        kinds = [e.get("kind") for e in events]
        assert "start" in kinds
        assert "completed" in kinds
        assert "shell" in kinds


# ---------------------------------------------------------------------------
# model client
# ---------------------------------------------------------------------------

class TestToolCallingModel:
    @pytest.mark.asyncio
    async def test_raises_model_error_on_server_failure(self):
        from computer_module.model import ToolCallingModel
        from model_module.errors import ModelError
        from openai import InternalServerError
        model = ToolCallingModel()

        mock_resp = MagicMock(); mock_resp.status_code = 500; mock_resp.headers = {}
        err = InternalServerError("fail", response=mock_resp, body=None)

        with patch("computer_module.model.AsyncOpenAI") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.chat.completions.create = AsyncMock(side_effect=err)
            mock_cls.return_value = mock_instance
            with pytest.raises(ModelError) as exc:
                await model.call(
                    [{"role": "user", "content": "hi"}],
                    [{"type": "function", "function": {"name": "t", "description": "d",
                      "parameters": {"type": "object", "properties": {}}}}]
                )
            assert exc.value.retryable is True

    @pytest.mark.asyncio
    async def test_returns_message_on_success(self):
        from computer_module.model import ToolCallingModel
        model = ToolCallingModel()
        mock_msg = MagicMock()
        mock_msg.tool_calls = []
        mock_msg.content = "hello"
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=mock_msg)]

        with patch("computer_module.model.AsyncOpenAI") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.chat.completions.create = AsyncMock(return_value=mock_resp)
            mock_cls.return_value = mock_instance
            result = await model.call([{"role": "user", "content": "hi"}], [])
        assert result.content == "hello"


# ---------------------------------------------------------------------------
# store -- unit tests (no DB; mock psycopg2)
# ---------------------------------------------------------------------------

class TestComputerStore:
    def test_create_returns_uuid(self):
        from computer_module.store import create_computer_task
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__enter__ = MagicMock(return_value=mock_cur)
        mock_cur.__exit__ = MagicMock(return_value=False)
        import uuid
        mock_cur.fetchone.return_value = (uuid.UUID("12345678-1234-5678-1234-567812345678"),)
        mock_conn.cursor.return_value = mock_cur
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)

        with patch("computer_module.store._connect", return_value=mock_conn):
            task_id = create_computer_task("u1", "sess1", "do something")
        assert task_id == "12345678-1234-5678-1234-567812345678"

    def test_get_returns_none_for_wrong_user(self):
        from computer_module.store import get_computer_task
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.__enter__ = MagicMock(return_value=mock_cur)
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone.return_value = None  # no row = not found / wrong user
        mock_conn.cursor.return_value = mock_cur

        with patch("computer_module.store._connect", return_value=mock_conn):
            result = get_computer_task("some-task-id", "wrong-user")
        assert result is None


# ---------------------------------------------------------------------------
# router -- endpoints (mock sandbox + store)
# ---------------------------------------------------------------------------

class TestComputerRouter:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from computer_module.computer_router import router
        from base_module.jwt_utils import issue_token
        app = FastAPI()
        app.include_router(router)
        return TestClient(app), issue_token("test-user-id", "testuser")

    def test_list_files_returns_entries(self, client):
        tc, token = client
        fake_entries = [{"name": "hello.py", "is_dir": False, "size": 10}]
        with patch("computer_module.computer_router.sandbox_manager") as mock_sbx:
            mock_sbx.list_dir = AsyncMock(return_value=fake_entries)
            r = tc.get("/computer/files?path=/home/user",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["entries"][0]["name"] == "hello.py"

    def test_read_file_returns_content(self, client):
        tc, token = client
        with patch("computer_module.computer_router.sandbox_manager") as mock_sbx:
            mock_sbx.read_file = AsyncMock(return_value="print('hello')")
            r = tc.get("/computer/file?path=/home/user/hi.py",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["content"] == "print('hello')"
        assert r.json()["truncated"] is False

    def test_read_file_truncates_large_content(self, client):
        tc, token = client
        big = "x" * 60_000
        with patch("computer_module.computer_router.sandbox_manager") as mock_sbx:
            mock_sbx.read_file = AsyncMock(return_value=big)
            r = tc.get("/computer/file?path=/home/user/big.py",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["truncated"] is True
        assert len(r.json()["content"]) == 50_000

    def test_get_task_returns_404_for_wrong_user(self, client):
        tc, token = client
        with patch("computer_module.computer_router.get_computer_task", return_value=None):
            r = tc.get("/computer/tasks/fake-task-id",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 404

    def test_list_tasks_returns_empty(self, client):
        tc, token = client
        with patch("computer_module.computer_router.list_computer_tasks", return_value=[]):
            r = tc.get("/computer/tasks",
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["tasks"] == []
