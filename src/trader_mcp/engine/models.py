"""Typed Pydantic v2 result models for the backtest/optimize engine (PRD §6 Phase 4).

These are the typed inputs/outputs the MCP-server engineer wraps as tool results
and exposes as backtest-report resources, and the surface the QA/parity engineer
asserts against. They are deliberately small, frozen, and ``extra="forbid"``
(mirroring :class:`trader_mcp.data.models._DataModel`) so a construction mistake
fails loudly rather than silently producing a malformed report.

All timestamps are timezone-aware UTC :class:`datetime`. Money/size values are
plain floats in the instrument's quote/base currency.

DETERMINISM: a :class:`BacktestReport` carries a :attr:`report_id` that is a stable
hash of ``(spec, data window, config)`` -- the same inputs always produce the same
id and the same numbers (see :mod:`trader_mcp.engine.backtest`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Why a position was closed. ``signal`` = a rule exit / opposite entry;
#: ``stop_loss``/``take_profit`` = a risk-limit exit; ``end_of_data`` = the
#: backtest window ended with the position still open (force-closed at the last
#: bar's close for accounting).
ExitReason = Literal["signal", "stop_loss", "take_profit", "end_of_data"]

#: Position side. v1 supports long and short (short only when a spec defines a
#: ``short`` entry rule and, for perps, an allowed leverage).
TradeSide = Literal["long", "short"]


class _EngineModel(BaseModel):
    """Base for engine result models: frozen and strict on declared fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class BacktestConfig(_EngineModel):
    """Knobs controlling a single backtest run (fill realism + reproducibility).

    The defaults are the documented, cross-validatable baseline:

    * ``initial_cash`` -- starting equity in quote currency.
    * ``slippage_pct`` -- a symmetric fraction applied adversarially to every fill
      price (buys fill higher, sells fill lower) on top of fees. ``0`` reproduces
      backtesting.py's frictionless fill for parity checks.
    * ``seed`` -- RNG seed. The engine is deterministic regardless (no randomness
      in the fill path), but the seed is recorded and threaded into Optuna so a
      sweep is reproducible.
    * ``funding_enabled`` -- whether to apply perpetual-swap funding (only takes
      effect for swap symbols; see :mod:`trader_mcp.engine.broker`).
    * ``funding_rate`` -- the per-interval funding rate fraction used when no live
      funding history is available (backtests run offline on cached OHLCV, which
      carries no funding stream, so this is a flat assumed rate -- documented).
    * ``funding_interval_hours`` -- funding cadence in hours (Coinbase/Kraken
      perps fund every 8h; configurable).
    """

    initial_cash: float = Field(default=10_000.0, gt=0)
    slippage_pct: float = Field(default=0.0, ge=0, le=100)
    seed: int = Field(default=0, ge=0)
    funding_enabled: bool = True
    funding_rate: float = Field(default=0.0001, ge=-1, le=1)
    funding_interval_hours: float = Field(default=8.0, gt=0)


class SimulatedTrade(_EngineModel):
    """One round-trip position the simulated broker opened and closed.

    ``size`` is the position size in base currency. ``pnl`` is realized profit in
    quote currency **net of all fees and funding** charged against the position;
    ``pnl_pct`` is that pnl as a fraction of the notional entry value
    (``entry_price * size``). ``fees_paid`` is the sum of entry + exit fees (and is
    already reflected in ``pnl``). ``funding_paid`` is the net funding charged over
    the hold (positive = paid out, reflected in ``pnl``); ``0`` for spot.
    """

    side: TradeSide
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    pnl_pct: float
    fees_paid: float
    funding_paid: float = 0.0
    bars_held: int
    exit_reason: ExitReason


class EquityPoint(_EngineModel):
    """One sample of the mark-to-market equity curve at a bar's close."""

    timestamp: datetime
    equity: float


class BacktestMetrics(_EngineModel):
    """Performance metrics computed natively from the equity curve + trades.

    All percentages are in percent units (e.g. ``12.5`` == 12.5%), not fractions.
    See :mod:`trader_mcp.engine.metrics` for the exact formulas and the
    annualization / risk-free assumptions. ``sharpe``/``sortino``/``calmar`` are
    ``0.0`` when undefined (e.g. zero volatility or no drawdown);
    ``profit_factor`` is ``inf`` when there are wins but no losses.
    """

    total_return_pct: float
    cagr_pct: float
    volatility_pct: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown_pct: float
    win_rate_pct: float
    profit_factor: float
    avg_trade_pct: float
    exposure_pct: float
    trade_count: int


