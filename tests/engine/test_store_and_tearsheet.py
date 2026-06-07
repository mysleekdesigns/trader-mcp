"""Backtest report store, comparison, and tear-sheet generation."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from trader_mcp.engine import (
    BacktestStore,
    compare_reports,
    generate_tearsheet,
    run_backtest,
)
from trader_mcp.errors import ValidationError
from trader_mcp.strategy import (
    DCAConfig,
    EntryRules,
    ExitRules,
    IndicatorSpec,
    PositionSizing,
    StrategySpec,
)

from .conftest import make_bars


def _spec(name: str = "ma-cross") -> StrategySpec:
    return StrategySpec(
        name=name,
        symbol="BTC/USD",
        timeframe="1h",
        strategy_type="rule",
        indicators=[
            IndicatorSpec(id="fast", kind="sma", params={"length": 5}),
            IndicatorSpec(id="slow", kind="sma", params={"length": 20}),
        ],
        entry=EntryRules(long="crossover(fast, slow)"),
        exit=ExitRules(long="crossunder(fast, slow)"),
        position_sizing=PositionSizing(mode="percent_equity", value=50),
    )


@pytest.fixture
def report():
    closes = [100 + 10 * math.sin(i / 10.0) for i in range(300)]
    bars = make_bars(closes, open_from_prev=True)
    return run_backtest(_spec(), bars)


def test_store_roundtrip(report, tmp_path: Path) -> None:
    store = BacktestStore(data_dir=tmp_path)
    assert not store.exists(report.report_id)
    path = store.save(report)
    assert path.is_file()
    assert store.exists(report.report_id)
    loaded = store.load(report.report_id)
    assert loaded.report_id == report.report_id
    assert loaded.final_equity == report.final_equity
    assert store.list_ids() == [report.report_id]
    assert store.delete(report.report_id)
    assert not store.exists(report.report_id)


def test_store_load_missing_raises(tmp_path: Path) -> None:
    store = BacktestStore(data_dir=tmp_path)
    with pytest.raises(ValidationError):
        store.load("does-not-exist")


def test_compare_reports_ranks_by_objective(report) -> None:
    closes = [100 + 5 * math.cos(i / 8.0) for i in range(300)]
    other = run_backtest(_spec("other"), make_bars(closes, open_from_prev=True))
    cmp = compare_reports([report, other], objective="total_return_pct")
    assert cmp.best in {report.report_id, other.report_id}
    assert len(cmp.reports) == 2
    # The 'best' really is the max objective_value row.
    best_row = max(cmp.reports, key=lambda r: r.objective_value)
    assert cmp.best == best_row.report_id


def test_compare_drawdown_ranks_ascending(report) -> None:
    closes = [100 + 5 * math.cos(i / 8.0) for i in range(300)]
    other = run_backtest(_spec("other"), make_bars(closes, open_from_prev=True))
    cmp = compare_reports([report, other], objective="max_drawdown_pct")
    best_row = min(cmp.reports, key=lambda r: r.objective_value)
    assert cmp.best == best_row.report_id


def test_compare_empty() -> None:
    cmp = compare_reports([], objective="sharpe")
    assert cmp.best is None
    assert cmp.reports == []


# quantstats pulls in matplotlib; in the strict (filterwarnings=error) suite its
# plotting stack emits assorted deprecation/user warnings. Allow exactly those
# noisy-but-benign categories for this one render path rather than globally.
@pytest.mark.filterwarnings(
    "ignore::DeprecationWarning",
    "ignore::UserWarning",
    "ignore::FutureWarning",
    "ignore::RuntimeWarning",
)
def test_tearsheet_generates_html(report, tmp_path: Path) -> None:
    result = generate_tearsheet(report, data_dir=tmp_path)
    # Either the HTML was written, or it degraded gracefully with a note (never raises).
    if result.html_path is not None:
        assert Path(result.html_path).is_file()
    else:
        assert result.note is not None
    assert result.metrics == report.metrics


def test_tearsheet_degrades_on_tiny_curve(tmp_path: Path) -> None:
    # A 2-bar DCA spec (no indicators that need a warm-up window) yields a
    # 2-point equity curve -- too short for quantstats to render, so the tear
    # sheet must degrade gracefully rather than raise.
    dca = StrategySpec(
        name="dca-tiny",
        symbol="BTC/USD",
        timeframe="1h",
        strategy_type="dca",
        dca=DCAConfig(amount_quote=100.0, interval_bars=1),
    )
    bars = make_bars([100.0, 101.0], open_from_prev=True)
    rep = run_backtest(dca, bars)
    result = generate_tearsheet(rep, data_dir=tmp_path)
    assert result.html_path is None
    assert result.note is not None
