"""Parse + compare helpers for the TradingView cross-validation oracle.

trader-mcp's event-driven interpreter is the source of truth; PRD §6 Phase 8 adds
TradingView's Strategy Tester as a *third* independent cross-validation oracle beside
``backtesting.py``. TradingView has no public API and its CDP bridge is fragile + ToS-gray,
so the oracle is fed by a **manual CSV export**: the user charts the SMA-cross Pine mirror
(``tests/fixtures/tradingview/sma_cross_BTCUSD_1h.pine``), runs Strategy Tester on a pinned
``COINBASE:BTCUSD 1h`` window, and exports the **List of Trades** to CSV.

The key design choice (so the comparison is robust to TradingView<->CCXT feed differences):
we compare **trade decisions, not OHLCV**. For each trade we check side, entry/exit *bar
index* (within +/-1 bar), and per-trade return -- not byte-identical candles. A cross that
lands one bar early/late is tolerated; a different *decision* is not.

This module is pure stdlib (csv/datetime/bisect) and intentionally decoupled from the
engine models -- ``compare_trades`` duck-types our trades via :class:`_OurTrade`. That keeps
the parser + matcher unit-testable on synthetic data (``test_tradingview_oracle.py``) without
any TradingView fixture, while the integration test (``test_crossvalidate_tradingview.py``)
wires it to a real export and skips when the fixture is absent.
"""

from __future__ import annotations

import bisect
import csv
import io
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class _OurTrade(Protocol):
    """Structural type for a trader-mcp ``SimulatedTrade`` (duck-typed, no import).

    Declared as read-only properties so the match is covariant -- the engine's
    ``side`` is a ``Literal["long", "short"]`` (narrower than ``str``), which only
    satisfies the protocol if the attribute is read-only.
    """

    @property
    def side(self) -> str: ...
    @property
    def entry_time(self) -> datetime: ...
    @property
    def exit_time(self) -> datetime: ...
    @property
    def pnl_pct(self) -> float: ...  # fraction of entry notional (e.g. 0.0238 == +2.38%)


@dataclass(frozen=True)
class TvTrade:
    """One round-trip trade parsed from a TradingView List-of-Trades export.

    ``pnl_pct`` is stored as a PERCENT (TradingView's "Profit %" column, e.g. ``2.38``),
    not a fraction -- :func:`compare_trades` scales our fractional ``pnl_pct`` by 100 before
    comparing so the two are apples-to-apples.
    """

    side: str  # "long" | "short"
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    pnl_pct: float


@dataclass(frozen=True)
class TradeMismatch:
    """A single divergence between our trades and TradingView's, for readable failures."""

    index: int
    field: str
    ours: object
    theirs: object

    def __str__(self) -> str:
        return (
            f"trade #{self.index} {self.field}: "
            f"trader-mcp={self.ours!r} tradingview={self.theirs!r}"
        )


# ----------------------------------------------------------------------------- parsing


