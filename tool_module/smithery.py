"""
Smithery Connect integration for ArkOS.

All MCP traffic flows through Smithery. We never spawn local MCP subprocesses,
never hold upstream OAuth tokens, and never implement per-provider OAuth flows.
Smithery maintains the OAuth apps and stores credentials on our behalf.

Public surface used by the rest of ArkOS:
  - SmitheryManager       ... drop-in replacement for MCPToolManager
  - AuthRequiredError     ... raised when a user needs to OAuth into a service
      .service            service name (e.g. "linear")
      .setup_url          URL to redirect the user into for Smithery's hosted OAuth
      .state              "auth_required" or "input_required"

Smithery REST endpoints used here
  PUT   {base}/connect/{namespace}/{connection_id}          upsert a connection
  POST  {base}/connect/{namespace}/{connection_id}/mcp      JSON-RPC 2.0 to that connection
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

LocalToolHandler = Callable[[dict[str, Any], str | None], Awaitable[Any]]

logger = logging.getLogger(__name__)

# Network timeout for all Smithery HTTP calls. A hung upstream must not
# block the agent indefinitely; 30s total / 10s connect is generous but bounded.
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AuthRequiredError(Exception):
    """Raised when a tool call can't proceed because the user hasn't completed
    a Smithery-hosted OAuth flow (or hasn't supplied required config) yet."""

    def __init__(
        self,
        service: str,
        user_id: str | None,
        setup_url: str | None = None,
        state: str = "auth_required",
        message: str | None = None,
    ):
        self.service = service
        self.user_id = user_id or "unknown"
        self.setup_url = setup_url
        self.state = state
        self.message = message or (
            f"Please connect {service} to continue"
            if state == "auth_required"
            else f"{service} needs additional configuration"
        )
        # Back-compat with state_tool.py which reads .service_info and .connect_url
        self.service_info = {"name": service}
        self.connect_url = setup_url or ""
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.state,
            "service": self.service,
            "service_name": self.service,
            "setup_url": self.setup_url,
            "connect_url": self.setup_url,
            "message": self.message,
        }


class SmitheryError(RuntimeError):
    """Any non-auth Smithery API failure."""


# ---------------------------------------------------------------------------
# Low level client
# ---------------------------------------------------------------------------


