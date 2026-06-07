"""Deterministic in-memory simulated broker (PRD §6 Phase 4).

SAFETY INVARIANT: this is a *pure simulation*. It never places, routes, or arms a
real order, never reads credentials, never touches the network. It walks bars and
:class:`trader_mcp.engine.interpreter.BarSignal` intents forward in time, applying
fees, slippage, funding, leverage, and the spec's risk exits, and produces closed
:class:`trader_mcp.engine.models.SimulatedTrade` records plus a mark-to-market
equity curve. Given the same (spec, bars, config) it produces byte-identical
output -- there is no randomness in the fill path.

Fill convention (no look-ahead -- the single rule the whole engine obeys):
    A :class:`BarSignal` at index ``t`` was derived from the **close of bar t**.
    The broker executes it at the **open of bar t+1** (backtesting.py's default).
    Concretely: while iterating, the *intent* recorded at bar ``t`` is acted on
    when we step to bar ``t+1``, filling at ``bar[t+1].open`` (then slippage). A
    signal on the final bar therefore never executes (there is no t+1). Risk exits
    (stop-loss / take-profit) are the one exception: they are intrabar price
    levels checked against bar ``t``'s own high/low while a position is open, and
    fill at the stop/target price on that bar -- this matches backtesting.py's
    intrabar SL/TP handling and is required for a stop to mean anything.

Fee/slippage model (documented assumptions):
    * Every market fill pays ``spec.fees.taker`` on its notional. Grid/limit-style
      fills could use ``maker``; v1's simulation treats all fills as taker for a
      conservative, cross-validatable baseline (stated loudly). The maker rate is
      carried in the spec for Phase 5 limit orders.
    * Slippage (``config.slippage_pct``) is applied adversarially: a buy fills at
      ``price * (1 + s)``, a sell at ``price * (1 - s)``. ``0`` reproduces a
      frictionless fill for parity vs backtesting.py.

Funding model for perps (documented; cached OHLCV carries no funding stream):
    When the symbol is a perpetual swap (unified ``BASE/QUOTE:SETTLE`` notation, a
    ``:`` present) and ``config.funding_enabled``, the broker charges funding on an
    open position every ``config.funding_interval_hours`` (default 8h) at a flat
    ``config.funding_rate`` fraction of the position's current notional. A long
    pays funding when the rate is positive (and receives it when negative); a short
    is the mirror. This is an *assumed flat rate* because the backtest runs offline
    on OHLCV only -- there is no historical funding series in the cache. The
    assumption is surfaced in the report note. Spot symbols pay no funding.

Position sizing (``spec.position_sizing``):
    * ``percent_equity`` -- ``value`` percent of current equity as notional.
    * ``fixed_quote``    -- ``value`` units of quote currency as notional.
    * ``fixed_base``     -- ``value`` units of base currency directly.
    Leverage (``spec.risk.max_leverage``) scales the notional for perps; spot uses
    1x. Margin is modeled as notional / leverage; the simulation does not enforce an
    affordability ceiling or issue margin calls in v1 (documented) -- an oversized
    position simply runs unmargined.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from trader_mcp.engine.models import EquityPoint, SimulatedTrade

if TYPE_CHECKING:
    from datetime import datetime

    import pandas as pd

    from trader_mcp.engine.interpreter import BarSignal
    from trader_mcp.engine.models import BacktestConfig
    from trader_mcp.strategy import StrategySpec


@dataclass
class _OpenPosition:
    """Internal mutable state for the single open position (v1 is single-position)."""

    side: str  # "long" | "short"
    entry_time: datetime
    entry_price: float
    size: float  # base units (always positive)
    entry_fee: float
    bars_held: int = 0
    funding_paid: float = 0.0
    #: Hours of position life accumulated since the last funding charge.
    hours_since_funding: float = 0.0
    #: Stop-loss / take-profit price levels (quote price), if the spec set them.
    stop_price: float | None = None
    take_price: float | None = None


@dataclass
class _GridState:
    """Internal state for a grid strategy: which levels currently hold inventory."""

    #: level price -> base size bought at that level (absent = level is empty).
    filled: dict[float, float] = field(default_factory=dict)


class SimulatedBroker:
    """Walks bars + signals forward, producing trades and an equity curve.

    Stateless between :meth:`run` calls; all state lives in locals so two runs of
    the same inputs are independent and identical.
    """

    def __init__(self, spec: StrategySpec, config: BacktestConfig) -> None:
        self.spec = spec
        self.config = config
        self.is_perp = ":" in spec.symbol
        self.leverage = (
            float(spec.risk.max_leverage) if (self.is_perp and spec.risk.max_leverage) else 1.0
        )
        #: Grid bookkeeping: filled level price -> (entry_time, entry_px, fee, bar_idx).
        #: Re-initialized at the start of each grid run so calls are independent.
        self._grid_entries: dict[float, tuple[datetime, float, float, int]] = {}

    # -- public ---------------------------------------------------------------
    def run(
        self,
        df: pd.DataFrame,
        signals: list[BarSignal],
        bar_ms: int,
    ) -> tuple[list[SimulatedTrade], list[EquityPoint]]:
        """Simulate the spec over ``df`` given precomputed per-bar ``signals``.

        ``df`` is the indicator-augmented OHLCV frame (index = tz-aware UTC
        timestamps); ``signals[i]`` is the intent derived from bar ``i``'s close.
        ``bar_ms`` is the timeframe's bar duration (for funding accrual). Returns
        the closed trades and the per-bar equity curve.
        """
        if self.spec.strategy_type == "grid":
            return self._run_grid(df, signals, bar_ms)
        if self.spec.strategy_type == "dca":
            return self._run_dca(df, signals)
        return self._run_rule(df, signals, bar_ms)

    # -- rule strategies ------------------------------------------------------
    def _run_rule(
        self,
        df: pd.DataFrame,
        signals: list[BarSignal],
        bar_ms: int,
    ) -> tuple[list[SimulatedTrade], list[EquityPoint]]:
        cash = self.config.initial_cash
        pos: _OpenPosition | None = None
        trades: list[SimulatedTrade] = []
        equity_curve: list[EquityPoint] = []

        opens = df["open"].to_numpy()
        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        closes = df["close"].to_numpy()
        times = df.index.to_list()
        bar_hours = bar_ms / 3_600_000.0
        n = len(df)

        for i in range(n):
            open_px = float(opens[i])

            # 1) Execute the PREVIOUS bar's intent at THIS bar's open (next-bar fill).
            if i > 0:
                prev = signals[i - 1]
                cash, pos = self._apply_rule_intent(prev, pos, trades, cash, open_px, times[i])

            # 2) Intrabar risk exits (SL/TP) checked against THIS bar's range.
            if pos is not None:
                cash, pos = self._check_risk_exits(
                    pos, trades, cash, float(highs[i]), float(lows[i]), times[i]
                )

            # 3) Funding accrual + bar bookkeeping for a still-open position.
            if pos is not None:
                pos.bars_held += 1
                self._accrue_funding(pos, float(closes[i]), bar_hours)

            # 4) Mark-to-market equity at this bar's close.
            equity_curve.append(
                EquityPoint(
                    timestamp=times[i],
                    equity=self._equity(cash, pos, float(closes[i])),
                )
            )

        # 5) Force-close any position at the final bar's close (accounting).
        if pos is not None and n > 0:
            last_close = float(closes[n - 1])
            cash += self._close(pos, trades, last_close, times[n - 1], "end_of_data")
            equity_curve[-1] = EquityPoint(timestamp=times[n - 1], equity=cash)

        return trades, equity_curve

    def _apply_rule_intent(
        self,
        sig: BarSignal,
        pos: _OpenPosition | None,
        trades: list[SimulatedTrade],
        cash: float,
        fill_open: float,
        now: datetime,
    ) -> tuple[float, _OpenPosition | None]:
        """Apply one bar's entry/exit intent at the next bar's open price."""
        # Exit first (so a reversal closes then re-opens on the same bar).
        if pos is not None:
            should_exit = (pos.side == "long" and sig.exit_long) or (
                pos.side == "short" and sig.exit_short
            )
            reverse = (pos.side == "long" and sig.enter_short) or (
                pos.side == "short" and sig.enter_long
            )
            if should_exit or reverse:
                exit_px = self._slip(fill_open, sell=pos.side == "long")
                cash += self._close(pos, trades, exit_px, now, "signal")
                pos = None

        # Entry (only when flat).
        if pos is None:
            if sig.enter_long:
                pos = self._open("long", cash, fill_open, now)
            elif sig.enter_short:
                pos = self._open("short", cash, fill_open, now)
        return cash, pos

    # -- grid strategies ------------------------------------------------------
    def _run_grid(
        self,
        df: pd.DataFrame,
        signals: list[BarSignal],
        bar_ms: int,
    ) -> tuple[list[SimulatedTrade], list[EquityPoint]]:
        """Grid: buy a slice of allocation at each triggered level, sell at the next
        level up. v1 grid is long-only spot mean-reversion: when a level's low is
        touched we buy ``alloc / levels`` notional; when price later closes above
        the next level up we realize that slice. Fills use the level price (a
        resting limit) plus slippage; funding does not apply (spot grid).
        """
        cfg = self.spec.grid
        assert cfg is not None
        self._grid_entries = {}
        levels = sorted(self._grid_levels())
        per_level_notional = self.config.initial_cash * (cfg.allocation_pct / 100.0) / len(levels)
        cash = self.config.initial_cash
        state = _GridState()
        trades: list[SimulatedTrade] = []
        equity_curve: list[EquityPoint] = []
        closes = df["close"].to_numpy()
        times = df.index.to_list()

        for i in range(len(df)):
            sig = signals[i - 1] if i > 0 else None
            if sig is not None:
                for lvl in sig.grid_buy_levels:
                    if lvl in state.filled:
                        continue
                    fill_px = self._slip(lvl, sell=False)
                    size = per_level_notional / fill_px
                    fee = fill_px * size * self.spec.fees.taker
                    cash -= fill_px * size + fee
                    state.filled[lvl] = size
                    # Record entry time on the level via a pending trade map below.
                    self._grid_entries.setdefault(lvl, (times[i], fill_px, fee, i))
            # Sell any held level whose target (next level up) is reached this bar.
            close_px = float(closes[i])
            for lvl in sorted(state.filled):
                target = self._grid_target(levels, lvl)
                if target is not None and close_px >= target:
                    size = state.filled.pop(lvl)
                    entry_time, entry_px, entry_fee, entry_idx = self._grid_entries.pop(lvl)
                    exit_px = self._slip(target, sell=True)
                    exit_fee = exit_px * size * self.spec.fees.taker
                    cash += exit_px * size - exit_fee
                    pnl = (exit_px - entry_px) * size - entry_fee - exit_fee
                    notional = entry_px * size
                    trades.append(
                        SimulatedTrade(
                            side="long",
                            entry_time=entry_time,
                            exit_time=times[i],
                            entry_price=entry_px,
                            exit_price=exit_px,
                            size=size,
                            pnl=pnl,
                            pnl_pct=(pnl / notional * 100.0) if notional else 0.0,
                            fees_paid=entry_fee + exit_fee,
                            funding_paid=0.0,
                            bars_held=i - entry_idx,
                            exit_reason="take_profit",
                        )
                    )
            equity_curve.append(
                EquityPoint(timestamp=times[i], equity=self._grid_equity(cash, state, close_px))
            )

        # Force-close remaining inventory at the final close.
        if df.shape[0] > 0:
            last_close = float(closes[-1])
            for lvl in sorted(state.filled):
                size = state.filled.pop(lvl)
                entry_time, entry_px, entry_fee, entry_idx = self._grid_entries.pop(lvl)
                exit_px = self._slip(last_close, sell=True)
                exit_fee = exit_px * size * self.spec.fees.taker
                cash += exit_px * size - exit_fee
                pnl = (exit_px - entry_px) * size - entry_fee - exit_fee
                notional = entry_px * size
                trades.append(
                    SimulatedTrade(
                        side="long",
                        entry_time=entry_time,
                        exit_time=times[-1],
                        entry_price=entry_px,
                        exit_price=exit_px,
                        size=size,
                        pnl=pnl,
                        pnl_pct=(pnl / notional * 100.0) if notional else 0.0,
                        fees_paid=entry_fee + exit_fee,
                        funding_paid=0.0,
                        bars_held=(len(df) - 1) - entry_idx,
                        exit_reason="end_of_data",
                    )
                )
            equity_curve[-1] = EquityPoint(timestamp=times[-1], equity=cash)
        return trades, equity_curve

    def _grid_levels(self) -> list[float]:
        cfg = self.spec.grid
        assert cfg is not None
        step = (cfg.upper - cfg.lower) / (cfg.levels - 1)
        return [cfg.lower + step * i for i in range(cfg.levels)]

    @staticmethod
    def _grid_target(levels: list[float], level: float) -> float | None:
        """The next grid level strictly above ``level`` (the take-profit), if any."""
        above = [lvl for lvl in levels if lvl > level]
        return min(above) if above else None

    def _grid_equity(self, cash: float, state: _GridState, close_px: float) -> float:
        return cash + sum(size * close_px for size in state.filled.values())

    # -- dca strategies -------------------------------------------------------
    def _run_dca(
        self,
        df: pd.DataFrame,
        signals: list[BarSignal],
    ) -> tuple[list[SimulatedTrade], list[EquityPoint]]:
        """DCA: spend a fixed quote amount at each scheduled buy, accumulate base,
        realize the whole accumulated position at the final bar (a buy-and-hold
        ladder). Each scheduled buy fills at the next bar's open (the no-look-ahead
        convention); the terminal sell is for accounting of the final equity.

        PARITY: the interpreter's DCA signal is a STATELESS, timestamp-anchored
        cadence (see :meth:`SpecInterpreter._dca_signals`), so it carries no
        ``max_purchases`` cap. The cap is enforced HERE, over the full run, by
        counting executed buys -- the broker walks the whole window identically in
        backtest and (Phase 5) live accumulation, so the cap stays consistent
        without reintroducing window-dependent state into the signal.
        """
        cfg = self.spec.dca
        assert cfg is not None
        cash = self.config.initial_cash
        base = 0.0
        cost_basis = 0.0
        fees_total = 0.0
        purchases = 0
        first_entry: datetime | None = None
        first_idx = 0
        trades: list[SimulatedTrade] = []
        equity_curve: list[EquityPoint] = []
        opens = df["open"].to_numpy()
        closes = df["close"].to_numpy()
        times = df.index.to_list()

        for i in range(len(df)):
            capped = cfg.max_purchases is not None and purchases >= cfg.max_purchases
            if i > 0 and signals[i - 1].dca_buy and not capped:
                fill_px = self._slip(float(opens[i]), sell=False)
                spend = min(cfg.amount_quote, cash)
                if spend > 0:
                    fee = spend * self.spec.fees.taker
                    qty = (spend - fee) / fill_px
                    base += qty
                    cost_basis += spend
                    fees_total += fee
                    cash -= spend
                    purchases += 1
                    if first_entry is None:
                        first_entry = times[i]
                        first_idx = i
            equity_curve.append(
                EquityPoint(timestamp=times[i], equity=cash + base * float(closes[i]))
            )

        if base > 0 and first_entry is not None:
            last_close = float(closes[-1])
            exit_px = self._slip(last_close, sell=True)
            exit_fee = exit_px * base * self.spec.fees.taker
            proceeds = exit_px * base - exit_fee
            cash += proceeds
            pnl = proceeds - cost_basis
            avg_entry = (cost_basis - fees_total) / base
            trades.append(
                SimulatedTrade(
                    side="long",
                    entry_time=first_entry,
                    exit_time=times[-1],
                    entry_price=avg_entry,
                    exit_price=exit_px,
                    size=base,
                    pnl=pnl,
                    pnl_pct=(pnl / cost_basis * 100.0) if cost_basis else 0.0,
                    fees_paid=fees_total + exit_fee,
                    funding_paid=0.0,
                    bars_held=(len(df) - 1) - first_idx,
                    exit_reason="end_of_data",
                )
            )
            equity_curve[-1] = EquityPoint(timestamp=times[-1], equity=cash)
        return trades, equity_curve

    # -- shared fill / accounting helpers -------------------------------------
    def _open(self, side: str, equity: float, fill_open: float, now: datetime) -> _OpenPosition:
        """Open a position sized per the spec, at the (slipped) next-bar open."""
        entry_px = self._slip(fill_open, sell=side == "short")
        size = self._target_size(equity, entry_px)
        fee = entry_px * size * self.spec.fees.taker
        stop, take = self._risk_levels(side, entry_px)
        return _OpenPosition(
            side=side,
            entry_time=now,
            entry_price=entry_px,
            size=size,
            entry_fee=fee,
            stop_price=stop,
            take_price=take,
        )

    def _target_size(self, equity: float, entry_px: float) -> float:
        """Resolve position size (base units) from the spec's sizing mode.

        ``percent_equity``/``fixed_quote`` express a quote notional that leverage
        scales (perps) before dividing by price; ``fixed_base`` is a direct base
        amount (leverage does not multiply an explicit base quantity).
        """
        sizing = self.spec.position_sizing
        if sizing.mode == "fixed_base":
            return sizing.value
        if sizing.mode == "percent_equity":
            notional = equity * (sizing.value / 100.0)
        else:  # fixed_quote
            notional = sizing.value
        notional *= self.leverage
        return notional / entry_px if entry_px > 0 else 0.0

    def _risk_levels(self, side: str, entry_px: float) -> tuple[float | None, float | None]:
        risk = self.spec.risk
        stop = take = None
        if risk.stop_loss_pct is not None:
            delta = entry_px * (risk.stop_loss_pct / 100.0)
            stop = entry_px - delta if side == "long" else entry_px + delta
        if risk.take_profit_pct is not None:
            delta = entry_px * (risk.take_profit_pct / 100.0)
            take = entry_px + delta if side == "long" else entry_px - delta
        return stop, take

    def _check_risk_exits(
        self,
        pos: _OpenPosition,
        trades: list[SimulatedTrade],
        cash: float,
        high: float,
        low: float,
        now: datetime,
    ) -> tuple[float, _OpenPosition | None]:
        """Check intrabar stop-loss / take-profit against this bar's high/low.

        Conservative tie-break: if both the stop and the target fall within the
        bar's range, the STOP is assumed to fill first (worst case for the trader).
        Fills at the stop/target price (no slippage beyond the level, matching
        backtesting.py's level fill).
        """
        if pos.side == "long":
            if pos.stop_price is not None and low <= pos.stop_price:
                cash += self._close(pos, trades, pos.stop_price, now, "stop_loss")
                return cash, None
            if pos.take_price is not None and high >= pos.take_price:
                cash += self._close(pos, trades, pos.take_price, now, "take_profit")
                return cash, None
        else:  # short
            if pos.stop_price is not None and high >= pos.stop_price:
                cash += self._close(pos, trades, pos.stop_price, now, "stop_loss")
                return cash, None
            if pos.take_price is not None and low <= pos.take_price:
                cash += self._close(pos, trades, pos.take_price, now, "take_profit")
                return cash, None
        return cash, pos

    def _close(
        self,
        pos: _OpenPosition,
        trades: list[SimulatedTrade],
        exit_px: float,
        now: datetime,
        reason: str,
    ) -> float:
        """Close ``pos`` at ``exit_px``, append the trade, and return the cash delta.

        The cash delta is the realized pnl (entry/exit fees and funding already
        netted in). The simulation tracks equity, not separate cash/margin ledgers,
        so returning realized pnl keeps the running equity exact for both long and
        short.
        """
        exit_fee = exit_px * pos.size * self.spec.fees.taker
        if pos.side == "long":
            gross = (exit_px - pos.entry_price) * pos.size
        else:
            gross = (pos.entry_price - exit_px) * pos.size
        pnl = gross - pos.entry_fee - exit_fee - pos.funding_paid
        notional = pos.entry_price * pos.size
        trades.append(
            SimulatedTrade(
                side=pos.side,  # type: ignore[arg-type]
                entry_time=pos.entry_time,
                exit_time=now,
                entry_price=pos.entry_price,
                exit_price=exit_px,
                size=pos.size,
                pnl=pnl,
                pnl_pct=(pnl / notional * 100.0) if notional else 0.0,
                fees_paid=pos.entry_fee + exit_fee,
                funding_paid=pos.funding_paid,
                bars_held=pos.bars_held,
                exit_reason=reason,  # type: ignore[arg-type]
            )
        )
        return pnl

    def _accrue_funding(self, pos: _OpenPosition, mark_px: float, bar_hours: float) -> None:
        """Charge perp funding when an interval elapses while a position is open."""
        if not (self.is_perp and self.config.funding_enabled):
            return
        pos.hours_since_funding += bar_hours
        while pos.hours_since_funding >= self.config.funding_interval_hours:
            pos.hours_since_funding -= self.config.funding_interval_hours
            notional = mark_px * pos.size
            charge = notional * self.config.funding_rate
            # Long pays positive funding; short receives it (mirror).
            pos.funding_paid += charge if pos.side == "long" else -charge

    def _equity(self, cash: float, pos: _OpenPosition | None, mark_px: float) -> float:
        """Mark-to-market equity = realized cash + unrealized pnl of open position."""
        if pos is None:
            return cash
        if pos.side == "long":
            unreal = (mark_px - pos.entry_price) * pos.size
        else:
            unreal = (pos.entry_price - mark_px) * pos.size
        return cash + unreal - pos.entry_fee - pos.funding_paid

    def _slip(self, price: float, *, sell: bool) -> float:
        """Apply adversarial slippage: buys fill higher, sells fill lower."""
        s = self.config.slippage_pct / 100.0
        return price * (1.0 - s) if sell else price * (1.0 + s)
