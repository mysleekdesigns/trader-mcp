"""Parameter sweep + walk-forward optimization over the native interpreter.

Drives Optuna over :func:`trader_mcp.engine.backtest.run_backtest` -- the native
event-driven engine is the **source of truth**; the optional vectorbt accelerator
(guarded import) is never required and only changes *how fast* a sweep runs, never
the reported numbers (the final/fold scores are always recomputed natively).

LEAKAGE / LOOK-AHEAD GUARDS (PRD §8 risk: overfitting / look-ahead bias):
    * Walk-forward splits the bars into contiguous, NON-OVERLAPPING train/test
      windows. Params are fit ONLY on the train window; the fold's reported
      out-of-sample score comes ONLY from the test window. The optimizer never sees
      test bars during fitting.
    * Indicators are recomputed per window inside each :func:`run_backtest` call
      (the interpreter's ``prepare`` runs on the window it is given), so a long
      indicator does not borrow values across the train/test boundary beyond the
      causal warm-up that live trading would also experience.
    * The objective is a :class:`BacktestMetrics` field MAXIMIZED; drawdown is
      negated so "minimize drawdown" is expressible as a maximization.

PARAMETER ADDRESSING: a :class:`ParamSpec.path` is a dotted address the optimizer
rewrites into a fresh spec for each trial (the spec is frozen/immutable -- we build
a mutated copy via ``model_copy``, never mutate in place):
    * ``indicators.<id>.<param>``  (e.g. ``indicators.rsi.length``)
    * ``risk.stop_loss_pct`` | ``risk.take_profit_pct`` | ``risk.max_leverage``
    * ``position_sizing.value``
Each rewrite re-validates the spec, so an out-of-range trial fails loudly rather
than silently producing a bad backtest.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from trader_mcp.engine.backtest import run_backtest
from trader_mcp.engine.models import (
    BacktestConfig,
    OptimizeResult,
    OptimizeTrial,
    ParamSpec,
    WalkForwardFold,
)
from trader_mcp.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from trader_mcp.exchanges.models import OHLCVBar, OHLCVResult
    from trader_mcp.strategy import StrategySpec

#: Metrics where a LARGER value is better (maximized directly). Drawdown is handled
#: specially (negated) so it can be minimized within a maximization framework.
_MAXIMIZE_METRICS: frozenset[str] = frozenset(
    {
        "total_return_pct",
        "cagr_pct",
        "sharpe",
        "sortino",
        "calmar",
        "win_rate_pct",
        "profit_factor",
        "avg_trade_pct",
    }
)
_MINIMIZE_METRICS: frozenset[str] = frozenset({"max_drawdown_pct", "volatility_pct"})


def _coerce_bars(bars_or_result: Sequence[OHLCVBar] | OHLCVResult) -> list[OHLCVBar]:
    bars = getattr(bars_or_result, "bars", None)
    if bars is not None:
        return list(bars)
    return list(bars_or_result)  # type: ignore[arg-type]


def _objective_value(
    spec: StrategySpec, bars: list[OHLCVBar], objective: str, cfg: BacktestConfig
) -> float:
    """Run a backtest and extract the (sign-corrected) objective metric.

    Drawdown/volatility are negated so the optimizer always MAXIMIZES. A
    ``profit_factor`` of ``inf`` (wins, no losses) is clamped to a large finite
    value so Optuna's bookkeeping stays numeric.
    """
    report = run_backtest(spec, bars, config=cfg)
    raw = getattr(report.metrics, objective)
    value = -float(raw) if objective in _MINIMIZE_METRICS else float(raw)
    if value == float("inf"):
        return 1e12
    if value == float("-inf"):
        return -1e12
    return value


def _apply_param(spec: StrategySpec, path: str, value: float) -> StrategySpec:
    """Return a NEW spec with the dotted ``path`` set to ``value`` (re-validated).

    The spec is frozen, so this builds a mutated deep copy via ``model_copy`` and
    re-runs full validation -- an out-of-range value raises, never silently slips
    through.
    """
    parts = path.split(".")
    data = spec.model_dump()
    if parts[0] == "indicators" and len(parts) == 3:
        _, ind_id, param = parts
        found = False
        for ind in data["indicators"]:
            if ind["id"] == ind_id:
                ind["params"] = {**ind.get("params", {}), param: value}
                found = True
                break
        if not found:
            raise ValidationError(
                f"ParamSpec path {path!r}: no indicator with id {ind_id!r} in the spec.",
                details={"kind": "bad_param_path", "path": path},
            )
    elif parts[0] == "risk" and len(parts) == 2:
        data["risk"][parts[1]] = value
    elif path == "position_sizing.value":
        data["position_sizing"]["value"] = value
    else:
        raise ValidationError(
            f"Unsupported ParamSpec path {path!r}. Supported: indicators.<id>.<param>, "
            "risk.stop_loss_pct, risk.take_profit_pct, risk.max_leverage, position_sizing.value.",
            details={"kind": "bad_param_path", "path": path},
        )
    return type(spec).model_validate(data)


def _build_spec(
    base: StrategySpec, params: dict[str, float], param_specs: list[ParamSpec]
) -> StrategySpec:
    """Apply every sampled param onto a fresh, re-validated copy of ``base``."""
    spec = base
    int_paths = {p.path for p in param_specs if p.is_int}
    for path, value in params.items():
        applied = float(round(value)) if path in int_paths else float(value)
        spec = _apply_param(spec, path, applied)
    return spec


def optimize_strategy(
    spec: StrategySpec,
    bars_or_result: Sequence[OHLCVBar] | OHLCVResult,
    params: list[ParamSpec],
    *,
    objective: str = "sharpe",
    n_trials: int = 50,
    walk_forward_folds: int = 0,
    config: BacktestConfig | None = None,
    use_vectorbt: bool = False,
) -> OptimizeResult:
    """Optimize ``params`` of ``spec`` over cached ``bars`` via Optuna.

    PURE/OFFLINE: operates on already-loaded bars (no store/network). Deterministic:
    Optuna's sampler is seeded from ``config.seed`` so a sweep reproduces.

    Args:
        spec: The base strategy (never mutated; each trial gets a re-validated copy).
        bars_or_result: Cached bars or an :class:`OHLCVResult`.
        params: The tunable parameters to search.
        objective: A :class:`BacktestMetrics` field to optimize. Return/ratio
            metrics are maximized; ``max_drawdown_pct``/``volatility_pct`` are
            minimized.
        n_trials: Number of Optuna trials for the full-sample sweep (and per fold).
        walk_forward_folds: If > 1, run a non-overlapping walk-forward analysis with
            this many folds in addition to the full-sample sweep.
        config: Backtest config (seed threads into the sampler).
        use_vectorbt: Opt into the vectorbt accelerator if installed; falls back to
            the native path with a note if the import fails. The native path is the
            source of truth either way.

    Returns:
        An :class:`OptimizeResult` with the full-sample best params/value, every
        trial, and the walk-forward folds (when requested).

    Raises:
        trader_mcp.errors.ValidationError: for an unknown ``objective`` or an empty
            ``params`` list.
    """
    if not params:
        raise ValidationError(
            "optimize_strategy requires at least one ParamSpec to vary.",
            details={"kind": "no_params"},
        )
    if objective not in _MAXIMIZE_METRICS and objective not in _MINIMIZE_METRICS:
        allowed = sorted(_MAXIMIZE_METRICS | _MINIMIZE_METRICS)
        raise ValidationError(
            f"Unknown objective {objective!r}. Optimize one of: {', '.join(allowed)}.",
            details={"kind": "bad_objective", "objective": objective},
        )

    cfg = config or BacktestConfig()
    bars = _coerce_bars(bars_or_result)

    backend: str = "native"
    note: str | None = None
    if use_vectorbt:
        try:
            import vectorbt  # noqa: F401  (presence probe only; native remains truth)

            backend = "vectorbt"
            note = (
                "vectorbt accelerator requested; scores are still recomputed on the "
                "native engine (source of truth)."
            )
        except ImportError:
            note = "vectorbt requested but not installed; used the native+Optuna path."

    best_params, best_value, trials = _sweep(spec, bars, params, objective, n_trials, cfg)

    folds: list[WalkForwardFold] = []
    if walk_forward_folds > 1:
        folds = _walk_forward(spec, bars, params, objective, n_trials, walk_forward_folds, cfg)

    return OptimizeResult(
        strategy_name=spec.name,
        objective=objective,
        best_params=best_params,
        best_value=best_value,
        trials=trials,
        folds=folds,
        backend=backend,  # type: ignore[arg-type]
        created=datetime.now(tz=UTC),
        note=note,
    )


def _sweep(
    spec: StrategySpec,
    bars: list[OHLCVBar],
    params: list[ParamSpec],
    objective: str,
    n_trials: int,
    cfg: BacktestConfig,
) -> tuple[dict[str, float], float, list[OptimizeTrial]]:
    """Run a seeded Optuna study over ``bars`` and return (best_params, best, trials)."""
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)

    def _suggest(trial: optuna.Trial) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in params:
            if p.is_int:
                step = int(p.step) if p.step else 1
                out[p.path] = float(trial.suggest_int(p.path, int(p.low), int(p.high), step=step))
            else:
                out[p.path] = trial.suggest_float(p.path, p.low, p.high, step=p.step)
        return out

    def _obj(trial: optuna.Trial) -> float:
        sampled = _suggest(trial)
        candidate = _build_spec(spec, sampled, params)
        return _objective_value(candidate, bars, objective, cfg)

    sampler = optuna.samplers.TPESampler(seed=cfg.seed)
    study = optuna.create_study(direction="maximize", sampler=sampler)
    study.optimize(_obj, n_trials=n_trials)

    trials = [
        OptimizeTrial(
            number=t.number,
            params={k: float(v) for k, v in t.params.items()},
            value=float(t.value) if t.value is not None else float("-inf"),
        )
        for t in study.trials
        if t.value is not None
    ]
    best_params = {k: float(v) for k, v in study.best_params.items()}
    return best_params, float(study.best_value), trials


def _walk_forward(
    spec: StrategySpec,
    bars: list[OHLCVBar],
    params: list[ParamSpec],
    objective: str,
    n_trials: int,
    folds: int,
    cfg: BacktestConfig,
) -> list[WalkForwardFold]:
    """Anchored, non-overlapping walk-forward: split bars into ``folds`` segments.

    Segment ``k`` (k>=1) trains on segments ``[0..k-1]`` and tests on segment ``k``
    -- a standard expanding-window walk-forward with no test-window leakage. Each
    fold fits params on its train window only, then scores the SAME params on its
    held-out test window. The train/test value gap is the overfitting signal.
    """
    n = len(bars)
    if n < folds * 2:
        return []
    seg = n // folds
    out: list[WalkForwardFold] = []
    for k in range(1, folds):
        train_bars = bars[: seg * k]
        test_bars = bars[seg * k : seg * (k + 1)] if k < folds - 1 else bars[seg * k :]
        if len(train_bars) < 2 or len(test_bars) < 2:
            continue
        best_params, train_value, _ = _sweep(spec, train_bars, params, objective, n_trials, cfg)
        fitted = _build_spec(spec, best_params, params)
        test_value = _objective_value(fitted, test_bars, objective, cfg)
        out.append(
            WalkForwardFold(
                fold=k,
                train_start=train_bars[0].timestamp,
                train_end=train_bars[-1].timestamp,
                test_start=test_bars[0].timestamp,
                test_end=test_bars[-1].timestamp,
                best_params=best_params,
                train_value=train_value,
                test_value=test_value,
            )
        )
    return out
