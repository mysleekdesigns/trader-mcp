"""Unit tests for the TradingView cross-validation oracle helper (PRD §6 Phase 8).

These exercise the PARSER + MATCHER on synthetic data so the machinery is proven without
any TradingView export -- the real trade-level comparison lives in
``test_crossvalidate_tradingview.py`` and skips until a fixture is dropped in. Keeping the
logic here (pure functions, no engine import) is what lets us validate it offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from tests._tradingview_oracle import (
    TvTrade,
    bar_index_at,
    compare_trades,
    parse_tradingview_trades,
)


@dataclass
class _FakeTrade:
    """Minimal stand-in for engine ``SimulatedTrade`` (matches the ``_OurTrade`` protocol)."""

    side: str
    entry_time: datetime
    exit_time: datetime
    pnl_pct: float  # PERCENT, like the real engine model (pnl/notional*100)


def _t(hour: int) -> datetime:
    return datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=hour)


_BARS = [_t(h) for h in range(48)]


# ------------------------------------------------------------------------------ parsing

# A realistic two-row-per-trade TradingView "List of Trades" export (Trade #, P&L on exit).
_CSV_STANDARD = """Trade #,Type,Signal,Date/Time,Price USD,Contracts,Profit %,Cumulative profit %
1,Entry Long,Long,2024-01-01 02:00,100.0,1,,
1,Exit Long,Long,2024-01-01 06:00,110.0,1,10.0,10.0
2,Entry Long,Long,2024-01-01 10:00,110.0,1,,
2,Exit Long,Long,2024-01-01 14:00,104.5,1,-5.0,4.5
"""

# A column-name variant: lowercase "Entry long", "Net P&L %", ISO timestamps with tz.
_CSV_VARIANT = """Trade #,Type,Date/Time,Price,Qty,Net P&L %
1,Exit long,2024-01-01T06:00:00Z,110.0,1,10.0
1,Entry long,2024-01-01T02:00:00Z,100.0,1,
"""


def test_parse_standard_export_pairs_entry_and_exit() -> None:
    trades = parse_tradingview_trades(_CSV_STANDARD)
    assert len(trades) == 2
    first = trades[0]
    assert first.side == "long"
    assert first.entry_time == _t(2)
    assert first.exit_time == _t(6)
    assert first.entry_price == pytest.approx(100.0)
    assert first.exit_price == pytest.approx(110.0)
    assert first.pnl_pct == pytest.approx(10.0)  # PERCENT, as TradingView reports it
    assert trades[1].pnl_pct == pytest.approx(-5.0)


def test_parse_tolerates_column_variants_and_row_order() -> None:
    # Exit row precedes entry row, lowercase type, "Net P&L %", "Price", tz-aware ISO.
    trades = parse_tradingview_trades(_CSV_VARIANT)
    assert len(trades) == 1
    assert trades[0].entry_time == _t(2)  # paired correctly despite exit-first ordering
    assert trades[0].exit_time == _t(6)
    assert trades[0].pnl_pct == pytest.approx(10.0)


def test_parse_handles_parenthesized_negative_and_currency_suffix() -> None:
    csv_text = (
        "Trade #,Type,Date/Time,Price USD,Profit %\n"
        "1,Entry Long,2024-01-01 02:00,1,234.50 USD,\n"  # commas + currency code
    ).replace("1,234.50 USD", '"1,234.50 USD"')
    csv_text += "1,Exit Long,2024-01-01 06:00,1300,(2.50)\n"  # parenthesized negative %
    trades = parse_tradingview_trades(csv_text)
    assert trades[0].entry_price == pytest.approx(1234.50)
    assert trades[0].pnl_pct == pytest.approx(-2.50)


def test_parse_drops_open_final_trade() -> None:
    # A final entry with no exit row = position still open at end of window -> dropped,
    # not an error (this is how TradingView lists an un-closed trade).
    open_tail = _CSV_STANDARD + "3,Entry Long,Long,2024-01-01 20:00,104.5,1,,\n"
    trades = parse_tradingview_trades(open_tail)
    assert len(trades) == 2  # the open trade #3 is dropped


def test_parse_raises_on_exit_without_entry() -> None:
    bad = "Trade #,Type,Date/Time,Price USD,Profit %\n1,Exit Long,2024-01-01 06:00,110,2.0\n"
    with pytest.raises(ValueError, match="exit leg but no entry"):
        parse_tradingview_trades(bad)


def test_parse_raises_on_missing_columns() -> None:
    with pytest.raises(ValueError, match="missing column"):
        parse_tradingview_trades("Foo,Bar\n1,2\n")


# ------------------------------------------------------------------------- bar indexing


def test_bar_index_at_snaps_to_nearest_bar() -> None:
    assert bar_index_at(_BARS, _t(5)) == 5
    # 5h30m is closer to bar 5 than bar 6 -> 5; exactly between ties to the earlier bar.
    assert bar_index_at(_BARS, _t(5) + timedelta(minutes=20)) == 5
    assert bar_index_at(_BARS, _t(5) + timedelta(minutes=40)) == 6
    assert bar_index_at(_BARS, _t(-3)) == 0  # before range -> first
    assert bar_index_at(_BARS, _t(999)) == len(_BARS) - 1  # after range -> last


# --------------------------------------------------------------------------- comparison


def test_compare_clean_match_has_no_mismatches() -> None:
    ours = [
        _FakeTrade("long", _t(2), _t(6), 10.0),  # +10% (PERCENT, like the engine)
        _FakeTrade("long", _t(10), _t(14), -5.0),
    ]
    theirs = parse_tradingview_trades(_CSV_STANDARD)
    assert compare_trades(ours, theirs, _BARS) == []


def test_compare_tolerates_one_bar_drift() -> None:
    ours = [_FakeTrade("long", _t(3), _t(6), 10.0), _FakeTrade("long", _t(10), _t(15), -5.0)]
    theirs = parse_tradingview_trades(_CSV_STANDARD)
    # entry off by 1 bar, exit off by 1 bar -- both within the default +/-1 tolerance.
    assert compare_trades(ours, theirs, _BARS) == []


def test_compare_flags_bar_drift_beyond_tolerance() -> None:
    ours = [_FakeTrade("long", _t(5), _t(6), 10.0), _FakeTrade("long", _t(10), _t(14), -5.0)]
    theirs = parse_tradingview_trades(_CSV_STANDARD)
    fields = {m.field for m in compare_trades(ours, theirs, _BARS)}
    assert "entry_bar" in fields  # entry is 3 bars early


def test_compare_flags_side_and_return_and_count() -> None:
    ours = [_FakeTrade("short", _t(2), _t(6), 50.0)]  # wrong side, wildly wrong return, 1 vs 2
    theirs = parse_tradingview_trades(_CSV_STANDARD)
    fields = {m.field for m in compare_trades(ours, theirs, _BARS)}
    assert {"side", "return_pct", "trade_count"} <= fields


def test_compare_return_uses_relative_tolerance_for_large_moves() -> None:
    # TradingView +50%; ours +49% -> within 5% relative (2.5pp), should pass.
    ours = [_FakeTrade("long", _t(2), _t(6), 49.0)]
    theirs = [TvTrade("long", _t(2), _t(6), 100.0, 150.0, 50.0)]
    assert compare_trades(ours, theirs, _BARS) == []
    # ours +40% vs +50% -> 10pp gap, exceeds max(0.5pp, 2.5pp) -> flagged.
    ours_bad = [_FakeTrade("long", _t(2), _t(6), 40.0)]
    assert any(m.field == "return_pct" for m in compare_trades(ours_bad, theirs, _BARS))
