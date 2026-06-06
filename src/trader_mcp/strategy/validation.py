"""Rich, AI-friendly strategy validation (PRD §6 Phase 3 "what's wrong + how to fix").

Pydantic's raw ``ValidationError`` is precise but terse; an AI author benefits
from a structured "this field is wrong, here is how to fix it" report. This module
turns every failure -- Pydantic field errors AND the spec's cross-field/whitelist
errors -- into a typed :class:`ValidationReport` of :class:`ValidationIssue` items.

Two entry points the MCP server wraps:
    * :func:`validate_strategy` -- never raises; returns a report (``ok`` +
      ``issues``). Use for the ``validate_strategy`` tool.
    * :func:`create_strategy` -- validates and returns the :class:`StrategySpec`,
      or raises :class:`trader_mcp.errors.ValidationError` whose ``details`` carry
      the same structured issues. Use for the ``create_strategy`` tool.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from trader_mcp.errors import ValidationError
from trader_mcp.strategy.spec import StrategySpec


class ValidationIssue(BaseModel):
    """One actionable validation problem: where it is, what's wrong, how to fix."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    location: str
    problem: str
    fix: str


class ValidationReport(BaseModel):
    """The result of validating a strategy: ``ok`` plus any :class:`ValidationIssue`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    issues: list[ValidationIssue] = []


#: Per-field "how to fix" hints keyed by the leaf field name, used to enrich
#: Pydantic's generic messages with concrete guidance.
_FIX_HINTS: dict[str, str] = {
    "timeframe": "use a supported timeframe such as 1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w.",
    "exchange": "use one of: coinbase, kraken, gemini, cryptocom.",
    "symbol": "provide a CCXT symbol like 'BTC/USD' (US venues quote USD/USDC).",
    "value": "provide a positive number; percent_equity must be <= 100.",
    "name": "provide a non-empty strategy name (<= 128 chars).",
    "kind": "use a whitelisted indicator kind (sma, ema, rsi, macd, bbands, "
    "donchian, atr, stoch, adx).",
    "levels": "grid 'levels' must be an integer >= 2.",
    "interval_bars": "dca 'interval_bars' must be an integer >= 1.",
}


def _loc_to_str(loc: tuple[Any, ...]) -> str:
    """Render a Pydantic error location tuple as a dotted path (``entry.long``)."""
    parts = [str(p) for p in loc]
    return ".".join(parts) if parts else "(root)"


#: Rule-expression sides the spec's model validator may flag. A model-level error
#: cannot carry a field ``loc``, so the spec encodes the side as a ``"entry.long:
#: ..."`` prefix on the message; we lift it back into a precise ``location`` here.
_RULE_SIDES: frozenset[str] = frozenset({"entry.long", "entry.short", "exit.long", "exit.short"})


def _issue_from_pydantic(err: Mapping[str, Any]) -> ValidationIssue:
    loc = err.get("loc", ())
    location = _loc_to_str(loc)
    leaf = str(loc[-1]) if loc else ""
    msg = str(err.get("msg", "invalid value"))

    # Pydantic prefixes model-validator messages with "Value error, ". Strip it so
    # we can recover any "entry.long: ..." side prefix the spec encoded.
    body = msg.removeprefix("Value error, ")
    side, sep, rest = body.partition(": ")
    if location == "(root)" and sep and side in _RULE_SIDES:
        return ValidationIssue(
            location=side,
            problem=rest,
            fix="use only whitelisted indicators/operators that resolve to a defined "
            "indicator output or OHLCV column.",
        )

    fix = _FIX_HINTS.get(leaf, "correct the value to satisfy the field's type/constraints.")
    return ValidationIssue(location=location, problem=msg, fix=fix)


def _report_from_pydantic(exc: PydanticValidationError) -> ValidationReport:
    issues = [_issue_from_pydantic(e) for e in exc.errors()]
    if not issues:  # pragma: no cover - pydantic always yields at least one
        issues = [ValidationIssue(location="(root)", problem=str(exc), fix="review the spec.")]
    return ValidationReport(ok=False, issues=issues)


def validate_strategy(data: dict[str, Any] | StrategySpec) -> ValidationReport:
    """Validate ``data`` and return a structured report; never raises.

    Accepts either a raw dict (typical from a tool call) or an already-built
    :class:`StrategySpec` (re-validated for round-trip safety). Every failure --
    type errors, range errors, the timeframe/whitelist/expression cross-checks --
    becomes a :class:`ValidationIssue` with a concrete ``fix``.
    """
    payload = data.model_dump() if isinstance(data, StrategySpec) else data
    try:
        StrategySpec.model_validate(payload)
    except PydanticValidationError as exc:
        return _report_from_pydantic(exc)
    return ValidationReport(ok=True, issues=[])


def create_strategy(data: dict[str, Any]) -> StrategySpec:
    """Validate ``data`` and return a :class:`StrategySpec`, or raise richly.

    Args:
        data: The raw strategy payload (dict).

    Returns:
        The validated, frozen :class:`StrategySpec`.

    Raises:
        trader_mcp.errors.ValidationError: on any failure, with ``details``
            carrying the structured issues (``problem`` + ``fix`` per location) so
            the MCP boundary can surface AI-actionable feedback.
    """
    try:
        return StrategySpec.model_validate(data)
    except PydanticValidationError as exc:
        report = _report_from_pydantic(exc)
        raise ValidationError(
            "Strategy spec is invalid: "
            + "; ".join(f"{i.location}: {i.problem}" for i in report.issues),
            details={
                "kind": "invalid_strategy_spec",
                "issues": [i.model_dump() for i in report.issues],
            },
        ) from None
