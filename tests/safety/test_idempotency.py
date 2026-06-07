"""Tests for deterministic client order ids and the dedupe registry.

Proves COIDs are deterministic (same inputs -> same id, with no time/randomness),
distinct inputs diverge, and the registry dedupes retried submissions.
"""

from __future__ import annotations

import pytest

from trader_mcp.errors import ValidationError
from trader_mcp.safety import (
    COID_PREFIX,
    IdempotencyRegistry,
    make_client_order_id,
)


def test_same_inputs_produce_same_id() -> None:
    a = make_client_order_id(session_id="s1", symbol="BTC/USD", side="buy", seq=3)
    b = make_client_order_id(session_id="s1", symbol="BTC/USD", side="buy", seq=3)
    assert a == b


def test_id_has_stable_prefix() -> None:
    coid = make_client_order_id(session_id="s1", symbol="BTC/USD", side="buy", seq=0)
    assert coid.startswith(f"{COID_PREFIX}-")


def test_different_seq_diverges() -> None:
    a = make_client_order_id(session_id="s1", symbol="BTC/USD", side="buy", seq=1)
    b = make_client_order_id(session_id="s1", symbol="BTC/USD", side="buy", seq=2)
    assert a != b


def test_different_side_diverges() -> None:
    a = make_client_order_id(session_id="s1", symbol="BTC/USD", side="buy", seq=1)
    b = make_client_order_id(session_id="s1", symbol="BTC/USD", side="sell", seq=1)
    assert a != b


def test_different_session_or_symbol_diverges() -> None:
    base = make_client_order_id(session_id="s1", symbol="BTC/USD", side="buy", seq=1)
    other_session = make_client_order_id(session_id="s2", symbol="BTC/USD", side="buy", seq=1)
    other_symbol = make_client_order_id(session_id="s1", symbol="ETH/USD", side="buy", seq=1)
    assert base != other_session
    assert base != other_symbol


def test_field_boundaries_are_unambiguous() -> None:
    # NUL-separated payload means concatenation collisions cannot happen.
    a = make_client_order_id(session_id="ab", symbol="c", side="buy", seq=0)
    b = make_client_order_id(session_id="a", symbol="bc", side="buy", seq=0)
    assert a != b


def test_side_is_normalized() -> None:
    a = make_client_order_id(session_id="s1", symbol="BTC/USD", side="BUY", seq=0)
    b = make_client_order_id(session_id="s1", symbol="BTC/USD", side="buy", seq=0)
    assert a == b


def test_none_seq_equals_zero() -> None:
    a = make_client_order_id(session_id="s1", symbol="BTC/USD", side="buy")
    b = make_client_order_id(session_id="s1", symbol="BTC/USD", side="buy", seq=0)
    assert a == b


def test_empty_session_raises() -> None:
    with pytest.raises(ValidationError):
        make_client_order_id(session_id="  ", symbol="BTC/USD", side="buy")


def test_empty_symbol_raises() -> None:
    with pytest.raises(ValidationError):
        make_client_order_id(session_id="s1", symbol="", side="buy")


def test_bad_side_raises() -> None:
    with pytest.raises(ValidationError):
        make_client_order_id(session_id="s1", symbol="BTC/USD", side="long")


def test_negative_seq_raises() -> None:
    with pytest.raises(ValidationError):
        make_client_order_id(session_id="s1", symbol="BTC/USD", side="buy", seq=-1)


def test_registry_dedupes() -> None:
    reg = IdempotencyRegistry()
    coid = make_client_order_id(session_id="s1", symbol="BTC/USD", side="buy", seq=1)
    assert not reg.seen(coid)
    assert reg.record(coid) is True  # first submission accepted
    assert reg.seen(coid)
    assert reg.record(coid) is False  # retry is a dedupe
    assert len(reg) == 1


def test_registry_clear() -> None:
    reg = IdempotencyRegistry()
    reg.record("tmcp-abc")
    reg.clear()
    assert len(reg) == 0
    assert not reg.seen("tmcp-abc")
