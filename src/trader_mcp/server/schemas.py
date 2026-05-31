"""Typed Pydantic v2 I/O models for the MCP tool surface.

Every tool takes a typed input and returns a typed output so FastMCP can emit an
``outputSchema`` / structured content. These Phase 0 models cover the two trivial
admin tools; ``mcp-server-engineer`` adds the rest from Phase 1.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    """Base model: forbid unexpected fields, validate on assignment."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class HealthCheckResult(_StrictModel):
    """Result of the ``health_check`` tool."""

    status: str = Field(description="Liveness status; 'ok' when the server is up.")
    version: str = Field(description="trader-mcp package version.")
    timestamp: datetime = Field(description="UTC time the check was produced.")


class ServerStatusResult(_StrictModel):
    """Result of the ``get_server_status`` tool."""

    name: str = Field(description="Server name advertised to MCP clients.")
    version: str = Field(description="trader-mcp package version.")
    transport: str = Field(description="Active MCP transport (e.g. 'stdio').")
    started_at: datetime = Field(description="UTC time the server process started.")
    uptime_seconds: float = Field(description="Seconds since the server started.")
    tool_count: int = Field(description="Number of registered MCP tools.")
