"""Arming state-machine tests.

Proves: wrong confirmation fails closed; correct confirmation grants an active,
expiring ticket; expiry prunes and de-arms; TTL is clamped to the max and a
non-positive TTL is rejected; the global scope arms every exchange; disarm(None)
clears all; per-exchange disarm is surgical.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trader_mcp.errors import SafetyError
from trader_mcp.safety.arming import (
    DEFAULT_ARM_TTL_SECONDS,
    GLOBAL_SCOPE,
    MAX_ARM_TTL_SECONDS,
    REQUIRED_CONFIRMATION,
    ArmingRegistry,
)

T0 = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)


def test_wrong_confirmation_raises() -> None:
    reg = ArmingRegistry()
    with pytest.raises(SafetyError):
        reg.arm(exchange="coinbase", confirm="i understand the risks", now=T0)
    assert reg.is_armed("coinbase", T0) is False


def test_correct_confirmation_grants_active_ticket() -> None:
    reg = ArmingRegistry()
    ticket = reg.arm(exchange="Coinbase", confirm=REQUIRED_CONFIRMATION, now=T0)
    assert ticket.exchange == "coinbase"  # normalized
    assert ticket.is_active(T0) is True
    assert reg.is_armed("coinbase", T0) is True
    assert ticket.seconds_remaining(T0) == pytest.approx(DEFAULT_ARM_TTL_SECONDS)


def test_expiry_deactivates_and_prunes() -> None:
    reg = ArmingRegistry()
    reg.arm(exchange="coinbase", confirm=REQUIRED_CONFIRMATION, now=T0, ttl_seconds=60)
    after = T0 + timedelta(seconds=61)
    assert reg.is_armed("coinbase", after) is False
    assert reg.active_tickets(after) == []


def test_ttl_clamped_to_max() -> None:
    reg = ArmingRegistry()
    ticket = reg.arm(
        exchange="coinbase",
        confirm=REQUIRED_CONFIRMATION,
        now=T0,
        ttl_seconds=MAX_ARM_TTL_SECONDS * 10,
    )
    assert ticket.seconds_remaining(T0) == pytest.approx(MAX_ARM_TTL_SECONDS)


def test_non_positive_ttl_raises() -> None:
    reg = ArmingRegistry()
    with pytest.raises(SafetyError):
        reg.arm(exchange="coinbase", confirm=REQUIRED_CONFIRMATION, now=T0, ttl_seconds=0)
    with pytest.raises(SafetyError):
        reg.arm(exchange="coinbase", confirm=REQUIRED_CONFIRMATION, now=T0, ttl_seconds=-5)


def test_global_scope_arms_all_exchanges() -> None:
    reg = ArmingRegistry()
    reg.arm(exchange=GLOBAL_SCOPE, confirm=REQUIRED_CONFIRMATION, now=T0)
    assert reg.is_armed("coinbase", T0) is True
    assert reg.is_armed("kraken", T0) is True


def test_disarm_none_clears_all() -> None:
    reg = ArmingRegistry()
    reg.arm(exchange="coinbase", confirm=REQUIRED_CONFIRMATION, now=T0)
    reg.arm(exchange="kraken", confirm=REQUIRED_CONFIRMATION, now=T0)
    reg.disarm(None)
    assert reg.active_tickets(T0) == []


def test_per_exchange_disarm_is_surgical() -> None:
    reg = ArmingRegistry()
    reg.arm(exchange="coinbase", confirm=REQUIRED_CONFIRMATION, now=T0)
    reg.arm(exchange="kraken", confirm=REQUIRED_CONFIRMATION, now=T0)
    reg.disarm("coinbase")
    assert reg.is_armed("coinbase", T0) is False
    assert reg.is_armed("kraken", T0) is True


def test_disarm_unarmed_is_noop() -> None:
    reg = ArmingRegistry()
    reg.disarm("coinbase")  # must not raise
    assert reg.active_tickets(T0) == []


def test_empty_exchange_rejected() -> None:
    reg = ArmingRegistry()
    with pytest.raises(SafetyError):
        reg.arm(exchange="   ", confirm=REQUIRED_CONFIRMATION, now=T0)


def test_seconds_remaining_zero_after_expiry() -> None:
    reg = ArmingRegistry()
    ticket = reg.arm(exchange="coinbase", confirm=REQUIRED_CONFIRMATION, now=T0, ttl_seconds=30)
    assert ticket.seconds_remaining(T0 + timedelta(seconds=100)) == 0.0
