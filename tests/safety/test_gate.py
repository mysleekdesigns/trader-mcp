"""Decision-matrix tests for the safe-by-default gate (:func:`evaluate_order`).

These prove the core invariant: paper simulates, testnet routes only for a
trade-enabled key on a US-eligible venue, read-only/ineligible are denied, the
global dry-run downgrades route->simulate, and -- the hard Phase-6 wall --
``live`` is ALWAYS denied.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trader_mcp.config import KeyScope
from trader_mcp.errors import SafetyError
from trader_mcp.safety import (
    GateDecision,
    SessionMode,
    evaluate_order,
)


def _eval(**overrides: object) -> GateDecision:
    """Call evaluate_order with sane defaults; dry_run pinned off unless overridden."""
    kwargs: dict[str, object] = {
        "mode": SessionMode.TESTNET,
        "exchange": "coinbase",
        "market_type": "spot",
        "key_scope": KeyScope.TRADE_ENABLED,
        "amount": 0.01,
        "notional": 500.0,
        "armed": False,
        "dry_run": False,  # isolate gate logic from ambient env in most tests
    }
    kwargs.update(overrides)
    return evaluate_order(**kwargs)  # type: ignore[arg-type]


def test_paper_always_simulates() -> None:
    decision = _eval(mode=SessionMode.PAPER, key_scope=KeyScope.READ_ONLY)
    assert decision.action == "simulate"
    assert decision.mode is SessionMode.PAPER
    assert decision.allowed


def test_paper_needs_no_scope_or_eligible_venue() -> None:
    # Even a read-only key on a non-eligible venue is fine in paper -- it is pure
    # simulation and never touches a real exchange.
    decision = _eval(
        mode=SessionMode.PAPER,
        exchange="bybit",
        key_scope=KeyScope.READ_ONLY,
    )
    assert decision.action == "simulate"


def test_paper_simulates_even_when_global_dry_run_off() -> None:
    decision = _eval(mode=SessionMode.PAPER, dry_run=False)
    assert decision.action == "simulate"


def test_testnet_trade_enabled_us_eligible_routes() -> None:
    decision = _eval(mode=SessionMode.TESTNET, dry_run=False)
    assert decision.action == "route"
    assert decision.mode is SessionMode.TESTNET
    assert decision.disclaimer is not None


def test_testnet_read_only_is_denied() -> None:
    decision = _eval(key_scope=KeyScope.READ_ONLY)
    assert decision.action == "deny"
    assert "read-only" in decision.reason
    assert not decision.allowed


def test_testnet_ineligible_venue_is_denied() -> None:
    decision = _eval(exchange="bybit")
    assert decision.action == "deny"
    assert "US-eligible" in decision.reason


def test_testnet_swap_on_non_perp_venue_is_denied() -> None:
    # Gemini is US-eligible for spot but has no CFTC-regulated perps.
    decision = _eval(exchange="gemini", market_type="swap")
    assert decision.action == "deny"
    assert "perp" in decision.reason.lower()


def test_testnet_swap_on_perp_venue_routes() -> None:
    decision = _eval(exchange="kraken", market_type="swap", dry_run=False)
    assert decision.action == "route"


@pytest.mark.parametrize("scope", [KeyScope.READ_ONLY, KeyScope.TRADE_ENABLED])
@pytest.mark.parametrize("armed", [True, False])
def test_live_is_always_denied(scope: KeyScope, armed: bool) -> None:
    """The Phase-6 wall: live is denied regardless of scope or armed flag."""
    decision = _eval(mode=SessionMode.LIVE, key_scope=scope, armed=armed, dry_run=False)
    assert decision.action == "deny"
    assert "Phase 6" in decision.reason


def test_live_string_mode_is_denied() -> None:
    decision = _eval(mode="live", dry_run=False)
    assert decision.action == "deny"
    assert "Phase 6" in decision.reason


def test_global_dry_run_downgrades_route_to_simulate() -> None:
    decision = _eval(mode=SessionMode.TESTNET, dry_run=True)
    assert decision.action == "simulate"
    assert "dry-run" in decision.reason
    # Even downgraded, the eligibility disclaimer is preserved.
    assert decision.disclaimer is not None


def test_global_dry_run_does_not_resurrect_denied_order() -> None:
    # A read-only testnet order is denied whether or not dry-run is on.
    assert _eval(key_scope=KeyScope.READ_ONLY, dry_run=True).action == "deny"
    assert _eval(key_scope=KeyScope.READ_ONLY, dry_run=False).action == "deny"


def test_unknown_mode_fails_closed() -> None:
    decision = _eval(mode="margin")
    assert decision.action == "deny"
    assert "unknown session mode" in decision.reason


def test_negative_amount_or_notional_is_denied() -> None:
    assert _eval(amount=-1.0).action == "deny"
    assert _eval(notional=-1.0).action == "deny"


def test_dry_run_defaults_to_safe_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no dry_run arg and no env override, routing is downgraded to simulate."""
    monkeypatch.delenv("TRADER_MCP_DRY_RUN", raising=False)
    decision = evaluate_order(
        mode=SessionMode.TESTNET,
        exchange="coinbase",
        market_type="spot",
        key_scope=KeyScope.TRADE_ENABLED,
        amount=0.01,
        notional=500.0,
    )
    assert decision.action == "simulate"


def test_env_can_disable_dry_run_to_allow_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADER_MCP_DRY_RUN", "false")
    decision = evaluate_order(
        mode=SessionMode.TESTNET,
        exchange="coinbase",
        market_type="spot",
        key_scope=KeyScope.TRADE_ENABLED,
        amount=0.01,
        notional=500.0,
    )
    assert decision.action == "route"


def test_gate_decision_is_frozen() -> None:
    decision = _eval(mode=SessionMode.PAPER)
    with pytest.raises(ValidationError):  # pydantic raises on mutating a frozen model
        decision.action = "route"  # type: ignore[misc]


def test_raise_if_denied_raises_on_deny_and_passes_on_allow() -> None:
    denied = _eval(key_scope=KeyScope.READ_ONLY)
    with pytest.raises(SafetyError):
        denied.raise_if_denied()
    allowed = _eval(mode=SessionMode.PAPER)
    assert allowed.raise_if_denied() is allowed
