"""The single unified CCXT (async) adapter for all four target exchanges.

One :class:`ExchangeAdapter` wraps one CCXT async client and exposes a typed,
read-only, market-data surface. Bybit is the reference exchange; BloFin, Toobit,
and WeeX reach the same methods through the identical code path -- there is no
per-exchange branching here (genuine differences live in
:mod:`trader_mcp.exchanges.registry` or as CCXT capability flags).

Invariants enforced in this module:
    * **Read-only.** No order placement/cancellation/routing methods exist here.
      Credentials are wired in read-only; trade-enabled paths do not exist until
      Phase 6.
    * **No raw CCXT exception escapes.** Every CCXT call goes through
      :meth:`ExchangeAdapter._call`, which retries transient faults with backoff
      and maps the final failure via :func:`trader_mcp.exchanges.errors.map_ccxt_error`.
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
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import ccxt
import ccxt.async_support as ccxt_async

from trader_mcp.config import ExchangeId, Settings, get_settings
from trader_mcp.errors import ConfigError
from trader_mcp.exchanges.errors import TRANSIENT_CCXT_ERRORS, map_ccxt_error
from trader_mcp.exchanges.models import (
    CredentialStatus,
    ExchangeCapabilities,
    FundingRate,
    Market,
    MarketType,
    OHLCVBar,
    OHLCVResult,
    OrderBook,
    OrderBookLevel,
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


def _create_ccxt_client(exchange_id: str, config: dict[str, Any]) -> Any:
    """Construct a real CCXT async client. Test seam -- monkeypatched in tests.

    This is the ONLY place a real CCXT client is instantiated. Keeping it a small,
    module-level function lets the test suite replace it with a fake client without
    any network access.

    Args:
        exchange_id: The CCXT client id (e.g. ``"bybit"``).
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
    ) -> None:
        """Low-level constructor. Prefer :meth:`create`.

        Args:
            exchange: The trader-mcp exchange id.
            client: A constructed CCXT async client (or test fake).
            testnet: Whether the client is in sandbox/testnet mode.
        """
        self.exchange_id: ExchangeId = exchange
        self.testnet: bool = testnet
        self._client: Any = client
        self._closed: bool = False

    # -- lifecycle ---------------------------------------------------------------

    @classmethod
    async def create(
        cls,
        exchange: ExchangeId,
        *,
        testnet: bool = False,
        settings: Settings | None = None,
    ) -> ExchangeAdapter:
        """Create and configure an adapter for ``exchange``.

        Builds a rate-limited CCXT client, wires in read-only credentials when they
        are configured for this exchange, and enables sandbox/testnet mode when
        requested and supported.

        Args:
            exchange: A supported trader-mcp exchange id.
            testnet: When ``True``, put the client in sandbox/testnet mode if the
                exchange supports it.
            settings: Optional settings override (defaults to the process-wide
                cached settings).

        Returns:
            A ready-to-use :class:`ExchangeAdapter`.
        """
        settings = settings or get_settings()
        ccxt_id = ccxt_id_for(exchange)

        config: dict[str, Any] = {"enableRateLimit": True}

        # Wire read-only credentials only when fully configured. Plaintext is
        # revealed exactly here, at the construction call site, and never logged.
        creds = settings.credentials_for(exchange)
        if creds.is_configured:
            api_key, api_secret = creds.reveal()
            config["apiKey"] = api_key
            config["secret"] = api_secret

        # Timeout + optional proxy (revealed here only; never logged). Lets a
        # geo-blocked host reach the exchanges over an HTTPS/SOCKS proxy or VPN.
        _apply_network_settings(config, settings)

        client = _create_ccxt_client(ccxt_id, config)

        if testnet:
            # Only attempt sandbox mode when the client/exchange supports it; some
            # CCXT clients raise NotSupported otherwise.
            set_sandbox = getattr(client, "set_sandbox_mode", None)
            if callable(set_sandbox):
                try:
                    set_sandbox(True)
                except ccxt.NotSupported:
                    _logger.warning(
                        "Exchange %s does not support testnet/sandbox; using live endpoints",
                        exchange,
                    )

        _logger.debug(
            "Created %s adapter (testnet=%s, credentialed=%s)",
            exchange,
            testnet,
            creds.is_configured,
        )
        return cls(exchange=exchange, client=client, testnet=testnet)

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
            # These four CEXes offer both spot and swap; default True if CCXT omits.
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
            symbol: Unified CCXT symbol (e.g. ``"BTC/USDT:USDT"``).
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
            # CCXT OHLCV row: [timestamp_ms, open, high, low, close, volume]
            if not row or len(row) < 6:
                continue
            ts = ms_to_datetime(row[0])
            if ts is None:
                continue
            # Coerce body cells via _to_float and skip any bar with a missing/
            # non-numeric cell. Exchanges emit None close/volume on thin or
            # just-opened candles; a bare float() would raise OUTSIDE _call and
            # let a raw exception escape the adapter.
            o = _to_float(row[1])
            h = _to_float(row[2])
            low = _to_float(row[3])
            c = _to_float(row[4])
            v = _to_float(row[5])
            if o is None or h is None or low is None or c is None or v is None:
                continue
            bars.append(
                OHLCVBar(
                    timestamp=ts,
                    open=o,
                    high=h,
                    low=low,
                    close=c,
                    volume=v,
                )
            )
        return OHLCVResult(
            exchange=self.exchange_id,
            symbol=symbol,
            timeframe=timeframe,
            bars=bars,
            count=len(bars),
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
            price = _to_float(t.get("price"))
            amount = _to_float(t.get("amount"))
            if price is None or amount is None:
                continue
            side = t.get("side")
            trades.append(
                Trade(
                    id=str(t["id"]) if t.get("id") is not None else None,
                    timestamp=ms_to_datetime(t.get("timestamp")),
                    side=side if side in ("buy", "sell") else None,
                    price=price,
                    amount=amount,
                    cost=_to_float(t.get("cost")),
                )
            )
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
