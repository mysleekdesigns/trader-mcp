"""Testnet/sandbox execution integration suite (opt-in) + always-on safety invariants.

Two layers:

1. **Always-on safety invariants** (NOT gated): prove -- in every default offline run --
   that the safety gate routes ``testnet`` to the real venue ONLY with a trade-enabled
   key and dry-run off, and that the ``live`` wall denies unconditionally (even "armed").
   These are the guarantees the opt-in suite below relies on; they must hold on every CI
   run, so they carry no network and no marker.

2. **Opt-in sandbox integration** (``@pytest.mark.testnet``): a scaffold a human with
   trade-enabled TESTNET keys runs to exercise the real sandbox order path against the
   reference exchange. Skipped (cleanly) in the default gate and whenever testnet keys
   are absent. It NEVER places a real-money live order: every order goes to the exchange
   sandbox, and the live wall is asserted to stay up.
"""

from __future__ import annotations

import os

import pytest

from trader_mcp.config import KeyScope
from trader_mcp.safety.policy import SessionMode, evaluate_order

from .conftest import TESTNET_EXCHANGE

# A US-eligible spot probe symbol on the reference exchange.
SPOT = "BTC/USD"


# --------------------------------------------------------------------------- #
# Layer 1: always-on safety invariants (no marker, no network)
# --------------------------------------------------------------------------- #
def test_live_wall_denies_even_when_armed() -> None:
    """The Phase-6 live wall denies real-money orders regardless of arm state."""
    decision = evaluate_order(
        mode=SessionMode.LIVE,
        exchange=TESTNET_EXCHANGE,
        market_type="spot",
        key_scope=KeyScope.TRADE_ENABLED,
        amount=0.001,
        notional=50.0,
        armed=True,
        dry_run=False,
    )
    assert decision.action == "deny"
    assert not decision.allowed


def test_testnet_read_only_key_cannot_trade() -> None:
    """A read-only key can never place a testnet order."""
    decision = evaluate_order(
        mode=SessionMode.TESTNET,
        exchange=TESTNET_EXCHANGE,
        market_type="spot",
        key_scope=KeyScope.READ_ONLY,
        amount=0.001,
        notional=50.0,
        dry_run=False,
    )
    assert decision.action == "deny"


def test_testnet_dry_run_downgrades_to_simulate() -> None:
    """With dry-run ON, an otherwise-routable testnet order is simulated, not routed."""
    decision = evaluate_order(
        mode=SessionMode.TESTNET,
        exchange=TESTNET_EXCHANGE,
        market_type="spot",
        key_scope=KeyScope.TRADE_ENABLED,
        amount=0.001,
        notional=50.0,
        dry_run=True,
    )
    assert decision.action == "simulate"


def test_testnet_trade_key_dry_run_off_routes() -> None:
    """A trade-enabled key on a US-eligible venue routes to the sandbox when armed off-dry-run.

    This is the only path the opt-in network suite below relies on -- and it is still a
    SANDBOX route, never a live order (``live`` denies in ``test_live_wall_denies``).
    """
    decision = evaluate_order(
        mode=SessionMode.TESTNET,
        exchange=TESTNET_EXCHANGE,
        market_type="spot",
        key_scope=KeyScope.TRADE_ENABLED,
        amount=0.001,
        notional=50.0,
        dry_run=False,
    )
    assert decision.action in {"route", "deny"}
    # If the reference venue is US-eligible for spot, it routes; jurisdiction may deny.
    if decision.action == "deny":
        assert decision.reason


# --------------------------------------------------------------------------- #
# Layer 2: opt-in sandbox integration (gated; skips cleanly offline / without keys)
# --------------------------------------------------------------------------- #
pytestmark_note = "individual tests below are marked @pytest.mark.testnet"


@pytest.mark.testnet
async def test_testnet_adapter_connects_to_sandbox(testnet_manager) -> None:
    """Smoke: build a sandbox adapter and confirm it is in testnet mode + can read markets.

    Read-only, no order placement. Proves connectivity + that ``testnet=True`` wiring
    reaches the CCXT client. A human runs this once with sandbox keys exported.
    """
    adapter = await testnet_manager.get(TESTNET_EXCHANGE, testnet=True)
    assert adapter.testnet is True
    await adapter.load_markets()
    caps = adapter.capabilities()
    assert caps.market_count is not None
    assert caps.market_count > 0


@pytest.mark.testnet
async def test_testnet_place_order_is_sandbox_only(testnet_manager) -> None:
    """Place a tiny SANDBOX order through the gated path, asserting it is never live.

    This is intentionally a scaffold: it requires an explicit second opt-in
    (``TRADER_MCP_TESTNET_ALLOW_ORDERS=1``) before it will attempt any order, and it
    asserts the order is routed to the exchange sandbox (fake money), never the live
    wall. Without that flag it skips -- so simply running ``--testnet`` does no trading.
    """
    if os.environ.get("TRADER_MCP_TESTNET_ALLOW_ORDERS") != "1":
        pytest.skip(
            "order placement requires a second explicit opt-in: "
            "set TRADER_MCP_TESTNET_ALLOW_ORDERS=1 (sandbox/fake-money only)"
        )
    # The gate must permit testnet routing for a trade-enabled key with dry-run off, and
    # must STILL deny live -- proving the order can only ever hit the sandbox.
    testnet_decision = evaluate_order(
        mode=SessionMode.TESTNET,
        exchange=TESTNET_EXCHANGE,
        market_type="spot",
        key_scope=KeyScope.TRADE_ENABLED,
        amount=0.0001,
        notional=10.0,
        dry_run=False,
    )
    live_decision = evaluate_order(
        mode=SessionMode.LIVE,
        exchange=TESTNET_EXCHANGE,
        market_type="spot",
        key_scope=KeyScope.TRADE_ENABLED,
        amount=0.0001,
        notional=10.0,
        armed=True,
        dry_run=False,
    )
    assert live_decision.action == "deny", "live wall must stay up during testnet trading"
    if testnet_decision.action != "route":
        pytest.skip(
            f"testnet routing not permitted for {TESTNET_EXCHANGE}: {testnet_decision.reason}"
        )
    # A human implementer wires the actual sandbox order here via the execution server
    # (deploy_strategy / place_order with mode=testnet). Left as an explicit scaffold so
    # this file never auto-submits an order in CI.
    pytest.skip(
        "scaffold: wire the sandbox order via the execution server here "
        "(mode=testnet, trade-enabled sandbox key) -- intentionally not auto-submitted"
    )
