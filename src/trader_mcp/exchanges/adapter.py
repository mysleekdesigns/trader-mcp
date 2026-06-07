"""The single unified CCXT (async) adapter for all four target exchanges.

One :class:`ExchangeAdapter` wraps one CCXT async client and exposes a typed,
read-only, market-data surface. Coinbase is the reference exchange; Kraken, Gemini,
and Crypto.com reach the same methods through the identical code path -- there is no
per-exchange branching here (genuine differences live in
:mod:`trader_mcp.exchanges.registry` or as CCXT capability flags).

Invariants enforced in this module:
    * **Key scoping.** Order placement/cancellation methods exist (Phase 5 paper/
      testnet plumbing) but every one refuses with a typed
      :class:`~trader_mcp.errors.ExchangeError` (``details["kind"] == "scope"``)
      when the adapter's credential scope is read-only. A read-only adapter is
      structurally unable to place orders. This module does NOT decide dry-run vs
      live -- that gate is the safety layer's job; here we only enforce key scope.
    * **No raw CCXT exception escapes.** Every CCXT call goes through
      :meth:`ExchangeAdapter._call`, which retries transient faults with backoff
      and maps the final failure via :func:`trader_mcp.exchanges.errors.map_ccxt_error`.
      WebSocket ``watch_*`` streams apply the same retry philosophy with bounded
      reconnection backoff.
    * **Secrets are radioactive.** Plaintext credentials are revealed only at the
      CCXT client construction call site; nothing is logged or returned.

Testability seam: real CCXT clients are constructed only inside
:func:`_create_ccxt_client`, which tests monkeypatch to inject a fake client. The
adapter touches the client solely through standard CCXT method/attribute names, so
a lightweight fake satisfies it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import ccxt
import ccxt.async_support as ccxt_async

from trader_mcp.config import ExchangeId, KeyScope, Settings, get_settings
from trader_mcp.errors import ConfigError, ExchangeError
from trader_mcp.exchanges.errors import TRANSIENT_CCXT_ERRORS, map_ccxt_error
from trader_mcp.exchanges.models import (
    Balance,
    BalanceEntry,
    ClockSkew,
    CredentialStatus,
    ExchangeCapabilities,
    FundingRate,
    Market,
    MarketType,
    OHLCVBar,
    OHLCVResult,
    Order,
    OrderBook,
    OrderBookLevel,
    OrderFee,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    RecentTradesResult,
    Ticker,
    Trade,
    ms_to_datetime,
)
from trader_mcp.exchanges.registry import ccxt_id_for, get_exchange_meta
from trader_mcp.logging_config import get_logger, redact

_logger = get_logger(__name__)

#: Maximum number of attempts (initial try + retries) for a transient failure.
MAX_RETRIES = 3

#: Base seconds for exponential backoff between retries (attempt N waits
#: ``BACKOFF_BASE_SECONDS * 2**(N-1)`` seconds).
BACKOFF_BASE_SECONDS = 0.5

#: Base seconds for WebSocket reconnection backoff. The Nth consecutive transient
#: WS failure waits ``min(WS_BACKOFF_BASE_SECONDS * 2**(N-1), WS_BACKOFF_MAX_SECONDS)``
#: seconds before reconnecting. A successful update resets the streak to zero.
WS_BACKOFF_BASE_SECONDS = 0.5

#: Ceiling (seconds) for the bounded WebSocket reconnection backoff.
WS_BACKOFF_MAX_SECONDS = 30.0

#: Default tolerance (milliseconds) for local-vs-exchange clock skew. Beyond this,
#: :meth:`ExchangeAdapter.check_clock_skew` flags the drift (and logs a redacted
#: warning), since signed-request exchanges reject requests when the local clock
#: drifts past their recvWindow. 1000 ms is a conservative fraction of the typical
#: 5000 ms exchange recvWindow.
CLOCK_SKEW_WARN_MS = 1000.0


def _create_ccxt_client(exchange_id: str, config: dict[str, Any]) -> Any:
    """Construct a real CCXT async client. Test seam -- monkeypatched in tests.

    This is the ONLY place a real CCXT client is instantiated. Keeping it a small,
    module-level function lets the test suite replace it with a fake client without
    any network access.

    Args:
        exchange_id: The CCXT client id (e.g. ``"coinbase"``).
        config: The CCXT constructor config (rate limiting, optional credentials).

    Returns:
        An instantiated ``ccxt.async_support.<exchange_id>`` client.
    """
    return getattr(ccxt_async, exchange_id)(config)


def _aiohttp_socks_available() -> bool:
    """Return whether the optional ``aiohttp_socks`` dependency is importable.

    SOCKS proxying through CCXT's async (aiohttp) client requires ``aiohttp_socks``.
    Kept as a tiny module-level seam so the SOCKS-dependency guard in
    :func:`_apply_network_settings` is unit-testable without installing the extra.
    """
    return importlib.util.find_spec("aiohttp_socks") is not None


def _apply_network_settings(config: dict[str, Any], settings: Settings) -> None:
    """Inject the request timeout and optional proxy into a CCXT constructor config.

    Always sets ``timeout`` (milliseconds). Wires ``httpsProxy`` or ``socksProxy``
    from settings so a geo-blocked host can reach the exchanges. Proxy URLs are
    :class:`~pydantic.SecretStr`; their plaintext is revealed only here, at the
    construction call site, and is never logged.

    Args:
        config: The CCXT constructor config dict to mutate in place.
        settings: Resolved application settings carrying timeout/proxy values.

    Raises:
        ConfigError: if both an HTTPS and a SOCKS proxy are configured (CCXT permits
            only one), or if a SOCKS proxy is configured without the optional
            ``aiohttp_socks`` dependency installed.
    """
    config["timeout"] = settings.request_timeout_ms

    https_proxy = settings.https_proxy
    socks_proxy = settings.socks_proxy
    if https_proxy is not None and socks_proxy is not None:
        raise ConfigError(
            "Set only one of TRADER_MCP_HTTPS_PROXY / TRADER_MCP_SOCKS_PROXY; "
            "CCXT does not allow both proxy types at once."
        )
    if https_proxy is not None:
        config["httpsProxy"] = https_proxy.get_secret_value()
    elif socks_proxy is not None:
        if not _aiohttp_socks_available():
            raise ConfigError(
                "A SOCKS proxy is configured (TRADER_MCP_SOCKS_PROXY) but the optional "
                "'aiohttp_socks' dependency is not installed. Install it with: "
                "uv sync --extra socks"
            )
        config["socksProxy"] = socks_proxy.get_secret_value()


async def _sleep(seconds: float) -> None:
    """Async sleep indirection so tests can patch out real backoff delays."""
    await asyncio.sleep(seconds)


def _now_ms() -> float:
    """Local wall-clock time in milliseconds since the epoch (test seam).

    Used by :meth:`ExchangeAdapter.check_clock_skew` to read the local clock both
    just before and just after the server-time round trip. Isolated as a tiny
    module-level function so tests can patch it deterministically without touching
    the real wall clock.
    """
    return time.time() * 1000.0


def _to_float(value: Any) -> float | None:
    """Best-effort float coercion for CCXT numeric fields (may be ``None``/str)."""
    if value is None:
        return None
    # Guard booleans explicitly: ``bool`` is a subclass of ``int``, so a stray
    # ``True``/``False`` in a numeric field would otherwise coerce to ``1.0``/``0.0``.
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ExchangeAdapter:
    """A read-only, typed wrapper around one CCXT async client.

    Construct via :meth:`create` (an async factory) rather than directly, so the
    client is configured with credentials/sandbox before first use.
    """

    def __init__(
        self,
        *,
        exchange: ExchangeId,
        client: Any,
        testnet: bool,
        key_scope: KeyScope = KeyScope.READ_ONLY,
    ) -> None:
        """Low-level constructor. Prefer :meth:`create`.

        Args:
            exchange: The trader-mcp exchange id.
            client: A constructed CCXT async client (or test fake).
            testnet: Whether the client is in sandbox/testnet mode.
            key_scope: The credential scope wired into this adapter. Defaults to
                read-only (safe by default); trade methods refuse unless this is
                :attr:`~trader_mcp.config.KeyScope.TRADE_ENABLED`.
        """
        self.exchange_id: ExchangeId = exchange
        self.testnet: bool = testnet
        self.key_scope: KeyScope = key_scope
        self._client: Any = client
        self._closed: bool = False

    # -- lifecycle ---------------------------------------------------------------

    @classmethod
    async def create(
        cls,
        exchange: ExchangeId,
        *,
        testnet: bool = False,
        sandbox: bool | None = None,
        settings: Settings | None = None,
    ) -> ExchangeAdapter:
        """Create and configure an adapter for ``exchange``.

        Builds a rate-limited CCXT client, wires in credentials when they are
        configured for this exchange (carrying their declared scope so trade
        methods can refuse a read-only key), and enables sandbox/testnet mode when
        requested.

        Args:
            exchange: A supported trader-mcp exchange id.
            testnet: When ``True``, put the client in sandbox/testnet mode. Refuses
                with a typed error if the exchange has no sandbox endpoint.
            sandbox: Alias for ``testnet`` (CCXT calls it sandbox mode). When given,
                it overrides ``testnet``; otherwise ``testnet`` is used.
            settings: Optional settings override (defaults to the process-wide
                cached settings).

        Returns:
            A ready-to-use :class:`ExchangeAdapter`.

        Raises:
            trader_mcp.errors.ExchangeError: if sandbox/testnet mode was requested
                but the exchange exposes no sandbox endpoint
                (``details["kind"] == "not_supported"``).
        """
        settings = settings or get_settings()
        ccxt_id = ccxt_id_for(exchange)
        want_sandbox = testnet if sandbox is None else sandbox

        config: dict[str, Any] = {"enableRateLimit": True}

        # Wire credentials only when fully configured. Plaintext is revealed
        # exactly here, at the construction call site, and never logged.
        creds = settings.credentials_for(exchange)
        if creds.is_configured:
            api_key, api_secret = creds.reveal()
            config["apiKey"] = api_key
            config["secret"] = api_secret

        # Timeout + optional proxy (revealed here only; never logged). Lets a
        # geo-blocked host reach the exchanges over an HTTPS/SOCKS proxy or VPN.
        _apply_network_settings(config, settings)

        client = _create_ccxt_client(ccxt_id, config)

        if want_sandbox:
            cls._enable_sandbox(client, exchange)

        _logger.debug(
            "Created %s adapter (testnet=%s, credentialed=%s, scope=%s)",
            exchange,
            want_sandbox,
            creds.is_configured,
            creds.scope.value,
        )
        return cls(
            exchange=exchange,
            client=client,
            testnet=want_sandbox,
            key_scope=creds.scope,
        )

    @staticmethod
    def _enable_sandbox(client: Any, exchange: ExchangeId) -> None:
        """Put ``client`` into sandbox/testnet mode, or refuse if unsupported.

        Coinbase sandbox / Kraken demo are the references. We refuse (rather than
        silently fall through to live endpoints) when the exchange exposes no
        ``urls.test`` sandbox endpoint, so an order never lands on production by
        accident.

        Raises:
            trader_mcp.errors.ExchangeError: if the exchange has no sandbox
                endpoint, or CCXT rejects ``set_sandbox_mode`` as unsupported.
        """
        urls: dict[str, Any] = getattr(client, "urls", {}) or {}
        set_sandbox = getattr(client, "set_sandbox_mode", None)
        if not urls.get("test") or not callable(set_sandbox):
            raise map_ccxt_error(
                ccxt.NotSupported(f"{exchange} has no sandbox/testnet endpoint"),
                exchange=exchange,
                op="create(sandbox)",
            )
        try:
            set_sandbox(True)
        except ccxt.NotSupported as exc:
            raise map_ccxt_error(exc, exchange=exchange, op="create(sandbox)") from exc

    async def aclose(self) -> None:
        """Close the underlying CCXT client (idempotent)."""
        if self._closed:
            return
        self._closed = True
        if not callable(getattr(self._client, "close", None)):
            return
        try:
            await self._client.close()
        except Exception as exc:
            _logger.debug("Error closing %s client: %s", self.exchange_id, redact(str(exc)))

    async def __aenter__(self) -> ExchangeAdapter:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    # -- retry / error mapping ---------------------------------------------------

    async def _call(self, op: str, coro_factory: Callable[[], Awaitable[Any]]) -> Any:
        """Run a CCXT coroutine with transient-retry + typed error mapping.

        Args:
            op: Adapter operation name (used in error ``details`` and logs).
            coro_factory: A zero-arg callable returning a fresh awaitable each call
                (a factory, not an awaitable, so retries can re-issue the request).

        Returns:
            The CCXT call's raw result.

        Raises:
            trader_mcp.errors.ExchangeError: Always, on failure -- never a raw CCXT
                exception. Transient faults are retried up to :data:`MAX_RETRIES`
                with exponential backoff before mapping the final failure.
        """
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await coro_factory()
            except TRANSIENT_CCXT_ERRORS as exc:
                last_exc = exc
                if attempt >= MAX_RETRIES:
                    break
                delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                _logger.warning(
                    "%s.%s transient error (attempt %d/%d), backing off %.2fs: %s",
                    self.exchange_id,
                    op,
                    attempt,
                    MAX_RETRIES,
                    delay,
                    redact(str(exc)),
                )
                await _sleep(delay)
            except Exception as exc:
                # Non-transient (bad symbol, auth, not-supported, generic): no retry.
                raise map_ccxt_error(exc, exchange=self.exchange_id, op=op) from exc
        # Exhausted retries on a transient fault.
        assert last_exc is not None
        raise map_ccxt_error(last_exc, exchange=self.exchange_id, op=op) from last_exc

    # -- key scoping -------------------------------------------------------------

    def _require_trade_scope(self, op: str) -> None:
        """Refuse a trade operation when the adapter's key scope is read-only.

        SAFETY INVARIANT: a read-only adapter must be structurally unable to place
        or cancel orders. Every order/position/balance-mutating path calls this
        first, so a read-only credential can never reach an order-routing CCXT call.
        This is independent of (and additional to) the safety layer's dry-run/armed
        gate; it enforces the *key's* declared scope.

        Raises:
            trader_mcp.errors.ExchangeError: with ``details["kind"] == "scope"``
                when :attr:`key_scope` is not trade-enabled.
        """
        if self.key_scope is KeyScope.TRADE_ENABLED:
            return
        raise ExchangeError(
            f"{self.exchange_id}.{op}: refused -- the configured API key is read-only "
            "and cannot place or modify orders.",
            details={
                "kind": "scope",
                "exchange": self.exchange_id,
                "op": op,
                "scope": self.key_scope.value,
            },
        )

    # -- capabilities (no network) ----------------------------------------------

    def capabilities(self) -> ExchangeCapabilities:
        """Return the exchange's static capabilities. Performs no network call.

        Derived from the CCXT client's static ``.has`` map and ``.timeframes``. A
        ``has`` value is treated as supported when truthy or the string
        ``"emulated"`` (CCXT emulates it client-side).
        """
        has: dict[str, Any] = getattr(self._client, "has", {}) or {}
        timeframes_map: dict[str, Any] = getattr(self._client, "timeframes", {}) or {}
        meta = get_exchange_meta(self.exchange_id)

        markets: dict[str, Any] | None = getattr(self._client, "markets", None)
        market_count = len(markets) if markets else None

        return ExchangeCapabilities(
            exchange=self.exchange_id,
            ccxt_id=ccxt_id_for(self.exchange_id),
            # Default True if CCXT omits the flag; the actual ``has`` map governs.
            # (Coinbase is spot-only, so its ``has_swap`` resolves False from CCXT.)
            has_spot=self._has(has, "spot", default=True),
            has_swap=self._has(has, "swap", default=True),
            has_websocket=meta.has_websocket,
            timeframes=list(timeframes_map.keys()),
            supports_ohlcv=self._has(has, "fetchOHLCV"),
            supports_order_book=self._has(has, "fetchOrderBook"),
            supports_trades=self._has(has, "fetchTrades"),
            supports_ticker=self._has(has, "fetchTicker"),
            supports_funding_rate=self._has(has, "fetchFundingRate"),
            supports_testnet=self._supports_testnet(),
            market_count=market_count,
        )

    @staticmethod
    def _has(has: dict[str, Any], feature: str, *, default: bool = False) -> bool:
        """Interpret a CCXT ``.has`` flag: truthy or ``"emulated"`` means supported."""
        value = has.get(feature, default)
        if value == "emulated":
            return True
        return bool(value)

    def _supports_testnet(self) -> bool:
        """Whether the client exposes a sandbox/testnet endpoint (``urls.test``)."""
        urls: dict[str, Any] = getattr(self._client, "urls", {}) or {}
        return bool(urls.get("test"))

    # -- markets -----------------------------------------------------------------

    async def load_markets(self, *, reload: bool = False) -> list[Market]:
        """Load (and cache) the exchange's markets, normalized to :class:`Market`.

        Args:
            reload: Force CCXT to re-fetch the market list from the exchange.

        Returns:
            All markets of supported type (spot/swap), normalized.
        """
        raw = await self._call("load_markets", lambda: self._client.load_markets(reload))
        # CCXT returns a {symbol: market} dict and also populates self._client.markets.
        markets_dict: dict[str, Any] = raw if isinstance(raw, dict) else self._client.markets
        result: list[Market] = []
        for market in markets_dict.values():
            normalized = self._normalize_market(market)
            if normalized is not None:
                result.append(normalized)
        return result

    def _normalize_market(self, market: dict[str, Any]) -> Market | None:
        """Normalize one CCXT market dict; return ``None`` for unsupported types."""
        raw_type = market.get("type")
        # We support only spot and (linear) swap markets in v1; skip futures,
        # options, etc. CCXT also flags these via boolean keys.
        if market.get("spot"):
            market_type: MarketType = "spot"
        elif market.get("swap"):
            market_type = "swap"
        elif raw_type in ("spot", "swap"):
            market_type = raw_type
        else:
            return None

        symbol = market.get("symbol")
        base = market.get("base")
        quote = market.get("quote")
        if not symbol or not base or not quote:
            return None

        precision: dict[str, Any] = market.get("precision") or {}
        limits: dict[str, Any] = market.get("limits") or {}
        amount_limits: dict[str, Any] = limits.get("amount") or {}
        cost_limits: dict[str, Any] = limits.get("cost") or {}

        return Market(
            exchange=self.exchange_id,
            symbol=symbol,
            base=base,
            quote=quote,
            settle=market.get("settle"),
            type=market_type,
            active=bool(market.get("active", True)),
            contract_size=_to_float(market.get("contractSize")),
            linear=market.get("linear"),
            inverse=market.get("inverse"),
            price_precision=_to_float(precision.get("price")),
            amount_precision=_to_float(precision.get("amount")),
            min_amount=_to_float(amount_limits.get("min")),
            max_amount=_to_float(amount_limits.get("max")),
            min_cost=_to_float(cost_limits.get("min")),
            maker_fee=_to_float(market.get("maker")),
            taker_fee=_to_float(market.get("taker")),
        )

    async def list_markets(
        self,
        *,
        market_type: MarketType | None = None,
        active_only: bool = True,
        limit: int | None = None,
    ) -> list[Market]:
        """List markets, optionally filtered by type/active flag and truncated.

        Loads markets if not already cached (no forced reload).

        Args:
            market_type: Restrict to ``"spot"`` or ``"swap"`` if given.
            active_only: Drop inactive/delisted markets.
            limit: Cap the number of returned markets.
        """
        markets = await self.load_markets(reload=False)
        filtered = [
            m
            for m in markets
            if (market_type is None or m.type == market_type) and (not active_only or m.active)
        ]
        if limit is not None:
            filtered = filtered[:limit]
        return filtered

    async def search_symbols(
        self,
        query: str,
        *,
        market_type: MarketType | None = None,
        limit: int = 50,
    ) -> list[Market]:
        """Case-insensitive substring search over symbol/base/quote.

        Args:
            query: Substring to match against the symbol, base, or quote.
            market_type: Restrict to ``"spot"`` or ``"swap"`` if given.
            limit: Max number of matches to return.
        """
        needle = query.strip().lower()
        markets = await self.list_markets(market_type=market_type, active_only=True)
        matches = [
            m
            for m in markets
            if needle in m.symbol.lower() or needle in m.base.lower() or needle in m.quote.lower()
        ]
        return matches[:limit]

    # -- market data -------------------------------------------------------------

    async def fetch_ticker(self, symbol: str) -> Ticker:
        """Fetch a normalized ticker snapshot for ``symbol``."""
        raw = await self._call("fetch_ticker", lambda: self._client.fetch_ticker(symbol))
        return self._normalize_ticker(symbol, raw)

    def _normalize_ticker(self, symbol: str, raw: dict[str, Any]) -> Ticker:
        return Ticker(
            exchange=self.exchange_id,
            symbol=raw.get("symbol") or symbol,
            timestamp=ms_to_datetime(raw.get("timestamp")),
            last=_to_float(raw.get("last")),
            bid=_to_float(raw.get("bid")),
            ask=_to_float(raw.get("ask")),
            high=_to_float(raw.get("high")),
            low=_to_float(raw.get("low")),
            open=_to_float(raw.get("open")),
            close=_to_float(raw.get("close")),
            base_volume=_to_float(raw.get("baseVolume")),
            quote_volume=_to_float(raw.get("quoteVolume")),
            change=_to_float(raw.get("change")),
            percentage=_to_float(raw.get("percentage")),
        )

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        *,
        since: datetime | None = None,
        limit: int = 200,
    ) -> OHLCVResult:
        """Fetch OHLCV candles for ``symbol``/``timeframe``.

        Args:
            symbol: Unified CCXT symbol (e.g. ``"BTC/USD"``).
            timeframe: CCXT timeframe key (e.g. ``"1m"``, ``"1h"``, ``"1d"``).
            since: Earliest candle open time (inclusive). Converted to ms.
            limit: Maximum number of candles to return.

        Returns:
            An :class:`OHLCVResult` with normalized, UTC-stamped bars.
        """
        since_ms = int(since.timestamp() * 1000) if since is not None else None
        raw = await self._call(
            "fetch_ohlcv",
            lambda: self._client.fetch_ohlcv(symbol, timeframe, since_ms, limit),
        )
        bars: list[OHLCVBar] = []
        for row in raw or []:
            bar = self._normalize_ohlcv_row(row)
            if bar is not None:
                bars.append(bar)
        return OHLCVResult(
            exchange=self.exchange_id,
            symbol=symbol,
            timeframe=timeframe,
            bars=bars,
            count=len(bars),
        )

    @staticmethod
    def _normalize_ohlcv_row(row: Any) -> OHLCVBar | None:
        """Normalize one CCXT OHLCV row into an :class:`OHLCVBar`, or ``None``.

        Shared by the REST :meth:`fetch_ohlcv` and the WS :meth:`watch_ohlcv`
        stream so a streamed bar is byte-identical in shape to a fetched one --
        the live runtime feeds these bars to the SAME interpreter the backtest
        uses, so any divergence would be a parity bug.

        A CCXT OHLCV row is ``[timestamp_ms, open, high, low, close, volume]``.
        Cells are coerced via :func:`_to_float`; a row that is short, has no usable
        timestamp, or has a missing/non-numeric body cell is dropped (returns
        ``None``). Exchanges emit ``None`` close/volume on thin or just-opened
        candles; a bare ``float()`` would raise outside ``_call`` and let a raw
        exception escape the adapter.
        """
        if not row or len(row) < 6:
            return None
        ts = ms_to_datetime(row[0])
        if ts is None:
            return None
        o = _to_float(row[1])
        h = _to_float(row[2])
        low = _to_float(row[3])
        c = _to_float(row[4])
        v = _to_float(row[5])
        if o is None or h is None or low is None or c is None or v is None:
            return None
        return OHLCVBar(timestamp=ts, open=o, high=h, low=low, close=c, volume=v)

    @staticmethod
    def _normalize_trade(raw: dict[str, Any]) -> Trade | None:
        """Normalize one CCXT trade dict into a :class:`Trade`, or ``None``.

        Shared by REST :meth:`fetch_recent_trades` and WS :meth:`watch_trades` so a
        streamed trade is shape-identical to a fetched one. A trade missing a
        usable price/amount is dropped (returns ``None``).
        """
        price = _to_float(raw.get("price"))
        amount = _to_float(raw.get("amount"))
        if price is None or amount is None:
            return None
        side = raw.get("side")
        return Trade(
            id=str(raw["id"]) if raw.get("id") is not None else None,
            timestamp=ms_to_datetime(raw.get("timestamp")),
            side=side if side in ("buy", "sell") else None,
            price=price,
            amount=amount,
            cost=_to_float(raw.get("cost")),
        )

    async def fetch_order_book(self, symbol: str, *, limit: int = 25) -> OrderBook:
        """Fetch a normalized order-book snapshot for ``symbol``.

        Args:
            symbol: Unified CCXT symbol.
            limit: Depth (number of price levels) to request per side.
        """
        raw = await self._call(
            "fetch_order_book",
            lambda: self._client.fetch_order_book(symbol, limit),
        )
        return self._normalize_order_book(symbol, raw)

    def _normalize_order_book(self, symbol: str, raw: dict[str, Any]) -> OrderBook:
        """Normalize a CCXT order-book payload into an :class:`OrderBook`.

        Shared by REST :meth:`fetch_order_book` and WS :meth:`watch_order_book` so a
        streamed book is shape-identical to a fetched one.
        """
        return OrderBook(
            exchange=self.exchange_id,
            symbol=raw.get("symbol") or symbol,
            timestamp=ms_to_datetime(raw.get("timestamp")),
            bids=self._normalize_levels(raw.get("bids")),
            asks=self._normalize_levels(raw.get("asks")),
        )

    @staticmethod
    def _normalize_levels(levels: Any) -> list[OrderBookLevel]:
        """Normalize CCXT ``[[price, amount], ...]`` rows into typed levels."""
        result: list[OrderBookLevel] = []
        for row in levels or []:
            if not row or len(row) < 2:
                continue
            price = _to_float(row[0])
            amount = _to_float(row[1])
            if price is None or amount is None:
                continue
            result.append(OrderBookLevel(price=price, amount=amount))
        return result

    async def fetch_recent_trades(self, symbol: str, *, limit: int = 50) -> RecentTradesResult:
        """Fetch recent public trades for ``symbol``.

        Args:
            symbol: Unified CCXT symbol.
            limit: Max number of trades to return.
        """
        raw = await self._call(
            "fetch_recent_trades",
            lambda: self._client.fetch_trades(symbol, None, limit),
        )
        trades: list[Trade] = []
        for t in raw or []:
            trade = self._normalize_trade(t)
            if trade is not None:
                trades.append(trade)
        return RecentTradesResult(
            exchange=self.exchange_id,
            symbol=symbol,
            trades=trades,
            count=len(trades),
        )

    async def fetch_funding_rate(self, symbol: str) -> FundingRate:
        """Fetch the current funding rate for a perpetual swap ``symbol``."""
        raw = await self._call(
            "fetch_funding_rate",
            lambda: self._client.fetch_funding_rate(symbol),
        )
        return FundingRate(
            exchange=self.exchange_id,
            symbol=raw.get("symbol") or symbol,
            funding_rate=_to_float(raw.get("fundingRate")),
            timestamp=ms_to_datetime(raw.get("timestamp")),
            next_funding_time=ms_to_datetime(raw.get("fundingTimestamp")),
            mark_price=_to_float(raw.get("markPrice")),
            index_price=_to_float(raw.get("indexPrice")),
            interval=raw.get("interval"),
        )

    # -- resilience / health (no auth) -------------------------------------------

    async def check_clock_skew(self, *, threshold_ms: float = CLOCK_SKEW_WARN_MS) -> ClockSkew:
        """Measure local-vs-exchange clock skew and flag drift past ``threshold_ms``.

        Reads the exchange's server time (CCXT ``fetch_time``, or its synchronous
        ``milliseconds()`` fallback) and compares it to the local clock. A signed-
        request exchange rejects requests whose timestamp drifts past its recvWindow,
        so surfacing skew here lets a status/health check warn an operator *before*
        an order or authed read fails with a confusing auth error. This is a public,
        unauthenticated read -- no credentials, no key scope required.

        The local clock is sampled both immediately before and immediately after the
        server-time round trip; the midpoint of those two samples is used as the
        local reference so network latency is split symmetrically rather than charged
        entirely to one side. ``skew_ms`` is ``local_mid_ms - server_time_ms`` (a
        positive value means the local clock is ahead of the exchange). A drift whose
        magnitude exceeds ``threshold_ms`` sets ``within_tolerance=False`` and emits a
        single redacted warning.

        Args:
            threshold_ms: Tolerance in milliseconds. ``abs(skew_ms)`` above this
                marks the result out of tolerance. Defaults to
                :data:`CLOCK_SKEW_WARN_MS`.

        Returns:
            A typed :class:`ClockSkew` snapshot. Carries no secret material.

        Raises:
            trader_mcp.errors.ExchangeError: if the server-time read fails (mapped
                and redacted by :meth:`_call`; never a raw CCXT exception).
        """
        before_ms = _now_ms()
        server_ms_raw = await self._call("fetch_time", self._server_time_factory())
        after_ms = _now_ms()

        server_ms = float(server_ms_raw)
        local_mid_ms = (before_ms + after_ms) / 2.0
        round_trip_ms = after_ms - before_ms
        skew_ms = local_mid_ms - server_ms
        within_tolerance = abs(skew_ms) <= threshold_ms

        if not within_tolerance:
            _logger.warning(
                "%s clock skew %.0fms exceeds tolerance %.0fms (round trip %.0fms); "
                "signed requests may be rejected -- check the host clock / NTP sync.",
                self.exchange_id,
                skew_ms,
                threshold_ms,
                round_trip_ms,
            )

        return ClockSkew(
            exchange=self.exchange_id,
            server_time=ms_to_datetime(server_ms) or datetime.now(UTC),
            local_time=ms_to_datetime(local_mid_ms) or datetime.now(UTC),
            skew_ms=skew_ms,
            round_trip_ms=round_trip_ms,
            threshold_ms=threshold_ms,
            within_tolerance=within_tolerance,
        )

    def _server_time_factory(self) -> Callable[[], Awaitable[Any]]:
        """Return a zero-arg coroutine factory that yields the exchange server time.

        Prefers the async unified ``fetch_time``; falls back to CCXT's synchronous
        ``milliseconds()`` (wrapped in a trivial coroutine) for a build/exchange that
        does not implement ``fetch_time``. Raises a typed not-supported error if
        neither is available, so :meth:`check_clock_skew` never hangs on a missing
        method.
        """
        fetch_time: Any = getattr(self._client, "fetch_time", None)
        if callable(fetch_time):

            async def _from_fetch_time() -> Any:
                awaitable: Any = fetch_time()
                return await awaitable

            return _from_fetch_time

        milliseconds = getattr(self._client, "milliseconds", None)
        if callable(milliseconds):

            async def _from_milliseconds() -> Any:
                return milliseconds()

            return _from_milliseconds

        raise map_ccxt_error(
            ccxt.NotSupported(f"{self.exchange_id} exposes no server-time method"),
            exchange=self.exchange_id,
            op="fetch_time",
        )

    # -- credentials -------------------------------------------------------------

    async def validate_credentials(self) -> CredentialStatus:
        """Check whether configured credentials are valid (read-only).

        If no credentials are configured, returns immediately with no network call.
        Otherwise attempts a minimal authed read to confirm validity and infer read
        scope. Per the v1 safety contract, credentials are read-only: on success we
        report ``scope="read_only"``, ``can_read=True``, ``can_trade=False``. We
        never claim trade scope. On any error, ``valid=False`` with a redacted
        message. The result never contains a secret.
        """
        settings = get_settings()
        creds = settings.credentials_for(self.exchange_id)
        if not creds.is_configured:
            return CredentialStatus(
                exchange=self.exchange_id,
                configured=False,
                valid=None,
                scope="unknown",
                can_read=False,
                can_trade=False,
                message="No API credentials configured for this exchange.",
            )

        # Minimal authed read. fetchBalance is the canonical private read in CCXT;
        # it confirms the key is valid and has read access without placing anything.
        try:
            await self._call("validate_credentials", lambda: self._client.fetch_balance())
        except Exception as exc:
            # _call maps to ExchangeError (redacted message); surface it as invalid.
            message = redact(str(exc))
            return CredentialStatus(
                exchange=self.exchange_id,
                configured=True,
                valid=False,
                scope="unknown",
                can_read=False,
                can_trade=False,
                message=message,
            )

        return CredentialStatus(
            exchange=self.exchange_id,
            configured=True,
            valid=True,
            scope="read_only",
            can_read=True,
            can_trade=False,
            message="Credentials are valid (read-only).",
        )

    # -- WebSocket streaming (CCXT Pro) ------------------------------------------

    def _require_websocket(self, op: str) -> None:
        """Refuse a WS stream when this exchange/build lacks WebSocket support.

        Guards against hanging: if the registry marks the exchange without WS, or
        the CCXT client exposes no ``watch_*`` method, we raise a clear typed error
        immediately instead of awaiting a method that does not exist.

        Raises:
            trader_mcp.errors.ExchangeError: ``details["kind"] == "not_supported"``.
        """
        meta = get_exchange_meta(self.exchange_id)
        has_method = callable(getattr(self._client, op, None))
        if meta.has_websocket and has_method:
            return
        raise map_ccxt_error(
            ccxt.NotSupported(f"{self.exchange_id} does not support WebSocket {op}"),
            exchange=self.exchange_id,
            op=op,
        )

    async def _watch_loop(
        self,
        op: str,
        watch_factory: Callable[[], Awaitable[Any]],
    ) -> AsyncIterator[Any]:
        """Drive a CCXT Pro ``watch_*`` method as a resilient async generator.

        Loops ``await watch_factory()`` and yields each raw update. Transient WS
        faults trigger bounded exponential reconnection backoff (resetting on a
        successful update); non-transient/auth faults are mapped via
        :func:`map_ccxt_error` and re-raised. ``asyncio.CancelledError`` propagates
        cleanly so the live runtime can stop a stream by cancelling its task.

        Yields:
            The raw CCXT update returned by each ``watch_*`` await.
        """
        self._require_websocket(op)
        failures = 0
        while True:
            try:
                update = await watch_factory()
            except asyncio.CancelledError:
                # Graceful stop: the live runtime cancels the consuming task.
                raise
            except TRANSIENT_CCXT_ERRORS as exc:
                failures += 1
                delay = min(
                    WS_BACKOFF_BASE_SECONDS * (2 ** (failures - 1)),
                    WS_BACKOFF_MAX_SECONDS,
                )
                _logger.warning(
                    "%s.%s transient WS error (reconnect #%d), backing off %.2fs: %s",
                    self.exchange_id,
                    op,
                    failures,
                    delay,
                    redact(str(exc)),
                )
                await _sleep(delay)
                continue
            except Exception as exc:
                # Non-transient (auth/not-supported/generic): do not reconnect.
                raise map_ccxt_error(exc, exchange=self.exchange_id, op=op) from exc
            failures = 0
            yield update

    async def watch_ohlcv(self, symbol: str, timeframe: str = "1h") -> AsyncIterator[OHLCVBar]:
        """Stream live OHLCV bars for ``symbol``/``timeframe`` via CCXT Pro.

        Each ``watch_ohlcv`` update is a list of recent candle rows (CCXT keeps a
        small rolling cache and returns the whole cache each await); we yield each
        row through the SAME :meth:`_normalize_ohlcv_row` the REST path uses, so a
        streamed :class:`OHLCVBar` is shape-identical to a fetched one (interpreter
        parity). Reconnects with bounded backoff on transient WS faults; stops
        cleanly on cancellation.

        Yields:
            One normalized :class:`OHLCVBar` per candle row in each update, oldest
            first.
        """
        async for update in self._watch_loop(
            "watch_ohlcv", lambda: self._client.watch_ohlcv(symbol, timeframe)
        ):
            for row in update or []:
                bar = self._normalize_ohlcv_row(row)
                if bar is not None:
                    yield bar

    async def watch_trades(self, symbol: str) -> AsyncIterator[Trade]:
        """Stream live public trades for ``symbol`` via CCXT Pro.

        Each ``watch_trades`` update is a list of recent trades; we yield each
        through the SAME :meth:`_normalize_trade` the REST path uses. Reconnects
        with bounded backoff on transient faults; stops cleanly on cancellation.

        Yields:
            One normalized :class:`Trade` per trade in each update, oldest first.
        """
        async for update in self._watch_loop(
            "watch_trades", lambda: self._client.watch_trades(symbol)
        ):
            for raw in update or []:
                trade = self._normalize_trade(raw)
                if trade is not None:
                    yield trade

    async def watch_order_book(self, symbol: str, *, limit: int = 25) -> AsyncIterator[OrderBook]:
        """Stream live order-book snapshots for ``symbol`` via CCXT Pro.

        Each ``watch_order_book`` update is a CCXT order-book payload (CCXT applies
        deltas internally and returns the merged book each await); we yield each
        through the SAME :meth:`_normalize_order_book` the REST path uses.
        Reconnects with bounded backoff on transient faults; stops cleanly on
        cancellation.

        Args:
            symbol: Unified CCXT symbol.
            limit: Depth (number of price levels) to request per side.

        Yields:
            One normalized :class:`OrderBook` per update.
        """
        async for update in self._watch_loop(
            "watch_order_book", lambda: self._client.watch_order_book(symbol, limit)
        ):
            yield self._normalize_order_book(symbol, update)

    # -- order lifecycle / account (trade-enabled scope only) --------------------

    async def create_order(
        self,
        symbol: str,
        type: OrderType,  # noqa: A002 -- matches CCXT's unified create_order signature
        side: OrderSide,
        amount: float,
        price: float | None = None,
        *,
        client_order_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Order:
        """Place an order (paper/testnet plumbing). Trade-enabled scope only.

        This is connectivity plumbing: it does NOT decide dry-run vs live (the
        safety layer does, upstream). It refuses outright when the adapter's key is
        read-only. The ``client_order_id`` is forwarded to CCXT's ``clientOrderId``
        param for idempotency (idempotency itself is enforced upstream).

        Args:
            symbol: Unified CCXT symbol.
            type: ``"market"`` or ``"limit"``.
            side: ``"buy"`` or ``"sell"``.
            amount: Order amount in base units.
            price: Limit price; required by CCXT for ``"limit"`` orders.
            client_order_id: Optional idempotency key forwarded as ``clientOrderId``.
            params: Optional extra CCXT params (merged; ``clientOrderId`` wins).

        Returns:
            The normalized :class:`Order`.

        Raises:
            trader_mcp.errors.ExchangeError: ``details["kind"] == "scope"`` if the
                key is read-only; otherwise the mapped CCXT failure.
        """
        self._require_trade_scope("create_order")
        call_params: dict[str, Any] = dict(params or {})
        if client_order_id is not None:
            call_params["clientOrderId"] = client_order_id
        raw = await self._call(
            "create_order",
            lambda: self._client.create_order(symbol, type, side, amount, price, call_params),
        )
        return self._normalize_order(symbol, raw)

    async def cancel_order(self, order_id: str, symbol: str) -> Order:
        """Cancel an order by id. Trade-enabled scope only.

        Raises:
            trader_mcp.errors.ExchangeError: ``details["kind"] == "scope"`` if the
                key is read-only; otherwise the mapped CCXT failure.
        """
        self._require_trade_scope("cancel_order")
        raw = await self._call(
            "cancel_order",
            lambda: self._client.cancel_order(order_id, symbol),
        )
        return self._normalize_order(symbol, raw)

    async def fetch_order(self, order_id: str, symbol: str) -> Order:
        """Fetch a single order by id. Trade-enabled scope only.

        Reading order state is a private/authed call, so it requires a trade-enabled
        key like the mutating paths.

        Raises:
            trader_mcp.errors.ExchangeError: ``details["kind"] == "scope"`` if the
                key is read-only; otherwise the mapped CCXT failure.
        """
        self._require_trade_scope("fetch_order")
        raw = await self._call(
            "fetch_order",
            lambda: self._client.fetch_order(order_id, symbol),
        )
        return self._normalize_order(symbol, raw)

    async def fetch_open_orders(self, symbol: str | None = None) -> list[Order]:
        """Fetch open orders, optionally for one ``symbol``. Trade-enabled scope only.

        Raises:
            trader_mcp.errors.ExchangeError: ``details["kind"] == "scope"`` if the
                key is read-only; otherwise the mapped CCXT failure.
        """
        self._require_trade_scope("fetch_open_orders")
        raw = await self._call(
            "fetch_open_orders",
            lambda: self._client.fetch_open_orders(symbol),
        )
        orders: list[Order] = []
        for o in raw or []:
            orders.append(self._normalize_order(o.get("symbol") or symbol or "", o))
        return orders

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[Position]:
        """Fetch open derivatives positions. Trade-enabled scope only.

        Spot-only exchanges (e.g. Coinbase) report no positions. Returns an empty
        list when the exchange has no ``fetch_positions`` support rather than
        hanging or raising.

        Raises:
            trader_mcp.errors.ExchangeError: ``details["kind"] == "scope"`` if the
                key is read-only; otherwise the mapped CCXT failure.
        """
        self._require_trade_scope("fetch_positions")
        if not callable(getattr(self._client, "fetch_positions", None)):
            return []
        raw = await self._call(
            "fetch_positions",
            lambda: self._client.fetch_positions(symbols),
        )
        positions: list[Position] = []
        for p in raw or []:
            normalized = self._normalize_position(p)
            if normalized is not None:
                positions.append(normalized)
        return positions

    async def fetch_balance(self) -> Balance:
        """Fetch the account balance. Trade-enabled scope only.

        ``validate_credentials`` also calls ``fetch_balance`` for a read-only
        liveness check, but this typed, normalized accessor is part of the trade
        surface and is scope-gated.

        Raises:
            trader_mcp.errors.ExchangeError: ``details["kind"] == "scope"`` if the
                key is read-only; otherwise the mapped CCXT failure.
        """
        self._require_trade_scope("fetch_balance")
        raw = await self._call("fetch_balance", lambda: self._client.fetch_balance())
        return self._normalize_balance(raw)

    # -- order/position/balance normalization -----------------------------------

    @staticmethod
    def _normalize_status(raw_status: Any) -> OrderStatus:
        """Map a CCXT raw order status to the stable :data:`OrderStatus` set."""
        known = ("open", "closed", "canceled", "rejected", "expired")
        if isinstance(raw_status, str) and raw_status in known:
            return raw_status  # type: ignore[return-value]
        return "unknown"

    def _normalize_order(self, symbol: str, raw: dict[str, Any]) -> Order:
        """Normalize a CCXT unified order dict into an :class:`Order`."""
        raw_type = raw.get("type")
        raw_side = raw.get("side")
        fee_raw = raw.get("fee")
        fee: OrderFee | None = None
        if isinstance(fee_raw, dict):
            fee = OrderFee(
                cost=_to_float(fee_raw.get("cost")),
                currency=fee_raw.get("currency"),
                rate=_to_float(fee_raw.get("rate")),
            )
        return Order(
            exchange=self.exchange_id,
            id=str(raw["id"]) if raw.get("id") is not None else None,
            client_order_id=raw.get("clientOrderId"),
            symbol=raw.get("symbol") or symbol,
            type=raw_type if raw_type in ("market", "limit") else None,
            side=raw_side if raw_side in ("buy", "sell") else None,
            status=self._normalize_status(raw.get("status")),
            price=_to_float(raw.get("price")),
            amount=_to_float(raw.get("amount")),
            filled=_to_float(raw.get("filled")),
            remaining=_to_float(raw.get("remaining")),
            average=_to_float(raw.get("average")),
            cost=_to_float(raw.get("cost")),
            fee=fee,
            timestamp=ms_to_datetime(raw.get("timestamp")),
        )

    def _normalize_position(self, raw: dict[str, Any]) -> Position | None:
        """Normalize a CCXT position dict into a :class:`Position`, or ``None``."""
        symbol = raw.get("symbol")
        if not symbol:
            return None
        raw_side = raw.get("side")
        return Position(
            exchange=self.exchange_id,
            symbol=symbol,
            side=raw_side if raw_side in ("long", "short") else None,
            contracts=_to_float(raw.get("contracts")),
            contract_size=_to_float(raw.get("contractSize")),
            entry_price=_to_float(raw.get("entryPrice")),
            mark_price=_to_float(raw.get("markPrice")),
            notional=_to_float(raw.get("notional")),
            unrealized_pnl=_to_float(raw.get("unrealizedPnl")),
            leverage=_to_float(raw.get("leverage")),
            liquidation_price=_to_float(raw.get("liquidationPrice")),
            timestamp=ms_to_datetime(raw.get("timestamp")),
        )

    def _normalize_balance(self, raw: dict[str, Any]) -> Balance:
        """Normalize a CCXT balance payload into a :class:`Balance`.

        CCXT returns ``{currency: {free, used, total}, ...}`` plus aggregate keys
        (``free``/``used``/``total``/``info``/``timestamp``). We keep only true
        per-currency entries that carry a non-zero free/used/total, to keep the
        payload small and AI-friendly.
        """
        aggregate_keys = {"free", "used", "total", "info", "timestamp", "datetime"}
        entries: dict[str, BalanceEntry] = {}
        for currency, value in raw.items():
            if currency in aggregate_keys or not isinstance(value, dict):
                continue
            free = _to_float(value.get("free"))
            used = _to_float(value.get("used"))
            total = _to_float(value.get("total"))
            if not (free or used or total):
                continue
            entries[currency] = BalanceEntry(
                currency=currency,
                free=free,
                used=used,
                total=total,
            )
        return Balance(
            exchange=self.exchange_id,
            entries=entries,
            timestamp=ms_to_datetime(raw.get("timestamp")),
        )
