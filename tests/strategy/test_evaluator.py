"""Security + correctness tests for the safe expression evaluator.

The evaluator is the strategy security boundary, so the bulk of these tests assert
that non-whitelisted constructs are HARD-REJECTED (default-deny): attribute access,
subscripts, unknown names/calls, lambdas, comprehensions, imports, dunders,
walrus, f-strings, etc. The rest assert that allowed expressions evaluate
correctly in BOTH scalar (live) and Series (backtest) modes, including the precise
crossover/crossunder semantics.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trader_mcp.errors import ValidationError
from trader_mcp.strategy.evaluator import (
    crossover,
    crossunder,
    evaluate_expression,
    validate_expression,
)

_ALLOWED = {"open", "high", "low", "close", "volume", "rsi", "sma", "fast", "slow", "m", "m_signal"}


# --------------------------------------------------------------------------- #
# REJECTED constructs (the security boundary)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "expr",
    [
        "close.real",  # attribute access
        "rsi.__class__",  # attribute -> dunder escape
        "close[0]",  # subscript
        "(lambda: 1)()",  # lambda
        "[c for c in close]",  # list comprehension
        "{c for c in close}",  # set comprehension
        "{k: v for k, v in close}",  # dict comprehension
        "(c for c in close)",  # generator
        "__import__('os')",  # import via builtin name (not whitelisted)
        "import os",  # not even an expression
        "open('secret')",  # 'open' is an OHLCV name, not callable -> non-whitelisted call
        "foobar",  # unknown name
        "unknown_func(close)",  # unknown call
        "rsi := 5",  # walrus
        "f'{rsi}'",  # f-string
        "'a string'",  # string constant
        "close ** 2",  # power operator (not allowed)
        "close // 2",  # floor div (not allowed)
        "close & 1",  # bitwise
        "close >> 1",  # shift
        "rsi if close else sma",  # conditional expression
        "[1, 2, 3]",  # list literal
        "(1, 2)",  # tuple literal
        "{1, 2}",  # set literal
        "{'a': 1}",  # dict literal
        "rsi is None",  # 'is' comparison not allowed
        "rsi in close",  # 'in' comparison not allowed
        "crossover(*[close, sma])",  # starred args
        "crossover(close, b=sma)",  # keyword args
        "min(close, key=sma)",  # keyword on builtin
        "__builtins__",  # dunder name
        "",  # empty
        "   ",  # blank
    ],
)
def test_rejects_unsafe_expression(expr: str) -> None:
    with pytest.raises(ValidationError) as exc:
        validate_expression(expr, _ALLOWED)
    # Every rejection carries an actionable, structured payload.
    assert exc.value.details.get("problem")
    assert exc.value.details.get("fix")


def test_unknown_name_lists_available_names() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_expression("nope > 5", {"rsi"})
    assert "rsi" in exc.value.details["fix"]


def test_evaluate_rejects_name_absent_from_context() -> None:
    # evaluate_expression derives allowed names from the context keys.
    with pytest.raises(ValidationError):
        evaluate_expression("missing > 1", {"rsi": 5.0})


# --------------------------------------------------------------------------- #
# ALLOWED constructs validate
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "expr",
    [
        "rsi < 30",
        "rsi > 70 and close > sma",
        "not (rsi < 30) or close <= sma",
        "30 < rsi < 70",  # chained comparison
        "close - sma > 0",
        "close * 1.01 >= high",
        "-close < 0",
        "abs(close - sma) < 5",
        "min(close, sma) > 10",
        "max(fast, slow) > 100",
        "crossover(fast, slow)",
        "crossunder(m, m_signal)",
        "close % 2 == 0",
    ],
)
def test_accepts_safe_expression(expr: str) -> None:
    validate_expression(expr, _ALLOWED)  # must not raise


# --------------------------------------------------------------------------- #
# Scalar evaluation (the live, single-bar path)
# --------------------------------------------------------------------------- #
def test_scalar_comparison() -> None:
    assert evaluate_expression("rsi < 30", {"rsi": 25.0}) is True
    assert evaluate_expression("rsi < 30", {"rsi": 35.0}) is False


def test_scalar_boolean_and_chained() -> None:
    ctx = {"rsi": 50.0, "close": 100.0, "sma": 90.0}
    assert evaluate_expression("rsi > 40 and close > sma", ctx) is True
    assert evaluate_expression("30 < rsi < 70", ctx) is True
    assert evaluate_expression("60 < rsi < 70", ctx) is False


def test_scalar_arithmetic_and_helpers() -> None:
    ctx = {"close": 105.0, "sma": 100.0}
    assert evaluate_expression("abs(close - sma) < 10", ctx) is True
    assert evaluate_expression("min(close, sma) == 100", ctx) is True
    assert evaluate_expression("max(close, sma) == 105", ctx) is True


def test_scalar_crossover_without_history_is_false() -> None:
    # A bare scalar context has no previous bar -> never a spurious signal.
    assert evaluate_expression("crossover(close, sma)", {"close": 10.0, "sma": 5.0}) is False
    assert evaluate_expression("crossunder(close, sma)", {"close": 1.0, "sma": 5.0}) is False


# --------------------------------------------------------------------------- #
# Series evaluation (the backtest, vectorized path)
# --------------------------------------------------------------------------- #
def test_series_comparison_returns_boolean_series() -> None:
    ctx = {"rsi": pd.Series([20.0, 40.0, 25.0])}
    result = evaluate_expression("rsi < 30", ctx)
    assert isinstance(result, pd.Series)
    assert result.tolist() == [True, False, True]


def test_series_boolean_and() -> None:
    ctx = {
        "close": pd.Series([10.0, 20.0, 30.0]),
        "sma": pd.Series([15.0, 15.0, 15.0]),
    }
    result = evaluate_expression("close > sma and close < 25", ctx)
    assert result.tolist() == [False, True, False]


def test_crossover_semantics_series() -> None:
    # a goes from below b to above b at index 2.
    a = pd.Series([1.0, 2.0, 5.0, 6.0])
    b = pd.Series([3.0, 3.0, 3.0, 3.0])
    result = crossover(a, b)
    assert result.tolist() == [False, False, True, False]
    # First bar is never a crossover.
    assert result.iloc[0] is np.False_ or result.iloc[0] == False  # noqa: E712


def test_crossunder_semantics_series() -> None:
    a = pd.Series([6.0, 5.0, 2.0, 1.0])
    b = pd.Series([3.0, 3.0, 3.0, 3.0])
    result = crossunder(a, b)
    assert result.tolist() == [False, False, True, False]


def test_crossover_equal_then_above_counts() -> None:
    # a_prev == b_prev then a > b qualifies (<= on prior bar).
    a = pd.Series([3.0, 4.0])
    b = pd.Series([3.0, 3.0])
    assert crossover(a, b).tolist() == [False, True]


def test_crossover_strict_no_signal_when_only_touching() -> None:
    # a stays equal to b -> no strict cross.
    a = pd.Series([3.0, 3.0, 3.0])
    b = pd.Series([3.0, 3.0, 3.0])
    assert crossover(a, b).tolist() == [False, False, False]


def test_series_crossover_via_expression() -> None:
    ctx = {
        "fast": pd.Series([1.0, 2.0, 5.0, 6.0]),
        "slow": pd.Series([3.0, 3.0, 3.0, 3.0]),
    }
    result = evaluate_expression("crossover(fast, slow)", ctx)
    assert result.tolist() == [False, False, True, False]


# --------------------------------------------------------------------------- #
# `not` parity: scalar (live) must return a real bool AND match Series (backtest)
# (regression: bool is an int subclass with __invert__, so `~False == -1` is wrong)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("rsi", "expected"),
    [(60.0, False), (40.0, True)],
)
def test_scalar_not_returns_bool(rsi: float, expected: bool) -> None:
    result = evaluate_expression("not (rsi > 50)", {"rsi": rsi})
    assert result is expected  # a genuine bool, never ~int (-1/-2)
    assert isinstance(result, bool)


def test_scalar_not_double_negation() -> None:
    assert evaluate_expression("not (not (rsi > 50))", {"rsi": 60.0}) is True
    assert evaluate_expression("not (not (rsi > 50))", {"rsi": 40.0}) is False


def test_series_not_returns_boolean_series() -> None:
    result = evaluate_expression("not (rsi > 50)", {"rsi": pd.Series([60.0, 40.0, 51.0])})
    assert isinstance(result, pd.Series)
    assert result.dtype == bool
    assert result.tolist() == [False, True, False]


def test_not_scalar_series_parity() -> None:
    # The SAME expression on the SAME values must agree element-for-element across
    # the live (scalar, per-bar) and backtest (vectorized Series) paths.
    values = [60.0, 40.0, 50.0, 51.0, 49.0]
    expr = "not (rsi > 50)"
    scalar_results = [evaluate_expression(expr, {"rsi": v}) for v in values]
    series_result = evaluate_expression(expr, {"rsi": pd.Series(values)}).tolist()
    assert scalar_results == series_result
    assert all(isinstance(r, bool) for r in scalar_results)


# --------------------------------------------------------------------------- #
# min/max arity (BLOCKER): single-arg reduce is rejected (lookahead + parity)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("expr", ["min(close)", "max(close)", "min(close, sma, high)", "max()"])
def test_min_max_require_exactly_two_args(expr: str) -> None:
    with pytest.raises(ValidationError) as exc:
        validate_expression(expr, {"close", "sma", "high"})
    assert "exactly 2 arguments" in exc.value.details["problem"]


def test_abs_requires_exactly_one_arg() -> None:
    with pytest.raises(ValidationError, match="exactly 1 argument"):
        validate_expression("abs(close, sma)", {"close", "sma"})
    with pytest.raises(ValidationError, match="exactly 1 argument"):
        validate_expression("abs()", {"close"})


def test_binary_min_max_are_elementwise_series() -> None:
    ctx = {"close": pd.Series([5.0, 1.0, 9.0]), "sma": pd.Series([3.0, 3.0, 3.0])}
    assert evaluate_expression("min(close, sma)", ctx).tolist() == [3.0, 1.0, 3.0]
    assert evaluate_expression("max(close, sma)", ctx).tolist() == [5.0, 3.0, 9.0]


def test_binary_min_max_no_lookahead() -> None:
    # Regression: a single-arg min(close) would compare each bar to the GLOBAL min
    # (incl. future bars). The binary form compares element-wise only, so bar 0 of
    # [5,1,9] does NOT see the future minimum (1).
    ctx = {"close": pd.Series([5.0, 1.0, 9.0]), "ref": pd.Series([5.0, 5.0, 5.0])}
    # close > min(close, ref): element-wise min is [5,1,5]; close>that = [F,F,T].
    assert evaluate_expression("close > min(close, ref)", ctx).tolist() == [False, False, True]


def test_min_max_scalar_series_parity() -> None:
    values_a = [5.0, 1.0, 9.0]
    values_b = [3.0, 3.0, 3.0]
    expr = "min(a, b) > 2"
    scalar = [
        evaluate_expression(expr, {"a": a, "b": b}) for a, b in zip(values_a, values_b, strict=True)
    ]
    series = evaluate_expression(
        "min(a, b) > 2", {"a": pd.Series(values_a), "b": pd.Series(values_b)}
    )
    assert scalar == series.tolist()


# --------------------------------------------------------------------------- #
# numpy-scalar contract (#2): live interpreter feeds np scalars; result is bool
# --------------------------------------------------------------------------- #
def test_numpy_scalar_context_returns_python_bool() -> None:
    # df.iloc[i]["close"] is an np.float64 in the live path.
    result = evaluate_expression("close > 50", {"close": np.float64(60.0)})
    assert result is True
    assert isinstance(result, bool)
    result2 = evaluate_expression(
        "rsi < 30 and close > 50", {"rsi": np.float64(25.0), "close": np.float64(60.0)}
    )
    assert result2 is True
    assert isinstance(result2, bool)


def test_numpy_scalar_parity_with_series() -> None:
    # The SAME expression over a DataFrame row (np scalars) vs the column (Series)
    # must agree element-for-element, and the scalar path yields genuine bools.
    df = pd.DataFrame({"rsi": [25.0, 55.0, 10.0], "close": [60.0, 40.0, 70.0]})
    expr = "rsi < 30 and close > 50"
    scalar = [evaluate_expression(expr, dict(df.iloc[i])) for i in range(len(df))]
    series = evaluate_expression(expr, {"rsi": df["rsi"], "close": df["close"]}).tolist()
    assert scalar == series
    assert all(isinstance(r, bool) for r in scalar)


# --------------------------------------------------------------------------- #
# divide-by-zero parity (#3): /0 and %0 -> NaN -> False in BOTH modes
# --------------------------------------------------------------------------- #
def test_div_by_zero_scalar_is_false_not_raise() -> None:
    # Scalar mode must not raise ZeroDivisionError; the comparison is NaN -> False.
    assert evaluate_expression("close / atr > 1", {"close": 5.0, "atr": 0.0}) is False
    assert evaluate_expression("close % atr > 1", {"close": 5.0, "atr": 0.0}) is False


def test_div_by_zero_series_is_false_not_inf() -> None:
    # Series mode must NOT yield inf -> True; it is NaN -> False. Bar 0 divides by
    # zero (-> False), bar 1 is 10/5 = 2 > 1 (-> True), proving non-zero still works.
    ctx = {"close": pd.Series([5.0, 10.0]), "atr": pd.Series([0.0, 5.0])}
    assert evaluate_expression("close / atr > 1", ctx).tolist() == [False, True]


def test_div_by_zero_scalar_series_parity() -> None:
    closes = [5.0, 5.0, 10.0]
    atrs = [0.0, 5.0, 2.0]
    expr = "close / atr > 1"
    scalar = [
        evaluate_expression(expr, {"close": c, "atr": a}) for c, a in zip(closes, atrs, strict=True)
    ]
    series = evaluate_expression(expr, {"close": pd.Series(closes), "atr": pd.Series(atrs)})
    assert scalar == series.tolist()
