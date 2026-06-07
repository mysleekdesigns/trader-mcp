"""Risk-limit model + pure check-function tests.

Proves: each limit is opt-in (None disables it); each breached limit yields exactly
one violation; multiple breaches accumulate; boundary equality is allowed and only
a strictly-greater value breaches.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trader_mcp.safety.risk import (
    OrderRiskContext,
    RiskCheck,
    RiskLimits,
    check_order_risk,
)


def _ctx(**overrides: object) -> OrderRiskContext:
    kwargs: dict[str, object] = {
        "order_notional": 100.0,
        "leverage": 1.0,
        "open_positions": 0,
        "resulting_position_notional": 100.0,
        "realized_loss_today": 0.0,
        "opens_new_position": False,
    }
    kwargs.update(overrides)
    return OrderRiskContext(**kwargs)  # type: ignore[arg-type]


def test_empty_limits_disable_everything() -> None:
    result = check_order_risk(
        _ctx(
            order_notional=1e9,
            resulting_position_notional=1e9,
            leverage=1000.0,
            realized_loss_today=1e9,
            open_positions=1000,
            opens_new_position=True,
        ),
        RiskLimits(),
    )
    assert result.ok is True
    assert result.passed is True
    assert result.violations == []


def test_max_order_notional_breach_one_violation() -> None:
    result = check_order_risk(_ctx(order_notional=101.0), RiskLimits(max_order_notional=100.0))
    assert result.ok is False
    assert len(result.violations) == 1
    assert "max_order_notional" in result.violations[0]


def test_order_notional_equal_to_limit_allowed() -> None:
    # == limit is allowed; only > breaches.
    result = check_order_risk(_ctx(order_notional=100.0), RiskLimits(max_order_notional=100.0))
    assert result.passed is True


def test_position_notional_breach() -> None:
    result = check_order_risk(
        _ctx(resulting_position_notional=250.0),
        RiskLimits(max_position_notional=200.0),
    )
    assert result.ok is False
    assert len(result.violations) == 1
    assert "max_position_notional" in result.violations[0]


def test_max_open_positions_counts_new_position() -> None:
    # 2 open + opening a new one = 3 projected, cap is 2 -> breach.
    result = check_order_risk(
        _ctx(open_positions=2, opens_new_position=True),
        RiskLimits(max_open_positions=2),
    )
    assert result.ok is False
    assert "max_open_positions" in result.violations[0]


def test_max_open_positions_not_breached_when_not_opening() -> None:
    # Adding to an existing position does not increment the count.
    result = check_order_risk(
        _ctx(open_positions=2, opens_new_position=False),
        RiskLimits(max_open_positions=2),
    )
    assert result.passed is True


def test_max_daily_loss_breach() -> None:
    result = check_order_risk(
        _ctx(realized_loss_today=500.0),
        RiskLimits(max_daily_loss=400.0),
    )
    assert result.ok is False
    assert "max_daily_loss" in result.violations[0]


def test_max_leverage_breach() -> None:
    result = check_order_risk(_ctx(leverage=10.0), RiskLimits(max_leverage=5.0))
    assert result.ok is False
    assert "max_leverage" in result.violations[0]


def test_leverage_unknown_skips_check() -> None:
    result = check_order_risk(_ctx(leverage=None), RiskLimits(max_leverage=1.0))
    assert result.passed is True


def test_multiple_breaches_accumulate() -> None:
    result = check_order_risk(
        _ctx(
            order_notional=1000.0,
            resulting_position_notional=1000.0,
            leverage=50.0,
            realized_loss_today=1000.0,
            open_positions=10,
            opens_new_position=True,
        ),
        RiskLimits(
            max_order_notional=100.0,
            max_position_notional=100.0,
            max_leverage=5.0,
            max_daily_loss=100.0,
            max_open_positions=2,
        ),
    )
    assert result.ok is False
    assert len(result.violations) == 5


def test_risk_check_is_frozen() -> None:
    result = RiskCheck(ok=True, violations=[])
    with pytest.raises(ValidationError):
        result.ok = False  # type: ignore[misc]


def test_negative_limit_rejected() -> None:
    with pytest.raises(ValidationError):
        RiskLimits(max_order_notional=-1.0)
    with pytest.raises(ValidationError):
        RiskLimits(max_open_positions=-1)


def test_limits_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        RiskLimits(unknown_field=1)  # type: ignore[call-arg]
