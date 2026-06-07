"""Whitelisted indicator registry backed by ``pandas-ta`` (PRD §5.1, §5.3).

This module is the **security boundary's data side**: it defines the closed set of
indicator *kinds* a strategy spec may declare, how each maps onto a deterministic
``pandas-ta`` call, and -- critically -- the **context column names** each
indicator contributes to the evaluation namespace. The safe expression evaluator
(:mod:`trader_mcp.strategy.evaluator`) may only reference names that appear in
OHLCV columns or in this registry's output set; nothing else is callable.

Output-naming convention (the contract the spec validator AND the future Phase 4
interpreter both rely on):

    * A **single-output** indicator (``sma``, ``ema``, ``rsi``, ``atr``) maps to a
      single context name equal to the indicator's ``id``. Example: an indicator
      ``{id: "fast", kind: "sma", length: 10}`` produces the column ``fast``.
    * A **multi-output** indicator maps to ``{id}`` for its primary line plus
      ``{id}_{suffix}`` for each additional line, using the fixed, documented
      suffixes below. Example: ``{id: "m", kind: "macd"}`` produces ``m`` (the
      MACD line), ``m_signal`` (signal), and ``m_hist`` (histogram).

This keeps every reference an AI writes in a rule (``m_signal > 0``) resolvable
without the author needing to know pandas-ta's internal column strings
(``MACDs_12_26_9``). The mapping is positional against pandas-ta's documented
column order, so it stays stable regardless of the parameter values baked into
those strings.

INVARIANT: indicators are deterministic and pure -- the same bars in always give
the same columns out, so backtest and live get identical values.

``pandas`` / ``pandas-ta`` are imported lazily so the MCP server (which lists
tools without computing anything) stays light.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from trader_mcp.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import pandas as pd

    from trader_mcp.strategy.spec import IndicatorSpec

#: The closed whitelist of indicator kinds a strategy may declare. Adding a kind
#: here (with a registry entry + tests) is the *only* way to extend what rule
#: expressions can reference.
IndicatorKind = Literal[
    "sma",
    "ema",
    "rsi",
    "macd",
    "bbands",
    "donchian",
    "atr",
    "stoch",
    "adx",
]

#: OHLCV columns always present in the evaluation context (lower-case), in
#: addition to any indicator outputs.
OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")

#: The minimum supported runtime for the indicator engine. ``pandas-ta`` 0.4.x
#: requires Python 3.12; on 3.11 the dependency is absent (see pyproject marker).
_MIN_PYTHON: tuple[int, int] = (3, 12)


@dataclass(frozen=True)
class IndicatorParam:
    """Metadata for one tunable parameter of an indicator kind."""

    name: str
    default: float | int
    #: Whether the param must be a positive integer (e.g. lookback ``length``).
    is_int: bool = True


@dataclass(frozen=True)
class IndicatorDef:
    """Static definition of one whitelisted indicator kind.

    ``compute`` takes the OHLCV DataFrame plus the resolved params and returns a
    list of output Series in the documented order (primary line first). The
    registry maps those to context names via :attr:`output_suffixes`.
    """

    kind: IndicatorKind
    summary: str
    params: tuple[IndicatorParam, ...]
    #: Suffixes for each output line, in order. ``""`` is the primary line (named
    #: after the indicator id with no suffix). A single-output indicator has
    #: exactly ``("",)``.
    output_suffixes: tuple[str, ...]
    compute: Callable[[pd.DataFrame, dict[str, float]], Sequence[pd.Series]]

    @property
    def is_multi_output(self) -> bool:
        return len(self.output_suffixes) > 1


@dataclass(frozen=True)
class IndicatorInfo:
    """AI-/server-facing description of a whitelisted indicator kind.

    This is the typed metadata the MCP server surfaces from ``list_indicators``:
    what the indicator is, which params it accepts (with defaults), and which
    context-name suffixes it contributes (so an author knows ``macd`` yields
    ``id``, ``id_signal``, ``id_hist``).
    """

    kind: IndicatorKind
    summary: str
    params: dict[str, float | int] = field(default_factory=dict)
    #: Output-name suffixes; ``""`` denotes the primary line (the bare id).
    output_suffixes: tuple[str, ...] = ("",)


def _require_engine() -> None:
    """Raise a clear error if the indicator engine is unavailable on this runtime."""
    if sys.version_info < _MIN_PYTHON:
        raise ValidationError(
            "Indicator computation requires Python 3.12+ (pandas-ta 0.4.x). "
            "The current interpreter is too old; run trader-mcp under Python 3.12.",
            details={"kind": "indicator_engine_unavailable"},
        )


# --------------------------------------------------------------------------- #
# pandas-ta compute adapters
#
# Each adapter calls pandas-ta and returns output Series in the SAME order as the
# registry's ``output_suffixes``. We select pandas-ta's multi-output columns by
# ORDINAL position (not by their param-baked names like ``MACDs_12_26_9``) so the
# mapping is independent of the chosen lengths/stds.
#
# ROBUSTNESS (Phase 5): pandas-ta returns ``None`` (not a NaN-filled object) when
# the input has fewer rows than an indicator's minimum lookback -- this happens on
# every per-bar live/paper call during warm-up, where the StrategyRuntime drives
# the interpreter over a growing trailing prefix. Calling ``.iloc`` on that
# ``None`` crashes. The two seams below absorb that:
#
#   * ``_nan_series`` builds a NaN-filled Series aligned to ``df``'s index, used
#     as the fallback output line.
#   * ``_pick`` selects ordinal columns from a (possibly ``None``) DataFrame and
#     ``_one`` passes through a (possibly ``None``) Series, each substituting NaN
#     series when pandas-ta returned nothing.
#
# This is parity-preserving: the backtest path computes over the FULL window so
# pandas-ta yields NaN in the warm-up positions, and the interpreter maps
# ``NaN -> False`` (no signal). The live path over a short prefix now yields the
# same NaN -> False at exactly those bars instead of crashing, and never changes
# the happy-path values (when pandas-ta returns data we pass it through unchanged).
# --------------------------------------------------------------------------- #
def _nan_series(df: pd.DataFrame) -> pd.Series:
    """A float NaN Series aligned to ``df``'s index (warm-up / insufficient-data fallback)."""
    import numpy as np
    import pandas as pd

    return pd.Series(np.nan, index=df.index, dtype="float64")


