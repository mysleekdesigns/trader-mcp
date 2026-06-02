"""A *paginating* fake CCXT client for fully-offline data-pipeline tests.

The Stage-A :mod:`tests._fakes` ``FakeCcxt`` returns a fixed canned OHLCV blob and
ignores ``since``/``limit`` -- fine for adapter normalization tests, useless for
exercising the paginated/resumable sync. This module adds a fake that:

    * generates a deterministic synthetic OHLCV series (one bar per timeframe
      step) across an arbitrary date range, and
    * implements ``fetch_ohlcv(symbol, timeframe, since, limit)`` that **honors**
      ``since`` (returns bars at/after it) and ``limit`` (caps the page), and
    * can be configured to **omit a window** so a gap can be detected/repaired.

Rows are emitted in the CCXT ``[ms, open, high, low, close, volume]`` shape so they
flow through the *real* :class:`trader_mcp.exchanges.adapter.ExchangeAdapter`
normalization path (same seam as ``conftest``: monkeypatch
``trader_mcp.exchanges.adapter._create_ccxt_client``).
"""

from __future__ import annotations

from typing import Any

from trader_mcp.data.timeframes import timeframe_ms

#: Default per-page cap a fake exchange enforces, independent of the requested
#: ``limit`` -- mirrors real exchanges (e.g. Coinbase caps a page well under any
#: large client-side limit), so the sync loop must page on the *returned* size.
DEFAULT_EXCHANGE_PAGE_CAP = 300


def synthetic_ohlcv(
    start_ms: int,
    count: int,
    step_ms: int,
    *,
    base_price: float = 20_000.0,
) -> list[list[float]]:
    """Generate ``count`` deterministic CCXT OHLCV rows starting at ``start_ms``.

    The series is a gentle deterministic walk so OHLC invariants hold
    (low <= open/close <= high) and resample aggregation has something to chew on.

    Returns:
        Rows shaped ``[ts_ms, open, high, low, close, volume]``.
    """
    rows: list[list[float]] = []
    for i in range(count):
        ts = start_ms + i * step_ms
        open_p = base_price + (i % 50) * 10.0
        close_p = open_p + ((i % 7) - 3) * 5.0
        high_p = max(open_p, close_p) + 7.5
        low_p = min(open_p, close_p) - 7.5
        volume = 100.0 + (i % 25)
        rows.append([float(ts), open_p, high_p, low_p, close_p, volume])
    return rows


class PaginatingFakeCcxt:
    """A fake ``ccxt.async_support`` client that paginates a synthetic OHLCV series.

    Construct with the full series' start/end and timeframe; ``fetch_ohlcv``
    serves slices honoring ``since``/``limit`` (further capped by
    :attr:`page_cap`). Set :attr:`omit_start_ms`/:attr:`omit_end_ms` to punch a gap
    in the served data, then clear them to let a repair pass backfill it.
    """

    def __init__(
        self,
        *,
        timeframe: str = "1h",
        start_ms: int,
        bar_count: int,
        page_cap: int = DEFAULT_EXCHANGE_PAGE_CAP,
        base_price: float = 20_000.0,
    ) -> None:
        self.step_ms = timeframe_ms(timeframe)
        self.start_ms = start_ms
        self.bar_count = bar_count
        self.page_cap = page_cap
        self._all_rows = synthetic_ohlcv(start_ms, bar_count, self.step_ms, base_price=base_price)

        # Gap window (inclusive, ms). When set, rows whose timestamp falls inside
        # are withheld from fetch_ohlcv responses to simulate missing data.
        self.omit_start_ms: int | None = None
        self.omit_end_ms: int | None = None

        # CCXT-surface attributes the adapter touches.
        self.has: dict[str, Any] = {"fetchOHLCV": True}
        self.timeframes: dict[str, Any] = {timeframe: timeframe}
        self.markets: dict[str, Any] | None = None
        self.urls: dict[str, Any] = {"test": "https://testnet.example"}

        # Introspection for tests.
        self.fetch_calls: list[tuple[int | None, int]] = []
        self.closed = False

    def omit_window(self, start_ms: int, end_ms: int) -> None:
        """Withhold rows in ``[start_ms, end_ms]`` from future responses (a gap)."""
        self.omit_start_ms = start_ms
        self.omit_end_ms = end_ms

    def clear_omission(self) -> None:
        """Stop withholding rows so a repair pass can backfill the gap."""
        self.omit_start_ms = None
        self.omit_end_ms = None

    def _is_omitted(self, ts_ms: int) -> bool:
        return (
            self.omit_start_ms is not None
            and self.omit_end_ms is not None
            and self.omit_start_ms <= ts_ms <= self.omit_end_ms
        )

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int | None,
        limit: int,
    ) -> list[list[float]]:
        """Return up to ``min(limit, page_cap)`` rows at/after ``since``.

        Honors the configured gap window: omitted rows are skipped, but the page
        still spans real timestamps so the sync cursor keeps advancing past the
        gap (exactly how a real exchange behaves when bars are missing).
        """
        self.fetch_calls.append((since, limit))
        floor = since if since is not None else self.start_ms
        cap = min(limit, self.page_cap)
        page: list[list[float]] = []
        for row in self._all_rows:
            ts = int(row[0])
            if ts < floor:
                continue
            if self._is_omitted(ts):
                continue
            page.append(row)
            if len(page) >= cap:
                break
        return page

    async def load_markets(self, reload: bool = False) -> dict[str, Any]:
        self.markets = {}
        return self.markets

    async def close(self) -> None:
        self.closed = True
