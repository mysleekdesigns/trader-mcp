"""DuckDB + Parquet persistence for the OHLCV cache (PRD §5.1).

One Parquet file per dataset, keyed by ``(exchange, symbol, timeframe)``::

    {data_dir} / {exchange} / {symbol_sanitized} / {timeframe}.parquet
    {data_dir} / {exchange} / {symbol_sanitized} / {timeframe}.meta.json(sidecar)

Symbols are sanitized for the filesystem by replacing ``/`` and ``:`` with ``-``
(``BTC/USD`` -> ``BTC-USD``; ``BTC/USD:USD`` -> ``BTC-USD-USD``). Sanitizing is
lossy (``BTC-USD`` is ambiguous), so the **canonical** CCXT symbol and the
``last_synced`` timestamp are persisted in a tiny JSON **sidecar** next to each
Parquet file -- the sidecar is the dataset's manifest of record.

DuckDB is the engine for *all* Parquet I/O and aggregation -- no pandas/pyarrow.
Reads go through ``read_parquet(...)``; writes go through ``COPY ... TO ... (FORMAT
PARQUET)`` against a staged in-memory table. Each operation opens a short-lived
in-memory DuckDB connection and closes it in a ``finally`` block, so no connection
leaks to trip ``filterwarnings = ["error"]`` in tests.

Timezone: timestamps persist as ``TIMESTAMPTZ`` (UTC) and always come back as
tz-aware UTC :class:`datetime`, mirroring
:func:`trader_mcp.exchanges.models.ms_to_datetime`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb

from trader_mcp.config import ExchangeId, get_settings
from trader_mcp.data.models import DatasetInfo, DatasetKey
from trader_mcp.exchanges.models import OHLCVBar, OHLCVResult
from trader_mcp.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

_logger = get_logger(__name__)


def sanitize_symbol(symbol: str) -> str:
    """Map a CCXT symbol to a filesystem-safe path segment.

    Replaces ``/`` and ``:`` (the two structural separators CCXT uses) with ``-``
    so ``BTC/USD`` -> ``BTC-USD`` and ``BTC/USD:USD`` -> ``BTC-USD-USD``.
    """
    return symbol.replace("/", "-").replace(":", "-")


class OHLCVStore:
    """File-based OHLCV cache backed by DuckDB + Parquet.

    Cheap to construct (no connection is opened until an operation runs). Pass an
    explicit ``data_dir`` or let it default to ``settings.data_dir``.
    """

    def __init__(self, data_dir: Path | str | None = None) -> None:
        """Create a store rooted at ``data_dir`` (defaults to ``settings.data_dir``).

        The directory is created lazily on first write, not here, so constructing a
        store never touches the filesystem.
        """
        if data_dir is None:
            data_dir = get_settings().data_dir
        self.data_dir: Path = Path(data_dir)

    # -- paths -------------------------------------------------------------------

    def path_for(self, key: DatasetKey) -> Path:
        """Return the Parquet file path for ``key`` (no I/O; may not exist)."""
        return (
            self.data_dir / key.exchange / sanitize_symbol(key.symbol) / f"{key.timeframe}.parquet"
        )

    def _meta_path(self, key: DatasetKey) -> Path:
        """Return the JSON sidecar path for ``key``."""
        return self.path_for(key).with_suffix(".meta.json")

    def exists(self, key: DatasetKey) -> bool:
        """Return whether a Parquet file is already on disk for ``key``."""
        return self.path_for(key).is_file()

    # -- sidecar manifest --------------------------------------------------------

    def _write_meta(self, key: DatasetKey, *, last_synced: datetime | None) -> None:
        """Write/refresh the sidecar manifest for ``key`` (canonical symbol + sync ts)."""
        meta = {
            "exchange": key.exchange,
            "symbol": key.symbol,
            "timeframe": key.timeframe,
            "last_synced": last_synced.astimezone(UTC).isoformat() if last_synced else None,
        }
        path = self._meta_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta), encoding="utf-8")

    def _read_meta(self, key: DatasetKey) -> dict[str, Any]:
        """Read the sidecar manifest for ``key`` ({} when absent/unreadable)."""
        path = self._meta_path(key)
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def last_synced(self, key: DatasetKey) -> datetime | None:
        """Return the recorded last-sync time for ``key`` (UTC), or ``None``."""
        raw = self._read_meta(key).get("last_synced")
        if not isinstance(raw, str):
            return None
        try:
            return datetime.fromisoformat(raw).astimezone(UTC)
        except ValueError:
            return None

    def mark_synced(self, key: DatasetKey, when: datetime | None = None) -> None:
        """Record a successful sync time for ``key`` in its sidecar manifest."""
        self._write_meta(key, last_synced=when or datetime.now(tz=UTC))

    # -- connection helper -------------------------------------------------------

    @staticmethod
    def _connect() -> duckdb.DuckDBPyConnection:
        """Open a fresh in-memory DuckDB connection (caller must close)."""
        return duckdb.connect(database=":memory:")

    def _read_rows(self, key: DatasetKey) -> list[tuple[Any, ...]]:
        """Return all stored rows for ``key`` as ``(ts_ms, o, h, l, c, v)`` tuples.

        Reads via DuckDB ``read_parquet`` and returns timestamps as integer epoch
        milliseconds (UTC) so callers stay independent of DuckDB's datetime types.
        Returns ``[]`` when the dataset does not exist.
        """
        path = self.path_for(key)
        if not path.is_file():
            return []
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT epoch_ms(timestamp), open, high, low, close, volume "
                "FROM read_parquet(?) ORDER BY timestamp ASC",
                [str(path)],
            ).fetchall()
        finally:
            con.close()
        return rows

    @staticmethod
    def _rows_to_bars(rows: Iterable[Sequence[Any]]) -> list[OHLCVBar]:
        """Convert ``(ts_ms, o, h, l, c, v)`` rows to tz-aware UTC bars."""
        bars: list[OHLCVBar] = []
        for ts_ms, o, h, low, c, v in rows:
            bars.append(
                OHLCVBar(
                    timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                    open=float(o),
                    high=float(h),
                    low=float(low),
                    close=float(c),
                    volume=float(v),
                )
            )
        return bars

    def _write_bars(self, key: DatasetKey, bars: list[OHLCVBar]) -> None:
        """Overwrite ``key``'s Parquet file with ``bars`` (assumed clean/sorted).

        Stages the bars into an in-memory DuckDB table with a ``TIMESTAMPTZ``
        column, then ``COPY``s it to a temp Parquet file and atomically renames it
        over the target so a crash mid-write cannot leave a half-written dataset.
        Always refreshes the sidecar manifest so the canonical symbol is recorded.
        """
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")

        con = self._connect()
        try:
            con.execute(
                "CREATE TABLE bars ("
                "timestamp TIMESTAMPTZ, open DOUBLE, high DOUBLE, "
                "low DOUBLE, close DOUBLE, volume DOUBLE)"
            )
            if bars:
                params = [
                    (b.timestamp.astimezone(UTC), b.open, b.high, b.low, b.close, b.volume)
                    for b in bars
                ]
                con.executemany("INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?)", params)
            con.execute(
                "COPY (SELECT * FROM bars ORDER BY timestamp ASC) TO ? (FORMAT PARQUET)",
                [str(tmp)],
            )
        finally:
            con.close()
        tmp.replace(path)
        # Preserve any existing last_synced; only ensure the manifest exists with
        # the canonical symbol (sync calls mark_synced explicitly).
        if not self._meta_path(key).is_file():
            self._write_meta(key, last_synced=None)

    # -- public API --------------------------------------------------------------

    def upsert_bars(self, key: DatasetKey, bars: list[OHLCVBar]) -> int:
        """Merge ``bars`` into ``key``'s dataset; dedupe by timestamp, sort ascending.

        Idempotent: re-upserting the same bars adds nothing. On a duplicate
        timestamp the *incoming* bar wins (a re-fetch supersedes the cached copy).

        Args:
            key: The dataset to merge into.
            bars: New bars (any order, possibly overlapping existing ones).

        Returns:
            The number of rows *added* (new timestamps), i.e.
            ``len(after) - len(before)``.
        """
        # Local import avoids a module-level cycle (quality imports models only).
        from trader_mcp.data.quality import normalize

        existing = self.read_bars(key).bars
        before = len(existing)
        # Incoming bars must come last so they win on duplicate timestamps.
        merged, _dupes = normalize([*existing, *bars], key.timeframe)
        self._write_bars(key, merged)
        added = len(merged) - before
        _logger.debug(
            "upsert %s %s %s: +%d rows (total %d)",
            key.exchange,
            key.symbol,
            key.timeframe,
            added,
            len(merged),
        )
        return added

    def read_bars(
        self,
        key: DatasetKey,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> OHLCVResult:
        """Read cached bars for ``key`` as an :class:`OHLCVResult` (backtester feed).

        Args:
            key: The dataset to read.
            since: Inclusive lower bound on bar open time (tz-aware UTC).
            until: Inclusive upper bound on bar open time (tz-aware UTC).
            limit: Cap the number of returned bars (the earliest ``limit`` within
                the window).

        Returns:
            An :class:`OHLCVResult` with ascending, UTC-stamped bars. Empty (and
            ``count == 0``) when the dataset does not exist or the window is empty.
        """
        rows = self._read_rows(key)
        bars = self._rows_to_bars(rows)
        if since is not None:
            since_utc = since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)
            bars = [b for b in bars if b.timestamp >= since_utc]
        if until is not None:
            until_utc = until.astimezone(UTC) if until.tzinfo else until.replace(tzinfo=UTC)
            bars = [b for b in bars if b.timestamp <= until_utc]
        if limit is not None:
            bars = bars[:limit]
        return OHLCVResult(
            exchange=key.exchange,
            symbol=key.symbol,
            timeframe=key.timeframe,
            bars=bars,
            count=len(bars),
        )

    def row_count(self, key: DatasetKey) -> int:
        """Return the number of cached bars for ``key`` (0 if absent)."""
        path = self.path_for(key)
        if not path.is_file():
            return 0
        con = self._connect()
        try:
            result = con.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()
        finally:
            con.close()
        return int(result[0]) if result else 0

    def last_timestamp(self, key: DatasetKey) -> datetime | None:
        """Return the latest cached bar open time for ``key`` (UTC), or ``None``.

        Used by the resumable sync cursor; avoids materializing the whole dataset.
        """
        path = self.path_for(key)
        if not path.is_file():
            return None
        con = self._connect()
        try:
            result = con.execute(
                "SELECT epoch_ms(max(timestamp)) FROM read_parquet(?)", [str(path)]
            ).fetchone()
        finally:
            con.close()
        if not result or result[0] is None:
            return None
        return datetime.fromtimestamp(result[0] / 1000, tz=UTC)

    def dataset_info(self, key: DatasetKey) -> DatasetInfo:
        """Return a listing summary for ``key`` (row count + first/last + last_synced)."""
        last_synced = self.last_synced(key)
        path = self.path_for(key)
        if not path.is_file():
            return DatasetInfo(
                exchange=key.exchange,
                symbol=key.symbol,
                timeframe=key.timeframe,
                row_count=0,
                start=None,
                end=None,
                last_synced=last_synced,
            )
        con = self._connect()
        try:
            result = con.execute(
                "SELECT count(*), epoch_ms(min(timestamp)), epoch_ms(max(timestamp)) "
                "FROM read_parquet(?)",
                [str(path)],
            ).fetchone()
        finally:
            con.close()
        count = int(result[0]) if result else 0
        start = (
            datetime.fromtimestamp(result[1] / 1000, tz=UTC)
            if result and result[1] is not None
            else None
        )
        end = (
            datetime.fromtimestamp(result[2] / 1000, tz=UTC)
            if result and result[2] is not None
            else None
        )
        return DatasetInfo(
            exchange=key.exchange,
            symbol=key.symbol,
            timeframe=key.timeframe,
            row_count=count,
            start=start,
            end=end,
            last_synced=last_synced,
        )

    def list_datasets(self) -> list[DatasetInfo]:
        """Scan the data-dir tree and summarize every cached dataset.

        Returns datasets sorted by (exchange, symbol, timeframe). The canonical
        symbol is recovered from each dataset's sidecar manifest; a Parquet without
        a readable sidecar falls back to its (sanitized) directory name. Files
        whose exchange segment is not a supported id are ignored.
        """
        if not self.data_dir.is_dir():
            return []
        supported: set[str] = set(ExchangeId.__args__)  # type: ignore[attr-defined]
        infos: list[DatasetInfo] = []
        for parquet in sorted(self.data_dir.glob("*/*/*.parquet")):
            timeframe = parquet.stem
            symbol_dir = parquet.parent.name
            exchange = parquet.parent.parent.name
            if exchange not in supported:
                continue
            # Probe the sidecar for the canonical symbol; fall back to the dir name.
            probe = DatasetKey(
                exchange=exchange,  # type: ignore[arg-type]  # guarded by `supported`
                symbol=symbol_dir,
                timeframe=timeframe,
            )
            meta = self._read_meta(probe)
            meta_symbol = meta.get("symbol")
            symbol = meta_symbol if isinstance(meta_symbol, str) else symbol_dir
            key = DatasetKey(
                exchange=exchange,  # type: ignore[arg-type]
                symbol=symbol,
                timeframe=timeframe,
            )
            infos.append(self.dataset_info(key))
        return infos