def _one(out: pd.Series | None, df: pd.DataFrame) -> pd.Series:
    """Pass through a pandas-ta Series, or a NaN series if it returned ``None``.

    Single-output adapters use this so a too-short input yields a NaN line instead
    of propagating ``None`` (which would later crash on ``.to_numpy()``).
    """
    return _nan_series(df) if out is None else out


def _pick(out: pd.DataFrame | None, df: pd.DataFrame, *positions: int) -> list[pd.Series]:
    """Select ordinal columns from a pandas-ta DataFrame, NaN-filling on ``None``.

    When pandas-ta has too few rows for a composite indicator it returns ``None``
    rather than a NaN-filled frame; this returns one NaN-filled Series per
    requested position so the adapter still yields the correct NUMBER of lines in
    the right order without touching ``.iloc`` on ``None``.
    """
    if out is None:
        nan = _nan_series(df)
        return [nan] * len(positions)
    return [out.iloc[:, pos] for pos in positions]


def _sma(df: pd.DataFrame, p: dict[str, float]) -> list[pd.Series]:
    from trader_mcp.indicators import _ta

    return [_one(_ta.sma(df["close"], length=int(p["length"])), df)]


def _ema(df: pd.DataFrame, p: dict[str, float]) -> list[pd.Series]:
    from trader_mcp.indicators import _ta

    return [_one(_ta.ema(df["close"], length=int(p["length"])), df)]


def _rsi(df: pd.DataFrame, p: dict[str, float]) -> list[pd.Series]:
    from trader_mcp.indicators import _ta

    return [_one(_ta.rsi(df["close"], length=int(p["length"])), df)]


def _macd(df: pd.DataFrame, p: dict[str, float]) -> list[pd.Series]:
    from trader_mcp.indicators import _ta

    out = _ta.macd(
        df["close"],
        fast=int(p["fast"]),
        slow=int(p["slow"]),
        signal=int(p["signal"]),
    )
    # pandas-ta column order: MACD, MACDh (hist), MACDs (signal).
    # Registry order is: macd line, signal, hist.
    return _pick(out, df, 0, 2, 1)


def _bbands(df: pd.DataFrame, p: dict[str, float]) -> list[pd.Series]:
    from trader_mcp.indicators import _ta

    out = _ta.bbands(df["close"], length=int(p["length"]), std=float(p["std"]))
    # pandas-ta column order: BBL (lower), BBM (mid), BBU (upper), BBB, BBP.
    # Registry primary line is the middle band; then upper, lower.
    return _pick(out, df, 1, 2, 0)


