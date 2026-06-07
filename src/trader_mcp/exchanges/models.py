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

#: Order side as normalized by CCXT's unified order schema.
OrderSide = Literal["buy", "sell"]

#: Order type as normalized by CCXT's unified order schema. v1 plumbing carries
#: ``market``/``limit``; richer types (stop, etc.) are out of scope here.
OrderType = Literal["market", "limit"]

#: Normalized order lifecycle status. CCXT reports the raw status as one of
#: ``open``/``closed``/``canceled``; anything else degrades to ``"unknown"``.
OrderStatus = Literal["open", "closed", "canceled", "rejected", "expired", "unknown"]

#: Position side for a derivatives position. Spot exchanges do not report these.
PositionSide = Literal["long", "short"]


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


class ClockSkew(_DomainModel):
    """Result of measuring local-vs-exchange clock skew (a resilience health check).

    Signed-request exchanges reject (or silently mis-time) requests when the local
    clock drifts too far from the exchange's server time. This typed snapshot lets a
    status/health check surface that drift before it causes auth/recvWindow failures.

    ``skew_ms`` is ``local_time_ms - server_time_ms``: a **positive** value means the
    local clock is *ahead* of the exchange; **negative** means it is *behind*.
    ``within_tolerance`` is ``False`` when ``abs(skew_ms)`` exceeds the configured
    warning threshold (``threshold_ms``). The measurement performs one network round
    trip; ``round_trip_ms`` is recorded so a large skew attributable to latency can be
    judged in context. This model carries no secret material.
    """

    exchange: ExchangeId
    server_time: datetime
    local_time: datetime
    skew_ms: float
    round_trip_ms: float
    threshold_ms: float
    within_tolerance: bool


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


class OrderFee(_DomainModel):
    """A normalized trading fee charged on an order/fill.

    All fields are optional: CCXT omits the fee until an order (partially) fills,
    and some exchanges never report a fee currency.
    """

    cost: float | None = None
    currency: str | None = None
    rate: float | None = None


class Order(_DomainModel):
    """A normalized order, as returned by create/cancel/fetch order calls.

    Wraps CCXT's unified order schema. Numeric/derived fields are optional because
    CCXT does not populate every field at every lifecycle stage (e.g. ``average``
    is ``None`` until there is a fill; ``price`` is ``None`` for a market order).
    The ``status`` is normalized to a small stable set; an unrecognized raw status
    degrades to ``"unknown"`` rather than failing.
    """

    exchange: ExchangeId
    id: str | None = None
    client_order_id: str | None = None
    symbol: str
    type: OrderType | None = None
    side: OrderSide | None = None
    status: OrderStatus = "unknown"
    price: float | None = None
    amount: float | None = None
    filled: float | None = None
    remaining: float | None = None
    average: float | None = None
    cost: float | None = None
    fee: OrderFee | None = None
    timestamp: datetime | None = None


class Position(_DomainModel):
    """A normalized open derivatives position.

    Returned by ``fetch_positions``. Spot-only exchanges (e.g. Coinbase) report no
    positions, so this is used by the swap-capable exchanges. Most fields are
    optional because CCXT's position schema is sparse and exchange-dependent.
    """

    exchange: ExchangeId
    symbol: str
    side: PositionSide | None = None
    contracts: float | None = None
    contract_size: float | None = None
    entry_price: float | None = None
    mark_price: float | None = None
    notional: float | None = None
    unrealized_pnl: float | None = None
    leverage: float | None = None
    liquidation_price: float | None = None
    timestamp: datetime | None = None


class BalanceEntry(_DomainModel):
    """A single currency's free/used/total balance."""

    currency: str
    free: float | None = None
    used: float | None = None
    total: float | None = None


class Balance(_DomainModel):
    """A normalized account balance: a per-currency map of free/used/total.

    Only currencies with a non-zero total (or a non-zero free/used) are retained,
    keyed by currency code, to keep the payload small and AI-friendly.
    """

    exchange: ExchangeId
    entries: dict[str, BalanceEntry]
    timestamp: datetime | None = None
