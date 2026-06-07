"""SafetyController aggregator tests.

Proves the full preflight pipeline and audit integration:
  * kill switch engaged -> preflight denies + audit 'denied';
  * risk breach -> preflight denies + audit 'denied';
  * paper -> simulate + audit 'order_intent';
  * **live -> still deny even when armed** (the wall holds);
  * arm/disarm/set_risk_limits each leave an audit entry;
  * status() reflects state.
"""

from __future__ import annotations

from trader_mcp.config import KeyScope
from trader_mcp.safety.arming import REQUIRED_CONFIRMATION
from trader_mcp.safety.controller import SafetyController, SafetyStatus
from trader_mcp.safety.policy import SessionMode
from trader_mcp.safety.risk import OrderRiskContext, RiskLimits


def _ctx(**overrides: object) -> OrderRiskContext:
    kwargs: dict[str, object] = {"order_notional": 100.0, "resulting_position_notional": 100.0}
    kwargs.update(overrides)
    return OrderRiskContext(**kwargs)  # type: ignore[arg-type]


def _preflight(controller: SafetyController, **overrides: object):
    kwargs: dict[str, object] = {
        "mode": SessionMode.PAPER,
        "exchange": "coinbase",
        "symbol": "BTC/USD",
        "market_type": "spot",
        "key_scope": KeyScope.TRADE_ENABLED,
        "side": "buy",
        "amount": 0.01,
        "notional": 100.0,
        "risk_ctx": _ctx(),
        "dry_run": False,
    }
    kwargs.update(overrides)
    return controller.preflight(**kwargs)  # type: ignore[arg-type]


def test_paper_simulates_and_audits_intent() -> None:
    c = SafetyController()
    decision = _preflight(c, mode=SessionMode.PAPER)
    assert decision.action == "simulate"
    entries = c.audit.entries(event="order_intent")
    assert len(entries) == 1
    assert entries[-1].decision == "simulate"
    assert entries[-1].symbol == "BTC/USD"


def test_kill_switch_denies_and_audits_denied() -> None:
    c = SafetyController()
    c.engage_kill_switch(exchange="coinbase", reason="halt now")
    decision = _preflight(c, mode=SessionMode.PAPER)
    assert decision.action == "deny"
    assert "kill switch" in decision.reason
    denied = c.audit.entries(event="denied")
    assert len(denied) == 1
    # No order_intent recorded for a denied-at-killswitch order.
    assert c.audit.entries(event="order_intent") == []


def test_global_kill_switch_halts_all_exchanges() -> None:
    c = SafetyController()
    c.engage_kill_switch(reason="global")
    assert _preflight(c, exchange="kraken").action == "deny"


def test_risk_breach_denies_and_audits() -> None:
    c = SafetyController(risk_limits=RiskLimits(max_order_notional=50.0))
    decision = _preflight(c, mode=SessionMode.PAPER, risk_ctx=_ctx(order_notional=500.0))
    assert decision.action == "deny"
    assert "risk limit breached" in decision.reason
    assert len(c.audit.entries(event="denied")) == 1


def test_kill_switch_checked_before_risk() -> None:
    # Both would deny; the reason proves kill switch wins (checked first).
    c = SafetyController(risk_limits=RiskLimits(max_order_notional=1.0))
    c.engage_kill_switch(exchange="coinbase", reason="halt")
    decision = _preflight(c, risk_ctx=_ctx(order_notional=999.0))
    assert "kill switch" in decision.reason


def test_live_still_denies_even_when_armed() -> None:
    c = SafetyController()
    # Arm the controller -- the wall must still hold.
    c.arm_live(exchange="coinbase", confirm=REQUIRED_CONFIRMATION)
    assert c.is_armed("coinbase") is True
    decision = _preflight(c, mode=SessionMode.LIVE)
    assert decision.action == "deny"
    assert decision.mode == SessionMode.LIVE


def test_set_risk_limits_records_audit() -> None:
    c = SafetyController()
    returned = c.set_risk_limits(RiskLimits(max_leverage=3.0))
    assert returned.max_leverage == 3.0
    assert c.get_risk_limits().max_leverage == 3.0
    assert len(c.audit.entries(event="set_risk_limits")) == 1


def test_arm_disarm_record_audit() -> None:
    c = SafetyController()
    c.arm_live(exchange="coinbase", confirm=REQUIRED_CONFIRMATION)
    assert len(c.audit.entries(event="arm")) == 1
    c.disarm(exchange="coinbase")
    assert c.is_armed("coinbase") is False
    assert len(c.audit.entries(event="disarm")) == 1


def test_kill_switch_engage_reset_record_audit() -> None:
    c = SafetyController()
    c.engage_kill_switch(exchange="coinbase", reason="x")
    c.reset_kill_switch(exchange="coinbase")
    assert c.is_halted("coinbase") is False
    ks_entries = c.audit.entries(event="kill_switch")
    assert [e.outcome for e in ks_entries] == ["engaged", "reset"]


def test_status_reflects_state() -> None:
    c = SafetyController()
    c.set_risk_limits(RiskLimits(max_order_notional=100.0))
    c.arm_live(exchange="coinbase", confirm=REQUIRED_CONFIRMATION)
    c.engage_kill_switch(exchange="kraken", reason="halt")
    status = c.status()
    assert isinstance(status, SafetyStatus)
    assert status.risk_limits.max_order_notional == 100.0
    assert [t.exchange for t in status.armed] == ["coinbase"]
    assert "kraken" in status.halted_scopes
    assert status.audit_entries == len(c.audit)


def test_audit_detail_redacted_through_controller() -> None:
    c = SafetyController()
    c.engage_kill_switch(
        exchange="coinbase",
        reason="apiKey=SUPERSECRETVALUE1234567890ABCDEFGHIJK",
    )
    entry = c.audit.entries(event="kill_switch")[-1]
    assert "SUPERSECRETVALUE1234567890ABCDEFGHIJK" not in (entry.detail or "")