def _donchian(df: pd.DataFrame, p: dict[str, float]) -> list[pd.Series]:
    from trader_mcp.indicators import _ta

    length = int(p["length"])
    out = _ta.donchian(df["high"], df["low"], lower_length=length, upper_length=length)
    # pandas-ta column order: DCL (lower), DCM (mid), DCU (upper).
    # Registry primary line is the middle channel; then upper, lower.
    return _pick(out, df, 1, 2, 0)


def _atr(df: pd.DataFrame, p: dict[str, float]) -> list[pd.Series]:
    from trader_mcp.indicators import _ta

    return [_one(_ta.atr(df["high"], df["low"], df["close"], length=int(p["length"])), df)]


def _stoch(df: pd.DataFrame, p: dict[str, float]) -> list[pd.Series]:
    from trader_mcp.indicators import _ta

    out = _ta.stoch(
        df["high"],
        df["low"],
        df["close"],
        k=int(p["k"]),
        d=int(p["d"]),
        smooth_k=int(p["smooth_k"]),
    )
    # pandas-ta column order: STOCHk, STOCHd, STOCHh. Primary line is %K; then %D.
    return _pick(out, df, 0, 1)


def _adx(df: pd.DataFrame, p: dict[str, float]) -> list[pd.Series]:
    from trader_mcp.indicators import _ta

    out = _ta.adx(df["high"], df["low"], df["close"], length=int(p["length"]))
    # pandas-ta column order: ADX, ADXR, DMP (+DI), DMN (-DI).
    # Registry order: adx (primary), +di (plus), -di (minus).
    return _pick(out, df, 0, 2, 3)


#: The registry: every whitelisted indicator kind -> its definition. This dict IS
#: the whitelist. ``output_suffixes[0]`` is always ``""`` (the bare-id primary
#: line); subsequent entries become ``{id}_{suffix}`` context names.
INDICATOR_REGISTRY: dict[str, IndicatorDef] = {
    "sma": IndicatorDef(
        kind="sma",
        summary="Simple moving average of close.",
        params=(IndicatorParam("length", 20),),
        output_suffixes=("",),
        compute=_sma,
    ),
    "ema": IndicatorDef(
        kind="ema",
        summary="Exponential moving average of close.",
        params=(IndicatorParam("length", 20),),
        output_suffixes=("",),
        compute=_ema,
    ),
    "rsi": IndicatorDef(
        kind="rsi",
        summary="Relative Strength Index of close (0-100).",
        params=(IndicatorParam("length", 14),),
        output_suffixes=("",),
        compute=_rsi,
    ),
    "macd": IndicatorDef(
        kind="macd",
        summary="MACD line, signal line, and histogram.",
        params=(
            IndicatorParam("fast", 12),
            IndicatorParam("slow", 26),
            IndicatorParam("signal", 9),
        ),
        output_suffixes=("", "signal", "hist"),
        compute=_macd,
    ),
    "bbands": IndicatorDef(
        kind="bbands",
        summary="Bollinger Bands: middle (primary), upper, lower.",
        params=(
            IndicatorParam("length", 20),
            IndicatorParam("std", 2.0, is_int=False),
        ),
        output_suffixes=("", "upper", "lower"),
        compute=_bbands,
    ),
    "donchian": IndicatorDef(
        kind="donchian",
        summary="Donchian channel: middle (primary), upper, lower.",
        params=(IndicatorParam("length", 20),),
        output_suffixes=("", "upper", "lower"),
        compute=_donchian,
    ),
    "atr": IndicatorDef(
        kind="atr",
        summary="Average True Range (volatility).",
        params=(IndicatorParam("length", 14),),
        output_suffixes=("",),
        compute=_atr,
    ),
    "stoch": IndicatorDef(
        kind="stoch",
        summary="Stochastic oscillator: %K (primary) and %D.",
        params=(
            IndicatorParam("k", 14),
            IndicatorParam("d", 3),
            IndicatorParam("smooth_k", 3),
        ),
        output_suffixes=("", "d"),
        compute=_stoch,
    ),
    "adx": IndicatorDef(
        kind="adx",
        summary="Average Directional Index (primary) with +DI/-DI.",
        params=(IndicatorParam("length", 14),),
        output_suffixes=("", "plus_di", "minus_di"),
        compute=_adx,
    ),
}


