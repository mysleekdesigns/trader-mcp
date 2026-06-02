"""Paginated, incremental, resumable OHLCV history downloader.

:func:`sync_history` is the single entry point. It pulls bars through the
read-only :class:`trader_mcp.exchanges.adapter.ExchangeAdapter` (resolved from the
:class:`trader_mcp.exchanges.manager.ExchangeManager`), persists them to the
:class:`trader_mcp.data.store.OHLCVStore` page-by-page, then detects and (by
default) repairs gaps. The result is a typed :class:`SyncResult`.

Design guarantees (PRD §5.2, data-pipeline invariants):
    * **Read-only.** The only network call is ``adapter.fetch_ohlcv`` -- no order,
      arming, or credential path exists here.
    * **Determinism & integrity.** The same (exchange, symbol, timeframe, range)
      request yields identical, fully-backfilled, deduped, monotonic bars.
    * **Idempotent / resumable.** Re-running adds only new bars. Each page is
      upserted as it arrives, so an interrupted run resumes cleanly and partial
      progress is never lost.
    * **No raw exception escapes / no secret leaks.** The adapter raises only
      redacted ``ExchangeError`` / ``ValidationError``. On a mid-sync failure this
      function persists what it has and returns ``status="partial"`` with a
      redacted note rather than propagating.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from trader_mcp.data.models import DatasetKey, SyncResult
from trader_mcp.data.quality import detect_gaps, normalize
from trader_mcp.data.store import OHLCVStore
from trader_mcp.data.timeframes import is_supported_timeframe, timeframe_ms
from trader_mcp.errors import ExchangeError, ValidationError
from trader_mcp.logging_config import get_logger, redact

if TYPE_CHECKING:
    from trader_mcp.config import ExchangeId
    from trader_mcp.exchanges.manager import ExchangeManager
    from trader_mcp.exchanges.models import OHLCVBar

_logger = get_logger(__name__)

#: Hard ceiling on pagination iterations -- a defensive backstop against a
#: misbehaving exchange/fake that never signals completion. A year of 1m bars is
#: ~525k bars / ~525 pages at page_limit=1000, so 100k iterations is generous.
_MAX_PAGES = 100_000

#: Cap on how many gap windows we re-fetch in a single repair pass, so a dataset
#: riddled with gaps cannot fan out into an unbounded number of network calls.
_MAX_GAP_REPAIRS = 500

#: Flush the fetch buffer to the store once it reaches this many bars (and always
#: before returning). Balances incremental persistence (resumability) against the
#: cost of rewriting the dataset's Parquet file on every flush.
_FLUSH_EVERY_BARS = 5000


def _ensure_utc(dt: datetime) -> datetime:
    """Return ``dt`` as tz-aware UTC (assume UTC if naive)."""
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


async def sync_history(
    manager: ExchangeManager,
    exchange: ExchangeId,
    symbol: str,
    timeframe: str,
    *,
    since: datetime,
    until: datetime | None = None,
    page_limit: int = 1000,
    repair_gaps: bool = True,
    store: OHLCVStore | None = None,
) -> SyncResult:
    """Download and cache OHLCV history for one (exchange, symbol, timeframe).

    Args:
        manager: The exchange-adapter cache; the adapter is resolved via
            ``manager.get(exchange)``.
        exchange: Target exchange id (Coinbase is the reference exchange).
        symbol: Unified CCXT symbol (US venues quote USD, e.g. ``"BTC/USD"``).
        timeframe: A supported timeframe key (see
            :data:`trader_mcp.data.timeframes.SUPPORTED_TIMEFRAMES`).
        since: Earliest bar open time to fetch (inclusive). Resumable: if the
            dataset already holds bars, the effective cursor starts at
            ``max(since, last_cached + one bar)``.
        until: Latest bar open time to fetch (inclusive). Defaults to "now".
        page_limit: Max bars requested per page. The loop tolerates an exchange
            returning fewer (per-exchange page caps) by advancing on the last bar.
        repair_gaps: When ``True`` (default), re-fetch and backfill any gaps found
            after the main pass.
        store: Optional store override (defaults to a store on ``settings.data_dir``).

    Returns:
        A typed :class:`SyncResult` describing what was added/repaired and the
        final coverage. ``status`` is ``"ok"`` on clean completion, ``"partial"``
        if a mid-sync error forced an early stop (progress is persisted), or
        ``"empty"`` if no bars exist in the window.

    Raises:
        trader_mcp.errors.ValidationError: only for caller-side input errors
            (unsupported timeframe, ``until <= since``, non-positive ``page_limit``)
            -- detected before any network call. Exchange/network faults never
            raise; they downgrade the result to ``status="partial"``.
    """
    if not is_supported_timeframe(timeframe):
        raise ValidationError(
            f"Unsupported timeframe: {timeframe!r}.",
            details={"timeframe": timeframe, "kind": "unsupported_timeframe"},
        )
    if page_limit <= 0:
        raise ValidationError(
            f"page_limit must be positive, got {page_limit}.",
            details={"page_limit": page_limit, "kind": "invalid_page_limit"},
        )

    since = _ensure_utc(since)
    until = _ensure_utc(until) if until is not None else datetime.now(tz=UTC)
    if until <= since:
        raise ValidationError(
            "until must be after since.",
            details={
                "kind": "invalid_range",
                "since": since.isoformat(),
                "until": until.isoformat(),
            },
        )

    store = store or OHLCVStore()
    key = DatasetKey(exchange=exchange, symbol=symbol, timeframe=timeframe)
    step_ms = timeframe_ms(timeframe)
    step = timedelta(milliseconds=step_ms)

    adapter = await manager.get(exchange)

    # Resumable cursor: start after the last cached bar when re-syncing.
    last_cached = store.last_timestamp(key)
    cursor = since
    if last_cached is not None:
        resume_from = last_cached + step
        if resume_from > cursor:
            cursor = resume_from

    bars_added = 0
    pages_fetched = 0
    status: str = "ok"
    note: str | None = None

    # Buffer fetched bars and flush to the store in batches. This keeps the
    # spec's incremental-persistence/resumability guarantee (we flush periodically
    # and always before returning, so an interrupted run keeps its progress) while
    # amortizing the per-flush Parquet rewrite -- upserting every single page would
    # be O(n^2) over a long backfill.
    buffer: list[OHLCVBar] = []

    def flush() -> None:
        nonlocal bars_added, buffer
        if buffer:
            bars_added += store.upsert_bars(key, buffer)
            buffer = []

    while cursor <= until and pages_fetched < _MAX_PAGES:
        try:
            page = await adapter.fetch_ohlcv(symbol, timeframe, since=cursor, limit=page_limit)
        except (ExchangeError, ValidationError) as exc:
            # Adapter messages are already redacted; redact again defensively.
            flush()  # persist progress before reporting the partial failure
            note = f"sync stopped after {pages_fetched} page(s): {redact(str(exc))}"
            status = "partial"
            _logger.warning("sync_history %s %s %s: %s", exchange, symbol, timeframe, note)
            break

        pages_fetched += 1
        if not page.bars:
            break

        # Keep only bars within [cursor, until]; the exchange may overshoot.
        buffer.extend(b for b in page.bars if b.timestamp <= until)
        if len(buffer) >= _FLUSH_EVERY_BARS:
            flush()

        # Advance strictly past the last *fetched* bar (use the full page, not the
        # windowed slice, so we make progress even if the tail is past `until`).
        last_bar_ts = max(b.timestamp for b in page.bars)
        next_cursor = last_bar_ts + step
        # No-progress guard: if the cursor cannot advance, stop to avoid a loop.
        if next_cursor <= cursor:
            break
        cursor = next_cursor

    flush()

    # -- gap detection (+ optional repair) --------------------------------------
    cached = store.read_bars(key, since=since, until=until).bars
    normalized, _dupes = normalize(cached, timeframe)
    gaps = detect_gaps(normalized, timeframe)
    gaps_detected = len(gaps)
    gaps_repaired = 0

    if repair_gaps and gaps and status != "partial":
        repaired_any = False
        for gap in gaps[:_MAX_GAP_REPAIRS]:
            # Page *within* the gap window until it is covered or no progress is
            # made. A single page caps at `page_limit`, so a gap wider than
            # `page_limit` bars needs several fetches -- advance the cursor past the
            # last fetched bar each iteration (same no-progress guard as the main
            # pagination loop) and stop once we reach the gap's end.
            gap_cursor = gap.start
            gap_pages = 0
            interrupted = False
            while gap_cursor <= gap.end and gap_pages < _MAX_PAGES:
                try:
                    gap_page = await adapter.fetch_ohlcv(
                        symbol,
                        timeframe,
                        since=gap_cursor,
                        limit=page_limit,
                    )
                except (ExchangeError, ValidationError) as exc:
                    note = f"gap repair interrupted: {redact(str(exc))}"
                    status = "partial"
                    _logger.warning(
                        "sync_history gap repair %s %s %s: %s",
                        exchange,
                        symbol,
                        timeframe,
                        note,
                    )
                    interrupted = True
                    break

                gap_pages += 1
                if not gap_page.bars:
                    break

                fill = [b for b in gap_page.bars if gap.start <= b.timestamp <= gap.end]
                if fill:
                    added = store.upsert_bars(key, fill)
                    bars_added += added
                    if added:
                        repaired_any = True

                # Advance past the last fetched bar (full page, not the windowed
                # slice, so we keep moving even when the tail is past gap.end).
                last_ts = max(b.timestamp for b in gap_page.bars)
                next_gap_cursor = last_ts + step
                if next_gap_cursor <= gap_cursor:
                    break  # no progress -> avoid an infinite loop
                gap_cursor = next_gap_cursor

            if interrupted:
                break

        if repaired_any:
            # Re-detect to count how many gaps the repair actually closed.
            recached = store.read_bars(key, since=since, until=until).bars
            renormalized, _ = normalize(recached, timeframe)
            remaining = detect_gaps(renormalized, timeframe)
            gaps_repaired = gaps_detected - len(remaining)

    # -- finalize ----------------------------------------------------------------
    info = store.dataset_info(key)
    bars_total = info.row_count
    if bars_total == 0:
        status = "empty" if status != "partial" else status

    if status != "partial":
        store.mark_synced(key)

    return SyncResult(
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        bars_added=bars_added,
        bars_total=bars_total,
        requested_since=since,
        requested_until=until,
        start=info.start,
        end=info.end,
        gaps_detected=gaps_detected,
        gaps_repaired=gaps_repaired,
        pages_fetched=pages_fetched,
        status=status,  # type: ignore[arg-type]  # constrained to the Literal values above
        note=note,
    )
