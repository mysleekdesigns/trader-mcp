"""Declarative strategy spec, templates, evaluator & store (``strategy-spec-engineer``).

A versioned, typed Pydantic v2 strategy spec plus a template library, a file-based
store, and -- the security boundary -- a safe sandboxed expression evaluator that
runs entry/exit rules against a strict whitelist of indicators/operators. There is
**no arbitrary code execution**: a rule is data (a string) that is statically
validated and then evaluated via an AST walk, never ``eval``/``exec``.

This package registers **no** MCP tools/resources/prompts (that is the server
lane) and does not import the MCP SDK. The MCP-server engineer wraps the public
surface below into the strategy-authoring tools; the Phase 4 backtest/live
interpreter consumes :class:`StrategySpec` + :func:`evaluate_expression` +
:func:`trader_mcp.indicators.compute_indicators` to produce signals.

Public surface (the integration contract):
    Spec models:
        * :class:`StrategySpec`, :data:`SCHEMA_VERSION`
        * :class:`IndicatorSpec`, :class:`EntryRules`, :class:`ExitRules`,
          :class:`PositionSizing`, :class:`RiskLimits`, :class:`Fees`,
          :class:`GridConfig`, :class:`DCAConfig`, :data:`StrategyType`
    Validation:
        * :func:`validate_strategy` ``(data) -> ValidationReport`` (never raises)
        * :func:`create_strategy` ``(dict) -> StrategySpec`` (raises richly)
        * :class:`ValidationReport`, :class:`ValidationIssue`
    Templates:
        * :func:`list_templates` ``() -> list[TemplateInfo]``
        * :func:`build_from_template` ``(id, overrides) -> StrategySpec``
        * :class:`TemplateInfo`
    Persistence:
        * :class:`StrategyStore` (``save``/``load``/``list``/``delete``/``exists``)
        * :class:`StrategyInfo`
    Evaluator (security boundary; also used by the Phase 4 interpreter):
        * :func:`validate_expression` ``(expr, allowed_names) -> None``
        * :func:`evaluate_expression` ``(expr, context) -> bool | Series``
"""

from __future__ import annotations

from trader_mcp.strategy.evaluator import evaluate_expression, validate_expression
from trader_mcp.strategy.spec import (
    SCHEMA_VERSION,
    DCAConfig,
    EntryRules,
    ExitRules,
    Fees,
    GridConfig,
    IndicatorSpec,
    PositionSizing,
    RiskLimits,
    StrategySpec,
    StrategyType,
)
from trader_mcp.strategy.store import StrategyInfo, StrategyStore
from trader_mcp.strategy.templates import (
    TemplateInfo,
    build_from_template,
    list_templates,
)
from trader_mcp.strategy.validation import (
    ValidationIssue,
    ValidationReport,
    create_strategy,
    validate_strategy,
)

__all__ = [
    "SCHEMA_VERSION",
    "DCAConfig",
    "EntryRules",
    "ExitRules",
    "Fees",
    "GridConfig",
    "IndicatorSpec",
    "PositionSizing",
    "RiskLimits",
    "StrategyInfo",
    "StrategySpec",
    "StrategyStore",
    "StrategyType",
    "TemplateInfo",
    "ValidationIssue",
    "ValidationReport",
    "build_from_template",
    "create_strategy",
    "evaluate_expression",
    "list_templates",
    "validate_expression",
    "validate_strategy",
]
