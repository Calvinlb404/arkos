"""
Tests for tool_module/smithery_resolver.py (MIT-207).

Test strategy
-------------
The two main tests (Google Calendar, Outlook) mock the Smithery HTTP API so
they run in CI without a live API key. Each test verifies the complete path:
    server_id  ->  qualified_name  ->  API call  ->  inputSchema dict

A third test (smoke/integration, skipped by default) can be run manually
against the real Smithery API once SMITHERY_API_KEY is set:

    pytest tool_module/test_smithery_resolver.py -m integration -v
"""

import pytest
import requests
from unittest.mock import MagicMock, patch

from tool_module.smithery_resolver import (
    SmitheryResolver,
    SmitheryResolverError,
    SERVER_ID_MAP,
    resolve_tool_schema,
    _get_default_resolver,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_module_resolver():
    """
    Reset the module-level singleton resolver before every test so cache
    state from one test does not bleed into another.
    """
    import tool_module.smithery_resolver as mod
    mod._default_resolver = None
    yield
    mod._default_resolver = None


def _mock_response(tools: list, status: int = 200) -> MagicMock:
    """
    Build a mock requests.Response with a JSON body of {"tools": <tools>}.
    """
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.json.return_value = {"tools": tools}
    mock_resp.raise_for_status.return_value = None
    return mock_resp


# ---------------------------------------------------------------------------
# Test 1: Google Calendar — resolve list_events schema
# ---------------------------------------------------------------------------


def test_google_calendar_list_events_schema():
    """
    resolve_tool_schema("google-calendar", "list_events") should return a
    valid JSON Schema dict with type "object" and a non-empty properties map.

    This covers the MIT-207 acceptance test:
        resolve_tool_schema("google-calendar", "list_events") returns a
        valid schema dict without error.

    The Smithery server for google-calendar is "cocal/google-calendar-mcp".
    The mock response mirrors what the real Smithery API returns for this
    server's list_events tool.
    """
    gcal_list_events_schema = {
        "type": "object",
        "properties": {
            "calendarId": {
                "type": "string",
                "description": "The ID of the calendar. Use 'primary' for the user's primary calendar.",
            },
            "timeMin": {
                "type": "string",
                "format": "date-time",
                "description": "Lower bound (inclusive) for an event's end time (RFC 3339).",
            },
            "timeMax": {
                "type": "string",
                "format": "date-time",
                "description": "Upper bound (exclusive) for an event's start time (RFC 3339).",
            },
            "maxResults": {
                "type": "integer",
                "description": "Maximum number of events returned.",
                "default": 10,
            },
            "singleEvents": {
                "type": "boolean",
                "description": "Whether to expand recurring events into instances.",
                "default": True,
            },
        },
        "required": ["calendarId"],
    }

    mock_tools = [
        {
            "name": "list_events",
            "description": "List events from a Google Calendar.",
            "inputSchema": gcal_list_events_schema,
        },
        {
            "name": "create_event",
            "description": "Create a new event in a Google Calendar.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "calendarId": {"type": "string"},
                    "summary": {"type": "string"},
                    "start": {"type": "string", "format": "date-time"},
                    "end": {"type": "string", "format": "date-time"},
                },
                "required": ["calendarId", "summary", "start", "end"],
            },
        },
    ]

    with patch("tool_module.smithery_resolver.requests.get") as mock_get:
        mock_get.return_value = _mock_response(mock_tools)

        schema = resolve_tool_schema("google-calendar", "list_events")

        # --- Verify the API was called with the correct Smithery URL ---
        expected_qualified_name = SERVER_ID_MAP["google-calendar"]  # "cocal/google-calendar-mcp"
        called_url = mock_get.call_args[0][0]
        assert expected_qualified_name in called_url, (
            f"Expected URL to contain '{expected_qualified_name}', got: {called_url}"
        )

        # --- Verify the returned schema is well-formed ---
        assert isinstance(schema, dict), "resolve_tool_schema must return a dict"
        assert schema["type"] == "object", "inputSchema type must be 'object'"
        assert "properties" in schema, "inputSchema must have a 'properties' key"
        assert "calendarId" in schema["properties"], (
            "list_events schema must include 'calendarId' property"
        )
        assert schema == gcal_list_events_schema, (
            "Returned schema must exactly match the Smithery API response"
        )

        # --- Verify caching: a second call must NOT hit the network again ---
        schema_again = resolve_tool_schema("google-calendar", "create_event")
        assert mock_get.call_count == 1, (
            "Second resolve on the same server must use the cache, not re-fetch"
        )
        assert schema_again["type"] == "object"


