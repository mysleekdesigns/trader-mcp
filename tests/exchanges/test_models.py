"""Tests for the exchange domain models' Pydantic v2 contract.

Asserts the frozen / ``extra="ignore"`` config holds and that ``ms_to_datetime``
produces tz-aware UTC datetimes (the conversion the adapter relies on).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trader_mcp.exchanges import Market, OHLCVBar, Ticker
from trader_mcp.exchanges.models import ms_to_datetime

FROZEN_MODELS = (Market, Ticker, OHLCVBar)


def test_ms_to_datetime_is_tz_aware_utc() -> None:
    dt = ms_to_datetime(1_704_164_645_000)
    assert dt is not None
    assert dt.tzinfo is UTC
    assert dt == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_ms_to_datetime_none_passthrough() -> None:
    assert ms_to_datetime(None) is None


def test_models_are_frozen() -> None:
    market = Market(
        exchange="bybit",
        symbol="BTC/USDT",
        base="BTC",
        quote="USDT",
        type="spot",
    )
    with pytest.raises(Exception, match=r"frozen|Instance is frozen"):
        market.symbol = "ETH/USDT"  # type: ignore[misc]


def test_models_ignore_extra_fields() -> None:
    """``extra="ignore"`` -- a richer-than-expected CCXT payload must not raise."""
    market = Market.model_validate(
        {
            "exchange": "bybit",
            "symbol": "BTC/USDT",
            "base": "BTC",
            "quote": "USDT",
            "type": "spot",
            "some_future_ccxt_field": 123,
        }
    )
    assert market.symbol == "BTC/USDT"
    assert not hasattr(market, "some_future_ccxt_field")


def test_optional_fields_degrade_to_none() -> None:
    """Lenient models: a partial payload degrades missing fields to None."""
    ticker = Ticker(exchange="bybit", symbol="BTC/USDT")
    assert ticker.last is None
    assert ticker.timestamp is None
