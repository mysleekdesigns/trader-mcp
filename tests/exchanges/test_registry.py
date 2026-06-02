"""Tests for the static exchange registry (no network, no fakes).

Validates the supported-exchange list, ordering (Coinbase first / reference), the
``is_supported`` guard, and ``get_exchange_meta`` typing.
"""

from __future__ import annotations

import pytest

from trader_mcp.exchanges import (
    ExchangeInfo,
    get_exchange_meta,
    is_supported,
    list_supported,
)
from trader_mcp.exchanges.registry import ccxt_id_for

EXPECTED_IDS = ["coinbase", "kraken", "gemini", "cryptocom"]


def test_list_supported_returns_all_four_in_order() -> None:
    infos = list_supported()
    assert [i.exchange for i in infos] == EXPECTED_IDS


def test_list_supported_returns_typed_models() -> None:
    for info in list_supported():
        assert isinstance(info, ExchangeInfo)
        # Frozen Pydantic v2 model -- assignment must fail.
        with pytest.raises(Exception, match=r"frozen|Instance is frozen|validation"):
            info.name = "mutated"  # type: ignore[misc]


def test_coinbase_is_the_reference_certified_exchange() -> None:
    infos = {i.exchange: i for i in list_supported()}
    # Coinbase is the reference exchange and is certified.
    assert infos["coinbase"].is_reference is True
    assert infos["coinbase"].reliability_tier == "certified"
    # Kraken is ALSO certified (both are CCXT-Certified per PRD §10) but not the
    # reference exchange.
    assert infos["kraken"].is_reference is False
    assert infos["kraken"].reliability_tier == "certified"
    # Gemini and Crypto.com are supported (non-reference).
    for other in ("gemini", "cryptocom"):
        assert infos[other].is_reference is False
        assert infos[other].reliability_tier == "supported"


def test_all_exchanges_advertise_websocket() -> None:
    assert all(i.has_websocket for i in list_supported())


@pytest.mark.parametrize("exchange", EXPECTED_IDS)
def test_is_supported_true_for_known(exchange: str) -> None:
    assert is_supported(exchange) is True


@pytest.mark.parametrize("exchange", ["", "binance", "COINBASE", "bybit", "okx"])
def test_is_supported_false_for_unknown(exchange: str) -> None:
    # "bybit" is a now-dropped offshore id; "COINBASE" is the wrong case (ids are
    # lowercase). Both must be rejected.
    assert is_supported(exchange) is False


@pytest.mark.parametrize("exchange", EXPECTED_IDS)
def test_get_exchange_meta_roundtrips(exchange: str) -> None:
    meta = get_exchange_meta(exchange)  # type: ignore[arg-type]
    assert meta.exchange == exchange
    assert meta.ccxt_id == ccxt_id_for(exchange)  # type: ignore[arg-type]


def test_get_exchange_meta_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        get_exchange_meta("nope")  # type: ignore[arg-type]
