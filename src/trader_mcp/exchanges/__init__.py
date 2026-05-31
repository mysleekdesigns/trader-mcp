"""Exchange connectivity (owned by ``exchange-adapter-engineer``).

A single unified CCXT (async) adapter for Bybit, BloFin, Toobit, and WeeX. Bybit is
the reference exchange, validated first. This package is read-only and market-data
only: it exposes no order placement/cancellation/routing -- those paths do not
exist until Phase 6. CCXT Pro WebSocket streaming lands in Phase 5.

Public surface:
    * :class:`ExchangeAdapter` -- the per-exchange typed, read-only client wrapper.
    * :class:`ExchangeManager` -- a process-wide adapter cache + lifecycle.
    * :func:`map_ccxt_error` -- maps any CCXT exception to a typed, redacted error.
    * Registry helpers: :func:`list_supported`, :func:`is_supported`,
      :func:`get_exchange_meta`.
    * The normalized Pydantic v2 domain models.
"""

from __future__ import annotations

from trader_mcp.exchanges.adapter import ExchangeAdapter
from trader_mcp.exchanges.errors import map_ccxt_error
from trader_mcp.exchanges.manager import ExchangeManager
from trader_mcp.exchanges.models import (
    CredentialStatus,
    ExchangeCapabilities,
    ExchangeInfo,
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
)
from trader_mcp.exchanges.registry import (
    get_exchange_meta,
    is_supported,
    list_supported,
)

__all__ = [
    "CredentialStatus",
    "ExchangeAdapter",
    "ExchangeCapabilities",
    "ExchangeInfo",
    "ExchangeManager",
    "FundingRate",
    "Market",
    "MarketType",
    "OHLCVBar",
    "OHLCVResult",
    "OrderBook",
    "OrderBookLevel",
    "RecentTradesResult",
    "Ticker",
    "Trade",
    "get_exchange_meta",
    "is_supported",
    "list_supported",
    "map_ccxt_error",
]