def list_indicators() -> list[IndicatorInfo]:
    """Return typed metadata for every whitelisted indicator kind.

    This is the surface the MCP server wraps so an AI author can discover the
    legal ``kind`` values, their params + defaults, and the context-name suffixes
    each contributes. Ordered by kind name for a stable listing.
    """
    return [
        IndicatorInfo(
            kind=defn.kind,
            summary=defn.summary,
            params={p.name: p.default for p in defn.params},
            output_suffixes=defn.output_suffixes,
        )
        for _, defn in sorted(INDICATOR_REGISTRY.items())
    ]


def output_names_for(indicator_id: str, kind: str) -> list[str]:
    """Return the context column names an indicator ``id`` of ``kind`` contributes.

    The primary line is the bare ``indicator_id``; each extra line is
    ``{indicator_id}_{suffix}`` per the documented convention. Raises
    :class:`ValidationError` for an unknown kind (default-deny).
    """
    defn = INDICATOR_REGISTRY.get(kind)
    if defn is None:
        raise ValidationError(
            f"Unknown indicator kind: {kind!r}. "
            f"Allowed kinds: {', '.join(sorted(INDICATOR_REGISTRY))}.",
            details={"kind": "unknown_indicator_kind", "value": kind},
        )
    names: list[str] = []
    for suffix in defn.output_suffixes:
        names.append(indicator_id if suffix == "" else f"{indicator_id}_{suffix}")
    return names


def allowed_names_for(indicators: Sequence[IndicatorSpec]) -> set[str]:
    """Return the full set of names a rule expression may reference for a spec.

    The allowed namespace is the OHLCV columns plus every output name contributed
    by the spec's indicators. The spec validator uses this to statically reject a
    rule that references an undefined name; the Phase 4 interpreter computes the
    same set when populating the evaluation context.
    """
    names: set[str] = set(OHLCV_COLUMNS)
    for ind in indicators:
        names.update(output_names_for(ind.id, ind.kind))
    return names


def df_from_bars(bars: Sequence[object]) -> pd.DataFrame:
    """Build an OHLCV :class:`pandas.DataFrame` from a sequence of bar models.

    Accepts any objects exposing ``timestamp/open/high/low/close/volume`` (e.g.
    :class:`trader_mcp.exchanges.models.OHLCVBar`). The frame is indexed by the
    tz-aware UTC timestamp and has lower-case OHLCV columns -- the exact shape the
    indicator adapters and evaluator expect.
    """
    _require_engine()
    import pandas as pd

    if not bars:
        return pd.DataFrame(
            {col: pd.Series(dtype="float64") for col in OHLCV_COLUMNS},
            index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
        )
    index = pd.DatetimeIndex([b.timestamp for b in bars], name="timestamp")  # type: ignore[attr-defined]
    return pd.DataFrame(
        {
            "open": [float(b.open) for b in bars],  # type: ignore[attr-defined]
            "high": [float(b.high) for b in bars],  # type: ignore[attr-defined]
            "low": [float(b.low) for b in bars],  # type: ignore[attr-defined]
            "close": [float(b.close) for b in bars],  # type: ignore[attr-defined]
            "volume": [float(b.volume) for b in bars],  # type: ignore[attr-defined]
        },
        index=index,
    )


def compute_indicators(
    indicators: Sequence[IndicatorSpec],
    df: pd.DataFrame,
) -> pd.DataFrame:
    """Return a copy of ``df`` with every indicator's output columns added.

    Each indicator in ``indicators`` is computed via its registry adapter and its
    output Series are assigned to the deterministic context names (see
    :func:`output_names_for`). The OHLCV columns are preserved. Determinism: the
    same bars + same indicator params always produce identical columns, so the
    backtest and live paths agree bar-for-bar.

    Raises:
        trader_mcp.errors.ValidationError: on an unknown kind or if the engine is
            unavailable on this Python runtime.
    """
    _require_engine()
    out = df.copy()
    for ind in indicators:
        defn = INDICATOR_REGISTRY.get(ind.kind)
        if defn is None:
            raise ValidationError(
                f"Unknown indicator kind: {ind.kind!r}. "
                f"Allowed kinds: {', '.join(sorted(INDICATOR_REGISTRY))}.",
                details={"kind": "unknown_indicator_kind", "value": ind.kind},
            )
        params = ind.resolved_params()
        series = defn.compute(df, params)
        names = output_names_for(ind.id, ind.kind)
        for name, ser in zip(names, series, strict=True):
            out[name] = ser.to_numpy()
    return out
