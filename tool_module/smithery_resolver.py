"""
Smithery tool schema resolver for MCP calls.

Queries the Smithery registry REST API (https://api.smithery.ai) to resolve
argument schemas for a given MCP server's tools at runtime. This removes the
need to hardcode tool schemas and ensures the agent always works with the
current schema for each tool.

Usage
-----
Simple module-level call (most common):

    from tool_module.smithery_resolver import resolve_tool_schema

    schema = resolve_tool_schema("google-calendar", "list_events")
    # Returns: {"type": "object", "properties": {...}}

Class-based usage (for cache control or custom API keys):

    from tool_module.smithery_resolver import SmitheryResolver

    resolver = SmitheryResolver(api_key="sk-...")
    schema = resolver.resolve_tool_schema("outlook", "send_email")
    resolver.clear_cache()

Server ID formats accepted
--------------------------
All three of the following refer to the same server:
    - ARKOS internal name:  "google-calendar"
    - Smithery qualifiedName: "cocal/google-calendar-mcp"
    - npm package name:       "@cocal/google-calendar-mcp"
"""

import logging
import os
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# Smithery public API base URL
SMITHERY_API_BASE = "https://api.smithery.ai"

# Maps ARKOS internal server names to Smithery qualifiedNames.
# qualifiedName format is "namespace/server-name" (no leading @).
# Add new entries here when new MCP servers are configured.
SERVER_ID_MAP: Dict[str, str] = {
    "google-calendar": "cocal/google-calendar-mcp",
    "brave-search": "brave/brave-search-mcp-server",
    "filesystem": "modelcontextprotocol/server-filesystem",
    "outlook": "loopwork-ai/mcp-outlook",
}


class SmitheryResolverError(Exception):
    """
    Raised when tool schema resolution fails.

    Covers API errors, network failures, unknown servers, and missing tools.
    """


