"""Per-strategy risk-limit model and the pure risk-check function (Phase 6).

INVARIANT (safety): risk limits are enforced server-side, never advisory. The
:class:`SafetyController` consults :func:`check_order_risk` on every order intent
*before* the order ever reaches the gate, and fails closed (deny) on any breach.

This module is pure data + a pure function: no I/O, no clocks, no globals. Every
limit is opt-in -- ``None`` means "unlimited / disabled". A limit is breached only
when the corresponding context value is *strictly greater* than the limit, so an
order whose value exactly equals the cap is allowed (``== limit`` is fine,
``> limit`` breaches). All amounts are in the quote currency.

Self-contained: imports only stdlib + pydantic. No execution/exchanges/server.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RiskLimits(BaseModel):
    """Per-strategy/global risk caps. Immutable.

    Every field defaults to ``None`` (the cap is disabled). A configured cap is a
    non-negative number; values exactly equal to a cap are allowed and only a
    strictly greater context value breaches it.

    Attributes:
        max_order_notional: Cap on a single order's notional (quote currency).
        max_position_notional: Cap on an open position's absolute notional.
        max_open_positions: Cap on the number of concurrent open positions.
        max_daily_loss: Cap on realized loss magnitude per UTC day (positive).
        max_leverage: Cap on order/position leverage.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_order_notional: float | None = Field(default=None)
    max_position_notional: float | None = Field(default=None)
    max_open_positions: int | None = Field(default=None)
    max_daily_loss: float | None = Field(default=None)
    max_leverage: float | None = Field(default=None)

    @field_validator(
        "max_order_notional",
        "max_position_notional",
        "max_daily_loss",
        "max_leverage",
    )
    @classmethod
    def _non_negative_float(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("risk limit must be >= 0 (None disables the limit)")
        return value

    @field_validator("max_open_positions")
    @classmethod
    def _non_negative_int(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("max_open_positions must be >= 0 (None disables the limit)")
        return value


class OrderRiskContext(BaseModel):
    """The measured/estimated state an order would produce, fed to the check.

    Immutable. All notionals are absolute, quote-currency magnitudes.

    Attributes:
        order_notional: Notional of the order being placed.
        leverage: Order/position leverage, if known.
        open_positions: Count of open positions BEFORE this order.
        resulting_position_notional: Abs notional of the symbol's position AFTER
            this order (best estimate).
        realized_loss_today: Magnitude of realized loss so far this UTC day (>= 0).
        opens_new_position: True if this order would open a position not already
            counted in ``open_positions``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    order_notional: float = Field(ge=0)
    leverage: float | None = Field(default=None)
    open_positions: int = Field(default=0, ge=0)
    resulting_position_notional: float = Field(default=0.0, ge=0)
    realized_loss_today: float = Field(default=0.0, ge=0)
    opens_new_position: bool = Field(default=False)


class RiskCheck(BaseModel):
    """Outcome of :func:`check_order_risk`. Immutable.

    Attributes:
        ok: True when no configured limit is breached.
        violations: One secret-free, human-readable reason per breached limit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    violations: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Alias for :attr:`ok` -- True when the order may proceed."""
        return self.ok


def check_order_risk(ctx: OrderRiskContext, limits: RiskLimits) -> RiskCheck:
    """Evaluate an order intent against the configured risk limits.

    Pure function. Each ``None`` limit is disabled and skipped. Each configured
    limit that is *strictly* exceeded by the corresponding context value adds
    exactly one violation string. The order passes (``ok=True``) iff there are no
    violations.

    Args:
        ctx: The measured/estimated effect of the order.
        limits: The active risk caps.

    Returns:
        A frozen :class:`RiskCheck`. Never raises for a breach -- it reports.
    """
    violations: list[str] = []

    if limits.max_order_notional is not None and ctx.order_notional > limits.max_order_notional:
        violations.append(
            f"order notional {ctx.order_notional} exceeds max_order_notional "
            f"{limits.max_order_notional}"
        )

    if (
        limits.max_position_notional is not None
        and ctx.resulting_position_notional > limits.max_position_notional
    ):
        violations.append(
            f"resulting position notional {ctx.resulting_position_notional} exceeds "
            f"max_position_notional {limits.max_position_notional}"
        )

    if limits.max_open_positions is not None:
        # An order opening a new position adds one to the pre-order count.
        projected = ctx.open_positions + (1 if ctx.opens_new_position else 0)
        if projected > limits.max_open_positions:
            violations.append(
                f"open positions {projected} would exceed max_open_positions "
                f"{limits.max_open_positions}"
            )

    if limits.max_daily_loss is not None and ctx.realized_loss_today > limits.max_daily_loss:
        violations.append(
            f"realized loss today {ctx.realized_loss_today} exceeds max_daily_loss "
            f"{limits.max_daily_loss}"
        )

    if (
        limits.max_leverage is not None
        and ctx.leverage is not None
        and ctx.leverage > limits.max_leverage
    ):
        violations.append(f"leverage {ctx.leverage} exceeds max_leverage {limits.max_leverage}")

    return RiskCheck(ok=not violations, violations=violations)