class SmitheryClient:
    """Thin REST client for the Smithery Connect API."""

    def __init__(self, api_key: str, namespace: str, base_url: str = "https://api.smithery.ai"):
        if not api_key:
            raise ValueError("SmitheryClient requires an api_key (set SMITHERY_API_KEY)")
        self.api_key = api_key
        self.namespace = namespace
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def upsert_connection(
        self,
        session: aiohttp.ClientSession,
        connection_id: str,
        mcp_url: str,
        *,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        return_url: str | None = None,
    ) -> dict[str, Any]:
        """
        PUT /connect/{namespace}/{connection_id}

        Returns the parsed JSON body. Callers should inspect .status to decide
        whether the connection is `connected`, `auth_required`, or `input_required`.

        When `return_url` is provided we forward it to Smithery as both the
        top-level `returnUrl` field AND inside metadata, so whichever the
        current Smithery API accepts picks it up. After the user finishes the
        hosted OAuth flow Smithery will redirect them to this URL.
        """
        url = f"{self.base_url}/connect/{self.namespace}/{connection_id}"
        body: dict[str, Any] = {"mcpUrl": mcp_url}
        if name:
            body["name"] = name
        meta = dict(metadata or {})
        if return_url:
            body["returnUrl"] = return_url
            meta.setdefault("returnUrl", return_url)
        if meta:
            body["metadata"] = meta
        if headers:
            body["headers"] = headers

        logger.debug("smithery PUT %s", url)
        async with session.put(url, json=body, headers=self._headers(), timeout=_HTTP_TIMEOUT) as resp:
            text = await resp.text()
            if resp.status == 401:
                # 401 means the user must complete an OAuth / setup flow, not a
                # transient server error. Raise AuthRequiredError so the caller
                # can surface a connect-prompt rather than treating this as a blip.
                raise AuthRequiredError(
                    service=connection_id,
                    user_id=None,
                    message=f"Smithery auth required for {connection_id}: {text}",
                )
            if resp.status >= 400:
                raise SmitheryError(f"upsert_connection {resp.status}: {text}")
            return await resp.json() if text else {}

    async def delete_connection(
        self,
        session: aiohttp.ClientSession,
        connection_id: str,
    ) -> None:
        """
        DELETE /connect/{namespace}/{connection_id}

        Removes the connection from Smithery so a subsequent upsert starts a
        fresh OAuth flow. Safe to call even if the connection doesn't exist
        (404 is silently ignored).
        """
        url = f"{self.base_url}/connect/{self.namespace}/{connection_id}"
        logger.debug("smithery DELETE %s", url)
        async with session.delete(url, headers=self._headers(), timeout=_HTTP_TIMEOUT) as resp:
            if resp.status not in (200, 204, 404):
                text = await resp.text()
                logger.warning("smithery DELETE %s returned %s: %s", url, resp.status, text[:200])

    async def jsonrpc(
        self,
        session: aiohttp.ClientSession,
        connection_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """
        POST /connect/{namespace}/{connection_id}/mcp

        Fires a JSON-RPC 2.0 request at the connection's MCP endpoint.
        Returns the `result` field on success, raises on error.
        """
        url = f"{self.base_url}/connect/{self.namespace}/{connection_id}/mcp"
        rpc_body = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex[:12],
            "method": method,
            "params": params or {},
        }
        headers = self._headers()
        headers["Accept"] = "application/json, text/event-stream"

        logger.debug("smithery POST %s method=%s", url, method)
        async with session.post(url, json=rpc_body, headers=headers, timeout=_HTTP_TIMEOUT) as resp:
            text = await resp.text()
            if resp.status == 401:
                raise AuthRequiredError(
                    service=connection_id,
                    user_id=None,
                    message=f"Smithery auth required for {connection_id}: {text}",
                )
            if resp.status >= 400:
                raise SmitheryError(f"jsonrpc {method} {resp.status}: {text}")

            data = {}
            if text:
                import json

                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    # Fallback for SSE format (text/event-stream)
                    parsed_ok = False
                    for line in text.splitlines():
                        if line.startswith("data: "):
                            try:
                                candidate = json.loads(line[6:])
                                if isinstance(candidate, dict) and ("result" in candidate or "error" in candidate):
                                    data = candidate
                                    parsed_ok = True
                                    break
                            except json.JSONDecodeError:
                                pass
                    if not parsed_ok:
                        raise SmitheryError(
                            f"jsonrpc {method}: could not parse response (not JSON or SSE): {text[:200]}"
                        )
            if "error" in data:
                err = data["error"] or {}
                raise SmitheryError(f"{method} rpc error {err.get('code')}: {err.get('message')}")
            return data.get("result", {})


# ---------------------------------------------------------------------------
# Connection status helpers
# ---------------------------------------------------------------------------