class SmitheryResolver:
    """
    Resolves MCP tool argument schemas from the Smithery registry.

    Fetches tool definitions (including inputSchema) for any registered MCP
    server via the Smithery REST API. Results are cached per server so
    repeated calls for tools on the same server cost only one HTTP request.

    Parameters
    ----------
    api_key : Optional[str]
        Smithery API key. Falls back to the ``SMITHERY_API_KEY`` environment
        variable. Unauthenticated requests are allowed for public servers but
        may be rate-limited.

    Examples
    --------
    >>> resolver = SmitheryResolver()
    >>> schema = resolver.resolve_tool_schema("google-calendar", "list_events")
    >>> schema["type"]
    'object'
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key: str = api_key or os.environ.get("SMITHERY_API_KEY", "")
        # Cache: qualified_name -> {tool_name: tool_dict}
        self._cache: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _to_qualified_name(self, server_id: str) -> str:
        """
        Normalise a server identifier to a Smithery qualifiedName.

        Priority:
        1. Look up in SERVER_ID_MAP (handles ARKOS internal names).
        2. Strip a leading ``@`` from npm-style names.
        3. Return as-is (assumes it is already a qualifiedName).

        Parameters
        ----------
        server_id : str
            ARKOS internal name, npm package name, or qualifiedName.

        Returns
        -------
        str
            Smithery qualifiedName (e.g. ``"cocal/google-calendar-mcp"``).
        """
        if server_id in SERVER_ID_MAP:
            return SERVER_ID_MAP[server_id]
        if server_id.startswith("@"):
            return server_id[1:]
        return server_id

    def _fetch_tools_for_server(self, qualified_name: str) -> Dict[str, Any]:
        """
        Call the Smithery API and return a dict of tool_name -> tool_dict.

        Parameters
        ----------
        qualified_name : str
            Smithery qualifiedName with the slash NOT percent-encoded
            (requests handles encoding internally).

        Returns
        -------
        Dict[str, Any]
            Mapping of tool name to the full tool object from the API.

        Raises
        ------
        SmitheryResolverError
            On HTTP errors or unexpected response shapes.
        """
        url = f"{SMITHERY_API_BASE}/servers/{qualified_name}"
        logger.info("Fetching tool schemas from Smithery: %s", url)

        try:
            response = requests.get(url, headers=self._headers(), timeout=10.0)
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise SmitheryResolverError(
                f"Smithery API returned {exc.response.status_code} for "
                f"server '{qualified_name}': {exc.response.text}"
            ) from exc
        except requests.RequestException as exc:
            raise SmitheryResolverError(
                f"Network error fetching schemas for '{qualified_name}': {exc}"
            ) from exc

        data = response.json()
        tools: List[Dict[str, Any]] = data.get("tools", [])

        if not isinstance(tools, list):
            raise SmitheryResolverError(
                f"Unexpected response shape from Smithery for '{qualified_name}': "
                f"'tools' field is not a list"
            )

        tool_map = {t["name"]: t for t in tools if "name" in t}
        logger.debug(
            "Cached %d tools for server '%s': %s",
            len(tool_map),
            qualified_name,
            list(tool_map.keys()),
        )
        return tool_map

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_tool_schema(self, server_id: str, tool_name: str) -> Dict[str, Any]:
        """
        Return the JSON Schema for a tool's input arguments.

        Queries the Smithery registry for the server, then extracts the
        ``inputSchema`` for the named tool. Server results are cached so that
        resolving multiple tools on the same server only makes one HTTP call.

        Parameters
        ----------
        server_id : str
            Server identifier. Accepts:

            * ARKOS internal name  – ``"google-calendar"``, ``"outlook"``
            * Smithery qualifiedName – ``"cocal/google-calendar-mcp"``
            * npm package name – ``"@cocal/google-calendar-mcp"``

        tool_name : str
            Exact name of the tool (e.g. ``"list_events"``, ``"send_email"``).

        Returns
        -------
        Dict[str, Any]
            JSON Schema object for the tool's input, e.g.::

                {
                    "type": "object",
                    "properties": {
                        "calendarId": {"type": "string"},
                        "timeMin":    {"type": "string", "format": "date-time"},
                    },
                    "required": ["calendarId"],
                }

        Raises
        ------
        SmitheryResolverError
            If the server cannot be reached, the tool is not found on that
            server, or the tool has no ``inputSchema``.

        Examples
        --------
        >>> resolver = SmitheryResolver()
        >>> schema = resolver.resolve_tool_schema("google-calendar", "list_events")
        >>> isinstance(schema, dict)
        True
        >>> schema["type"]
        'object'
        """
        qualified_name = self._to_qualified_name(server_id)

        # Use cached tool map if available, otherwise fetch from API
        if qualified_name not in self._cache:
            self._cache[qualified_name] = self._fetch_tools_for_server(qualified_name)

        tool_map = self._cache[qualified_name]

        if tool_name not in tool_map:
            available = sorted(tool_map.keys())
            raise SmitheryResolverError(
                f"Tool '{tool_name}' not found on Smithery server '{qualified_name}'. "
                f"Available tools: {available}"
            )

        input_schema = tool_map[tool_name].get("inputSchema")
        if input_schema is None:
            raise SmitheryResolverError(
                f"Tool '{tool_name}' on server '{qualified_name}' has no inputSchema "
                f"in the Smithery registry."
            )

        return input_schema

    def list_tools(self, server_id: str) -> List[str]:
        """
        Return a list of tool names available on a server.

        Useful for discovery before calling resolve_tool_schema.

        Parameters
        ----------
        server_id : str
            Server identifier (ARKOS name, qualifiedName, or npm package name).

        Returns
        -------
        List[str]
            Sorted list of tool names registered on this server.
        """
        qualified_name = self._to_qualified_name(server_id)
        if qualified_name not in self._cache:
            self._cache[qualified_name] = self._fetch_tools_for_server(qualified_name)
        return sorted(self._cache[qualified_name].keys())

    def clear_cache(self) -> None:
        """
        Evict all cached server tool maps.

        Call this to force a fresh API fetch on the next resolve, for example
        if you know a server has been updated on Smithery.
        """
        self._cache.clear()
        logger.debug("Smithery schema cache cleared")


# ---------------------------------------------------------------------------
# Module-level convenience API
# ---------------------------------------------------------------------------

_default_resolver: Optional[SmitheryResolver] = None


def _get_default_resolver() -> SmitheryResolver:
    """Return (and lazily create) the module-level singleton resolver."""
    global _default_resolver
    if _default_resolver is None:
        _default_resolver = SmitheryResolver()
    return _default_resolver


def resolve_tool_schema(server_id: str, tool_name: str) -> Dict[str, Any]:
    """
    Resolve the argument schema for a tool from the Smithery registry.

    This is the primary public interface for MIT-207. It uses a module-level
    singleton resolver with an in-memory cache so repeated calls for tools on
    the same server are free after the first network round trip.

    Parameters
    ----------
    server_id : str
        Server identifier. Accepts ARKOS internal names (``"google-calendar"``,
        ``"outlook"``), Smithery qualifiedNames, or npm package names.
    tool_name : str
        Exact name of the tool to resolve.

    Returns
    -------
    Dict[str, Any]
        JSON Schema for the tool's input arguments.

    Raises
    ------
    SmitheryResolverError
        If the server or tool cannot be found, or the API request fails.

    Examples
    --------
    Acceptance test from MIT-207:

    >>> schema = resolve_tool_schema("google-calendar", "list_events")
    >>> schema["type"]
    'object'

    Outlook example:

    >>> schema = resolve_tool_schema("outlook", "send_email")
    >>> "properties" in schema
    True
    """
    return _get_default_resolver().resolve_tool_schema(server_id, tool_name)
