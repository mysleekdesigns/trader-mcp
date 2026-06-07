"""Phase 6 guardrails-only END-TO-END integration tests (PRD §5.3, §6, §7).

QA's cross-cutting slice for Phase 6. The unit tests live elsewhere:

  * ``tests/safety/`` exercises each safety primitive (risk/arming/kill_switch/
    audit/controller) in isolation, including arm-TTL time-advance expiry;
  * ``tests/test_safety_tools.py`` exercises each safety MCP tool one at a time.

This file is the layer ABOVE those: it drives the safety tools TOGETHER, through
the registered FastMCP app, against a real (paper) execution session -- the full
operator story -- and it proves the load-bearing INVARIANT for the phase:

    The Phase 6 guardrails wrap the order-DECISION path only. They must NOT alter
    fill math, signal logic, or any observable paper OrderRecord / broker state
    when nothing is breached. The preflight wrapper is a PASS-THROUGH whenever the
    kill switch is disengaged and risk limits are non-binding.

Two flavors of proof:

  1. ``test_preflight_is_pass_through_when_nothing_breached`` -- a focused parity
     assertion: a paper ``place_order`` with risk limits set GENEROUSLY (non-
     binding) and the kill switch DISENGAGED produces a byte-identical OrderRecord
     (every field but the wall-clock ``timestamp``) to the same flow with NO limits
     at all. The guardrail adds an audit trail and nothing else.
  2. ``test_paper_deploy_regression_no_limits_kill_off`` -- with no limits and the
     kill switch off, a normal paper ``deploy_strategy`` run produces the same
     orders/trades/PnL it did pre-guardrails (anchored to the Phase 5 fixture's
     bar count + a stable PnL snapshot run twice).

Plus the full operator flow (set limits -> arm -> place -> breach/deny -> kill ->
reset) with an ORDERED audit-trail assertion and a no-secret-shaped-material probe.

Fully offline & deterministic: a tmp ``data_dir`` seeded with one synthetic dataset
+ one saved strategy (the Phase 2/3/4/5 pattern), no network, fixed-seed bars.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests._fakes import SECRET_TOKEN
from trader_mcp.config import get_settings
from trader_mcp.data.models import DatasetKey
from trader_mcp.data.store import OHLCVStore
from trader_mcp.exchanges.models import OHLCVBar
from trader_mcp.execution import OrderRecord, SessionInfo, SessionStatus
from trader_mcp.safety import REQUIRED_CONFIRMATION
from trader_mcp.server.app import build_app
from trader_mcp.strategy import (
    EntryRules,
    ExitRules,
    Fees,
    IndicatorSpec,
    PositionSizing,
    StrategySpec,
    StrategyStore,
)
from trader_mcp.strategy import RiskLimits as StrategyRiskLimits

_EXCHANGE = "coinbase"
_SYMBOL = "BTC/USD"
_TIMEFRAME = "1h"
_STRATEGY = "sma-cross-phase6"
_N_BARS = 300
_SEED = 7  # same seed/shape as the Phase 5 execution-tools fixture (regression anchor)

# The redaction placeholder the audit log substitutes for secret-shaped text. If it
# ever appears in an audit entry on a paper session, something leaked a secret into
# a detail string (paper sessions carry NO credentials, so nothing should redact).
_REDACTION_PLACEHOLDER = "***REDACTED***"


def _structured(result: Any) -> dict[str, Any]:
    assert isinstance(result, tuple)
    structured = result[1]
    assert isinstance(structured, dict)
    return structured


def _seed_bars() -> list[OHLCVBar]:
    """Deterministic OHLCV identical in shape to the Phase 5 execution fixture."""
    rng = np.random.default_rng(_SEED)
    t = np.arange(_N_BARS)
    closes = np.maximum(
        100.0 + 8.0 * np.sin(t / 15.0) + rng.normal(0, 1, _N_BARS).cumsum() * 0.1, 1.0
    )
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    bars: list[OHLCVBar] = []
    prev = float(closes[0])
    for i, c in enumerate(closes):
        close = float(c)
        open_px = prev if i > 0 else close
        bars.append(
            OHLCVBar(
                timestamp=t0 + timedelta(hours=i),
                open=open_px,
                high=max(open_px, close) * 1.002,
                low=min(open_px, close) * 0.998,
                close=close,
                volume=1.0,
            )
        )
        prev = close
    return bars


def _spec(name: str = _STRATEGY) -> StrategySpec:
    return StrategySpec(
        name=name,
        exchange=_EXCHANGE,
        symbol=_SYMBOL,
        timeframe=_TIMEFRAME,
        strategy_type="rule",
        indicators=[
            IndicatorSpec(id="fast", kind="sma", params={"length": 10}),
            IndicatorSpec(id="slow", kind="sma", params={"length": 30}),
        ],
        entry=EntryRules(long="crossover(fast, slow)", short="crossunder(fast, slow)"),
        exit=ExitRules(long="crossunder(fast, slow)", short="crossover(fast, slow)"),
        position_sizing=PositionSizing(mode="percent_equity", value=20.0),
        risk=StrategyRiskLimits(stop_loss_pct=3.0, take_profit_pct=6.0),
        fees=Fees(),
    )


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("TRADER_MCP_DATA_DIR", str(tmp_path))
    # Force the safe-by-default dry-run ON regardless of the developer's environment.
    monkeypatch.delenv("TRADER_MCP_DRY_RUN", raising=False)
    get_settings.cache_clear()
    try:
        yield tmp_path
    finally:
        get_settings.cache_clear()


def _seed(data_dir: Path) -> None:
    OHLCVStore(data_dir).upsert_bars(
        DatasetKey(exchange=_EXCHANGE, symbol=_SYMBOL, timeframe=_TIMEFRAME), _seed_bars()
    )
    StrategyStore(data_dir).save(_spec())


@pytest.fixture
def seeded_app(data_dir: Path) -> Any:
    _seed(data_dir)
    return build_app()


async def _start_paper(app: Any) -> SessionInfo:
    return SessionInfo.model_validate(
        _structured(
            await app.call_tool(
                "start_session",
                {"mode": "paper", "exchange": _EXCHANGE, "symbol": _SYMBOL},
            )
        )
    )


async def _place(app: Any, session_id: str, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "session_id": session_id,
        "symbol": _SYMBOL,
        "side": "buy",
        "amount": 0.01,
    }
    args.update(overrides)
    return _structured(await app.call_tool("place_order", args))


def _record_without_timestamp(record: OrderRecord) -> dict[str, Any]:
    """A record's identity for parity comparison, EXCLUDING the wall-clock timestamp.

    A manual paper market order is stamped ``_last_bar_time or now()``; with no bars
    processed on a manual session that is ``datetime.now()`` -- the only non-
    deterministic field. Every OTHER field is determined purely by the order inputs
    and the (pass-through) gate decision, so we compare on those.
    """
    return record.model_dump(exclude={"timestamp"})


# --------------------------------------------------------------------------- #
# 1) THE parity-unchanged proof: preflight is a PASS-THROUGH when nothing breaches
# --------------------------------------------------------------------------- #
async def test_preflight_is_pass_through_when_nothing_breached(data_dir: Path) -> None:
    """Generous (non-binding) limits + kill switch off == NO guardrail at all.

    The most important Phase 6 QA assertion: wrapping ``place_order`` in
    ``SafetyController.preflight`` must NOT change the resulting paper OrderRecord
    when nothing is breached. We run the identical paper order on two independent
    apps -- one with risk limits set GENEROUSLY (non-binding) and the kill switch
    explicitly disengaged, one with NO limits configured at all -- and assert the
    two OrderRecords are byte-identical on every field but the wall-clock timestamp.

    If they differ, the guardrail wiring perturbed an observable on the paper path
    (a bug to localize), not just the decision metadata.
    """
    _seed(data_dir)
    order_args: dict[str, Any] = {
        "symbol": _SYMBOL,
        "side": "buy",
        "amount": 0.25,
        "type": "limit",
        "price": 100.0,
        "seq": 1,  # pin the COID so both paths derive the IDENTICAL client order id
    }

    # Path A: NO limits set, kill switch never touched (the bare gate).
    app_bare = build_app()
    info_a = await _start_paper(app_bare)
    rec_a = OrderRecord.model_validate(
        _structured(
            await app_bare.call_tool("place_order", {"session_id": info_a.session_id, **order_args})
        )
    )

    # Path B: GENEROUS, non-binding limits + an explicit disengaged kill switch.
    app_guarded = build_app()
    await app_guarded.call_tool(
        "set_risk_limits",
        {
            "max_order_notional": 1_000_000.0,
            "max_position_notional": 1_000_000.0,
            "max_open_positions": 1000,
            "max_daily_loss": 1_000_000.0,
        },
    )
    # Reset (idempotent) to assert the switch is provably disengaged on this scope.
    await app_guarded.call_tool("kill_switch", {"action": "reset", "exchange": _EXCHANGE})
    info_b = await _start_paper(app_guarded)
    rec_b = OrderRecord.model_validate(
        _structured(
            await app_guarded.call_tool(
                "place_order", {"session_id": info_b.session_id, **order_args}
            )
        )
    )

    # The guardrail is a pure pass-through: every observable order field matches.
    assert _record_without_timestamp(rec_a) == _record_without_timestamp(rec_b), (
        "guardrail wiring changed an observable paper OrderRecord when nothing was "
        f"breached: bare={_record_without_timestamp(rec_a)} "
        f"guarded={_record_without_timestamp(rec_b)}"
    )
    # Both simulated, same COID (so we know we compared the same logical order).
    assert rec_a.simulated is True
    assert rec_b.simulated is True
    assert rec_a.client_order_id == rec_b.client_order_id

    # And the broker STATE after the order matches too (open orders byte-for-byte
    # modulo timestamp) -- the wrapper does not perturb the paper book.
    open_a = _structured(
        await app_bare.call_tool("get_open_orders", {"session_id": info_a.session_id})
    )
    open_b = _structured(
        await app_guarded.call_tool("get_open_orders", {"session_id": info_b.session_id})
    )
    assert open_a["count"] == open_b["count"] == 1
    a_orders = [_record_without_timestamp(OrderRecord.model_validate(o)) for o in open_a["orders"]]
    b_orders = [_record_without_timestamp(OrderRecord.model_validate(o)) for o in open_b["orders"]]
    assert a_orders == b_orders


# --------------------------------------------------------------------------- #
# 2) The full operator story, end to end, with an ORDERED audit-trail assertion.
# --------------------------------------------------------------------------- #
async def test_full_operator_flow_audit_trail_ordered(seeded_app: Any) -> None:
    """set limits -> arm -> place (pass) -> breach/deny -> kill (cancel) -> reset -> flow.

    Exercises every safety tool TOGETHER against one paper session and asserts the
    audit trail records the operator's actions in the expected ORDER, and that no
    entry carries secret-shaped material.
    """
    app = seeded_app

    # -- set risk limits: small order ok, but a big one will breach max_order_notional.
    await app.call_tool(
        "set_risk_limits",
        {"max_order_notional": 100.0, "max_open_positions": 5},
    )

    # -- arm live trading (correct phrase). Guardrails-only: gates nothing, but audits.
    await app.call_tool(
        "arm_live_trading",
        {"exchange": _EXCHANGE, "confirm": REQUIRED_CONFIRMATION, "ttl_seconds": 300},
    )

    info = await _start_paper(app)

    # -- a passing order: notional = 0.5 * 100.0 = 50.0 <= 100.0 -> simulated.
    passing = OrderRecord.model_validate(
        await _place(app, info.session_id, amount=0.5, type="limit", price=100.0, seq=1)
    )
    assert passing.simulated is True

    # -- a breaching order: notional = 2.0 * 100.0 = 200.0 > 100.0 -> DENIED (redacted).
    with pytest.raises(Exception):  # noqa: PT011,B017 -- SafetyError lives behind the SDK
        await _place(app, info.session_id, amount=2.0, type="limit", price=100.0, seq=2)

    # -- engage the kill switch for the scope -> denies new orders + cancels open ones.
    ks = _structured(
        await app.call_tool(
            "kill_switch", {"action": "engage", "exchange": _EXCHANGE, "reason": "operator halt"}
        )
    )
    assert ks["engaged"] is True
    assert ks["orders_canceled"] >= 1  # the passing order was open and got canceled

    # -- a subsequent order on the halted scope is denied.
    with pytest.raises(Exception):  # noqa: PT011,B017
        await _place(app, info.session_id, amount=0.5, type="limit", price=100.0, seq=3)

    # -- open orders are now empty (the passing order was canceled by the halt).
    open_now = _structured(await app.call_tool("get_open_orders", {"session_id": info.session_id}))
    assert open_now["count"] == 0

    # -- reset the halt -> orders flow again.
    await app.call_tool("kill_switch", {"action": "reset", "exchange": _EXCHANGE})
    after = OrderRecord.model_validate(
        await _place(app, info.session_id, amount=0.5, type="limit", price=100.0, seq=4)
    )
    assert after.simulated is True

    # ---- AUDIT TRAIL: assert the operator's actions appear in the expected order.
    full = _structured(await app.call_tool("get_audit_log", {}))
    events = [e["event"] for e in full["entries"]]

    # The exact ordered backbone of the operator story (a subsequence of the trail;
    # order_result entries interleave, which we tolerate via subsequence matching).
    expected_backbone = [
        "set_risk_limits",  # configure caps
        "arm",  # explicit opt-in
        "order_intent",  # passing order admitted by the gate
        "denied",  # breaching order rejected on the risk rung
        "kill_switch",  # halt engaged
        "denied",  # order on the halted scope rejected
        "kill_switch",  # halt reset
        "order_intent",  # order flows again after reset
    ]
    assert _is_subsequence(expected_backbone, events), (
        f"audit backbone {expected_backbone} is not an ordered subsequence of {events}"
    )

    # The first denial is a RISK breach; the second is the KILL SWITCH halt.
    denials = [e for e in full["entries"] if e["event"] == "denied"]
    assert len(denials) >= 2
    assert "risk limit" in (denials[0].get("detail") or "")
    assert "kill switch" in (denials[1].get("detail") or "").lower()

    # A passing order produced an order_result (the broker ack) too.
    results = _structured(await app.call_tool("get_audit_log", {"event": "order_result"}))
    assert results["count"] >= 2  # the first passing order + the post-reset order

    # ---- NO SECRET-SHAPED MATERIAL anywhere in the (paper) audit trail.
    _assert_audit_has_no_secret_material(full["entries"])


# --------------------------------------------------------------------------- #
# 3) Arm expiry surfaced in status; disarm empties it (no wall-clock sleep).
# --------------------------------------------------------------------------- #
async def test_arm_reflected_in_status_then_disarm_empties(seeded_app: Any) -> None:
    """arm (tiny ttl) is reflected in get_safety_status().armed; disarm empties it.

    We do NOT sleep on the wall clock to observe real expiry -- the controller stamps
    ``now`` internally and we can't inject it here. Time-advance expiry is covered by
    ``tests/safety/test_arming.py``. Here we assert the arm's PRESENCE (armed_until >
    armed_at, surfaced in status) and that ``disarm_live_trading`` removes it.
    """
    app = seeded_app
    out = _structured(
        await app.call_tool(
            "arm_live_trading",
            {"exchange": _EXCHANGE, "confirm": REQUIRED_CONFIRMATION, "ttl_seconds": 1},
        )
    )
    armed_at = datetime.fromisoformat(out["armed_at"])
    armed_until = datetime.fromisoformat(out["armed_until"])
    assert armed_until > armed_at  # a positive (if tiny) lifetime

    status = _structured(await app.call_tool("get_safety_status", {}))
    assert any(t["exchange"] == _EXCHANGE for t in status["armed"])

    # Disarm empties the ticket from the status snapshot.
    await app.call_tool("disarm_live_trading", {"exchange": _EXCHANGE})
    status2 = _structured(await app.call_tool("get_safety_status", {}))
    assert not any(t["exchange"] == _EXCHANGE for t in status2["armed"])


# --------------------------------------------------------------------------- #
# 4) Regression: with no limits + kill switch off, a paper deploy runs unchanged.
# --------------------------------------------------------------------------- #
async def test_paper_deploy_regression_no_limits_kill_off(data_dir: Path) -> None:
    """A normal paper deploy_strategy run is byte-stable under the guardrails.

    With NO risk limits configured and the kill switch never engaged, the Phase 5
    paper-deploy path must be untouched by the Phase 6 wiring: the same interpreter/
    runtime consumes the same cached bars and produces the same bars_processed,
    orders_submitted, trades_closed, and realized/unrealized PnL on two independent
    runs. (The deploy path does not pre-screen each runtime order through preflight;
    this regression guards that the guardrail wiring did not silently change the
    paper replay accounting.)
    """
    snapshots: list[tuple[int, int, int, float, float]] = []
    for _ in range(2):
        _seed(data_dir)
        app = build_app()
        info = await _start_paper(app)
        deployed = SessionInfo.model_validate(
            _structured(
                await app.call_tool(
                    "deploy_strategy",
                    {"session_id": info.session_id, "strategy_name": _STRATEGY},
                )
            )
        )
        assert deployed.strategy_name == _STRATEGY

        # Drain the finite cached feed, then stop.
        for _ in range(_N_BARS + 50):
            await asyncio.sleep(0)
        status = SessionStatus.model_validate(
            _structured(await app.call_tool("stop_strategy", {"session_id": info.session_id}))
        )
        assert status.bars_processed == _N_BARS
        assert status.error is None

        pnl = _structured(await app.call_tool("get_pnl", {"session_id": info.session_id}))
        snapshots.append(
            (
                status.bars_processed,
                status.orders_submitted,
                status.trades_closed,
                round(float(pnl["realized"]), 8),
                round(float(pnl["unrealized"]), 8),
            )
        )
        get_settings.cache_clear()

    # Determinism: two independent paper deploys agree on every accounting figure.
    assert snapshots[0] == snapshots[1], f"paper deploy drifted across runs: {snapshots}"
    # Non-vacuous: the strategy actually traded over the synthetic window.
    assert snapshots[0][1] > 0, "degenerate: deploy submitted no orders to compare"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """True iff ``needle`` appears as an ordered (not necessarily contiguous) subsequence."""
    it = iter(haystack)
    return all(token in it for token in needle)


def _assert_audit_has_no_secret_material(entries: list[dict[str, Any]]) -> None:
    """Assert no audit entry carries secret-shaped material.

    Paper sessions carry NO credentials, so nothing should ever be redacted on this
    path. We assert (a) the fixture's known secret token never appears, (b) the
    redaction placeholder never appears (it would mean a secret reached a detail
    string), and (c) no key-ish marker leaks. We probe the FULL serialized entry,
    not just ``detail``, so a leak into any field trips the check.
    """
    for entry in entries:
        blob = json.dumps(entry, default=str)
        assert SECRET_TOKEN not in blob, f"secret token leaked into audit entry: {entry!r}"
        assert _REDACTION_PLACEHOLDER not in blob, (
            f"a secret reached an audit detail (placeholder present): {entry!r}"
        )
        lowered = blob.lower()
        assert "apikey=" not in lowered, f"key-ish material in audit entry: {entry!r}"
        assert "api_key=" not in lowered, f"key-ish material in audit entry: {entry!r}"
