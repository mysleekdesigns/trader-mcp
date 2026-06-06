"""Starter strategy template library (PRD §6 Phase 3).

Seven curated templates, each a *valid* :class:`StrategySpec` an AI can start from
and tweak rather than authoring from scratch (the ``/author-strategy`` flow). All
default to Coinbase BTC/USD 1h spot with sensible parameters and conservative risk
limits. Every template passes the spec's own validation (asserted by the tests).

Templates:
    * ``ma_cross``       -- fast/slow EMA crossover (trend following)
    * ``rsi_reversion``  -- RSI mean-reversion (buy oversold / sell overbought)
    * ``donchian_break`` -- Donchian channel breakout
    * ``macd``           -- MACD signal-line crossover
    * ``bollinger``      -- Bollinger Band mean-reversion
    * ``grid``           -- price-grid strategy (non-rule)
    * ``dca``            -- dollar-cost averaging (non-rule)

``build_from_template`` applies a shallow dict of overrides (top-level spec fields)
and re-validates, so an unsafe/invalid override is rejected with the same rich
errors as :func:`trader_mcp.strategy.validation.create_strategy`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from trader_mcp.errors import ValidationError
from trader_mcp.strategy.spec import (
    DCAConfig,
    EntryRules,
    ExitRules,
    Fees,
    GridConfig,
    IndicatorSpec,
    PositionSizing,
    RiskLimits,
    StrategySpec,
)

_EXCHANGE = "coinbase"
_SYMBOL = "BTC/USD"
_TIMEFRAME = "1h"


class TemplateInfo(BaseModel):
    """AI-/server-facing description of a strategy template."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    summary: str
    strategy_type: str
    #: Top-level spec fields a caller may override via ``build_from_template``.
    overridable: list[str]


def _ma_cross() -> StrategySpec:
    return StrategySpec(
        name="ma-cross-btc",
        description=(
            "Go long when the fast EMA crosses above the slow EMA; exit on the reverse cross."
        ),
        exchange=_EXCHANGE,
        symbol=_SYMBOL,
        timeframe=_TIMEFRAME,
        indicators=[
            IndicatorSpec(id="fast", kind="ema", params={"length": 20}),
            IndicatorSpec(id="slow", kind="ema", params={"length": 50}),
        ],
        entry=EntryRules(long="crossover(fast, slow)", short="crossunder(fast, slow)"),
        exit=ExitRules(long="crossunder(fast, slow)", short="crossover(fast, slow)"),
        position_sizing=PositionSizing(mode="percent_equity", value=10.0),
        risk=RiskLimits(stop_loss_pct=3.0, take_profit_pct=6.0),
        fees=Fees(),
    )


def _rsi_reversion() -> StrategySpec:
    return StrategySpec(
        name="rsi-mean-reversion-btc",
        description="Buy when RSI is oversold (<30) and exit when it returns to the midline (>50).",
        exchange=_EXCHANGE,
        symbol=_SYMBOL,
        timeframe=_TIMEFRAME,
        indicators=[IndicatorSpec(id="rsi", kind="rsi", params={"length": 14})],
        entry=EntryRules(long="rsi < 30", short="rsi > 70"),
        exit=ExitRules(long="rsi > 50", short="rsi < 50"),
        position_sizing=PositionSizing(mode="percent_equity", value=5.0),
        risk=RiskLimits(stop_loss_pct=2.0, take_profit_pct=4.0),
        fees=Fees(),
    )


def _donchian_break() -> StrategySpec:
    return StrategySpec(
        name="donchian-breakout-btc",
        description=(
            "Enter long when price breaks above the Donchian upper channel; exit below the middle."
        ),
        exchange=_EXCHANGE,
        symbol=_SYMBOL,
        timeframe=_TIMEFRAME,
        indicators=[IndicatorSpec(id="dc", kind="donchian", params={"length": 20})],
        entry=EntryRules(long="close > dc_upper", short="close < dc_lower"),
        exit=ExitRules(long="close < dc", short="close > dc"),
        position_sizing=PositionSizing(mode="percent_equity", value=10.0),
        risk=RiskLimits(stop_loss_pct=4.0, take_profit_pct=10.0),
        fees=Fees(),
    )


def _macd() -> StrategySpec:
    return StrategySpec(
        name="macd-crossover-btc",
        description=(
            "Go long when the MACD line crosses above its signal line; exit on the reverse cross."
        ),
        exchange=_EXCHANGE,
        symbol=_SYMBOL,
        timeframe=_TIMEFRAME,
        indicators=[
            IndicatorSpec(id="m", kind="macd", params={"fast": 12, "slow": 26, "signal": 9})
        ],
        entry=EntryRules(long="crossover(m, m_signal)", short="crossunder(m, m_signal)"),
        exit=ExitRules(long="crossunder(m, m_signal)", short="crossover(m, m_signal)"),
        position_sizing=PositionSizing(mode="percent_equity", value=10.0),
        risk=RiskLimits(stop_loss_pct=3.0, take_profit_pct=6.0),
        fees=Fees(),
    )


