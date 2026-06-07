"""THE one event-driven spec interpreter -- backtest AND live (PRD §5.3, §7).

INVARIANT (the reason this module exists): there is exactly **one** signal core,
shared by the vectorized backtest path and the per-bar live path. Feeding the same
bars to :meth:`SpecInterpreter.signals` (whole-window, vectorized) and to
:meth:`SpecInterpreter.signal_at` bar-by-bar (the streaming/live path Phase 5
plugs into) MUST yield identical decisions. There is no second strategy engine and
no forked logic: both paths call the *same* :func:`trader_mcp.strategy.evaluator.
evaluate_expression`, the former in Series mode, the latter in scalar mode over a
trailing window. Any divergence between the two is a parity bug -- QA's parity
suite asserts this exactly.

Fill convention (no look-ahead; see also :mod:`trader_mcp.engine.backtest`):
    A decision derived from the **close of bar t** is *recorded against bar t* but
    is only ever **executed at or after bar t's close** -- in the backtest broker,
    at the **open of bar t+1** (matching backtesting.py's default next-bar-open
    fill). This module's job is purely to decide; the broker applies the timing.
    Because every signal at index ``t`` uses only data up to and including bar
    ``t``'s close (indicators are causal, ``crossover`` looks back one bar), there
    is no look-ahead. The last bar's "execute next bar" never happens (no bar
    t+1), so a signal on the final bar is dropped by the broker -- documented.

Strategy types (the spec discriminator drives the branch):
    * ``"rule"`` -- entry/exit expressions decide long/short entries and exits.
    * ``"grid"`` -- staggered price levels across ``[lower, upper]``; a level is a
      buy trigger when the bar's low touches/crosses it (mean-reversion grid).
    * ``"dca"``  -- a scheduled buy every ``interval_bars`` bars (time, not price).

The interpreter emits *intents* as a typed :class:`BarSignal` per bar; the broker
turns intents into fills. It never touches money, orders, or the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from trader_mcp.indicators import compute_indicators
from trader_mcp.strategy import evaluate_expression

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pandas as pd

    from trader_mcp.strategy import StrategySpec


@dataclass(frozen=True)
class BarSignal:
    """The interpreter's decision for a single bar (an intent, not a fill).

    For a ``"rule"`` strategy: ``enter_long``/``enter_short`` request opening a
    position that side; ``exit_long``/``exit_short`` request closing an open
    position that side. A bar may carry both an exit and an opposite entry (a
    reversal). For ``"grid"``: ``grid_buy_levels`` lists the grid prices triggered
    this bar. For ``"dca"``: ``dca_buy`` requests the scheduled purchase.

    The broker decides what to actually do given current position state; the
    interpreter only states what the *spec* wants at this bar.
    """

    enter_long: bool = False
    enter_short: bool = False
    exit_long: bool = False
    exit_short: bool = False
    grid_buy_levels: tuple[float, ...] = ()
    dca_buy: bool = False


class SpecInterpreter:
    """Shared signal core for one :class:`StrategySpec` (backtest + live).

    Construct once from a spec; it precomputes nothing until given bars. The two
    public methods are the two execution modes of the SAME logic:

    * :meth:`signals` -- vectorized: a whole indicator-augmented DataFrame in, a
      list of :class:`BarSignal` (one per bar) out. Used by the backtest runner.
    * :meth:`signal_at` -- per-bar: a trailing-window DataFrame in (the last row is
      "now"), one :class:`BarSignal` for that last bar out. Used by the live
      runtime. Internally it delegates to the very same rule evaluation, so its
      output for bar ``t`` is identical to ``signals(...)[t]`` given the same data.
    """

    #: How many trailing rows the per-bar/live path must supply so ``crossover``/
    #: ``crossunder`` (which look back one bar) match the vectorized result. Two is
    #: the minimum; a caller may pass more (the interpreter only reads the last 2).
    LIVE_WINDOW: int = 2

    def __init__(self, spec: StrategySpec) -> None:
        self.spec = spec

    # -- column preparation ---------------------------------------------------
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return ``df`` augmented with this spec's indicator output columns.

        Pure and deterministic (delegates to
        :func:`trader_mcp.indicators.compute_indicators`). The backtest runner
        calls this once over the whole window; the live runtime calls it per
        trailing window. Either way the indicator values at a given bar are
        identical -- the indicators are causal, so a trailing window long enough to
        warm them up reproduces the full-window value at its last row.
        """
        return compute_indicators(self.spec.indicators, df)

    # -- vectorized (backtest) path -------------------------------------------
    def signals(self, df: pd.DataFrame) -> list[BarSignal]:
        """Compute a :class:`BarSignal` for every bar of an indicator-augmented frame.

        ``df`` must already carry the indicator columns (call :meth:`prepare`
        first). Branches on ``strategy_type``. For ``"rule"`` strategies every
        entry/exit expression is evaluated ONCE in vectorized Series mode (the fast
        path), then zipped into per-bar signals. This is the source of truth the
        per-bar path must match.
        """
        n = len(df)
        if n == 0:
            return []
        if self.spec.strategy_type == "grid":
            return self._grid_signals(df)
        if self.spec.strategy_type == "dca":
            return self._dca_signals(df)
        return self._rule_signals(df)

    def _rule_signals(self, df: pd.DataFrame) -> list[BarSignal]:
        context = {col: df[col] for col in df.columns}
        enter_long = self._eval_series(self.spec.entry.long, df, context)
        enter_short = self._eval_series(self.spec.entry.short, df, context)
        exit_long = self._eval_series(self.spec.exit.long, df, context)
        exit_short = self._eval_series(self.spec.exit.short, df, context)
        out: list[BarSignal] = []
        for i in range(len(df)):
            out.append(
                BarSignal(
                    enter_long=bool(enter_long[i]),
                    enter_short=bool(enter_short[i]),
                    exit_long=bool(exit_long[i]),
                    exit_short=bool(exit_short[i]),
                )
            )
        return out

    @staticmethod
    def _eval_series(
        expr: str | None,
        df: pd.DataFrame,
        context: Mapping[str, object],
    ) -> list[bool]:
        """Evaluate one optional rule expression to a per-bar list of bools.

        ``None`` (no rule that side) -> all-False. A scalar/boolean result (a
        constant rule like ``True``) is broadcast across every bar. A Series result
        is taken bar-for-bar with NaN -> False (an undefined indicator value during
        the warm-up window is never a signal).
        """
        n = len(df)
        if expr is None:
            return [False] * n
        result = evaluate_expression(expr, context)
        if hasattr(result, "__len__") and not isinstance(result, (str, bytes)):
            import pandas as pd

            series = pd.Series(result)
            filled = series.fillna(False).astype(bool)
            return [bool(v) for v in filled.tolist()]
        return [bool(result)] * n

    # -- per-bar (live) path --------------------------------------------------
    def signal_at(self, window: pd.DataFrame) -> BarSignal:
        """Compute the :class:`BarSignal` for the LAST bar of a trailing ``window``.

        This is the live/streaming entry point Phase 5 will drive with a rolling
        buffer of recent bars. ``window`` must already carry indicator columns
        (call :meth:`prepare` on the trailing buffer) and hold at least
        :attr:`LIVE_WINDOW` rows so ``crossover``/``crossunder`` see the previous
        bar.

        PARITY: this returns exactly ``self.signals(window)[-1]`` -- it *is* the
        vectorized path evaluated over the trailing window and the last element
        taken. Evaluating the window vectorially (rather than feeding bare scalars)
        is the documented contract that makes one-bar-back helpers agree with the
        full backtest. Keeping a single implementation here is what guarantees no
        fork between backtest and live.
        """
        if len(window) == 0:
            return BarSignal()
        return self.signals(window)[-1]

    # -- grid -----------------------------------------------------------------
    def _grid_levels(self) -> list[float]:
        cfg = self.spec.grid
        assert cfg is not None  # guaranteed by spec validation for strategy_type grid
        step = (cfg.upper - cfg.lower) / (cfg.levels - 1)
        return [cfg.lower + step * i for i in range(cfg.levels)]

    def _grid_signals(self, df: pd.DataFrame) -> list[BarSignal]:
        """A grid level is *triggered* on bar t when price trades down through it.

        We treat each level as a resting buy: it fires on the first bar whose
        ``low <= level < prev_low`` (price dipped to/through the level this bar).
        Using only the current and previous bar's lows keeps the decision causal
        (no look-ahead) and identical in vectorized and per-bar evaluation. The
        broker enforces one fill per level (no re-buying a level until it is
        cleared); the interpreter only reports which levels were touched.
        """
        levels = self._grid_levels()
        lows = df["low"].to_numpy()
        out: list[BarSignal] = []
        prev_low = float("inf")
        for i in range(len(df)):
            low = float(lows[i])
            triggered = tuple(lvl for lvl in levels if low <= lvl < prev_low)
            out.append(BarSignal(grid_buy_levels=triggered))
            prev_low = low
        return out

    # -- dca ------------------------------------------------------------------
    def _dca_signals(self, df: pd.DataFrame) -> list[BarSignal]:
        """A scheduled buy on a fixed TIMESTAMP grid every ``interval_bars`` bars.

        PARITY INVARIANT (the reason this is timestamp-anchored, not positional): a
        bar fires a buy when ``(bar_epoch_ms // timeframe_ms) % interval_bars == 0``
        -- a STATELESS function of the bar's OWN timestamp. It depends on nothing
        outside the current bar, so ``signals(df)[i]`` and ``signal_at(window)[-1]``
        are identical regardless of how long the trailing window is. The previous
        implementation keyed off the DataFrame's positional index (``i %
        interval_bars``), which equals the absolute bar index only over a full
        prefix; over the bounded trailing window the live runtime keeps, the
        positional index drifts and DCA fired on a different cadence -- a
        backtest<->live divergence (pinned by QA).

        ``max_purchases`` is intentionally NOT enforced here: a per-bar cap would
        require counting prior fires, reintroducing window-dependent state. The cap
        is applied at the broker over the full run instead (see
        :meth:`SimulatedBroker._run_dca`), which keeps this signal pure and the
        vectorized/per-bar paths equal by construction.

        The bucket index is computed against the bar's UTC epoch milliseconds and
        the timeframe's cadence, so the grid is absolute (anchored at the Unix
        epoch) and reproducible -- two runs over any sub-window of the same data
        fire on exactly the same calendar bars.
        """
        from trader_mcp.data import timeframe_ms

        cfg = self.spec.dca
        assert cfg is not None  # guaranteed by spec validation for strategy_type dca
        tf_ms = timeframe_ms(self.spec.timeframe)
        out: list[BarSignal] = []
        for ts in df.index:
            epoch_ms = int(ts.value // 1_000_000)  # pandas Timestamp.value is ns
            bucket = epoch_ms // tf_ms
            out.append(BarSignal(dca_buy=(bucket % cfg.interval_bars == 0)))
        return out
