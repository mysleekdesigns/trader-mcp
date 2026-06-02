"""Tests for the Phase 1 MCP market-data tool surface (build_app + fake client).

These tools wrap the read-only exchange adapter and return typed Pydantic v2
models, so every structured tool output must validate against its declared model.
The single offline seam is the same one the adapter tests use:
``trader_mcp.exchanges.adapter._create_ccxt_client`` is monkeypatched to a fake.

The wrapper-result schemas (``ExchangesResult``/``SymbolSearchResult``/
``MarketsResult``) and the 9 market-data tools are a hard Phase 1 contract -- these
imports/assertions are unconditional, so any regression that drops a tool or a
wrapper model fails loudly here rather than skipping.
"""

from __future__ import annotations

from typing import Any

import pytest

import trader_mcp.exchanges.adapter as adapter_module
from tests._fakes import (
    SECRET_TOKEN,
    FakeCcxt,
    assert_no_secret,
    ccxt_error,
    make_create_factory,
)
from trader_mcp.exchanges import (
    ExchangeCapabilities,
    FundingRate,
    OHLCVResult,
    OrderBook,
    RecentTradesResult,
    Ticker,
)
from trader_mcp.server.app import build_app
from trader_mcp.server.schemas import (
    ExchangesResult,
    MarketsResult,
    SymbolSearchResult,
)

EXCHANGE_IDS = {"coinbase", "kraken", "gemini", "cryptocom"}

#: The 9 Phase 1 market-data/discovery tools build_app must register.
PHASE1_TOOLS = (
    "list_exchanges",
    "get_exchange_capabilities",
    "search_symbols",
    "list_markets",
    "get_ticker",
    "get_ohlcv",
    "get_order_book",
    "get_recent_trades",
    "get_funding_rate",
)


def _structured(result: Any) -> dict[str, Any]:
    """Extract the structured-output dict from a FastMCP ``call_tool`` result."""
    assert isinstance(result, tuple)
    structured = result[1]
    assert isinstance(structured, dict)
    return structured


async def _tools_by_name(app: Any) -> dict[str, Any]:
    return {t.name: t for t in await app.list_tools()}


def _exchange_enum(input_schema: dict[str, Any]) -> set[str] | None:
    """Pull the ``exchange`` parameter's enum from a tool inputSchema, if present."""
    props = input_schema.get("properties", {})
    exch = props.get("exchange")
    if not isinstance(exch, dict):
        return None
    if "enum" in exch:
        return set(exch["enum"])
    # Optional/anyOf form: search nested branches for the enum.
    for branch in exch.get("anyOf", []):
        if isinstance(branch, dict) and "enum" in branch:
            return set(branch["enum"])
    return None


@pytest.fixture
def app_with_fake(monkeypatch: pytest.MonkeyPatch, no_credentials: None) -> Any:
    """A built app whose adapters wrap a fresh fake CCXT client per exchange."""
    clients = [FakeCcxt() for _ in range(4)]
    monkeypatch.setattr(adapter_module, "_create_ccxt_client", make_create_factory(clients=clients))
    return build_app()


# --------------------------------------------------------------------------- #
# Registration + schema advertisement
# --------------------------------------------------------------------------- #
async def test_all_eleven_tools_registered() -> None:
    app = build_app()
    names = set(await _tools_by_name(app))
    assert {"health_check", "get_server_status"} <= names
    assert set(PHASE1_TOOLS) <= names
    assert len(names) >= 11


async def test_phase1_tools_advertise_structured_output_schema() -> None:
    app = build_app()
    by_name = await _tools_by_name(app)
    for name in PHASE1_TOOLS:
        tool = by_name[name]
        assert tool.outputSchema is not None, f"{name} must advertise an outputSchema"
        assert tool.outputSchema.get("type") == "object"


async def test_phase1_tools_constrain_exchange_enum() -> None:
    """Every tool taking an ``exchange`` must constrain it to the 4 supported ids."""
    app = build_app()
    by_name = await _tools_by_name(app)
    for name in PHASE1_TOOLS:
        if name == "list_exchanges":
            continue  # no exchange parameter
        enum = _exchange_enum(by_name[name].inputSchema)
        assert enum is not None, f"{name} must expose an 'exchange' enum"
        assert enum == EXCHANGE_IDS, f"{name} exchange enum was {enum}"


