"""Tests for the FastMCP server skeleton and Phase 0 admin tools.

Asserts the server boots, registers both admin tools with structured
``outputSchema``, and that tool outputs validate against the declared Pydantic v2
models in :mod:`trader_mcp.server.schemas`. Typed-I/O is the Phase 0 contract;
``qa-parity-engineer`` extends this when the tool surface grows in Phase 1+.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from trader_mcp import __version__
from trader_mcp.server.app import SERVER_NAME, TRANSPORT, build_app
from trader_mcp.server.schemas import HealthCheckResult, ServerStatusResult


def _structured(result: Any) -> dict[str, Any]:
    """Extract the structured-output dict from a FastMCP ``call_tool`` result.

    FastMCP returns ``(unstructured_content, structured_dict)`` for tools with
    structured output; this isolates the typed dict for assertions.
    """
    assert isinstance(result, tuple)
    structured = result[1]
    assert isinstance(structured, dict)
    return structured


# --------------------------------------------------------------------------- #
# Registration + schema advertisement
# --------------------------------------------------------------------------- #
async def test_server_registers_admin_tools() -> None:
    app = build_app()
    assert app.name == SERVER_NAME

    tools = await app.list_tools()
    names = {tool.name for tool in tools}
    assert {"health_check", "get_server_status"} <= names


async def test_admin_tools_advertise_structured_output_schema() -> None:
    """Both admin tools must expose a structured ``outputSchema`` (object type)."""
    app = build_app()
    by_name = {tool.name: tool for tool in await app.list_tools()}

    for name in ("health_check", "get_server_status"):
        schema = by_name[name].outputSchema
        assert schema is not None, f"{name} must advertise an outputSchema"
        assert schema.get("type") == "object"
        # Required keys mirror the Pydantic model fields (no Optional in Phase 0).
        assert "properties" in schema

    health_props = by_name["health_check"].outputSchema["properties"]  # type: ignore[index]
    assert set(health_props) == set(HealthCheckResult.model_fields)

    status_props = by_name["get_server_status"].outputSchema["properties"]  # type: ignore[index]
    assert set(status_props) == set(ServerStatusResult.model_fields)


async def test_admin_tools_advertise_input_schema() -> None:
    """Even no-arg tools must advertise a JSON-Schema ``inputSchema`` object."""
    app = build_app()
    by_name = {tool.name: tool for tool in await app.list_tools()}
    for name in ("health_check", "get_server_status"):
        assert by_name[name].inputSchema.get("type") == "object"


# --------------------------------------------------------------------------- #
# health_check behavior + typed output
# --------------------------------------------------------------------------- #
async def test_health_check_returns_ok() -> None:
    app = build_app()
    structured = _structured(await app.call_tool("health_check", {}))
    assert structured["status"] == "ok"
    assert structured["version"] == __version__
    assert "timestamp" in structured


async def test_health_check_output_validates_against_model() -> None:
    """The structured payload must round-trip through the declared output model."""
    app = build_app()
    structured = _structured(await app.call_tool("health_check", {}))

    result = HealthCheckResult.model_validate(structured)
    assert result.status == "ok"
    assert isinstance(result.version, str)
    assert isinstance(result.timestamp, datetime)


# --------------------------------------------------------------------------- #
# get_server_status behavior + typed output
# --------------------------------------------------------------------------- #
async def test_get_server_status_reports_metadata() -> None:
    app = build_app()
    structured = _structured(await app.call_tool("get_server_status", {}))
    assert structured["name"] == SERVER_NAME
    assert structured["transport"] == TRANSPORT
    assert structured["tool_count"] >= 2
    assert structured["uptime_seconds"] >= 0


async def test_get_server_status_output_validates_against_model() -> None:
    app = build_app()
    structured = _structured(await app.call_tool("get_server_status", {}))

    result = ServerStatusResult.model_validate(structured)
    assert result.name == SERVER_NAME
    assert result.version == __version__
    assert result.transport == TRANSPORT
    # Typed fields, not just truthy values.
    assert isinstance(result.transport, str)
    assert isinstance(result.tool_count, int)
    assert isinstance(result.uptime_seconds, float)
    assert isinstance(result.started_at, datetime)


async def test_get_server_status_tool_count_matches_registry() -> None:
    """The reported ``tool_count`` must equal the live registered-tool count."""
    app = build_app()
    registered = len(await app.list_tools())
    structured = _structured(await app.call_tool("get_server_status", {}))
    assert structured["tool_count"] == registered


# --------------------------------------------------------------------------- #
# Error surface
# --------------------------------------------------------------------------- #
async def test_unknown_tool_is_rejected() -> None:
    """Calling an unregistered tool must raise rather than silently succeed.

    The concrete error type is the MCP SDK's ``ToolError``, which lives behind the
    :mod:`trader_mcp.server._sdk` isolation boundary; we match on its stable
    message instead of importing the SDK type into a test.
    """
    app = build_app()
    with pytest.raises(Exception, match=r"(?i)unknown tool"):
        await app.call_tool("does_not_exist", {})
