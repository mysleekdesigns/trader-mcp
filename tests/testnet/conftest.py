"""Fixtures for the opt-in TESTNET integration suite.

These tests exercise the paper/testnet execution path against a real exchange
**sandbox** (fake money). They are excluded from the default offline run (the root
``tests/conftest.py`` skips ``@pytest.mark.testnet`` unless ``--testnet`` or
``TRADER_MCP_TESTNET_TESTS=1``) and NEVER place real-money live orders -- the Phase-6
live wall stays up regardless of arm state.

A human with sandbox credentials runs them by exporting trade-enabled TESTNET keys for
the reference exchange (Coinbase) and opting in. Without those keys, the network tests
skip cleanly with a clear reason; the safety-invariant tests in this package run
unconditionally so the live wall is proven in every gate.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from trader_mcp.exchanges import ExchangeManager

#: Reference exchange for the testnet suite (PRD: Coinbase is validated first).
TESTNET_EXCHANGE = os.environ.get("TRADER_MCP_TESTNET_EXCHANGE", "coinbase")


def _has_testnet_credentials() -> bool:
    """True only if explicit, trade-enabled TESTNET keys are present in the env.

    We require purpose-named env vars (not the shared trade keys) so a human cannot
    accidentally point this suite at a real-money key. Secrets are never read into the
    test body -- only their presence is checked here.
    """
    return bool(
        os.environ.get("TRADER_MCP_TESTNET_API_KEY")
        and os.environ.get("TRADER_MCP_TESTNET_API_SECRET")
    )


@pytest.fixture
async def testnet_manager() -> AsyncIterator[ExchangeManager]:
    """A manager for the sandbox suite; skips cleanly when no testnet keys are set."""
    if not _has_testnet_credentials():
        pytest.skip(
            "no testnet credentials: set TRADER_MCP_TESTNET_API_KEY / "
            "TRADER_MCP_TESTNET_API_SECRET (trade-enabled sandbox key) to run"
        )
    mgr = ExchangeManager()
    try:
        yield mgr
    finally:
        await mgr.aclose_all()
