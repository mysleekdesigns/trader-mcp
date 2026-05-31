"""Console entry point: run the trader-mcp MCP server over stdio.

Wired to ``[project.scripts] trader-mcp = "trader_mcp.server:main"``. The actual
``main`` is re-exported from :mod:`trader_mcp.server`.
"""

from __future__ import annotations

from trader_mcp.logging_config import get_logger
from trader_mcp.server.app import TRANSPORT, build_app

logger = get_logger(__name__)


def main() -> None:
    """Build the FastMCP app and serve it over stdio.

    This blocks, owning the event loop, until the client disconnects. Logging goes
    to stderr so stdout stays a clean JSON-RPC channel.
    """
    logger.info("Starting trader-mcp MCP server over %s transport.", TRANSPORT)
    app = build_app()
    app.run(transport=TRANSPORT)
