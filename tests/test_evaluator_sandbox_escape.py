"""SECURITY suite for the safe expression evaluator (no-arbitrary-code boundary).

INVARIANT under test: strategy specs are **data, never code**. Every rule string
is parsed and walked against a default-deny whitelist; anything not explicitly
allowed must be HARD-REJECTED (raise ``ValidationError``) BEFORE any evaluation.
Wave 1 already has an evaluator unit suite; this one is complementary -- it
focuses on (a) exhaustively throwing escape vectors at ``validate_expression`` /
``evaluate_expression`` and (b) the dual scalar/Series PARITY that underpins the
future one-interpreter invariant (the same logical result whether a name maps to
a scalar -- the live path -- or to a pandas Series -- the backtest path).

If ANY escape vector were to evaluate instead of being rejected, that is a
BLOCKER (it violates "specs are data, never code"). The tests assert rejection.
"""

from __future__ import annotations

import pytest

from trader_mcp.errors import ValidationError
from trader_mcp.strategy import evaluate_expression, validate_expression

#: A generous allowed-name set so rejections are about the CONSTRUCT, not an
#: unknown name (names like ``close``/``sma`` resolve; the escape is what's bad).
_ALLOWED: set[str] = {"open", "high", "low", "close", "volume", "sma", "rsi"}


# --------------------------------------------------------------------------- #
# Hard-rejection of every escape vector (static validation must raise).
# --------------------------------------------------------------------------- #
_ESCAPES: list[tuple[str, str]] = [
    ("attribute_access", "close.__class__"),
    ("attribute_method", "close.real"),
    ("dunder_name", "__import__"),
    ("dunder_in_call", "__import__('os')"),
    ("subscript", "close[0]"),
    ("subscript_slice", "close[0:1]"),
    ("call_open", "open('x')"),  # 'open' is an OHLCV name but not callable
    ("call_eval", "eval(close)"),
    ("call_exec", "exec(close)"),
    ("call_import", "__import__('os')"),
    ("call_getattr", "getattr(close, 'x')"),
    ("lambda", "(lambda: 1)()"),
    ("list_comprehension", "[x for x in close]"),
    ("set_comprehension", "{x for x in close}"),
    ("dict_comprehension", "{x: 1 for x in close}"),
    ("generator_expr", "min(x for x in close)"),
    ("walrus", "(x := close) > 0"),
    ("ternary_ifexp", "1 if close else 0"),
    ("fstring", "f'{close}'"),
    ("string_constant", "'abc'"),
    ("list_literal", "[1, 2, 3]"),
    ("tuple_literal", "(1, 2)"),
    ("dict_literal", "{1: 2}"),
    ("set_literal", "{1, 2}"),
    ("starred_arg", "max(*close)"),
    ("keyword_arg", "crossover(close, b=sma)"),
    ("power_operator", "close ** 2"),
    ("floordiv_operator", "close // 2"),
    ("bitwise_or", "close | sma"),
    ("bitwise_and", "close & sma"),
    ("shift_operator", "close << 1"),
    ("is_operator", "close is None"),
    ("in_operator", "close in sma"),
    ("unknown_name", "definitely_not_a_name > 0"),
    ("computed_call_target", "(abs if close else min)(close)"),
    ("import_statement_like", "close.__globals__"),
    ("empty_expression", "   "),
]


@pytest.mark.parametrize("expr", [e[1] for e in _ESCAPES], ids=[e[0] for e in _ESCAPES])
def test_validate_expression_rejects_escape(expr: str) -> None:
    """Static validation HARD-rejects every escape vector with a ValidationError."""
    with pytest.raises(ValidationError) as excinfo:
        validate_expression(expr, _ALLOWED)
    # The rejection is structured and actionable (problem + fix), and never leaks.
    err = excinfo.value
    assert err.details.get("problem")
    assert err.details.get("fix")


@pytest.mark.parametrize("expr", [e[1] for e in _ESCAPES], ids=[e[0] for e in _ESCAPES])
def test_evaluate_expression_never_executes_escape(expr: str) -> None:
    """evaluate_expression rejects an escape BEFORE any execution (no side effects)."""
    # evaluate_expression validates with the context keys as the allowed set; pass a
    # context that grants the benign names so the ONLY reason to raise is the escape.
    context = dict.fromkeys(_ALLOWED, 1.0)
    with pytest.raises(ValidationError):
        evaluate_expression(expr, context)


