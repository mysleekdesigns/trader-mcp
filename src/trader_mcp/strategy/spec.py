"""Versioned, typed Pydantic v2 strategy spec (PRD §5.3).

A strategy is **declarative data, never code**. This module models that data: a
:class:`StrategySpec` carries the instrument/timeframe, the indicators it computes,
the entry/exit rule expressions, position sizing, risk limits, and fees. Rule
expressions are plain strings that are statically validated against the safe
evaluator's whitelist at construction time -- so an invalid or unsafe spec can
never be built.

The spec is **execution-agnostic**: the very same object is consumed by the
backtest interpreter and the live runtime (the one-interpreter invariant). Nothing
here knows about bars, fills, or order routing.

Grid & DCA strategies do not fit the pure entry/exit-rule shape, so the spec uses
a ``strategy_type`` discriminator:

    * ``"rule"`` (default) -- entry/exit rule expressions drive the strategy; the
      ``grid``/``dca`` blocks must be absent.
    * ``"grid"`` -- a price-grid strategy; the ``grid`` block is required and the
      entry/exit rules are optional (the grid logic, not rules, generates orders).
    * ``"dca"``  -- dollar-cost-averaging on a fixed cadence; the ``dca`` block is
      required and the entry/exit rules are optional.

This keeps everything one typed, JSON-schema-able contract while letting the Phase
4 interpreter branch on ``strategy_type`` for non-rule strategies.

All produced models are frozen and ``extra="forbid"`` (mirroring
:class:`trader_mcp.data.models._DataModel`) so malformed input fails loudly.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trader_mcp.config import ExchangeId
from trader_mcp.data.timeframes import SUPPORTED_TIMEFRAMES, is_supported_timeframe
from trader_mcp.errors import ValidationError
from trader_mcp.indicators.registry import (
    INDICATOR_REGISTRY,
    IndicatorKind,
    allowed_names_for,
)
from trader_mcp.strategy.evaluator import validate_expression

#: Spec schema version. Bump on any breaking change to the shape below; the store
#: persists this so old specs can be detected/migrated.
SCHEMA_VERSION = "1.0"

#: How a strategy is structured (the spec discriminator -- see module docstring).
StrategyType = Literal["rule", "grid", "dca"]


class _SpecModel(BaseModel):
    """Base for strategy-spec models: frozen and strict on declared fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class IndicatorSpec(_SpecModel):
    """One indicator instance the strategy computes.

    ``id`` is the unique handle a rule references (and the base of this
    indicator's context column names -- see
    :func:`trader_mcp.indicators.registry.output_names_for`). ``kind`` selects a
    whitelisted indicator; ``params`` overrides the kind's default params (any
    omitted param uses the registry default). ``params`` keys are validated against
    the kind's known params.
    """

    id: str = Field(min_length=1)
    kind: IndicatorKind
    params: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_id_and_params(self) -> IndicatorSpec:
        if not self.id.isidentifier() or self.id.startswith("__"):
            raise ValueError(
                f"indicator id {self.id!r} must be a valid identifier "
                "(letters/digits/underscore, not starting with a digit or '__')"
            )
        defn = INDICATOR_REGISTRY[self.kind]  # kind already constrained by Literal
        known = {p.name for p in defn.params}
        unknown = set(self.params) - known
        if unknown:
            raise ValueError(
                f"unknown param(s) {sorted(unknown)} for indicator kind {self.kind!r}; "
                f"valid params: {sorted(known)}"
            )
        for name, value in self.params.items():
            param = next(p for p in defn.params if p.name == name)
            if param.is_int and (float(value) <= 0 or float(value) != int(value)):
                raise ValueError(
                    f"param {name!r} of {self.kind!r} must be a positive integer, got {value!r}"
                )
            if not param.is_int and float(value) <= 0:
                raise ValueError(f"param {name!r} of {self.kind!r} must be positive, got {value!r}")
        return self

    def resolved_params(self) -> dict[str, float]:
        """Return the kind's default params merged with this spec's overrides."""
        defn = INDICATOR_REGISTRY[self.kind]
        params: dict[str, float] = {p.name: float(p.default) for p in defn.params}
        params.update({k: float(v) for k, v in self.params.items()})
        return params


class EntryRules(_SpecModel):
    """Entry rule expressions. ``None`` means "no entry on this side"."""

    long: str | None = None
    short: str | None = None


class ExitRules(_SpecModel):
    """Exit rule expressions. ``None`` means "no rule-based exit on this side"."""

    long: str | None = None
    short: str | None = None


class PositionSizing(_SpecModel):
    """How much to allocate per position.

    * ``percent_equity`` -- ``value`` percent of account equity (0 < value <= 100).
    * ``fixed_quote``    -- ``value`` units of the quote currency (e.g. USD).
    * ``fixed_base``     -- ``value`` units of the base currency (e.g. BTC).
    """

    mode: Literal["percent_equity", "fixed_quote", "fixed_base"] = "percent_equity"
    value: float = Field(gt=0)

    @model_validator(mode="after")
    def _check_percent(self) -> PositionSizing:
        if self.mode == "percent_equity" and self.value > 100:
            raise ValueError("percent_equity sizing value must be <= 100")
        return self