def _to_float(raw: str | None) -> float | None:
    """Lenient numeric parse: strips currency/%/commas/whitespace; ``( )`` => negative."""
    if raw is None:
        return None
    s = raw.strip()
    if not s or s in {"-", "—", "N/A", "n/a"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace("%", "").replace("$", "").strip()
    # Drop a trailing currency code like "USD" if present ("42000 USD").
    parts = s.split()
    if len(parts) > 1:
        s = parts[0]
    if not s:
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    return -val if neg else val


def _parse_dt(raw: str) -> datetime:
    """Parse a TradingView Date/Time cell to an aware UTC datetime.

    Accepts ISO 8601 (with or without a ``T``/timezone) and a couple of common
    explicit formats. A naive timestamp is ASSUMED to already be UTC -- the fixture
    README instructs exporting in UTC, since TradingView otherwise stamps exchange-local
    time and a tz offset would silently shift every bar index.
    """
    s = raw.strip()
    candidate = s.replace("T", " ")
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                dt = datetime.strptime(candidate, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        raise ValueError(f"unrecognized TradingView Date/Time: {raw!r}")
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _find_key(keys: list[str], *needles: str) -> str | None:
    """First header key whose lowercase form contains ALL of ``needles``."""
    for k in keys:
        low = k.lower()
        if all(n in low for n in needles):
            return k
    return None


def parse_tradingview_trades(text: str) -> list[TvTrade]:
    """Parse a TradingView "List of Trades" CSV export into round-trip :class:`TvTrade`.

    TradingView emits TWO rows per trade -- an "Entry long/short" row and an
    "Exit long/short" row -- linked by a "Trade #" column, with the realized P&L on the
    exit row. Column names drift across TradingView versions, so columns are matched
    fuzzily (``type``/``date``/``price``/``profit %``) rather than by exact header.

    Raises ``ValueError`` if the required columns are missing or a trade lacks an
    entry/exit leg -- a malformed export should fail loudly, not silently compare wrong.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("empty TradingView export (no header row)")
    keys = [k for k in reader.fieldnames if k]

    type_k = _find_key(keys, "type")
    date_k = _find_key(keys, "date")
    price_k = _find_key(keys, "price")
    # "Profit %" / "Net P&L %" / "P&L %" -- a percent column that is NOT cumulative/run-up/drawdown.
    pct_k = next(
        (
            k
            for k in keys
            if "%" in k.lower()
            and ("profit" in k.lower() or "p&l" in k.lower() or "pnl" in k.lower())
            and "cumulative" not in k.lower()
            and "run" not in k.lower()
            and "draw" not in k.lower()
        ),
        None,
    )
    trade_k = _find_key(keys, "trade")  # "Trade #"
    missing = [n for n, k in (("type", type_k), ("date", date_k), ("price", price_k)) if k is None]
    if missing:
        raise ValueError(
            f"TradingView export missing column(s): {', '.join(missing)} (have {keys})"
        )
    # Narrow Optional for type-checkers (the missing-column raise above guarantees these).
    assert type_k is not None
    assert date_k is not None
    assert price_k is not None

    # Group rows by trade id (fall back to sequential pairing if there is no "Trade #").
    groups: OrderedDict[str, list[dict[str, str]]] = OrderedDict()
    for seq, row in enumerate(reader):
        tid = (row.get(trade_k) or "").strip() if trade_k else ""
        key = tid or f"_seq{seq // 2}"
        groups.setdefault(key, []).append(row)

    trades: list[TvTrade] = []
    for tid, rows in groups.items():
        entry = next((r for r in rows if "entry" in (r.get(type_k) or "").lower()), None)
        exit_ = next((r for r in rows if "exit" in (r.get(type_k) or "").lower()), None)
        if exit_ is None:
            # An entry with no exit is a position still OPEN at the end of the test window
            # (TradingView lists it with only an entry row). It is not a round-trip, so it
            # cannot be compared -- drop it rather than fail the whole parse.
            continue
        if entry is None:
            # An exit with no matching entry is genuinely malformed -- fail loudly.
            raise ValueError(f"TradingView trade {tid!r} has an exit leg but no entry")
        type_val = (entry.get(type_k) or "").lower()
        side = "short" if "short" in type_val else "long"
        entry_px = _to_float(entry.get(price_k))
        exit_px = _to_float(exit_.get(price_k))
        if entry_px is None or exit_px is None:
            raise ValueError(f"TradingView trade {tid!r} has a non-numeric price")
        pnl_pct = _to_float(exit_.get(pct_k)) if pct_k else None
        trades.append(
            TvTrade(
                side=side,
                entry_time=_parse_dt(entry[date_k]),
                exit_time=_parse_dt(exit_[date_k]),
                entry_price=entry_px,
                exit_price=exit_px,
                pnl_pct=pnl_pct if pnl_pct is not None else 0.0,
            )
        )
    return trades


# --------------------------------------------------------------------------- comparison


def bar_index_at(bar_times: list[datetime], when: datetime) -> int:
    """Index of the bar whose timestamp is nearest ``when`` (ties -> earlier bar).

    ``bar_times`` must be sorted ascending. Both engines' fill timestamps are mapped
    through this same function, so the comparison is about *relative* bar alignment, not
    absolute clock equality -- which is what makes it robust to small feed differences.
    """
    if not bar_times:
        raise ValueError("no bars to index against")
    pos = bisect.bisect_left(bar_times, when)
    if pos == 0:
        return 0
    if pos >= len(bar_times):
        return len(bar_times) - 1
    before, after = bar_times[pos - 1], bar_times[pos]
    return pos if (after - when) < (when - before) else pos - 1


def compare_trades(
    ours: Sequence[_OurTrade],
    theirs: Sequence[TvTrade],
    bar_times: list[datetime],
    *,
    bar_tol: int = 1,
    return_tol_pp: float = 0.5,
    return_rel_tol: float = 0.05,
    count_tol: int = 0,
) -> list[TradeMismatch]:
    """Compare two trade lists at the DECISION level; return mismatches (empty == aligned).

    Trades are paired by entry order (both sorted by entry time, then zipped). For each
    pair we assert: same ``side``; entry and exit fall on the same bar within ``+/-bar_tol``
    bars; and per-trade return agrees within ``max(return_tol_pp, return_rel_tol*|theirs|)``
    percentage points. A trade-count difference beyond ``count_tol`` is itself a mismatch
    (the surplus trades are then unpaired and reported).

    Our ``pnl_pct`` is a FRACTION; TradingView's is a PERCENT -- ours is scaled by 100 here.
    """
    mismatches: list[TradeMismatch] = []

    if abs(len(ours) - len(theirs)) > count_tol:
        mismatches.append(TradeMismatch(-1, "trade_count", len(ours), len(theirs)))

    ours_sorted = sorted(ours, key=lambda t: t.entry_time)
    theirs_sorted = sorted(theirs, key=lambda t: t.entry_time)

    for i, (o, t) in enumerate(zip(ours_sorted, theirs_sorted, strict=False)):
        if o.side != t.side:
            mismatches.append(TradeMismatch(i, "side", o.side, t.side))

        o_entry, t_entry = (
            bar_index_at(bar_times, o.entry_time),
            bar_index_at(bar_times, t.entry_time),
        )
        if abs(o_entry - t_entry) > bar_tol:
            mismatches.append(TradeMismatch(i, "entry_bar", o_entry, t_entry))

        o_exit, t_exit = bar_index_at(bar_times, o.exit_time), bar_index_at(bar_times, t.exit_time)
        if abs(o_exit - t_exit) > bar_tol:
            mismatches.append(TradeMismatch(i, "exit_bar", o_exit, t_exit))

        ours_pct = o.pnl_pct * 100.0
        tol = max(return_tol_pp, return_rel_tol * abs(t.pnl_pct))
        if abs(ours_pct - t.pnl_pct) > tol:
            mismatches.append(
                TradeMismatch(i, "return_pct", round(ours_pct, 4), round(t.pnl_pct, 4))
            )

    return mismatches