def test_assignment_statement_is_not_an_expression() -> None:
    """A bare assignment isn't valid eval-mode syntax and is rejected as such."""
    with pytest.raises(ValidationError):
        validate_expression("x = 1", _ALLOWED)


def test_dunder_name_rejected_even_if_whitelisted_shape() -> None:
    """A dunder name is forbidden regardless of the allowed-name set."""
    with pytest.raises(ValidationError):
        validate_expression("__class__ > 0", _ALLOWED | {"__class__"})


# --------------------------------------------------------------------------- #
# ALLOWED expressions evaluate correctly in BOTH modes (scalar vs Series).
# These do NOT compute indicators (just arithmetic/comparison on given values),
# so the only dependency is pandas, guarded with importorskip for the 3.11 leg.
# --------------------------------------------------------------------------- #
_DUAL_CASES: list[tuple[str, dict[str, float], bool]] = [
    ("rsi < 30", {"rsi": 25.0}, True),
    ("rsi < 30", {"rsi": 35.0}, False),
    ("30 < rsi < 70", {"rsi": 50.0}, True),  # chained comparison
    ("30 < rsi < 70", {"rsi": 80.0}, False),
    ("close > sma and rsi < 70", {"close": 10.0, "sma": 5.0, "rsi": 60.0}, True),
    ("close > sma and rsi < 70", {"close": 3.0, "sma": 5.0, "rsi": 60.0}, False),
    ("close < sma or rsi > 50", {"close": 3.0, "sma": 5.0, "rsi": 40.0}, True),
    ("close < sma or rsi > 50", {"close": 10.0, "sma": 5.0, "rsi": 40.0}, False),
    ("abs(close - sma) > 1", {"close": 10.0, "sma": 5.0}, True),
    ("close * 2 - sma > 0", {"close": 3.0, "sma": 5.0}, True),
    ("close % 2 == 0", {"close": 4.0}, True),
    ("close % 2 == 0", {"close": 5.0}, False),
]


@pytest.mark.parametrize(
    ("expr", "ctx", "expected"),
    _DUAL_CASES,
    ids=[f"{c[0]}::{c[2]}" for c in _DUAL_CASES],
)
def test_scalar_and_series_modes_agree(expr: str, ctx: dict[str, float], expected: bool) -> None:
    """The same expression yields the same logical result for scalars AND Series.

    This is the dual-mode parity the one-interpreter invariant rests on: feeding a
    constant Series of the scalar value must reproduce the scalar's boolean at
    every index.
    """
    pd = pytest.importorskip("pandas")

    # Scalar (live) mode: a plain bool.
    scalar_result = evaluate_expression(expr, ctx)
    assert isinstance(scalar_result, bool)
    assert scalar_result is expected

    # Series (backtest) mode: a constant Series of the same values over 4 bars.
    series_ctx = {name: pd.Series([value] * 4) for name, value in ctx.items()}
    series_result = evaluate_expression(expr, series_ctx)
    assert hasattr(series_result, "iloc"), "Series context must yield a Series"
    assert series_result.astype(bool).tolist() == [expected] * 4


def test_not_operator_series_mode_is_boolean() -> None:
    """``not`` over a Series yields a proper boolean Series (backtest path)."""
    pd = pytest.importorskip("pandas")
    result = evaluate_expression("not (rsi > 50)", {"rsi": pd.Series([40.0] * 3)})
    assert result.astype(bool).tolist() == [True, True, True]


def test_not_operator_scalar_series_parity() -> None:
    """Scalar ``not`` returns a genuine bool and matches Series-mode element-for-element.

    A Python ``bool`` is an ``int`` (it exposes ``__invert__``), so a naive ``~``
    would yield ``-1`` for ``not False``. The evaluator's ``_logical_not`` helper
    instead uses Python ``not`` for scalars (real ``bool``) and ``~`` only for real
    pandas Series -- so the live (scalar) and backtest (Series) paths agree, which
    the one-interpreter invariant requires.
    """
    pd = pytest.importorskip("pandas")

    values = [60.0, 40.0, 50.0, 51.0, 49.0]
    # rsi > 50 -> [T, F, F, T, F]; not (...) -> [F, T, T, F, T]
    expected = [False, True, True, False, True]

    # Scalar (live) mode: each result is a genuine bool, never an int like -1.
    scalar_results = [evaluate_expression("not (rsi > 50)", {"rsi": v}) for v in values]
    for result in scalar_results:
        assert isinstance(result, bool)
        assert result is True or result is False  # never -1 or other int
    assert scalar_results == expected

    # Series (backtest) mode: a boolean Series matching the scalar results.
    series_result = evaluate_expression("not (rsi > 50)", {"rsi": pd.Series(values)})
    assert series_result.astype(bool).tolist() == expected

    # Element-for-element parity between the two execution modes.
    assert scalar_results == series_result.astype(bool).tolist()


