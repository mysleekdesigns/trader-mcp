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

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from mcp.server.fastmcp import FastMCP

__all__ = ["FastMCP", "Lifespan", "create_fastmcp"]

#: A FastMCP lifespan factory: given the app, returns an async context manager
#: whose ``__aenter__``/``__aexit__`` bracket the server's serving lifetime. We
#: type the yielded value as ``Any`` because trader-mcp does not thread a typed
#: lifespan context through the SDK; we only use the exit hook for cleanup.
Lifespan = Callable[[FastMCP], AbstractAsyncContextManager[Any]]


def create_fastmcp(
    name: str,
    *,
    instructions: str | None = None,
    lifespan: Lifespan | None = None,
) -> FastMCP:
    """Construct a :class:`FastMCP` application.

    Centralizing construction here means call sites never touch the SDK directly,
    so an SDK-major upgrade is a single-file change.

    Args:
        name: Human-readable server name advertised to MCP clients.
        instructions: Optional server-level instructions for the client/model.
        lifespan: Optional async-context-manager factory ``app -> CM`` bracketing
            the server's serving lifetime. Its ``__aexit__`` runs on shutdown, so
            it is the SDK-isolated place to release process-wide resources (e.g.
            close cached exchange adapters). The lifespan is entered by the
            transport runners (``run_stdio_async`` etc.), not by ``build_app`` or
            ``list_tools``.

    Returns:
        A configured :class:`FastMCP` instance (transport chosen at ``run`` time).
    """
    return FastMCP(name=name, instructions=instructions, lifespan=lifespan)