# ---------------------------------------------------------------------------
# Test 2: Outlook — resolve send_email schema
# ---------------------------------------------------------------------------


def test_outlook_send_email_schema():
    """
    resolve_tool_schema("outlook", "send_email") should return a valid JSON
    Schema dict for the Outlook MCP send_email tool.

    The Smithery server for Outlook is mapped to "loopwork-ai/mcp-outlook"
    via SERVER_ID_MAP. The mock response mirrors what the Smithery API returns
    for the send_email tool on that server.
    """
    outlook_send_email_schema = {
        "type": "object",
        "properties": {
            "to": {
                "type": "array",
                "items": {"type": "string", "format": "email"},
                "description": "List of recipient email addresses.",
            },
            "subject": {
                "type": "string",
                "description": "Subject line of the email.",
            },
            "body": {
                "type": "string",
                "description": "Plain-text or HTML body of the email.",
            },
            "cc": {
                "type": "array",
                "items": {"type": "string", "format": "email"},
                "description": "Optional CC recipients.",
            },
            "importance": {
                "type": "string",
                "enum": ["low", "normal", "high"],
                "description": "Message importance level.",
                "default": "normal",
            },
        },
        "required": ["to", "subject", "body"],
    }

    mock_tools = [
        {
            "name": "send_email",
            "description": "Send an email via Microsoft Outlook / Microsoft 365.",
            "inputSchema": outlook_send_email_schema,
        },
        {
            "name": "list_emails",
            "description": "List emails from an Outlook mailbox folder.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": "Mailbox folder name (e.g. 'Inbox', 'Sent Items').",
                        "default": "Inbox",
                    },
                    "top": {
                        "type": "integer",
                        "description": "Maximum number of messages to return.",
                        "default": 20,
                    },
                },
            },
        },
        {
            "name": "create_calendar_event",
            "description": "Create a calendar event in Outlook Calendar.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "start": {"type": "string", "format": "date-time"},
                    "end": {"type": "string", "format": "date-time"},
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string", "format": "email"},
                    },
                },
                "required": ["subject", "start", "end"],
            },
        },
    ]

    with patch("tool_module.smithery_resolver.requests.get") as mock_get:
        mock_get.return_value = _mock_response(mock_tools)

        schema = resolve_tool_schema("outlook", "send_email")

        # --- Verify the API was called with the correct Smithery URL ---
        expected_qualified_name = SERVER_ID_MAP["outlook"]  # "loopwork-ai/mcp-outlook"
        called_url = mock_get.call_args[0][0]
        assert expected_qualified_name in called_url, (
            f"Expected URL to contain '{expected_qualified_name}', got: {called_url}"
        )

        # --- Verify the returned schema is well-formed ---
        assert isinstance(schema, dict), "resolve_tool_schema must return a dict"
        assert schema["type"] == "object", "inputSchema type must be 'object'"
        assert "properties" in schema, "inputSchema must have a 'properties' key"

        required = schema.get("required", [])
        assert "to" in required, "send_email schema must require 'to'"
        assert "subject" in required, "send_email schema must require 'subject'"
        assert "body" in required, "send_email schema must require 'body'"
        assert schema == outlook_send_email_schema, (
            "Returned schema must exactly match the Smithery API response"
        )

        # --- Also resolve a second tool on the same server to test caching ---
        list_schema = resolve_tool_schema("outlook", "list_emails")
        assert mock_get.call_count == 1, (
            "Second resolve on the same server must use the cache"
        )
        assert list_schema["type"] == "object"
        assert "folder" in list_schema["properties"]


# ---------------------------------------------------------------------------
# Additional unit tests for error handling and helper behaviour
# ---------------------------------------------------------------------------


