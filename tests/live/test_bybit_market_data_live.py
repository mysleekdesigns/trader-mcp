"""Deep live-validation of all 9 market-data paths on real Bybit (reference exchange).

Opt-in only (``--live`` / ``TRADER_MCP_LIVE_TESTS=1``). Bybit is the reference
exchange (PRD §6): these assert real-data invariants, not just non-empty responses.
Run from an allowed-region vantage point (VPN/proxy/unblocked CI). Symbols are the
canonical BTC/USDT spot + BTC/USDT:USDT perp.
"""

from __future__ import annotations

import pytest

from trader_mcp.exchanges import ExchangeManager

pytestmark = pytest.mark.live

SPOT = "BTC/USDT"
PERP = "BTC/USDT:USDT"


async def test_capabilities_reports_markets(manager: ExchangeManager) -> None:
    adapter = await manager.get("bybit")
    await adapter.load_markets()
    caps = adapter.capabilities()
    assert caps.exchange == "bybit"
    assert caps.market_count is not None
    assert caps.market_count > 0
    assert caps.supports_ohlcv
    assert caps.supports_ticker


async def test_list_and_search_markets(manager: ExchangeManager) -> None:
    adapter = await manager.get("bybit")
    markets = await adapter.list_markets(limit=10)
    assert markets
    matches = await adapter.search_symbols("BTC", limit=10)
    assert matches
    assert all("btc" in m.symbol.lower() or m.base.lower() == "btc" for m in matches)


async def test_ticker_bid_below_ask(manager: ExchangeManager) -> None:
    adapter = await manager.get("bybit")
    ticker = await adapter.fetch_ticker(SPOT)
    assert ticker.bid is not None
    assert ticker.ask is not None
    assert ticker.bid < ticker.ask
    assert ticker.last is not None
    assert ticker.last > 0


async def test_ohlcv_invariants(manager: ExchangeManager) -> None:
    adapter = await manager.get("bybit")
    result = await adapter.fetch_ohlcv(SPOT, "1h", limit=100)
    assert result.bars, "no candles returned"
    timestamps = [b.timestamp for b in result.bars]
    # Strictly increasing, timezone-aware UTC.
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == len(timestamps)
    assert all(b.timestamp.tzinfo is not None for b in result.bars)
    for bar in result.bars:
        assert bar.high >= max(bar.open, bar.close)
        assert bar.low <= min(bar.open, bar.close)


async def test_order_book_has_spread(manager: ExchangeManager) -> None:
    adapter = await manager.get("bybit")
    book = await adapter.fetch_order_book(SPOT, limit=10)
    assert book.bids
    assert book.asks
    assert book.bids[0].price < book.asks[0].price


async def test_recent_trades(manager: ExchangeManager) -> None:
    adapter = await manager.get("bybit")
    result = await adapter.fetch_recent_trades(SPOT, limit=20)
    assert result.trades
    assert all(t.price > 0 and t.amount > 0 for t in result.trades)


async def test_funding_rate_on_perp(manager: ExchangeManager) -> None:
    adapter = await manager.get("bybit")
    funding = await adapter.fetch_funding_rate(PERP)
    assert funding.symbol
    assert funding.funding_rate is not None