def _parse_status(raw: Any) -> tuple[str, str | None]:
    """
    Smithery's `status` can be either a bare string or an object with `state` and
    `setupUrl`. Normalize to (state, setup_url).
    """
    if raw is None:
        return "unknown", None
    if isinstance(raw, str):
        return raw, None
    if isinstance(raw, dict):
        return raw.get("state", "unknown"), raw.get("setupUrl") or raw.get("authorizationUrl")
    return "unknown", None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class SmitheryManager:
    """
    Replaces the old stdio/HTTP hybrid MCPToolManager. Every server in the
    config is reached via Smithery Connect.

    Config shape expected:

        mcp_servers:
          linear:
            mcp_url: "https://linear.run.tools"
            requires_auth: true
          brave-search:
            mcp_url: "https://brave.run.tools"
            requires_auth: false
            headers:
              braveApiKey: "${BRAVE_API_KEY}"

    Public attributes/methods preserved for the rest of the codebase:
      - clients                (dict, kept empty for compatibility with old code)
      - _tool_registry         tool_name -> server_name
      - initialize_servers()   connects shared (no-auth) servers at startup
      - list_all_tools()       {server: {tool_name: tool_spec}}
      - call_tool(name, args, user_id)
      - get_user_service_status(user_id), get_missing_services(user_id)
    """

    SHARED_PREFIX = "arkos-shared"  # namespace-local id prefix for shared connections

    def __init__(self, servers: dict[str, dict[str, Any]], smithery_config: dict[str, Any]):
        self.servers = servers or {}

        api_key = smithery_config.get("api_key")
        namespace = smithery_config.get("namespace", "arkos")
        base_url = smithery_config.get("base_url", "https://api.smithery.ai")
        self.client = SmitheryClient(api_key=api_key, namespace=namespace, base_url=base_url)

        # kept for back-compat with callers that previously poked at MCPToolManager internals
        self.clients: dict[str, Any] = {}

        # SHARED tools only: tool_name -> server_name. No-auth servers are the
        # same for everyone, so a global map is correct here.
        self._tool_registry: dict[str, str] = {}

        # PER-USER tools: user_id -> {tool_name -> server_name}. Keeps one user's
        # tool routing out of another's (MULTIUSER Task 2 / cross-user fix).
        self._user_tool_registry: dict[str, dict[str, str]] = {}

        # {server_name: [tool_spec, ...]} for no-auth shared servers (seeded at init)
        self._shared_tools: dict[str, list[dict[str, Any]]] = {}

        # {user_id: {server_name: [tool_spec, ...]}} for per-user servers (lazy)
        self._user_tools: dict[str, dict[str, list[dict[str, Any]]]] = {}

        # {user_id: {server_name: setup_url}} for connections waiting on OAuth
        self._pending: dict[str, dict[str, str]] = {}

        # Local (non-Smithery) tools registered in-process. Useful for things
        # like the browser automation tool that talks to Browserless directly
        # over CDP. Shape: {tool_name: {"spec": dict, "handler": coroutine}}.
        self._local_tools: dict[str, dict[str, Any]] = {}

    def register_local_tool(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: LocalToolHandler,
    ) -> None:
        """Register an in-process tool that bypasses Smithery.

        The handler is awaited as `handler(arguments, user_id)`. The tool shows
        up under the synthetic server name "local" in `list_all_tools()`.
        """
        spec = {"name": name, "description": description, "inputSchema": input_schema}
        self._local_tools[name] = {"spec": spec, "handler": handler}
        self._tool_registry[name] = "local"

    # ---------- config helpers ----------

    def _shared_conn_id(self, server_name: str) -> str:
        return f"{self.SHARED_PREFIX}__{server_name}"

    def _user_conn_id(self, user_id: str, server_name: str) -> str:
        safe = user_id.replace(":", "_").replace("/", "_")
        return f"user-{safe}__{server_name}"

    def _requires_auth(self, server_name: str) -> bool:
        spec = self.servers.get(server_name, {})
        return bool(spec.get("requires_auth"))

    # ---------- init + introspection ----------

    async def initialize_servers(self) -> None:
        """Connect every no-auth server so their tools are available globally."""
        if not self.servers:
            logger.info("smithery manager: no mcp_servers configured")
            return

        async with aiohttp.ClientSession() as session:
            for server_name, spec in self.servers.items():
                if self._requires_auth(server_name):
                    logger.info("smithery: deferring per-user server '%s'", server_name)
                    continue

                connection_id = self._shared_conn_id(server_name)
                try:
                    conn = await self.client.upsert_connection(
                        session,
                        connection_id,
                        mcp_url=spec["mcp_url"],
                        name=spec.get("name", server_name),
                        headers=spec.get("headers"),
                    )
                    state, setup_url = _parse_status(conn.get("status"))
                    if state != "connected":
                        logger.warning(
                            "smithery: shared server '%s' came back state=%s, setup=%s",
                            server_name,
                            state,
                            setup_url,
                        )
                        continue

                    tools = await self._fetch_tools(session, connection_id)
                    self._shared_tools[server_name] = tools
                    for tool in tools:
                        tname = tool.get("name")
                        if tname:
                            self._tool_registry[tname] = server_name
                    logger.info(
                        "smithery: shared '%s' connected with %d tools",
                        server_name,
                        len(tools),
                    )

                except Exception as e:
                    logger.error("smithery: failed to init '%s': %s", server_name, e)

    async def _fetch_tools(self, session: aiohttp.ClientSession, connection_id: str) -> list[dict[str, Any]]:
        result = await self.client.jsonrpc(session, connection_id, "tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        return tools

    async def _ensure_user_server(
        self,
        session: aiohttp.ClientSession,
        user_id: str,
        server_name: str,
        *,
        return_url: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Ensure a per-user connection for `server_name` exists and is connected.
        Returns the list of tools for that connection. On auth_required /
        input_required, raises AuthRequiredError with the setup URL.

        `return_url`, when provided, is passed through to Smithery so that the
        user is redirected back to the ark app after completing OAuth.
        """
        spec = self.servers.get(server_name)
        if not spec:
            raise SmitheryError(f"no config for server '{server_name}'")

        connection_id = self._user_conn_id(user_id, server_name)

        conn = await self.client.upsert_connection(
            session,
            connection_id,
            mcp_url=spec["mcp_url"],
            name=spec.get("name", server_name),
            metadata={"userId": user_id},
            headers=spec.get("headers"),
            return_url=return_url,
        )
        state, setup_url = _parse_status(conn.get("status"))

        if state == "connected":
            tools = await self._fetch_tools(session, connection_id)
            self._user_tools.setdefault(user_id, {})[server_name] = tools
            user_reg = self._user_tool_registry.setdefault(user_id, {})
            for tool in tools:
                tname = tool.get("name")
                if tname:
                    user_reg[tname] = server_name
            # clear any stale pending auth (pop from the real nested dict, not a copy)
            if user_id in self._pending:
                self._pending[user_id].pop(server_name, None)
            return tools

        # needs auth or config
        self._pending.setdefault(user_id, {})[server_name] = setup_url or ""
        raise AuthRequiredError(
            service=server_name,
            user_id=user_id,
            setup_url=setup_url,
            state=state,
            message=(
                f"To use {server_name}, open this link to connect it: {setup_url}"
                if setup_url
                else f"{server_name} requires setup but no setupUrl was returned"
            ),
        )

    async def list_all_tools(self, user_id: str | None = None) -> dict[str, dict[str, dict[str, Any]]]:
        """
        {server_name: {tool_name: tool_spec_with_metadata}}

        Always includes shared (no-auth) tools. Includes per-user tools ONLY for
        the given `user_id` -- one user never sees another's connected tools.
        With user_id=None, only shared tools are returned (see UNSAFE_DECISIONS U9
        for the shared system-prompt path that still calls it without a user).
        """
        out: dict[str, dict[str, dict[str, Any]]] = {}

        def pack(server_name: str, tools: list[dict[str, Any]]) -> None:
            server_tools: dict[str, Any] = {}
            for tool in tools:
                tname = tool.get("name")
                if not tname:
                    continue
                enriched = dict(tool)
                enriched["_server"] = server_name
                enriched["_id"] = f"{server_name}.{tname}"
                server_tools[tname] = enriched
            out[server_name] = server_tools

        for server_name, tools in self._shared_tools.items():
            pack(server_name, tools)

        # Locally-registered tools (e.g. browser automation) are shared, not
        # per-user, so they always appear.
        if self._local_tools:
            pack("local", [entry["spec"] for entry in self._local_tools.values()])

        # Per-user tools: only the calling user's. Never union across users.
        if user_id:
            for server_name, tools in (self._user_tools.get(user_id) or {}).items():
                pack(server_name, tools)

        return out

    # ---------- tool execution ----------

    def _resolve_server(self, tool_name: str, user_id: str | None) -> str | None:
        """
        Resolve a tool to its server, scoped to the calling user.

        Checks the user's per-user registry first, then the shared registry.
        A tool another user connected is never visible here (cross-user fix).
        """
        if user_id:
            user_server = self._user_tool_registry.get(user_id, {}).get(tool_name)
            if user_server:
                return user_server
        return self._tool_registry.get(tool_name)  # shared (no-auth) tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user_id: str | None = None,
    ) -> Any:
        # Locally-registered tools (e.g. browser automation) skip Smithery entirely.
        local = self._local_tools.get(tool_name)
        if local is not None:
            return await local["handler"](arguments, user_id)

        server_name = self._resolve_server(tool_name, user_id)

        async with aiohttp.ClientSession() as session:
            # If we don't know where this tool lives yet, it might belong to a
            # per-user server we haven't lazily connected. Try each per-user
            # server until one of them claims the tool.
            if not server_name:
                if not user_id:
                    raise AuthRequiredError(
                        service="unknown",
                        user_id="unknown",
                        message=(
                            f"Tool '{tool_name}' is not registered. User needs to connect a Smithery service first."
                        ),
                    )
                for candidate, spec in self.servers.items():
                    if not spec.get("requires_auth"):
                        continue
                    try:
                        await self._ensure_user_server(session, user_id, candidate)
                    except AuthRequiredError:
                        # Not connected yet; keep scanning other servers.
                        continue
                    except SmitheryError as e:
                        # Network/server error on this candidate; log and skip
                        # rather than aborting the whole tool call.
                        logger.warning("skipping server %s during discovery: %s", candidate, e)
                        continue
                    resolved = self._resolve_server(tool_name, user_id)
                    if resolved:
                        server_name = resolved
                        break

            if not server_name:
                raise ValueError(f"Unknown tool: {tool_name}")

            # Decide which connection id to POST against
            if self._requires_auth(server_name):
                if not user_id:
                    raise AuthRequiredError(service=server_name, user_id=None)
                # guarantees the connection is live (or raises AuthRequiredError)
                await self._ensure_user_server(session, user_id, server_name)
                connection_id = self._user_conn_id(user_id, server_name)
            else:
                connection_id = self._shared_conn_id(server_name)

            result = await self.client.jsonrpc(
                session,
                connection_id,
                "tools/call",
                {"name": tool_name, "arguments": arguments},
            )
            return result

    # ---------- dashboard helpers ----------

    def get_user_service_status(self, user_id: str) -> dict[str, dict[str, Any]]:
        """
        {service_name: {connected: bool, setup_url: str|None, name: str}}
        for every per-user service, so the frontend can render a connections panel.
        """
        status: dict[str, dict[str, Any]] = {}
        user_connected = set((self._user_tools.get(user_id) or {}).keys())
        user_pending = self._pending.get(user_id, {})
        for server_name, spec in self.servers.items():
            if not spec.get("requires_auth"):
                continue
            status[server_name] = {
                "connected": server_name in user_connected,
                "name": spec.get("name", server_name),
                "setup_url": user_pending.get(server_name),
            }
        return status

    def get_missing_services(self, user_id: str) -> list[dict[str, Any]]:
        return [
            {"service": svc, **info}
            for svc, info in self.get_user_service_status(user_id).items()
            if not info["connected"]
        ]

    async def revoke_user_server(self, user_id: str, server_name: str) -> None:
        """Delete the Smithery connection for this user+server and clear local state.

        Unlike the in-memory-only disconnect, this tells Smithery to remove the
        stored token so a subsequent connect() starts a fresh OAuth flow instead
        of hitting a REVOKED/stale token.
        """
        connection_id = self._user_conn_id(user_id, server_name)
        import aiohttp as _aiohttp
        async with _aiohttp.ClientSession() as session:
            await self.client.delete_connection(session, connection_id)

        # Clear local caches
        (self._user_tools.get(user_id) or {}).pop(server_name, None)
        (self._pending.get(user_id) or {}).pop(server_name, None)
        user_reg = self._user_tool_registry.get(user_id)
        if user_reg:
            self._user_tool_registry[user_id] = {
                t: s for t, s in user_reg.items() if s != server_name
            }

    async def shutdown(self) -> None:
        """No persistent sessions to close (each call opens its own ClientSession)."""
        self.clients.clear()
        self._tool_registry.clear()
        self._user_tool_registry.clear()
        self._shared_tools.clear()
        self._user_tools.clear()
        self._pending.clear()