class RiskLimits(_SpecModel):
    """Per-strategy risk parameters (all optional; coordinated with safety layer).

    ``stop_loss_pct`` / ``take_profit_pct`` are percentages of entry price (> 0).
    ``max_leverage`` applies to US-legal perps only; spot strategies leave it
    ``None`` (or 1). These are the spec-side declarations; the safety engine
    enforces account-level limits separately.
    """

    stop_loss_pct: float | None = Field(default=None, gt=0, le=100)
    take_profit_pct: float | None = Field(default=None, gt=0)
    max_leverage: float | None = Field(default=None, ge=1, le=125)


class Fees(_SpecModel):
    """Taker/maker fee fractions (e.g. 0.0006 == 0.06%)."""

    taker: float = Field(default=0.0006, ge=0, le=1)
    maker: float = Field(default=0.0002, ge=0, le=1)


class GridConfig(_SpecModel):
    """A price-grid strategy: evenly spaced buy/sell levels in a band.

    ``lower``/``upper`` bound the grid (quote price); ``levels`` is the number of
    grid lines (>= 2); ``allocation_pct`` is the share of equity the whole grid
    uses. The interpreter places staggered limit orders across the band.
    """

    lower: float = Field(gt=0)
    upper: float = Field(gt=0)
    levels: int = Field(ge=2, le=200)
    allocation_pct: float = Field(gt=0, le=100, default=50.0)

    @model_validator(mode="after")
    def _check_bounds(self) -> GridConfig:
        if self.upper <= self.lower:
            raise ValueError("grid 'upper' must be greater than 'lower'")
        return self


class DCAConfig(_SpecModel):
    """A dollar-cost-averaging strategy: buy a fixed amount every N bars.

    ``amount_quote`` is the quote-currency spend per purchase; ``interval_bars`` is
    the cadence (>= 1 bar). ``max_purchases`` optionally caps total buys.
    """

    amount_quote: float = Field(gt=0)
    interval_bars: int = Field(ge=1)
    max_purchases: int | None = Field(default=None, ge=1)


class StrategySpec(_SpecModel):
    """A complete, validated declarative strategy (the single shared contract).

    Built once and reused unchanged by backtest and live. Construction runs full
    cross-field validation: the timeframe is supported, indicator ids are unique,
    every name referenced in any entry/exit expression resolves to an OHLCV column
    or a defined indicator output, each expression passes the safe-evaluator's
    static whitelist check, and the correct discriminated block is present for the
    ``strategy_type``.
    """

    schema_version: str = SCHEMA_VERSION
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    exchange: ExchangeId = "coinbase"
    symbol: str = Field(min_length=1)
    timeframe: str = "1h"
    strategy_type: StrategyType = "rule"

    indicators: list[IndicatorSpec] = Field(default_factory=list)
    entry: EntryRules = Field(default_factory=EntryRules)
    exit: ExitRules = Field(default_factory=ExitRules)
    position_sizing: PositionSizing = Field(
        default_factory=lambda: PositionSizing(mode="percent_equity", value=5.0)
    )
    risk: RiskLimits = Field(default_factory=RiskLimits)
    fees: Fees = Field(default_factory=Fees)

    grid: GridConfig | None = None
    dca: DCAConfig | None = None

    @model_validator(mode="after")
    def _validate_cross_fields(self) -> StrategySpec:
        # Timeframe must be on the supported whitelist.
        if not is_supported_timeframe(self.timeframe):
            raise ValueError(
                f"unsupported timeframe {self.timeframe!r}; "
                f"supported: {', '.join(SUPPORTED_TIMEFRAMES)}"
            )

        # Indicator ids must be unique.
        ids = [ind.id for ind in self.indicators]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate indicator id(s): {dupes}; ids must be unique")

        # Discriminator: the right block present, rules required only for "rule".
        self._validate_type_blocks()

        # Every rule expression: safe + every referenced name resolves.
        allowed = allowed_names_for(self.indicators)
        for side, expr in self._rule_expressions():
            try:
                validate_expression(expr, allowed)
            except ValidationError as exc:
                raise ValueError(f"{side}: {exc.message}") from None
        return self

    def _validate_type_blocks(self) -> None:
        if self.strategy_type == "grid":
            if self.grid is None:
                raise ValueError("strategy_type 'grid' requires a 'grid' block")
            if self.dca is not None:
                raise ValueError("strategy_type 'grid' must not include a 'dca' block")
        elif self.strategy_type == "dca":
            if self.dca is None:
                raise ValueError("strategy_type 'dca' requires a 'dca' block")
            if self.grid is not None:
                raise ValueError("strategy_type 'dca' must not include a 'grid' block")
        else:  # "rule"
            if self.grid is not None or self.dca is not None:
                raise ValueError("strategy_type 'rule' must not include a 'grid'/'dca' block")
            if not any(expr for _, expr in self._rule_expressions()):
                raise ValueError("strategy_type 'rule' requires at least one entry/exit rule")

    def _rule_expressions(self) -> list[tuple[str, str]]:
        """Return ``(location, expression)`` for every non-null rule string."""
        pairs: list[tuple[str, str]] = []
        for side, value in (
            ("entry.long", self.entry.long),
            ("entry.short", self.entry.short),
            ("exit.long", self.exit.long),
            ("exit.short", self.exit.short),
        ):
            if value is not None:
                pairs.append((side, value))
        return pairs


#: Annotated alias used by tools that accept a spec as a payload.
StrategySpecModel = Annotated[StrategySpec, Field(description="A declarative strategy spec")]


def spec_json_schema() -> dict[str, Any]:
    """Return the JSON schema for :class:`StrategySpec` (for docs / MCP outputSchema)."""
    return StrategySpec.model_json_schema()