def _bollinger() -> StrategySpec:
    return StrategySpec(
        name="bollinger-reversion-btc",
        description="Buy when price closes below the lower band; exit at the middle band.",
        exchange=_EXCHANGE,
        symbol=_SYMBOL,
        timeframe=_TIMEFRAME,
        indicators=[IndicatorSpec(id="bb", kind="bbands", params={"length": 20, "std": 2.0})],
        entry=EntryRules(long="close < bb_lower", short="close > bb_upper"),
        exit=ExitRules(long="close > bb", short="close < bb"),
        position_sizing=PositionSizing(mode="percent_equity", value=5.0),
        risk=RiskLimits(stop_loss_pct=2.5, take_profit_pct=5.0),
        fees=Fees(),
    )


def _grid() -> StrategySpec:
    return StrategySpec(
        name="grid-btc",
        description="A price-grid bot: staggered buy/sell limit orders across a band.",
        exchange=_EXCHANGE,
        symbol=_SYMBOL,
        timeframe=_TIMEFRAME,
        strategy_type="grid",
        grid=GridConfig(lower=40_000.0, upper=80_000.0, levels=20, allocation_pct=50.0),
        position_sizing=PositionSizing(mode="percent_equity", value=50.0),
        risk=RiskLimits(),
        fees=Fees(),
    )


def _dca() -> StrategySpec:
    return StrategySpec(
        name="dca-btc",
        description="Dollar-cost average: buy a fixed USD amount of BTC every 24 bars.",
        exchange=_EXCHANGE,
        symbol=_SYMBOL,
        timeframe=_TIMEFRAME,
        strategy_type="dca",
        dca=DCAConfig(amount_quote=100.0, interval_bars=24, max_purchases=None),
        position_sizing=PositionSizing(mode="fixed_quote", value=100.0),
        risk=RiskLimits(),
        fees=Fees(),
    )


#: Registry of template builders, keyed by template id. Each builder returns a
#: fresh, fully-valid :class:`StrategySpec`.
_TEMPLATES: dict[str, tuple[str, str, Callable[[], StrategySpec]]] = {
    "ma_cross": ("MA crossover", "Fast/slow EMA crossover trend following.", _ma_cross),
    "rsi_reversion": (
        "RSI mean-reversion",
        "Buy oversold RSI, exit at the midline.",
        _rsi_reversion,
    ),
    "donchian_break": (
        "Donchian breakout",
        "Enter on a break of the Donchian channel.",
        _donchian_break,
    ),
    "macd": ("MACD crossover", "MACD line crossing its signal line.", _macd),
    "bollinger": (
        "Bollinger mean-reversion",
        "Fade moves outside the Bollinger Bands.",
        _bollinger,
    ),
    "grid": ("Grid", "Staggered limit orders across a price band.", _grid),
    "dca": ("DCA", "Periodic fixed-amount accumulation.", _dca),
}

#: Top-level spec fields a caller may override when building from a template. This
#: is BOTH the advertised set (``TemplateInfo.overridable``) and the accepted set
#: (the unknown-override guard), so the two never drift. ``schema_version`` is
#: deliberately excluded -- it is owned by the model/store, not a caller override.
_OVERRIDABLE: list[str] = [
    "name",
    "description",
    "exchange",
    "symbol",
    "timeframe",
    "strategy_type",
    "indicators",
    "entry",
    "exit",
    "position_sizing",
    "risk",
    "fees",
    "grid",
    "dca",
]


def list_templates() -> list[TemplateInfo]:
    """Return metadata for every template (id, title, summary, overridable fields)."""
    return [
        TemplateInfo(
            id=tid,
            title=title,
            summary=summary,
            strategy_type=builder().strategy_type,
            overridable=list(_OVERRIDABLE),
        )
        for tid, (title, summary, builder) in _TEMPLATES.items()
    ]


def build_from_template(template_id: str, overrides: dict[str, Any] | None = None) -> StrategySpec:
    """Build a :class:`StrategySpec` from ``template_id``, applying ``overrides``.

    Overrides are a shallow merge over the template's top-level spec fields, then
    the result is re-validated (so an unsafe rule or out-of-range value in an
    override is rejected with the spec's normal errors).

    Args:
        template_id: One of :func:`list_templates`'s ids.
        overrides: Optional top-level field overrides.

    Returns:
        A validated :class:`StrategySpec`.

    Raises:
        trader_mcp.errors.ValidationError: for an unknown ``template_id`` (lists the
            valid ids) or if applying ``overrides`` produces an invalid spec.
    """
    entry = _TEMPLATES.get(template_id)
    if entry is None:
        raise ValidationError(
            f"Unknown template id {template_id!r}. "
            f"Valid templates: {', '.join(sorted(_TEMPLATES))}.",
            details={
                "kind": "unknown_template",
                "value": template_id,
                "valid": sorted(_TEMPLATES),
            },
        )
    spec = entry[2]()
    if not overrides:
        return spec

    unknown = set(overrides) - set(_OVERRIDABLE)
    if unknown:
        raise ValidationError(
            f"Unknown override field(s) {sorted(unknown)}. Overridable: {', '.join(_OVERRIDABLE)}.",
            details={"kind": "unknown_override", "fields": sorted(unknown)},
        )

    merged = spec.model_dump()
    merged.update(overrides)
    # Re-validate via the rich path so overrides get the same structured errors.
    from trader_mcp.strategy.validation import create_strategy

    return create_strategy(merged)
