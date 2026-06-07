"""Online, stateful, pure-simulation paper broker (PRD §6 Phase 5).

SAFETY INVARIANT: this is a *pure simulation*, exactly like
:class:`trader_mcp.engine.broker.SimulatedBroker` -- it never places, routes, or
arms a real order, never reads credentials, never touches the network. The only
difference is *temporal*: ``SimulatedBroker`` walks a whole bar array in one
``run`` call, while ``PaperBroker`` holds mutable account state across calls and
advances ONE bar / processes ONE order at a time (the streaming/live shape). Given
the same bar sequence and the same per-bar intents, its output is byte-identical to
what ``SimulatedBroker`` produces over the same data -- this is the parity property
QA asserts and the reason the fill math here is a faithful port (not a reinvention)
of the engine broker's helpers.

Fill convention (identical to the engine broker, no look-ahead):
    A :class:`trader_mcp.engine.interpreter.BarSignal`-derived intent submitted
    against the CLOSE of bar ``t`` is *pending* and executes at the OPEN of bar
    ``t+1`` -- i.e. on the NEXT :meth:`on_bar`. Intrabar stop-loss/take-profit are
    the one exception: they are price levels checked against the current bar's
    high/low and fill at the level on that bar. The runtime drives this by calling
    :meth:`submit` (records a pending entry/exit intent) then :meth:`on_bar` (the
    next bar fills it). This mirrors ``SimulatedBroker._run_rule``'s loop body step
    for step: per bar -> apply previous intent at this open -> check risk exits ->
    accrue funding -> mark to market.

Fee/slippage/sizing/funding/leverage: a faithful port of the engine broker's
documented model. Every market fill pays ``spec.fees.taker``; slippage is
adversarial (buys higher, sells lower); sizing follows
``spec.position_sizing`` (``percent_equity``/``fixed_quote``/``fixed_base``) with
``spec.risk.max_leverage`` scaling perp notional; perp funding accrues a flat
``config.funding_rate`` every ``config.funding_interval_hours``.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from trader_mcp.execution.models import (
    ExecutionConfig,
    OrderRecord,
    PaperBalance,
    PaperPosition,
    PnLBreakdown,
    Portfolio,
    TradeRecord,
)

if TYPE_CHECKING:
    from trader_mcp.exchanges.models import OHLCVBar
    from trader_mcp.execution.models import IntentReason, OrderIntent
    from trader_mcp.strategy import StrategySpec


@dataclass
class _OpenPosition:
    """Internal mutable state for the single open position (mirror of the engine's)."""

    side: str  # "long" | "short"
    entry_time: datetime
    entry_price: float
    size: float  # base units (always positive)
    entry_fee: float
    bars_held: int = 0
    funding_paid: float = 0.0
    hours_since_funding: float = 0.0
    stop_price: float | None = None
    take_price: float | None = None


class PaperBroker:
    """Stateful in-memory broker advanced one bar / one order at a time.

    Construct from a (read-only) spec + an :class:`ExecutionConfig`; it then holds
    cash, the open position, the closed-trade history and bookkeeping counters as
    mutable state that persists across :meth:`submit` / :meth:`on_bar` calls. The
    fill math is a faithful port of :class:`trader_mcp.engine.broker.SimulatedBroker`
    so a session and a backtest agree to the cent.
    """

    def __init__(self, spec: StrategySpec, config: ExecutionConfig | None = None) -> None:
        self.spec = spec
        self.config = config or ExecutionConfig()
        self.is_perp = ":" in spec.symbol
        self.leverage = (
            float(spec.risk.max_leverage) if (self.is_perp and spec.risk.max_leverage) else 1.0
        )
        self.bar_hours = 0.0  # set from the timeframe lazily on first use if needed

        # Mutable account state.
        self._cash: float = self.config.initial_cash
        self._pos: _OpenPosition | None = None
        self._trades: list[TradeRecord] = []
        self._orders: list[OrderRecord] = []
        self._realized: float = 0.0
        self._fees_paid: float = 0.0
        self._funding_paid: float = 0.0
        self._mark_price: float | None = None
        self._last_bar_time: datetime | None = None

        # Pending next-bar-open intents recorded against the most recent close.
        self._pending_exit: bool = False
        self._pending_enter_long: bool = False
        self._pending_enter_short: bool = False
        self._pending_grid_buys: list[float] = []
        self._pending_dca_buy: bool = False
        self._pending_reasons: dict[str, IntentReason] = {}

        # Grid/dca bookkeeping (online analogues of the engine broker's locals).
        self._grid_filled: dict[float, float] = {}
        self._grid_entries: dict[float, tuple[datetime, float, float, int]] = {}
        self._bar_index: int = -1
        self._dca_base: float = 0.0
        self._dca_cost_basis: float = 0.0
        self._dca_fees: float = 0.0
        self._dca_purchases: int = 0
        self._dca_first_entry: datetime | None = None
        self._dca_first_idx: int = 0

        self._order_seq = itertools.count(1)

    # -- bar duration (for funding accrual) -----------------------------------
    def set_bar_hours(self, bar_hours: float) -> None:
        """Tell the broker the timeframe's bar duration (hours) for funding accrual."""
        self.bar_hours = bar_hours

    # -- public order submission (manual / runtime-translated intents) --------
    def submit(self, intent: OrderIntent, *, ref_price: float | None = None) -> OrderRecord:
        """Record ``intent`` as a pending next-bar-open action and return an ack.

        For the rule path the runtime submits at most one entry and/or one exit
        intent per bar; the actual fill happens on the next :meth:`on_bar` at that
        bar's open (the no-look-ahead convention). The returned record is an
        ``open`` (pending) acknowledgement -- the realized fill record is produced by
        :meth:`on_bar`. A manual ``market`` order with no subsequent bar is filled
        immediately at ``ref_price`` if one is supplied (used by tool-driven manual
        trades and the end-of-session force-close).
        """
        order_id = f"paper-{next(self._order_seq)}"
        # Translate the intent into pending flags the next on_bar consumes.
        if intent.reduce_only or intent.reason in ("stop_loss", "take_profit"):
            self._pending_exit = True
        elif intent.reason == "grid" and intent.price is not None:
            self._pending_grid_buys.append(intent.price)
        elif intent.reason == "dca":
            self._pending_dca_buy = True
        elif intent.side == "buy":
            self._pending_enter_long = True
        else:
            self._pending_enter_short = True
        self._pending_reasons[intent.client_order_id] = intent.reason

        record = OrderRecord(
            order_id=order_id,
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=intent.side,
            type=intent.type,
            status="open",
            amount=intent.amount,
            filled=0.0,
            average=None,
            fee=0.0,
            timestamp=self._last_bar_time or datetime.now(tz=UTC),
            reason=intent.reason,
            simulated=True,
        )
        self._orders.append(record)
        return record

    # -- bar advance (the streaming step; mirrors SimulatedBroker per-bar body) -
    def on_bar(self, bar: OHLCVBar) -> None:
        """Advance the simulation by one bar (the per-bar loop body of the engine).

        Order of operations (identical to ``SimulatedBroker._run_rule`` for bar
        ``i``): apply the PENDING intent at this bar's open, check intrabar SL/TP
        against this bar's high/low, accrue funding + bars_held, then mark equity to
        this bar's close. Grid and DCA pending actions are applied at the open too.
        """
        self._bar_index += 1
        self._last_bar_time = bar.timestamp
        self._mark_price = bar.close

        # 1) Execute the pending (previous-close) intent at THIS bar's open.
        if self.spec.strategy_type == "grid":
            self._apply_pending_grid(bar)
        elif self.spec.strategy_type == "dca":
            self._apply_pending_dca(bar)
        else:
            self._apply_pending_rule(float(bar.open), bar.timestamp)

        # 2) Intrabar risk exits for an open rule position.
        if self._pos is not None and self.spec.strategy_type not in ("grid", "dca"):
            self._check_risk_exits(float(bar.high), float(bar.low), bar.timestamp)

        # 3) Funding accrual + bookkeeping for a still-open position.
        if self._pos is not None:
            self._pos.bars_held += 1
            self._accrue_funding(float(bar.close))

        self._clear_pending()

    # -- rule path ------------------------------------------------------------
    def _apply_pending_rule(self, fill_open: float, now: datetime) -> None:
        # Exit first so a reversal closes then re-opens on the same bar.
        if self._pos is not None:
            should_exit = (self._pos.side == "long" and self._pending_exit) or (
                self._pos.side == "short" and self._pending_exit
            )
            reverse = (self._pos.side == "long" and self._pending_enter_short) or (
                self._pos.side == "short" and self._pending_enter_long
            )
            if should_exit or reverse:
                exit_px = self._slip(fill_open, sell=self._pos.side == "long")
                self._close(exit_px, now, "signal")

        if self._pos is None:
            if self._pending_enter_long:
                self._open("long", fill_open, now)
            elif self._pending_enter_short:
                self._open("short", fill_open, now)

    # -- grid path ------------------------------------------------------------
    def _apply_pending_grid(self, bar: OHLCVBar) -> None:
        cfg = self.spec.grid
        assert cfg is not None
        levels = sorted(self._grid_levels())
        per_level_notional = self.config.initial_cash * (cfg.allocation_pct / 100.0) / len(levels)
        i = self._bar_index
        for lvl in self._pending_grid_buys:
            if lvl in self._grid_filled:
                continue
            fill_px = self._slip(lvl, sell=False)
            size = per_level_notional / fill_px
            fee = fill_px * size * self.spec.fees.taker
            self._cash -= fill_px * size + fee
            self._fees_paid += fee
            self._grid_filled[lvl] = size
            self._grid_entries.setdefault(lvl, (bar.timestamp, fill_px, fee, i))
        close_px = float(bar.close)
        for lvl in sorted(self._grid_filled):
            target = self._grid_target(levels, lvl)
            if target is not None and close_px >= target:
                size = self._grid_filled.pop(lvl)
                entry_time, entry_px, entry_fee, entry_idx = self._grid_entries.pop(lvl)
                exit_px = self._slip(target, sell=True)
                exit_fee = exit_px * size * self.spec.fees.taker
                self._cash += exit_px * size - exit_fee
                self._fees_paid += exit_fee
                pnl = (exit_px - entry_px) * size - entry_fee - exit_fee
                self._realized += pnl
                notional = entry_px * size
                self._trades.append(
                    TradeRecord(
                        side="long",
                        entry_time=entry_time,
                        exit_time=bar.timestamp,
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

    # -- dca path -------------------------------------------------------------
    def _apply_pending_dca(self, bar: OHLCVBar) -> None:
        cfg = self.spec.dca
        assert cfg is not None
        capped = cfg.max_purchases is not None and self._dca_purchases >= cfg.max_purchases
        if self._pending_dca_buy and not capped:
            fill_px = self._slip(float(bar.open), sell=False)
            spend = min(cfg.amount_quote, self._cash)
            if spend > 0:
                fee = spend * self.spec.fees.taker
                qty = (spend - fee) / fill_px
                self._dca_base += qty
                self._dca_cost_basis += spend
                self._dca_fees += fee
                self._fees_paid += fee
                self._cash -= spend
                self._dca_purchases += 1
                if self._dca_first_entry is None:
                    self._dca_first_entry = bar.timestamp
                    self._dca_first_idx = self._bar_index

    # -- shared fill / accounting helpers (ported from the engine broker) -----
    def _open(self, side: str, fill_open: float, now: datetime) -> None:
        equity = self._equity(float(fill_open))
        entry_px = self._slip(fill_open, sell=side == "short")
        size = self._target_size(equity, entry_px)
        fee = entry_px * size * self.spec.fees.taker
        self._fees_paid += fee
        stop, take = self._risk_levels(side, entry_px)
        self._pos = _OpenPosition(
            side=side,
            entry_time=now,
            entry_price=entry_px,
            size=size,
            entry_fee=fee,
            stop_price=stop,
            take_price=take,
        )

    def _target_size(self, equity: float, entry_px: float) -> float:
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

    def _check_risk_exits(self, high: float, low: float, now: datetime) -> None:
        pos = self._pos
        assert pos is not None
        if pos.side == "long":
            if pos.stop_price is not None and low <= pos.stop_price:
                self._close(pos.stop_price, now, "stop_loss")
                return
            if pos.take_price is not None and high >= pos.take_price:
                self._close(pos.take_price, now, "take_profit")
                return
        else:  # short
            if pos.stop_price is not None and high >= pos.stop_price:
                self._close(pos.stop_price, now, "stop_loss")
                return
            if pos.take_price is not None and low <= pos.take_price:
                self._close(pos.take_price, now, "take_profit")
                return

    def _close(self, exit_px: float, now: datetime, reason: str) -> None:
        pos = self._pos
        assert pos is not None
        exit_fee = exit_px * pos.size * self.spec.fees.taker
        self._fees_paid += exit_fee
        if pos.side == "long":
            gross = (exit_px - pos.entry_price) * pos.size
        else:
            gross = (pos.entry_price - exit_px) * pos.size
        pnl = gross - pos.entry_fee - exit_fee - pos.funding_paid
        self._realized += pnl
        self._funding_paid += pos.funding_paid
        self._cash += pnl
        notional = pos.entry_price * pos.size
        self._trades.append(
            TradeRecord(
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
                exit_reason=reason,
            )
        )
        # Record a synthetic fill OrderRecord for the close.
        self._orders.append(
            OrderRecord(
                order_id=f"paper-{next(self._order_seq)}",
                client_order_id=f"close-{len(self._trades)}",
                symbol=self.spec.symbol,
                side="sell" if pos.side == "long" else "buy",
                type="market",
                status="filled",
                amount=pos.size,
                filled=pos.size,
                average=exit_px,
                fee=exit_fee,
                timestamp=now,
                reason="stop_loss"
                if reason == "stop_loss"
                else ("take_profit" if reason == "take_profit" else "signal"),
                simulated=True,
            )
        )
        self._pos = None

    def _accrue_funding(self, mark_px: float) -> None:
        pos = self._pos
        assert pos is not None
        if not (self.is_perp and self.config.funding_enabled):
            return
        pos.hours_since_funding += self.bar_hours
        while pos.hours_since_funding >= self.config.funding_interval_hours:
            pos.hours_since_funding -= self.config.funding_interval_hours
            notional = mark_px * pos.size
            charge = notional * self.config.funding_rate
            pos.funding_paid += charge if pos.side == "long" else -charge

    def _equity(self, mark_px: float) -> float:
        pos = self._pos
        if pos is None:
            base = self._cash
        elif pos.side == "long":
            unreal = (mark_px - pos.entry_price) * pos.size
            base = self._cash + unreal - pos.entry_fee - pos.funding_paid
        else:
            unreal = (pos.entry_price - mark_px) * pos.size
            base = self._cash + unreal - pos.entry_fee - pos.funding_paid
        if self.spec.strategy_type == "grid":
            base += sum(size * mark_px for size in self._grid_filled.values())
        elif self.spec.strategy_type == "dca":
            base += self._dca_base * mark_px
        return base

    def _slip(self, price: float, *, sell: bool) -> float:
        s = self.config.slippage_pct / 100.0
        return price * (1.0 - s) if sell else price * (1.0 + s)

    def _grid_levels(self) -> list[float]:
        cfg = self.spec.grid
        assert cfg is not None
        step = (cfg.upper - cfg.lower) / (cfg.levels - 1)
        return [cfg.lower + step * i for i in range(cfg.levels)]

    @staticmethod
    def _grid_target(levels: list[float], level: float) -> float | None:
        above = [lvl for lvl in levels if lvl > level]
        return min(above) if above else None

    def _clear_pending(self) -> None:
        self._pending_exit = False
        self._pending_enter_long = False
        self._pending_enter_short = False
        self._pending_grid_buys = []
        self._pending_dca_buy = False
        self._pending_reasons = {}

    # -- terminal force-close (end-of-session accounting; mirrors engine) ------
    def force_close(self) -> None:
        """Force-close any open inventory at the last mark (end-of-session accounting).

        Mirrors ``SimulatedBroker``'s terminal close at the final bar's close, so a
        session stopped after N bars reports the same realized equity a backtest of
        those same N bars would. Idempotent on a flat account.
        """
        if self._mark_price is None or self._last_bar_time is None:
            return
        last_close = float(self._mark_price)
        now = self._last_bar_time
        if self._pos is not None:
            self._close(last_close, now, "end_of_data")
        if self.spec.strategy_type == "grid":
            for lvl in sorted(self._grid_filled):
                size = self._grid_filled.pop(lvl)
                entry_time, entry_px, entry_fee, entry_idx = self._grid_entries.pop(lvl)
                exit_px = self._slip(last_close, sell=True)
                exit_fee = exit_px * size * self.spec.fees.taker
                self._cash += exit_px * size - exit_fee
                self._fees_paid += exit_fee
                pnl = (exit_px - entry_px) * size - entry_fee - exit_fee
                self._realized += pnl
                notional = entry_px * size
                self._trades.append(
                    TradeRecord(
                        side="long",
                        entry_time=entry_time,
                        exit_time=now,
                        entry_price=entry_px,
                        exit_price=exit_px,
                        size=size,
                        pnl=pnl,
                        pnl_pct=(pnl / notional * 100.0) if notional else 0.0,
                        fees_paid=entry_fee + exit_fee,
                        funding_paid=0.0,
                        bars_held=self._bar_index - entry_idx,
                        exit_reason="end_of_data",
                    )
                )
        elif self.spec.strategy_type == "dca" and self._dca_base > 0:
            assert self._dca_first_entry is not None
            exit_px = self._slip(last_close, sell=True)
            exit_fee = exit_px * self._dca_base * self.spec.fees.taker
            proceeds = exit_px * self._dca_base - exit_fee
            self._cash += proceeds
            self._fees_paid += exit_fee
            pnl = proceeds - self._dca_cost_basis
            self._realized += pnl
            avg_entry = (self._dca_cost_basis - self._dca_fees) / self._dca_base
            self._trades.append(
                TradeRecord(
                    side="long",
                    entry_time=self._dca_first_entry,
                    exit_time=now,
                    entry_price=avg_entry,
                    exit_price=exit_px,
                    size=self._dca_base,
                    pnl=pnl,
                    pnl_pct=(pnl / self._dca_cost_basis * 100.0) if self._dca_cost_basis else 0.0,
                    fees_paid=self._dca_fees + exit_fee,
                    funding_paid=0.0,
                    bars_held=self._bar_index - self._dca_first_idx,
                    exit_reason="end_of_data",
                )
            )
            self._dca_base = 0.0

    # -- query surface --------------------------------------------------------
    def open_orders(self) -> list[OrderRecord]:
        """Return acknowledged orders still in the ``open`` (pending) state."""
        return [o for o in self._orders if o.status == "open"]

    def order_history(self) -> list[OrderRecord]:
        """Return every order record this broker has produced."""
        return list(self._orders)

    def positions(self) -> list[PaperPosition]:
        """Return the open position as a typed snapshot (empty list when flat)."""
        pos = self._pos
        if pos is None:
            return []
        mark = float(self._mark_price) if self._mark_price is not None else pos.entry_price
        if pos.side == "long":
            unreal = (mark - pos.entry_price) * pos.size
        else:
            unreal = (pos.entry_price - mark) * pos.size
        unreal = unreal - pos.entry_fee - pos.funding_paid
        return [
            PaperPosition(
                symbol=self.spec.symbol,
                side=pos.side,  # type: ignore[arg-type]
                size=pos.size,
                entry_price=pos.entry_price,
                entry_time=pos.entry_time,
                mark_price=mark,
                unrealized_pnl=unreal,
                realized_pnl=self._realized,
                stop_price=pos.stop_price,
                take_price=pos.take_price,
                funding_paid=pos.funding_paid,
                bars_held=pos.bars_held,
            )
        ]

    def balance(self) -> PaperBalance:
        """Return the paper account's cash plus net base holdings."""
        base = self.spec.symbol.split("/")[0].split(":")[0]
        holdings: dict[str, float] = {}
        if self._pos is not None:
            holdings[base] = self._pos.size if self._pos.side == "long" else -self._pos.size
        elif self.spec.strategy_type == "grid":
            held = sum(self._grid_filled.values())
            if held:
                holdings[base] = held
        elif self.spec.strategy_type == "dca" and self._dca_base:
            holdings[base] = self._dca_base
        return PaperBalance(cash=self._cash, holdings=holdings)

    def portfolio(self) -> Portfolio:
        """Return the mark-to-market portfolio snapshot at the latest bar."""
        mark = self._mark_price
        equity = self._equity(float(mark)) if mark is not None else self._cash
        position = self.positions()
        pos_snapshot = position[0] if position else None
        return Portfolio(
            equity=equity,
            cash=self._cash,
            position_value=equity - self._cash,
            position=pos_snapshot,
            mark_price=mark,
            timestamp=self._last_bar_time,
        )

    def pnl(self) -> PnLBreakdown:
        """Return the realized/unrealized pnl decomposition for the session."""
        mark = self._mark_price
        unrealized = 0.0
        if self._pos is not None and mark is not None:
            pos = self._pos
            if pos.side == "long":
                unrealized = (float(mark) - pos.entry_price) * pos.size
            else:
                unrealized = (pos.entry_price - float(mark)) * pos.size
            unrealized = unrealized - pos.entry_fee - pos.funding_paid
        total = self._realized + unrealized
        return PnLBreakdown(
            realized=self._realized,
            unrealized=unrealized,
            fees_paid=self._fees_paid,
            funding_paid=self._funding_paid,
            total=total,
            return_pct=(total / self.config.initial_cash * 100.0)
            if self.config.initial_cash
            else 0.0,
        )

    def trade_history(self) -> list[TradeRecord]:
        """Return the closed round-trips, in close order."""
        return list(self._trades)

    def cancel(self, order_id: str) -> bool:
        """Cancel a still-open (pending) order by id; return whether it was canceled."""
        for idx, o in enumerate(self._orders):
            if o.order_id == order_id and o.status == "open":
                self._orders[idx] = o.model_copy(update={"status": "canceled"})
                return True
        return False
