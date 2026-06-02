"""Typed domain models for normalized exchange market data.

Every value returned by the exchange adapter is one of these Pydantic v2 models.
They wrap CCXT's (loosely-typed) unified payloads into a stable, typed surface the
rest of trader-mcp (MCP server, data pipeline, backtest engine) codes against.

Design notes:
    * These models wrap *external* data, so they are intentionally **lenient**:
      optional/derived fields default to ``None`` rather than failing on a partial
      CCXT payload. We do not use ``extra="forbid"`` here -- a missing CCXT field
      must degrade to ``None``, not raise.
    * Timestamps come from CCXT as integer milliseconds since epoch. Use
      :func:`ms_to_datetime` to normalize them to timezone-aware UTC ``datetime``.
    * ``CredentialStatus`` is a safety-sensitive model and must NEVER contain any
      secret material (no API key/secret, no auth header).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from trader_mcp.config import ExchangeId

#: Market kinds trader-mcp supports in v1: spot and perpetual/linear swaps.
MarketType = Literal["spot", "swap"]

#: Reliability tier of a supported exchange. ``certified`` is the reference
#: exchange (Coinbase), validated first in every phase; Kraken is also
#: ``certified``. Gemini and Crypto.com are ``supported``.
ReliabilityTier = Literal["certified", "supported"]


def ms_to_datetime(ms: float | int | None) -> datetime | None:
    """Convert a CCXT millisecond timestamp to a timezone-aware UTC datetime.

    Args:
        ms: Milliseconds since the Unix epoch, or ``None``.

    Returns:
        A timezone-aware UTC :class:`datetime`, or ``None`` when ``ms`` is ``None``.
    """
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


class _DomainModel(BaseModel):
    """Base for normalized market-data models.

    Lenient on extra CCXT fields (ignored) so that adapter normalization never
    breaks on a richer-than-expected payload, but strict on the types of the
    fields we do declare.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)


class Market(_DomainModel):
    """A normalized tradable market (symbol) on an exchange.

    Fields beyond the core symbol/base/quote are optional because CCXT does not
    populate every field for every exchange; missing values degrade to ``None``.
    """

    exchange: ExchangeId
    symbol: str
    base: str
    quote: str
    settle: str | None = None
    type: MarketType
    active: bool = True
    contract_size: float | None = None
    linear: bool | None = None
    inverse: bool | None = None
    price_precision: float | None = None
    amount_precision: float | None = None
    min_amount: float | None = None
    max_amount: float | None = None
    min_cost: float | None = None
    maker_fee: float | None = None
    taker_fee: float | None = None


class ExchangeInfo(_DomainModel):
    """Static metadata describing a supported exchange (no network call)."""

    exchange: ExchangeId
    ccxt_id: str
    name: str
    is_reference: bool
    reliability_tier: ReliabilityTier
    has_websocket: bool


class ExchangeCapabilities(_DomainModel):
    """What an exchange supports, derived from CCXT's static ``.has`` map.

    Computed without any network call (CCXT's capability flags are static). A
    capability is considered supported when the CCXT ``has`` value is truthy or the
    string ``"emulated"`` (CCXT emulates the call client-side).
    """

    exchange: ExchangeId
    ccxt_id: str
    has_spot: bool
    has_swap: bool
    has_websocket: bool
    timeframes: list[str]
    supports_ohlcv: bool
    supports_order_book: bool
    supports_trades: bool
    supports_ticker: bool
    supports_funding_rate: bool
    supports_testnet: bool
    market_count: int | None = None


class Ticker(_DomainModel):
    """A normalized ticker snapshot for one symbol."""

    exchange: ExchangeId
    symbol: str
    timestamp: datetime | None = None
    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    high: float | None = None
    low: float | None = None
    open: float | None = None
    close: float | None = None
    base_volume: float | None = None
    quote_volume: float | None = None
    change: float | None = None
    percentage: float | None = None


class OHLCVBar(_DomainModel):
    """A single OHLCV candle with a timezone-aware UTC open timestamp."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class OHLCVResult(_DomainModel):
    """A batch of OHLCV candles for a symbol/timeframe."""

    exchange: ExchangeId
    symbol: str
    timeframe: str
    bars: list[OHLCVBar]
    count: int


class OrderBookLevel(_DomainModel):
    """A single (price, amount) level in an order book."""

    price: float
    amount: float


class OrderBook(_DomainModel):
    """A normalized order-book snapshot (bids descending, asks ascending)."""

    exchange: ExchangeId
    symbol: str
    timestamp: datetime | None = None
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]


class Trade(_DomainModel):
    """A single public trade print."""

    id: str | None = None
    timestamp: datetime | None = None
    side: Literal["buy", "sell"] | None = None
    price: float
    amount: float
    cost: float | None = None


class RecentTradesResult(_DomainModel):
    """A batch of recent public trades for a symbol."""

    exchange: ExchangeId
    symbol: str
    trades: list[Trade]
    count: int


class FundingRate(_DomainModel):
    """A normalized perpetual-swap funding-rate snapshot."""

    exchange: ExchangeId
    symbol: str
    funding_rate: float | None = None
    timestamp: datetime | None = None
    next_funding_time: datetime | None = None
    mark_price: float | None = None
    index_price: float | None = None
    interval: str | None = None


class CredentialStatus(_DomainModel):
    """Result of checking an exchange credential.

    SAFETY: this model is returned across the MCP boundary. It MUST NEVER contain
    any secret material (API key/secret, auth header). Only configured/valid flags,
    the (read-only) scope, and a redacted human-readable ``message``.
    """

    exchange: ExchangeId
    configured: bool
    valid: bool | None = None
    scope: Literal["read_only", "trade_enabled", "unknown"] = "unknown"
    can_read: bool = False
    can_trade: bool = False
    message: str
