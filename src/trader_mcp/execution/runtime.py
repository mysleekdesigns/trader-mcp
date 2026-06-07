"""The live/paper strategy runtime -- INVARIANT 1's live half (PRD §5.3, §6 Phase 5).

THE INVARIANT: there is exactly ONE strategy engine. This runtime makes EVERY
strategy decision by calling :meth:`trader_mcp.engine.SpecInterpreter.signal_at`
over a trailing rolling buffer -- the same per-bar method the engine's own parity
test drives, which is itself the vectorized :meth:`SpecInterpreter.signals` path
evaluated over the window and the last row taken. The runtime reimplements NO
signal logic; it only (a) keeps a rolling buffer, (b) asks the interpreter what the
spec wants this bar, and (c) translates that :class:`BarSignal` into
:class:`OrderIntent`s for the broker. Feeding the same bars here bar-by-bar and to
:func:`trader_mcp.engine.run_backtest` over the whole window yields identical
trades/fills/equity -- any divergence is a parity bug.

Buffer & timing: ``on_bar(bar)`` appends the bar, prepares the trailing window
(long enough to warm the spec's indicators), computes the signal for THIS bar's
close, then submits the resulting intents as PENDING -- they fill on the NEXT bar's
open in the :class:`~trader_mcp.execution.paper_broker.PaperBroker`, exactly the
backtest's next-bar-open convention. CRUCIAL ORDERING: the broker's ``on_bar``
(which fills the previous bar's pending intent at THIS open and checks intrabar
SL/TP) runs BEFORE we compute and submit this bar's intent -- mirroring
``SimulatedBroker._run_rule`` step for step.

Safe-by-default: the runtime emits intents into the pure-simulation PaperBroker; it
never imports the exchange adapter and never places a real order. A (future)
testnet broker is injected via the :class:`BrokerProtocol`, so the runtime is fully
offline-testable and has no hard dependency on ``exchanges/``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from trader_mcp.engine import SpecInterpreter
from trader_mcp.execution.models import OrderIntent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from trader_mcp.engine.interpreter import BarSignal
    from trader_mcp.exchanges.models import OHLCVBar
    from trader_mcp.execution.models import OrderRecord
    from trader_mcp.strategy import StrategySpec


@runtime_checkable
class BarFeed(Protocol):
    """A source of bars the runtime consumes -- the ONLY backtest/live difference.

    Backtest replays cached history through this; paper/testnet streams real bars
    through it. The runtime depends on this structural protocol (not on
    ``exchanges/``) so it is fully offline-testable. An implementation yields
    :class:`trader_mcp.exchanges.models.OHLCVBar` (or any structurally-compatible
    object with ``timestamp``/``open``/``high``/``low``/``close``/``volume``) in
    ascending time order; iteration ends when the feed is exhausted/closed.
    """

    def __aiter__(self) -> AsyncIterator[OHLCVBar]: ...


@runtime_checkable
class BrokerProtocol(Protocol):
    """The broker surface the runtime drives -- implemented by :class:`PaperBroker`.

    Structural so a (future) testnet/live broker can be injected without the runtime
    importing it. The runtime calls exactly these: tell the broker the bar duration,
    advance it one bar, submit intents, and force-close on stop.
    """

    def set_bar_hours(self, bar_hours: float) -> None: ...

    def on_bar(self, bar: OHLCVBar) -> None: ...

    def submit(self, intent: OrderIntent, *, ref_price: float | None = ...) -> OrderRecord: ...

    def force_close(self) -> None: ...


class StrategyRuntime:
    """Drives ONE strategy live over a streamed bar feed via the shared interpreter.

    Holds a :class:`SpecInterpreter` (the one engine), a rolling bar buffer, and a
    broker. Construct, then either drive it manually with :meth:`on_bar` or run the
    async :meth:`run` loop over an injected :class:`BarFeed`.
    """

    #: Trailing-bar retention. ``buffer_size=None`` (the default) means UNBOUNDED:
    #: retain the FULL prefix of history, so the window handed to
    #: :meth:`SpecInterpreter.signal_at` at bar ``i`` is exactly the first ``i+1``
    #: bars and therefore ``signals(window)[-1] == signals(full_series)[i]``
    #: byte-for-byte for EVERY indicator type -- including EWM-based ones (RSI / EMA
    #: / MACD via pandas-ta), whose value at index ``i`` depends on the WHOLE history
    #: ``0..i`` (Wilder/EWM smoothing), NOT a finite trailing window. This is the
    #: exact (not approximate) backtest<->live parity guarantee. Unbounded retention
    #: is fine for v1: even a long-running session's bar list is trivial in RAM.
    #:
    #: A caller MAY pass an explicit integer ``buffer_size`` to cap memory, but doing
    #: so SACRIFICES exact parity for EWM indicators (the truncated history changes
    #: their value) -- it is only safe for finite-window indicators (SMA / Donchian)
    #: and the stateless grid/DCA cadences. The default is the safe, parity-correct
    #: choice; capping is an opt-in approximation for advanced callers.
    def __init__(
        self,
        spec: StrategySpec,
        broker: BrokerProtocol,
        *,
        buffer_size: int | None = None,
    ) -> None:
        self.spec = spec
        self.broker = broker
        self.interpreter = SpecInterpreter(spec)
        #: ``None`` => unbounded (retain the full prefix; exact parity). An explicit
        #: int caps retention (an approximation for EWM indicators -- see above).
        self.buffer_size = buffer_size
        self._buffer: list[OHLCVBar] = []
        self._bars_processed = 0
        self._last_bar_time = None
        self._intent_seq = 0
        from trader_mcp.data import timeframe_ms

        self._bar_hours = timeframe_ms(spec.timeframe) / 3_600_000.0
        self.broker.set_bar_hours(self._bar_hours)
        self._stopped = False

    def _slowest_indicator_len(self) -> int:
        """Longest declared indicator lookback (0 if the spec has no indicators)."""
        max_len = 0
        for ind in self.spec.indicators:
            for key in ("length", "slow", "fast", "period", "window"):
                val = ind.params.get(key)
                if isinstance(val, (int, float)):
                    max_len = max(max_len, int(val))
        return max_len

    def _min_signal_bars(self) -> int:
        """Minimum buffer length before the interpreter is asked for a decision.

        The gate exists ONLY to avoid handing an indicator a window too short to be
        defined (which raises). It must NEVER suppress a bar the vectorized backtest
        signals on -- otherwise the live path drops a trade the backtest takes (a
        parity bug).

        A length-``L`` indicator is first DEFINED at absolute bar index ``L-1``; with
        a full prefix buffer the buffer length after appending bar ``i`` is ``i+1``,
        so the indicator is defined as soon as the buffer reaches ``L`` rows. A
        ``crossover`` (1-bar lookback) can fire as early as bar ``L`` -- buffer length
        ``L+1``. Gating at ``L+1`` therefore lets through every bar the backtest can
        signal on, while still skipping the genuinely-undefined warm-up rows (which
        the vectorized path scores as NaN -> ``False`` anyway, so the skip is
        parity-equal). For a spec with NO indicators (grid/DCA) ONE bar suffices: the
        grid signal at the first window row keys off ``prev_low = +inf`` and the DCA
        signal is anchored to the bar's own absolute timestamp -- both must fire from
        bar 0 exactly as the vectorized path does, so gating any higher would suppress
        a bar-0 trigger the backtest takes.

        IMPORTANT: this returns ``slowest + 1`` (NOT ``slowest + LIVE_WINDOW``). The
        earlier ``+ LIVE_WINDOW`` over-suppressed the first valid crossover entry --
        a confirmed backtest<->live parity bug.
        """
        slowest = self._slowest_indicator_len()
        if slowest == 0:
            return 1
        return slowest + 1

    # -- per-bar driver -------------------------------------------------------
    def on_bar(self, bar: OHLCVBar) -> BarSignal:
        """Advance one bar: fill pending intents, then decide + submit this bar's intent.

        Returns the :class:`BarSignal` the interpreter produced for this bar (handy
        for tests/inspection). Ordering is the parity-critical contract:

        1. ``broker.on_bar(bar)`` -- fills the PREVIOUS bar's pending intent at this
           bar's open and checks intrabar SL/TP (the engine's per-bar body).
        2. append to the bar buffer (UNBOUNDED by default -- the full prefix -- so the
           window at bar ``i`` is exactly the first ``i+1`` bars and EWM indicators
           match the backtest; trimmed only if an explicit ``buffer_size`` was set,
           which is an opt-in approximation for finite-window specs).
        3. ``interpreter.signal_at(prepare(window))`` -- the ONE engine deciding
           what the spec wants at THIS bar's close.
        4. translate to :class:`OrderIntent`s and ``broker.submit`` them PENDING --
           they fill on the next bar's open.
        """
        # 1) Broker advances first: previous pending intent fills at this open.
        self.broker.on_bar(bar)

        # 2) Maintain the bar buffer (unbounded by default; cap only if asked).
        self._buffer.append(bar)
        if self.buffer_size is not None and len(self._buffer) > self.buffer_size:
            self._buffer = self._buffer[-self.buffer_size :]
        self._bars_processed += 1
        self._last_bar_time = bar.timestamp

        # 3) THE one engine: decide this bar's signal over the trailing window.
        #    During warm-up (too few bars to define the slowest indicator) the
        #    vectorized full-window path yields NaN -> False, so a no-op signal here
        #    is parity-equal -- and avoids handing the indicator a degenerate window.
        from trader_mcp.engine.interpreter import BarSignal
        from trader_mcp.indicators import df_from_bars

        if len(self._buffer) < self._min_signal_bars():
            return BarSignal()

        window = self.interpreter.prepare(df_from_bars(self._buffer))
        signal = self.interpreter.signal_at(window)

        # 4) Translate the signal into pending intents for the broker.
        for intent in self._intents_for(signal, bar):
            self.broker.submit(intent, ref_price=float(bar.close))

        return signal

    def _intents_for(self, signal: BarSignal, bar: OHLCVBar) -> list[OrderIntent]:
        """Translate a :class:`BarSignal` into broker intents (no sizing duplicated).

        The runtime does NOT compute sizes -- it states intent (enter long/short,
        exit, grid buy at level, DCA buy); the broker resolves the size from
        ``spec.position_sizing`` exactly as the backtest broker does, so there is a
        single sizing implementation. ``amount`` here is a nominal positive
        placeholder (the broker sizes the actual fill); it is carried only so the
        :class:`OrderIntent` model (which requires ``amount > 0``) is well-formed and
        the safety layer sees a non-zero request.
        """
        intents: list[OrderIntent] = []
        symbol = self.spec.symbol

        def coid(tag: str) -> str:
            self._intent_seq += 1
            return f"{tag}-{self._bars_processed}-{self._intent_seq}"

        if self.spec.strategy_type == "grid":
            for lvl in signal.grid_buy_levels:
                intents.append(
                    OrderIntent(
                        symbol=symbol,
                        side="buy",
                        type="limit",
                        amount=1.0,
                        price=lvl,
                        client_order_id=coid("grid"),
                        reason="grid",
                    )
                )
            return intents

        if self.spec.strategy_type == "dca":
            if signal.dca_buy:
                intents.append(
                    OrderIntent(
                        symbol=symbol,
                        side="buy",
                        type="market",
                        amount=1.0,
                        client_order_id=coid("dca"),
                        reason="dca",
                    )
                )
            return intents

        # Rule path: exit (reduce_only) then opposite/entry, mirroring the broker's
        # exit-before-entry ordering on a reversal bar.
        if signal.exit_long or signal.exit_short:
            intents.append(
                OrderIntent(
                    symbol=symbol,
                    side="sell" if signal.exit_long else "buy",
                    type="market",
                    amount=1.0,
                    client_order_id=coid("exit"),
                    reduce_only=True,
                    reason="signal",
                )
            )
        if signal.enter_long:
            intents.append(
                OrderIntent(
                    symbol=symbol,
                    side="buy",
                    type="market",
                    amount=1.0,
                    client_order_id=coid("enter"),
                    reason="signal",
                )
            )
        elif signal.enter_short:
            intents.append(
                OrderIntent(
                    symbol=symbol,
                    side="sell",
                    type="market",
                    amount=1.0,
                    client_order_id=coid("enter"),
                    reason="signal",
                )
            )
        return intents

    # -- async run loop -------------------------------------------------------
    async def run(self, feed: BarFeed) -> None:
        """Consume ``feed`` bar-by-bar via :meth:`on_bar` until exhausted/cancelled.

        Cancellation-safe: an :class:`asyncio.CancelledError` (the session ``stop``)
        propagates after the current bar completes, leaving consistent state. The
        loop yields control between bars so a stop is responsive on an unbounded
        live feed.
        """
        try:
            async for bar in feed:
                if self._stopped:
                    break
                self.on_bar(bar)
                await asyncio.sleep(0)  # cooperative yield point for cancellation
        except asyncio.CancelledError:
            raise

    def stop(self) -> None:
        """Signal the run loop to halt at the next bar boundary."""
        self._stopped = True

    def finalize(self) -> None:
        """Force-close open inventory for end-of-session accounting (delegates to broker)."""
        self.broker.force_close()

    # -- inspection -----------------------------------------------------------
    @property
    def bars_processed(self) -> int:
        """Number of bars driven through :meth:`on_bar`."""
        return self._bars_processed

    @property
    def last_bar_time(self):
        """Timestamp of the most recently processed bar (``None`` before the first)."""
        return self._last_bar_time
