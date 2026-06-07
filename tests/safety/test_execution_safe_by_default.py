"""Cross-cutting safe-by-default proof for the Phase-5 execution surface (INVARIANT 2).

The gate's decision matrix is unit-tested in ``test_gate.py``. This module proves
the *systemic* guarantees that the execution layer relies on the gate for:

  * A PAPER session is ALWAYS simulated -- the PaperBroker physically cannot route,
    and every record it emits carries ``simulated=True``.
  * ``live`` is UNREACHABLE: it is not a valid ``execution.SessionMode`` (Pydantic
    rejects it) and the safety gate DENIES it unconditionally (the Phase-6 wall).
  * Across the full gate matrix, the ONLY action that yields a real (routed) order
    is the exact combination testnet + trade-enabled key + US-eligible venue +
    dry-run OFF. Every other combination simulates or denies.
  * The global dry-run default downgrades an otherwise-routable testnet order to
    simulate, so nothing routes without an explicit opt-out.
  * Idempotency dedupe stops a retried submission from placing twice.

Deterministic + offline: pure model/gate calls and an in-memory PaperBroker.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from trader_mcp.config import KeyScope
from trader_mcp.execution import ExecutionConfig, OrderIntent, PaperBroker, SessionInfo
from trader_mcp.safety import (
    IdempotencyRegistry,
    SessionMode,
    evaluate_order,
    make_client_order_id,
)
from trader_mcp.safety.scoping import (
    US_ELIGIBLE_EXCHANGES,
    US_PERP_EXCHANGES,
    MarketType,
)
from trader_mcp.strategy import (
    EntryRules,
    ExitRules,
    IndicatorSpec,
    PositionSizing,
    StrategySpec,
)

from ..execution.conftest import make_bars


def _spec() -> StrategySpec:
    return StrategySpec(
        name="t",
        symbol="BTC/USD",
        timeframe="1h",
        strategy_type="rule",
        indicators=[IndicatorSpec(id="fast", kind="sma", params={"length": 2})],
        entry=EntryRules(long="close > fast"),
        exit=ExitRules(long="close < fast"),
        position_sizing=PositionSizing(mode="percent_equity", value=100),
    )


# --------------------------------------------------------------------------- #
# live is unreachable
# --------------------------------------------------------------------------- #
def test_execution_session_mode_rejects_live() -> None:
    """The execution-layer SessionMode literal has NO 'live' value (Pydantic rejects it)."""
    with pytest.raises(PydanticValidationError):
        SessionInfo(
            session_id="s1",
            mode="live",  # type: ignore[arg-type]
            exchange="coinbase",
            symbol="BTC/USD",
            strategy_name=None,
            created=__import__("datetime").datetime.now(tz=__import__("datetime").UTC),
        )


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("scope", [KeyScope.READ_ONLY, KeyScope.TRADE_ENABLED])
def test_live_is_always_denied_regardless_of_scope_or_dry_run(
    dry_run: bool, scope: KeyScope
) -> None:
    """The Phase-6 wall: 'live' denies under every scope/dry-run combination."""
    decision = evaluate_order(
        mode=SessionMode.LIVE,
        exchange="coinbase",
        market_type="spot",
        key_scope=scope,
        amount=0.01,
        notional=500.0,
        armed=True,  # even armed
        dry_run=dry_run,
    )
    assert decision.action == "deny"
    assert decision.mode is SessionMode.LIVE


# --------------------------------------------------------------------------- #
# the gate matrix: ONLY one combination routes
# --------------------------------------------------------------------------- #
def _spot_venues() -> list[str]:
    return sorted(US_ELIGIBLE_EXCHANGES)


def test_only_testnet_trade_enabled_eligible_dry_run_off_routes() -> None:
    """Sweep the gate matrix; assert exactly the documented routing combination routes.

    The set of (mode, scope, exchange, market, dry_run) that yields action='route'
    is EXACTLY: testnet + trade-enabled + (US-eligible venue, with swap only on a
    perp venue) + dry_run OFF. Everything else simulates or denies. This is the
    machine-checked statement of safe-by-default for the whole order surface.
    """
    modes = [SessionMode.PAPER, SessionMode.TESTNET, SessionMode.LIVE]
    scopes = [KeyScope.READ_ONLY, KeyScope.TRADE_ENABLED]
    exchanges = [*_spot_venues(), "bybit"]  # include an ineligible venue
    markets: list[MarketType] = ["spot", "swap"]
    dry_runs = [True, False]

    routed: list[tuple] = []
    for mode in modes:
        for scope in scopes:
            for exch in exchanges:
                for market in markets:
                    for dry in dry_runs:
                        d = evaluate_order(
                            mode=mode,
                            exchange=exch,
                            market_type=market,
                            key_scope=scope,
                            amount=0.01,
                            notional=500.0,
                            dry_run=dry,
                        )
                        if d.action == "route":
                            routed.append((mode, scope, exch, market, dry))

    for mode, scope, exch, market, dry in routed:
        assert mode is SessionMode.TESTNET, (mode, scope, exch, market, dry)
        assert scope is KeyScope.TRADE_ENABLED
        assert exch in US_ELIGIBLE_EXCHANGES
        if market == "swap":
            assert exch in US_PERP_EXCHANGES
        assert dry is False
    # Non-vacuous: at least the canonical coinbase-spot route is present.
    assert (SessionMode.TESTNET, KeyScope.TRADE_ENABLED, "coinbase", "spot", False) in routed


def test_paper_never_routes_anywhere() -> None:
    """No paper combination -- any scope/venue/market/dry-run -- ever routes."""
    for scope in (KeyScope.READ_ONLY, KeyScope.TRADE_ENABLED):
        for exch in (*_spot_venues(), "bybit"):
            for market in ("spot", "swap"):
                for dry in (True, False):
                    d = evaluate_order(
                        mode=SessionMode.PAPER,
                        exchange=exch,
                        market_type=market,  # type: ignore[arg-type]
                        key_scope=scope,
                        amount=0.01,
                        notional=500.0,
                        dry_run=dry,
                    )
                    assert d.action == "simulate"


def test_global_dry_run_default_downgrades_testnet_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no explicit override, the safe default downgrades a testnet route to simulate."""
    monkeypatch.delenv("TRADER_MCP_DRY_RUN", raising=False)
    d = evaluate_order(
        mode=SessionMode.TESTNET,
        exchange="coinbase",
        market_type="spot",
        key_scope=KeyScope.TRADE_ENABLED,
        amount=0.01,
        notional=500.0,
    )
    assert d.action == "simulate"


