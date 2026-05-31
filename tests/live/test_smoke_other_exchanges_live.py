"""Live smoke test for the non-reference exchanges: BloFin, Toobit, WeeX.

Opt-in only (``--live`` / ``TRADER_MCP_LIVE_TESTS=1``). These prove basic
connectivity (load markets + ticker + ohlcv) without asserting the deep invariants
reserved for Bybit. Capability gaps are *tolerated*: a path the exchange does not
support is recorded as a ``skip`` (a quirk to log in ``notes/exchange-quirks.md``)
rather than a hard failure.
"""

from __future__ import annotations

import pytest

from trader_mcp.errors import ExchangeError
from trader_mcp.exchanges import ExchangeManager

pytestmark = pytest.mark.live

OTHER_EXCHANGES = ["blofin", "toobit", "weex"]


@pytest.fixture(params=OTHER_EXCHANGES)
def exchange_id(request: pytest.FixtureRequest) -> str:
    return request.param


async def test_markets_load(manager: ExchangeManager, exchange_id: str) -> None:
    adapter = await manager.get(exchange_id)  # type: ignore[arg-type]
    markets = await adapter.list_markets(active_only=True)
    assert markets, f"{exchange_id}: no active markets returned"


async def test_ticker_and_ohlcv(manager: ExchangeManager, exchange_id: str) -> None:
    adapter = await manager.get(exchange_id)  # type: ignore[arg-type]
    spot = await adapter.list_markets(market_type="spot", active_only=True, limit=1)
    if not spot:
        pytest.skip(f"{exchange_id}: no spot market available to probe (quirk)")
    symbol = spot[0].symbol

    ticker = await adapter.fetch_ticker(symbol)
    assert ticker.symbol

    try:
        result = await adapter.fetch_ohlcv(symbol, "1h", limit=10)
    except ExchangeError as exc:
        if exc.details.get("kind") == "not_supported":
            pytest.skip(f"{exchange_id}: fetch_ohlcv not supported (quirk): {symbol}")
        raise
    assert result.bars, f"{exchange_id}: empty OHLCV for {symbol}"
