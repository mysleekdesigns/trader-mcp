"""Optimizer + walk-forward: determinism, leakage guards, param application."""

from __future__ import annotations

import math

import pytest

from trader_mcp.engine import ParamSpec, optimize_strategy
from trader_mcp.errors import ValidationError
from trader_mcp.strategy import EntryRules, ExitRules, IndicatorSpec, PositionSizing, StrategySpec

from .conftest import make_bars


def _spec() -> StrategySpec:
    return StrategySpec(
        name="rsi-mr",
        symbol="BTC/USD",
        timeframe="1h",
        strategy_type="rule",
        indicators=[IndicatorSpec(id="rsi", kind="rsi", params={"length": 14})],
        entry=EntryRules(long="rsi < 30"),
        exit=ExitRules(long="rsi > 55"),
        position_sizing=PositionSizing(mode="percent_equity", value=50),
    )


@pytest.fixture
def trend_bars() -> list:
    closes = [100 + 10 * math.sin(i / 12.0) + 0.02 * i for i in range(300)]
    return make_bars(closes, open_from_prev=True)


# Optuna emits a benign ExperimentalWarning on some samplers; keep the strict
# global filterwarnings=error but allow exactly this narrow category here.
@pytest.mark.filterwarnings("ignore::FutureWarning")
def test_optimize_is_deterministic(trend_bars: list) -> None:
    spec = _spec()
    params = [ParamSpec(path="indicators.rsi.length", low=7, high=21, is_int=True)]
    a = optimize_strategy(spec, trend_bars, params, objective="sharpe", n_trials=6)
    b = optimize_strategy(spec, trend_bars, params, objective="sharpe", n_trials=6)
    assert a.best_params == b.best_params
    assert a.best_value == b.best_value


def test_walk_forward_folds_do_not_overlap(trend_bars: list) -> None:
    spec = _spec()
    params = [ParamSpec(path="indicators.rsi.length", low=7, high=21, is_int=True)]
    res = optimize_strategy(
        spec, trend_bars, params, objective="sharpe", n_trials=4, walk_forward_folds=3
    )
    assert res.folds, "expected walk-forward folds"
    for f in res.folds:
        # Train window strictly precedes its test window (no leakage).
        assert f.train_end is not None
        assert f.test_start is not None
        assert f.train_end <= f.test_start


def test_optimize_rejects_empty_params(trend_bars: list) -> None:
    with pytest.raises(ValidationError):
        optimize_strategy(_spec(), trend_bars, [], objective="sharpe", n_trials=2)


def test_optimize_rejects_unknown_objective(trend_bars: list) -> None:
    params = [ParamSpec(path="indicators.rsi.length", low=7, high=21, is_int=True)]
    with pytest.raises(ValidationError):
        optimize_strategy(_spec(), trend_bars, params, objective="not_a_metric", n_trials=2)


def test_param_spec_rejects_inverted_bounds() -> None:
    with pytest.raises(ValueError, match="high"):
        ParamSpec(path="indicators.rsi.length", low=21, high=7, is_int=True)


def test_bad_param_path_raises(trend_bars: list) -> None:
    params = [ParamSpec(path="indicators.nope.length", low=7, high=21, is_int=True)]
    with pytest.raises(ValidationError):
        optimize_strategy(_spec(), trend_bars, params, objective="sharpe", n_trials=2)