# --------------------------------------------------------------------------- #
# list_exchanges -- static, no fake needed
# --------------------------------------------------------------------------- #
async def test_list_exchanges_returns_four_coinbase_first() -> None:
    app = build_app()
    structured = _structured(await app.call_tool("list_exchanges", {}))
    result = ExchangesResult.model_validate(structured)
    assert result.count == 4
    assert [e.exchange for e in result.exchanges] == ["coinbase", "kraken", "gemini", "cryptocom"]


# --------------------------------------------------------------------------- #
# Per-tool: structured output validates against the declared model
# --------------------------------------------------------------------------- #
async def test_get_exchange_capabilities(app_with_fake: Any) -> None:
    structured = _structured(
        await app_with_fake.call_tool("get_exchange_capabilities", {"exchange": "coinbase"})
    )
    caps = ExchangeCapabilities.model_validate(structured)
    assert caps.exchange == "coinbase"
    assert caps.supports_ohlcv is True


async def test_search_symbols(app_with_fake: Any) -> None:
    structured = _structured(
        await app_with_fake.call_tool("search_symbols", {"exchange": "coinbase", "query": "btc"})
    )
    result = SymbolSearchResult.model_validate(structured)
    assert result.exchange == "coinbase"
    assert result.query == "btc"
    assert result.count == len(result.markets)
    assert all("BTC" in m.base or "BTC" in m.symbol for m in result.markets)


async def test_list_markets(app_with_fake: Any) -> None:
    structured = _structured(
        await app_with_fake.call_tool("list_markets", {"exchange": "coinbase"})
    )
    result = MarketsResult.model_validate(structured)
    assert result.exchange == "coinbase"
    assert result.count == len(result.markets)
    assert result.count >= 1


async def test_get_ticker(app_with_fake: Any) -> None:
    structured = _structured(
        await app_with_fake.call_tool("get_ticker", {"exchange": "coinbase", "symbol": "BTC/USD"})
    )
    ticker = Ticker.model_validate(structured)
    assert ticker.symbol == "BTC/USD"
    assert ticker.last == 42000.5


async def test_get_ohlcv(app_with_fake: Any) -> None:
    structured = _structured(
        await app_with_fake.call_tool(
            "get_ohlcv", {"exchange": "coinbase", "symbol": "BTC/USD", "timeframe": "1h"}
        )
    )
    result = OHLCVResult.model_validate(structured)
    assert result.timeframe == "1h"
    assert result.count == len(result.bars) == 3


async def test_get_order_book(app_with_fake: Any) -> None:
    structured = _structured(
        await app_with_fake.call_tool(
            "get_order_book", {"exchange": "coinbase", "symbol": "BTC/USD"}
        )
    )
    book = OrderBook.model_validate(structured)
    assert book.symbol == "BTC/USD"
    assert len(book.bids) == 2
    assert len(book.asks) == 2


async def test_get_recent_trades(app_with_fake: Any) -> None:
    structured = _structured(
        await app_with_fake.call_tool(
            "get_recent_trades", {"exchange": "coinbase", "symbol": "BTC/USD"}
        )
    )
    result = RecentTradesResult.model_validate(structured)
    assert result.count == len(result.trades) == 2


async def test_get_funding_rate(app_with_fake: Any) -> None:
    structured = _structured(
        await app_with_fake.call_tool(
            "get_funding_rate", {"exchange": "coinbase", "symbol": "ETH/USD:USD"}
        )
    )
    funding = FundingRate.model_validate(structured)
    assert funding.symbol == "ETH/USD:USD"
    assert funding.interval == "8h"


# --------------------------------------------------------------------------- #
# No-leak: a ccxt error embedding a secret must surface redacted via the tool
# --------------------------------------------------------------------------- #
async def test_tool_error_path_redacts_secret(
    monkeypatch: pytest.MonkeyPatch, no_credentials: None, no_sleep: list[float]
) -> None:
    client = FakeCcxt()
    client.raise_on["fetch_ticker"] = ccxt_error("auth", leak=True)
    monkeypatch.setattr(adapter_module, "_create_ccxt_client", make_create_factory(client))
    app = build_app()
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - error type lives behind the SDK
        await app.call_tool("get_ticker", {"exchange": "coinbase", "symbol": "BTC/USD"})
    rendered = str(excinfo.value)
    assert SECRET_TOKEN not in rendered
    assert_no_secret(rendered)
