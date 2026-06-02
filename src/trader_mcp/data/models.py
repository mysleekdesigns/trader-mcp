"""Typed Pydantic v2 models for the historical-data pipeline.

These are the typed inputs/outputs the MCP-server engineer will wrap as tool
results and expose as dataset resources. They are deliberately small, frozen, and
built on the existing domain types: the canonical bar/result models live in
:mod:`trader_mcp.exchanges.models` (:class:`OHLCVBar` / :class:`OHLCVResult`) and
are reused verbatim -- this module never redefines a bar.

All timestamps are timezone-aware UTC :class:`datetime` (the store persists and
returns UTC, mirroring :func:`trader_mcp.exchanges.models.ms_to_datetime`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from trader_mcp.config import ExchangeId


class _DataModel(BaseModel):
    """Base for data-pipeline models: frozen and strict on declared fields.

    Unlike the lenient market-data models (which wrap external CCXT payloads),
    these are produced internally from already-validated data, so they use
    ``extra="forbid"`` to catch construction mistakes early.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetKey(_DataModel):
    """Identity of a cached OHLCV dataset: one (exchange, symbol, timeframe)."""

    exchange: ExchangeId
    symbol: str
    timeframe: str


class Gap(_DataModel):
    """A contiguous run of missing bars between two cached timestamps.

    ``start`` is the open time of the first missing bar and ``end`` is the open
    time of the last missing bar (both inclusive, tz-aware UTC). ``missing_bars``
    is the count of bars in the run.
    """

    start: datetime
    end: datetime
    missing_bars: int


class DatasetInfo(_DataModel):
    """A listing summary for one cached dataset (cheap, no full scan needed).

    ``start``/``end`` are the first/last cached bar open times (``None`` when the
    dataset is empty). ``last_synced`` is when a sync last wrote to it, if known.
    """

    exchange: ExchangeId
    symbol: str
    timeframe: str
    row_count: int
    start: datetime | None = None
    end: datetime | None = None
    last_synced: datetime | None = None


class DatasetInspection(_DataModel):
    """A coverage/quality report for one cached dataset (the ``inspect`` result).

    ``expected_bars`` is how many bars *should* exist between ``start`` and
    ``end`` at this cadence; ``missing_bars`` is the shortfall, broken out into
    contiguous :class:`Gap` runs. ``duplicates_removed`` counts duplicate
    timestamps dropped during normalization, and ``monotonic_ok`` reports whether
    the stored series is strictly increasing in time.
    """

    exchange: ExchangeId
    symbol: str
    timeframe: str
    row_count: int
    expected_bars: int
    missing_bars: int
    gaps: list[Gap]
    duplicates_removed: int
    monotonic_ok: bool
    start: datetime | None = None
    end: datetime | None = None


class SyncResult(_DataModel):
    """The typed outcome of a :func:`trader_mcp.data.sync.sync_history` run.

    ``status`` is ``"ok"`` when the sync completed cleanly, ``"partial"`` when a
    mid-sync error forced an early stop (progress is still persisted), and
    ``"empty"`` when the requested window yielded no bars at all.
    """

    exchange: ExchangeId
    symbol: str
    timeframe: str
    bars_added: int
    bars_total: int
    requested_since: datetime
    requested_until: datetime | None
    start: datetime | None
    end: datetime | None
    gaps_detected: int
    gaps_repaired: int
    pages_fetched: int
    status: Literal["ok", "partial", "empty"]
    note: str | None = None