def test_unknown_tool_raises_error():
    """SmitheryResolverError is raised when the tool does not exist on the server."""
    mock_tools = [{"name": "existing_tool", "inputSchema": {"type": "object", "properties": {}}}]

    with patch("tool_module.smithery_resolver.requests.get") as mock_get:
        mock_get.return_value = _mock_response(mock_tools)

        with pytest.raises(SmitheryResolverError, match="not found"):
            resolve_tool_schema("google-calendar", "nonexistent_tool")


def test_api_http_error_raises_resolver_error():
    """HTTP 4xx/5xx from Smithery is wrapped in SmitheryResolverError."""
    error_response = MagicMock()
    error_response.status_code = 404
    error_response.text = "Server not found"
    http_error = requests.HTTPError(response=error_response)

    with patch("tool_module.smithery_resolver.requests.get") as mock_get:
        mock_get.return_value.raise_for_status.side_effect = http_error

        with pytest.raises(SmitheryResolverError):
            resolve_tool_schema("google-calendar", "list_events")


def test_qualified_name_normalisation():
    """_to_qualified_name handles all three input formats correctly."""
    resolver = SmitheryResolver()

    # ARKOS internal name via SERVER_ID_MAP
    assert resolver._to_qualified_name("google-calendar") == "googlecalendar"
    assert resolver._to_qualified_name("outlook") == "outlook"

    # npm package name with leading @
    assert resolver._to_qualified_name("@cocal/google-calendar-mcp") == "cocal/google-calendar-mcp"

    # Already a qualifiedName — returned as-is
    assert resolver._to_qualified_name("some-org/some-server") == "some-org/some-server"


def test_list_tools_returns_sorted_names():
    """list_tools() returns a sorted list of tool names for a server."""
    mock_tools = [
        {"name": "create_event", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "list_events", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "delete_event", "inputSchema": {"type": "object", "properties": {}}},
    ]

    with patch("tool_module.smithery_resolver.requests.get") as mock_get:
        mock_get.return_value = _mock_response(mock_tools)

        resolver = SmitheryResolver()
        tools = resolver.list_tools("google-calendar")

        assert tools == ["create_event", "delete_event", "list_events"]


def test_clear_cache_forces_refetch():
    """clear_cache() evicts cached data so the next call hits the network."""
    mock_tools = [{"name": "list_events", "inputSchema": {"type": "object", "properties": {}}}]

    with patch("tool_module.smithery_resolver.requests.get") as mock_get:
        mock_get.return_value = _mock_response(mock_tools)

        resolver = SmitheryResolver()
        resolver.resolve_tool_schema("google-calendar", "list_events")
        assert mock_get.call_count == 1

        resolver.clear_cache()
        resolver.resolve_tool_schema("google-calendar", "list_events")
        assert mock_get.call_count == 2, "After clear_cache a new fetch must occur"


# ---------------------------------------------------------------------------
# Integration tests (skipped unless SMITHERY_API_KEY is set)
# ---------------------------------------------------------------------------


# @pytest.mark.integration
@pytest.mark.skipif(
    not __import__("os").environ.get("SMITHERY_API_KEY"),
    reason="SMITHERY_API_KEY not set; skipping live integration test",
)
def test_integration_google_calendar_live():
    """
    Live integration test: calls the real Smithery API for google-calendar.

    Run with:
        SMITHERY_API_KEY=sk-... pytest tool_module/test_smithery_resolver.py \
            -m integration -v
    """
    schema = resolve_tool_schema("google-calendar", "GOOGLECALENDAR_EVENTS_LIST")
    assert isinstance(schema, dict)
    assert schema.get("type") == "object"
    assert "properties" in schema


# @pytest.mark.integration
@pytest.mark.skipif(
    not __import__("os").environ.get("SMITHERY_API_KEY"),
    reason="SMITHERY_API_KEY not set; skipping live integration test",
)
def test_integration_outlook_live():
    """
    Live integration test: calls the real Smithery API for the Outlook server.

    Run with:
        SMITHERY_API_KEY=sk-... pytest tool_module/test_smithery_resolver.py \
            -m integration -v
    """
    resolver = SmitheryResolver()
    tools = resolver.list_tools("outlook")
    assert len(tools) > 0, "Outlook server must expose at least one tool"

    # Resolve schema for the first available tool
    first_tool = tools[0]
    schema = resolver.resolve_tool_schema("outlook", first_tool)
    assert isinstance(schema, dict)
    assert schema.get("type") == "object"