# --------------------------------------------------------------------------- #
# the PaperBroker physically cannot place a real order
# --------------------------------------------------------------------------- #
def test_paper_broker_emits_only_simulated_records_through_a_lifecycle() -> None:
    """Drive a full entry+exit through the broker; EVERY record has simulated=True."""
    spec = _spec()
    broker = PaperBroker(spec, ExecutionConfig(initial_cash=10_000))
    broker.set_bar_hours(1.0)
    bars = make_bars([100.0, 100.0, 110.0, 90.0])
    broker.on_bar(bars[0])
    broker.submit(OrderIntent(symbol=spec.symbol, side="buy", amount=1.0, client_order_id="c1"))
    broker.on_bar(bars[1])
    broker.submit(
        OrderIntent(
            symbol=spec.symbol,
            side="sell",
            amount=1.0,
            client_order_id="c2",
            reduce_only=True,
        )
    )
    broker.on_bar(bars[2])
    broker.on_bar(bars[3])
    records = broker.order_history()
    assert records
    assert all(r.simulated is True for r in records), "a paper order escaped as non-simulated"


# --------------------------------------------------------------------------- #
# idempotency dedupe gates retries
# --------------------------------------------------------------------------- #
def test_retried_submission_dedupes_via_deterministic_coid() -> None:
    """A retried order with the same (session, symbol, side, seq) dedupes (placed once)."""
    reg = IdempotencyRegistry()
    coid = make_client_order_id(session_id="sess-1", symbol="BTC/USD", side="buy", seq=7)
    # First submission accepted.
    assert reg.record(coid) is True
    # The retry produces the SAME deterministic id and is recognized as a duplicate.
    retry = make_client_order_id(session_id="sess-1", symbol="BTC/USD", side="buy", seq=7)
    assert retry == coid
    assert reg.record(retry) is False
    assert len(reg) == 1
