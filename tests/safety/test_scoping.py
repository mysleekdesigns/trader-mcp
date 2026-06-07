"""Tests for key-scope and jurisdiction enforcement.

Proves a read-only credential is structurally refused at the trade boundary and
that the jurisdiction check flags non-eligible venues/markets with a typed,
secret-free error.
"""

from __future__ import annotations

import pytest

from trader_mcp.config import KeyScope
from trader_mcp.errors import SafetyError
from trader_mcp.safety import (
    JURISDICTION_DISCLAIMER,
    US_ELIGIBLE_EXCHANGES,
    check_jurisdiction,
    require_trade_scope,
)


def test_require_trade_scope_accepts_trade_enabled() -> None:
    # Should not raise.
    require_trade_scope(KeyScope.TRADE_ENABLED)


def test_require_trade_scope_refuses_read_only() -> None:
    with pytest.raises(SafetyError) as excinfo:
        require_trade_scope(KeyScope.READ_ONLY)
    assert "read-only" in excinfo.value.message
    assert excinfo.value.code == "safety_error"


def test_require_trade_scope_error_carries_no_secret() -> None:
    with pytest.raises(SafetyError) as excinfo:
        require_trade_scope(KeyScope.READ_ONLY)
    # The error references only the enum, never any key material.
    assert excinfo.value.details == {
        "key_scope": "read_only",
        "required": "trade_enabled",
    }


@pytest.mark.parametrize("exchange", sorted(US_ELIGIBLE_EXCHANGES))
def test_jurisdiction_spot_allowed_on_all_eligible(exchange: str) -> None:
    disclaimer = check_jurisdiction(exchange, "spot")
    assert disclaimer == JURISDICTION_DISCLAIMER


def test_jurisdiction_case_insensitive() -> None:
    assert check_jurisdiction("CoinBase", "spot") == JURISDICTION_DISCLAIMER


@pytest.mark.parametrize("exchange", ["bybit", "blofin", "toobit", "weex", "binance"])
def test_jurisdiction_rejects_offshore_venue(exchange: str) -> None:
    with pytest.raises(SafetyError) as excinfo:
        check_jurisdiction(exchange, "spot")
    assert "not US-eligible" in excinfo.value.message


@pytest.mark.parametrize("exchange", ["coinbase", "kraken"])
def test_jurisdiction_allows_perps_on_cftc_venues(exchange: str) -> None:
    assert check_jurisdiction(exchange, "swap") == JURISDICTION_DISCLAIMER


@pytest.mark.parametrize("exchange", ["gemini", "cryptocom"])
def test_jurisdiction_rejects_perps_on_non_cftc_venues(exchange: str) -> None:
    with pytest.raises(SafetyError) as excinfo:
        check_jurisdiction(exchange, "swap")
    assert "perp" in excinfo.value.message.lower()
