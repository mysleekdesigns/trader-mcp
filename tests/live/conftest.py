"""Fixtures for the opt-in live exchange-validation suite.

These tests hit REAL exchange public endpoints and are excluded from the default
run (the root ``tests/conftest.py`` skips ``@pytest.mark.live`` tests unless
``--live`` or ``TRADER_MCP_LIVE_TESTS=1``). Every adapter is created through an
:class:`~trader_mcp.exchanges.ExchangeManager` whose teardown closes the real
aiohttp sessions, so the global ``filterwarnings = ["error"]`` never trips on an
unclosed client.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from trader_mcp.exchanges import ExchangeManager


@pytest.fixture
async def manager() -> AsyncIterator[ExchangeManager]:
    """A live :class:`ExchangeManager` that closes every adapter on teardown."""
    mgr = ExchangeManager()
    try:
        yield mgr
    finally:
        await mgr.aclose_all()