# --------------------------------------------------------------------------- #
# crossover/crossunder semantics: True ONLY on the transition bar; first bar
# never signals. Asserted in Series mode AND validated as the documented
# scalar-no-history behavior (a bare scalar context never spuriously signals).
# --------------------------------------------------------------------------- #
def test_crossover_series_signals_only_on_transition_bar() -> None:
    """crossover(a, b) is True only on the bar a goes from <=b to >b (not the 1st)."""
    pd = pytest.importorskip("pandas")

    # a vs b: a starts below, crosses above at index 2, stays above, dips at 4.
    a = pd.Series([1.0, 2.0, 6.0, 7.0, 3.0])
    b = pd.Series([5.0, 5.0, 5.0, 5.0, 5.0])
    ctx = {"a": a, "b": b}

    over = evaluate_expression("crossover(a, b)", ctx)
    # Only index 2 is a strict cross from <=b to >b; index 0 (first bar) never.
    assert over.astype(bool).tolist() == [False, False, True, False, False]

    under = evaluate_expression("crossunder(a, b)", ctx)
    # a goes from >b (index 3) to <b (index 4): crossunder at index 4 only.
    assert under.astype(bool).tolist() == [False, False, False, False, True]


def test_crossover_first_bar_never_signals_series() -> None:
    """Even if a > b at the very first bar, there is no prior bar -> no signal."""
    pd = pytest.importorskip("pandas")
    a = pd.Series([10.0, 11.0])
    b = pd.Series([5.0, 5.0])
    over = evaluate_expression("crossover(a, b)", {"a": a, "b": b})
    assert over.astype(bool).tolist() == [False, False]


def test_crossover_scalar_no_history_never_signals() -> None:
    """In a bare scalar context (no prior bar) crossover yields False -- never a
    spurious live signal (the documented scalar contract)."""
    result = evaluate_expression("crossover(close, sma)", {"close": 10.0, "sma": 5.0})
    assert isinstance(result, bool)
    assert result is False


# --------------------------------------------------------------------------- #
# End-to-end Series-mode evaluation over REAL computed indicator columns.
# This is the only test here that computes indicators, so it skips when the
# indicator engine (pandas-ta) is unavailable (the 3.11 CI leg).
# --------------------------------------------------------------------------- #
def test_series_mode_over_computed_indicators() -> None:
    """A rule evaluates over genuinely-computed indicator columns (backtest path)."""
    pytest.importorskip("pandas_ta")
    pd = pytest.importorskip("pandas")

    from trader_mcp.indicators import allowed_names_for, compute_indicators, df_from_bars
    from trader_mcp.strategy import IndicatorSpec

    # A rising-then-falling close series long enough for an SMA(3).
    closes = [10.0, 11.0, 12.0, 13.0, 12.0, 11.0, 10.0, 9.0]

    class _Bar:
        def __init__(self, i: int, c: float) -> None:
            self.timestamp = pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(hours=i)
            self.open = c
            self.high = c + 1
            self.low = c - 1
            self.close = c
            self.volume = 100.0

    bars = [_Bar(i, c) for i, c in enumerate(closes)]
    indicators = [IndicatorSpec(id="sma", kind="sma", params={"length": 3})]

    df = df_from_bars(bars)
    enriched = compute_indicators(indicators, df)

    allowed = allowed_names_for(indicators)
    assert {"close", "sma"} <= allowed

    context = {name: enriched[name] for name in allowed if name in enriched.columns}
    signal = evaluate_expression("close > sma", context)

    # A boolean Series aligned to the bars; the rising leg is above its own SMA.
    assert hasattr(signal, "iloc")
    assert signal.astype(bool).tolist()[3] is True  # close=13 well above SMA(3)
    # On the falling leg close drops below the trailing SMA.
    assert signal.astype(bool).tolist()[-1] is False
