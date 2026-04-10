"""Tests for transport classes: StdioTransport, HTTPTransport, OAuthManager."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tool_module.transports.base import MCPTransport
from tool_module.transports.http import HTTPTransport, OAuthManager
from tool_module.transports.stdio import StdioTransport

# --- MCPTransport (ABC) ---


class TestMCPTransportABC:
    def test_is_abstract(self):
        with pytest.raises(TypeError):
            MCPTransport()

    def test_subclass_must_implement(self):
        class Incomplete(MCPTransport):
            pass

        with pytest.raises(TypeError):
            Incomplete()


# --- StdioTransport ---


class TestStdioTransport:
    def test_init(self):
        t = StdioTransport(command="npx", args=["-y", "server"], env={"KEY": "val"})
        assert t.command == "npx"
        assert t.args == ["-y", "server"]
        assert t.env == {"KEY": "val"}
        assert t.process is None
        assert t.request_id == 0

    def test_init_default_env(self):
        t = StdioTransport(command="npx", args=[])
        assert t.env == {}

    @pytest.mark.asyncio
    async def test_connect_starts_subprocess(self):
        t = StdioTransport(command="echo", args=["hello"])
        mock_proc = MagicMock()
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            mock_exec.return_value = mock_proc
            await t.connect()
            assert t.process is mock_proc
            mock_exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_failure_raises(self):
        t = StdioTransport(command="nonexistent", args=[])
        with (
            patch(
                "asyncio.create_subprocess_exec",
                new_callable=AsyncMock,
                side_effect=FileNotFoundError("not found"),
            ),
            pytest.raises(RuntimeError, match="connection failed"),
        ):
            await t.connect()

    @pytest.mark.asyncio
    async def test_send_request_not_connected(self):
        t = StdioTransport(command="x", args=[])
        with pytest.raises(RuntimeError, match="not connected"):
            await t.send_request("test", {})

    @pytest.mark.asyncio
    async def test_send_request_success(self):
        t = StdioTransport(command="x", args=[])
        response_data = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline = AsyncMock(return_value=(json.dumps(response_data) + "\n").encode())
        t.process = mock_proc

        result = await t.send_request("tools/list", {})
        assert result == response_data
        mock_proc.stdin.write.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_request_empty_response_raises(self):
        t = StdioTransport(command="x", args=[])
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline = AsyncMock(return_value=b"")
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"some error")
        t.process = mock_proc

        with pytest.raises(RuntimeError, match="closed connection"):
            await t.send_request("test", {})

    @pytest.mark.asyncio
    async def test_send_request_increments_id(self):
        t = StdioTransport(command="x", args=[])
        response = {"jsonrpc": "2.0", "id": 1, "result": {}}

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline = AsyncMock(return_value=(json.dumps(response) + "\n").encode())
        t.process = mock_proc

        await t.send_request("method1", {})
        assert t.request_id == 1
        await t.send_request("method2", {})
        assert t.request_id == 2

    @pytest.mark.asyncio
    async def test_send_notification_not_connected(self):
        t = StdioTransport(command="x", args=[])
        with pytest.raises(RuntimeError, match="not connected"):
            await t.send_notification("test", {})

    @pytest.mark.asyncio
    async def test_send_notification_success(self):
        t = StdioTransport(command="x", args=[])
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        t.process = mock_proc

        await t.send_notification("initialized", {})
        mock_proc.stdin.write.assert_called_once()
        # Verify it's a notification (no "id" field)
        sent = json.loads(mock_proc.stdin.write.call_args[0][0].decode())
        assert "id" not in sent
        assert sent["method"] == "initialized"

    @pytest.mark.asyncio
    async def test_close_no_process(self):
        t = StdioTransport(command="x", args=[])
        await t.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_close_terminates_process(self):
        t = StdioTransport(command="x", args=[])
        mock_proc = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock()
        t.process = mock_proc

        with patch("asyncio.wait_for", new_callable=AsyncMock):
            await t.close()
        assert t.process is None


# --- HTTPTransport ---


class TestHTTPTransport:
    def test_init_no_auth(self):
        t = HTTPTransport(url="https://example.com/mcp")
        assert t.url == "https://example.com/mcp"
        assert t.access_token is None
        assert t.oauth_manager is None

    def test_init_bearer_auth(self):
        t = HTTPTransport(
            url="https://example.com/mcp",
            auth_config={"type": "bearer", "token": "abc123"},
        )
        assert t.access_token == "abc123"
        assert t.oauth_manager is None

    def test_init_oauth_auth(self):
        t = HTTPTransport(
            url="https://example.com/mcp",
            auth_config={
                "type": "oauth",
                "client_id": "my_client",
                "scopes": ["read", "write"],
            },
        )
        assert t.oauth_manager is not None
        assert isinstance(t.oauth_manager, OAuthManager)

    @pytest.mark.asyncio
    async def test_connect_creates_session(self):
        t = HTTPTransport(url="https://example.com/mcp")
        await t.connect()
        assert t.session is not None
        await t.close()

    @pytest.mark.asyncio
    async def test_send_request_not_connected(self):
        t = HTTPTransport(url="https://example.com/mcp")
        with pytest.raises(RuntimeError, match="not connected"):
            await t.send_request("test", {})

    @pytest.mark.asyncio
    async def test_send_notification_not_connected(self):
        t = HTTPTransport(url="https://example.com/mcp")
        with pytest.raises(RuntimeError, match="not connected"):
            await t.send_notification("test", {})

    @pytest.mark.asyncio
    async def test_close(self):
        t = HTTPTransport(url="https://example.com/mcp")
        await t.connect()
        assert t.session is not None
        await t.close()
        assert t.session is None

    @pytest.mark.asyncio
    async def test_close_no_session(self):
        t = HTTPTransport(url="https://example.com/mcp")
        await t.close()  # Should not raise


# --- OAuthManager ---


class TestOAuthManager:
    def test_init(self):
        mgr = OAuthManager(
            server_url="https://mcp.example.com",
            client_id="my_client",
            scopes=["read", "write"],
        )
        assert mgr.server_url == "https://mcp.example.com"
        assert mgr.client_id == "my_client"
        assert mgr.scopes == ["read", "write"]
        assert mgr.redirect_uri == "http://localhost:8765/callback"

    def test_generate_code_verifier(self):
        mgr = OAuthManager("https://x.com", "client", [])
        verifier = mgr._generate_code_verifier()
        assert isinstance(verifier, str)
        assert len(verifier) > 20
        # Should be URL-safe base64 (no padding)
        assert "=" not in verifier

    def test_generate_code_verifier_unique(self):
        mgr = OAuthManager("https://x.com", "client", [])
        v1 = mgr._generate_code_verifier()
        v2 = mgr._generate_code_verifier()
        assert v1 != v2

    def test_generate_code_challenge(self):
        mgr = OAuthManager("https://x.com", "client", [])
        verifier = mgr._generate_code_verifier()
        challenge = mgr._generate_code_challenge(verifier)
        assert isinstance(challenge, str)
        assert len(challenge) > 20
        assert "=" not in challenge
        # Challenge should differ from verifier
        assert challenge != verifier

    def test_generate_code_challenge_deterministic(self):
        mgr = OAuthManager("https://x.com", "client", [])
        verifier = "test_verifier_string"
        c1 = mgr._generate_code_challenge(verifier)
        c2 = mgr._generate_code_challenge(verifier)
        assert c1 == c2

    def test_is_token_expired_always_false(self):
        mgr = OAuthManager("https://x.com", "client", [])
        assert mgr._is_token_expired({"access_token": "abc"}) is False

    def test_load_cached_token_no_file(self, tmp_path):
        mgr = OAuthManager("https://x.com", "client", [])
        mgr.token_cache_file = tmp_path / "nonexistent.json"
        assert mgr._load_cached_token() is None

    def test_load_and_save_cached_token(self, tmp_path):
        mgr = OAuthManager("https://x.com", "client", [])
        mgr.token_cache_file = tmp_path / "tokens.json"

        tokens = {"access_token": "abc", "refresh_token": "xyz"}
        mgr._save_tokens(tokens)
        assert mgr.token_cache_file.exists()

        loaded = mgr._load_cached_token()
        assert loaded["access_token"] == "abc"
        assert loaded["refresh_token"] == "xyz"

    def test_save_tokens_sets_permissions(self, tmp_path):
        mgr = OAuthManager("https://x.com", "client", [])
        mgr.token_cache_file = tmp_path / "tokens.json"
        mgr._save_tokens({"access_token": "secret"})
        assert oct(mgr.token_cache_file.stat().st_mode & 0o777) == "0o600"
