"""The ONLY module permitted to import the MCP SDK directly.

INVARIANT (PRD §4, locked): the MCP Python SDK is pinned to 1.x and isolated
behind this thin interface to contain SDK-v2 churn. Tool/business modules must
import from :mod:`trader_mcp.server` (which re-exports the names below), never
``import mcp`` directly.

``mcp-server-engineer`` owns and extends this isolation layer from Phase 1. Keep
the surface minimal: re-export the FastMCP application class and any SDK types we
genuinely need, and add adapters here rather than leaking SDK imports elsewhere.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

__all__ = ["FastMCP", "create_fastmcp"]


def create_fastmcp(name: str, *, instructions: str | None = None) -> FastMCP:
    """Construct a :class:`FastMCP` application.

    Centralizing construction here means call sites never touch the SDK directly,
    so an SDK-major upgrade is a single-file change.

    Args:
        name: Human-readable server name advertised to MCP clients.
        instructions: Optional server-level instructions for the client/model.

    Returns:
        A configured :class:`FastMCP` instance (transport chosen at ``run`` time).
    """
    return FastMCP(name=name, instructions=instructions)