class BacktestReport(_EngineModel):
    """The full, typed outcome of one backtest run (a saved/served resource).

    ``report_id`` is a deterministic hash of the (spec, data window, config) so the
    same backtest always addresses the same report. ``equity_curve`` is sampled at
    every bar close; ``trades`` is the closed round-trips. ``note`` carries any
    advisory (e.g. "position force-closed at end of data").
    """

    report_id: str
    strategy_name: str
    exchange: str
    symbol: str
    timeframe: str
    strategy_type: str
    start: datetime | None
    end: datetime | None
    bars: int
    initial_cash: float
    final_equity: float
    metrics: BacktestMetrics
    trades: list[SimulatedTrade]
    equity_curve: list[EquityPoint]
    config: BacktestConfig
    created: datetime
    note: str | None = None


# --------------------------------------------------------------------------- #
# Optimization / walk-forward models
# --------------------------------------------------------------------------- #
class ParamSpec(_EngineModel):
    """A tunable parameter for :func:`trader_mcp.engine.optimize.optimize_strategy`.

    ``path`` is a dotted address into the spec that Optuna varies. Supported
    targets (validated by the optimizer): an indicator param
    (``indicators.<id>.<param>``, e.g. ``indicators.rsi.length``), a risk limit
    (``risk.stop_loss_pct`` / ``risk.take_profit_pct`` / ``risk.max_leverage``), or
    a sizing value (``position_sizing.value``).

    ``low``/``high`` bound the search (inclusive). ``step`` is an optional grid
    step; when ``is_int`` is true the param is sampled as an integer. ``low`` must
    be ``<= high``.
    """

    path: str = Field(min_length=1)
    low: float
    high: float
    step: float | None = Field(default=None, gt=0)
    is_int: bool = False

    def _check(self) -> None:  # pragma: no cover - exercised via model_validator
        if self.high < self.low:
            raise ValueError(f"ParamSpec {self.path!r}: high ({self.high}) < low ({self.low})")

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
        self._check()


class OptimizeTrial(_EngineModel):
    """One evaluated parameter set in an optimization run."""

    number: int
    params: dict[str, float]
    value: float


class WalkForwardFold(_EngineModel):
    """One train/test split of a walk-forward analysis (no window overlap).

    ``train_start``/``train_end`` bound the in-sample window the params were fit
    on; ``test_start``/``test_end`` bound the out-of-sample window the fitted
    params were then scored on. ``best_params`` are the in-sample winners;
    ``train_value`` / ``test_value`` are the objective on each window (the gap
    between them is the overfitting signal QA/users should read).
    """

    fold: int
    train_start: datetime | None
    train_end: datetime | None
    test_start: datetime | None
    test_end: datetime | None
    best_params: dict[str, float]
    train_value: float
    test_value: float


class OptimizeResult(_EngineModel):
    """The typed outcome of a parameter sweep and/or walk-forward analysis.

    ``best_params`` / ``best_value`` are the full-sample sweep winner under
    ``objective`` (a :class:`BacktestMetrics` field name, maximized). ``trials`` is
    every evaluated set (sweep). ``folds`` is the walk-forward breakdown when
    requested (empty otherwise). ``backend`` records whether the optional vectorbt
    accelerator was used or the native+Optuna source-of-truth path.
    """

    strategy_name: str
    objective: str
    best_params: dict[str, float]
    best_value: float
    trials: list[OptimizeTrial]
    folds: list[WalkForwardFold]
    backend: Literal["native", "vectorbt"]
    created: datetime
    note: str | None = None


class TearsheetResult(_EngineModel):
    """The result of generating a tear sheet for a report.

    ``html_path`` is the written HTML file (``None`` when quantstats was
    unavailable and generation degraded gracefully). ``metrics`` echoes the
    report's native metrics so a caller always gets numbers even without the HTML.
    ``note`` explains any degradation.
    """

    report_id: str
    html_path: str | None
    metrics: BacktestMetrics
    note: str | None = None


class BacktestComparison(_EngineModel):
    """A side-by-side comparison of several backtest reports (the ``compare`` tool).

    ``reports`` lists one row per compared report with its key metrics; ``best`` is
    the ``report_id`` that maximizes the chosen ``objective`` metric.
    """

    objective: str
    best: str | None
    reports: list[BacktestComparisonRow]


class BacktestComparisonRow(_EngineModel):
    """One report's headline numbers within a :class:`BacktestComparison`."""

    report_id: str
    strategy_name: str
    symbol: str
    timeframe: str
    total_return_pct: float
    cagr_pct: float
    sharpe: float
    max_drawdown_pct: float
    win_rate_pct: float
    trade_count: int
    objective_value: float


# Resolve the forward reference used in BacktestComparison.
BacktestComparison.model_rebuild()
