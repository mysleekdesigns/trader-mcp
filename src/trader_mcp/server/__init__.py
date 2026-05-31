"""MCP server package (owned by ``mcp-server-engineer``).

Hosts the FastMCP application, the typed tool surface, and the SDK-isolation
wrapper (:mod:`trader_mcp.server._sdk` is the only place that imports the MCP SDK).
Re-exports :func:`main` (the ``trader-mcp`` console entry point) and
:func:`build_app` so other modules never import the SDK directly.
"""

from __future__ import annotations

from trader_mcp.server.app import build_app
from trader_mcp.server.main import main

__all__ = ["build_app", "main"]
