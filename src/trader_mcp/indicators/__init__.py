"""Whitelisted indicator library (owned by ``strategy-spec-engineer``).

A thin, deterministic wrapper over ``pandas-ta`` exposing only a closed whitelist
of indicator kinds to the strategy evaluator -- never arbitrary computation. The
registry defines, for each kind, its tunable params (with defaults) and the
**context column names** it contributes to the evaluation namespace.

Output-naming convention (the integration contract the spec validator and the
Phase 4 interpreter both rely on):

    * single-output indicator -> one context name equal to the indicator ``id``;
    * multi-output indicator   -> ``{id}`` (primary line) plus ``{id}_{suffix}``
      for each extra line, using fixed suffixes:
        - ``macd``     -> ``id``, ``id_signal``, ``id_hist``
        - ``bbands``   -> ``id`` (middle), ``id_upper``, ``id_lower``
        - ``donchian`` -> ``id`` (middle), ``id_upper``, ``id_lower``
        - ``stoch``    -> ``id`` (%K), ``id_d`` (%D)
        - ``adx``      -> ``id`` (ADX), ``id_plus_di``, ``id_minus_di``

This package registers **no** MCP tools/resources (that is the server lane) and
imports ``pandas``/``pandas-ta`` lazily so listing tools stays light.

Public surface (the contract the server engineer wraps):
    * :data:`INDICATOR_REGISTRY` -- the whitelist (kind -> definition).
    * :func:`list_indicators` -> ``list[IndicatorInfo]`` -- typed metadata.
    * :func:`compute_indicators` ``(indicators, df) -> DataFrame`` -- add columns.
    * :func:`df_from_bars` ``(bars) -> DataFrame`` -- OHLCV frame from bar models.
    * :func:`allowed_names_for` ``(indicators) -> set[str]`` -- legal rule names.
    * :func:`output_names_for` ``(id, kind) -> list[str]`` -- a kind's context names.
    * Types: :class:`IndicatorInfo`, :class:`IndicatorKind`, :data:`OHLCV_COLUMNS`.
"""

from __future__ import annotations

from trader_mcp.indicators.registry import (
    INDICATOR_REGISTRY,
    OHLCV_COLUMNS,
    IndicatorInfo,
    IndicatorKind,
    allowed_names_for,
    compute_indicators,
    df_from_bars,
    list_indicators,
    output_names_for,
)

__all__ = [
    "INDICATOR_REGISTRY",
    "OHLCV_COLUMNS",
    "IndicatorInfo",
    "IndicatorKind",
    "allowed_names_for",
    "compute_indicators",
    "df_from_bars",
    "list_indicators",
    "output_names_for",
]
