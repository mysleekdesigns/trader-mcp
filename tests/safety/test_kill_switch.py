"""Kill-switch tests.

Proves: a global engage halts every exchange; a per-exchange engage halts only
that one; reset(None) clears global + all per-exchange; the reason is surfaced and
the global reason takes precedence.
"""

from __future__ import annotations

from trader_mcp.safety.arming import GLOBAL_SCOPE
from trader_mcp.safety.kill_switch import KillSwitchState


def test_default_not_halted() -> None:
    ks = KillSwitchState()
    assert ks.is_halted("coinbase") is False
    assert ks.halted_scopes() == []
    assert ks.reason_for("coinbase") is None


def test_global_halts_everything() -> None:
    ks = KillSwitchState()
    ks.engage(reason="market emergency")
    assert ks.is_halted("coinbase") is True
    assert ks.is_halted("kraken") is True
    assert ks.halted_scopes() == [GLOBAL_SCOPE]
    assert ks.reason_for("kraken") == "market emergency"


def test_per_exchange_halts_only_that_one() -> None:
    ks = KillSwitchState()
    ks.engage(exchange="Coinbase", reason="bad fills")
    assert ks.is_halted("coinbase") is True  # normalized match
    assert ks.is_halted("kraken") is False
    assert ks.halted_scopes() == ["coinbase"]
    assert ks.reason_for("coinbase") == "bad fills"
    assert ks.reason_for("kraken") is None


def test_reset_none_clears_all() -> None:
    ks = KillSwitchState()
    ks.engage(reason="global")
    ks.engage(exchange="coinbase", reason="local")
    ks.reset(exchange=None)
    assert ks.is_halted("coinbase") is False
    assert ks.halted_scopes() == []


def test_reset_per_exchange_is_surgical() -> None:
    ks = KillSwitchState()
    ks.engage(exchange="coinbase")
    ks.engage(exchange="kraken")
    ks.reset(exchange="coinbase")
    assert ks.is_halted("coinbase") is False
    assert ks.is_halted("kraken") is True


def test_global_reason_takes_precedence() -> None:
    ks = KillSwitchState()
    ks.engage(exchange="coinbase", reason="local")
    ks.engage(reason="GLOBAL HALT")
    assert ks.reason_for("coinbase") == "GLOBAL HALT"


def test_reset_unengaged_is_noop() -> None:
    ks = KillSwitchState()
    ks.reset(exchange="coinbase")  # must not raise
    assert ks.is_halted("coinbase") is False
