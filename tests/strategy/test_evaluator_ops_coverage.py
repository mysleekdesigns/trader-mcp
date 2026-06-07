"""Targeted coverage for the safe evaluator's operator + helper branches.

These exercise genuine, parity-relevant behaviour of the whitelisted evaluator:
unary minus / unary plus, addition, the ``<=`` / ``>=`` / ``!=`` comparisons, and the
binary ``min`` / ``max`` helpers -- in BOTH the scalar form (the live, per-bar path) and
the pandas-Series form (the backtest, vectorized path). The two paths must agree, which
is exactly the backtest<->live parity guarantee at the evaluation boundary.

This complements ``tests/strategy/test_evaluator.py`` (semantics) and
``tests/test_evaluator_sandbox_escape.py`` (security) without modifying them.
"""

from __future__ import annotations

import pandas as pd
import pytest

from trader_mcp.strategy.evaluator import evaluate_expression


def _scalar_ctx() -> dict[str, float]:
    return {"a": 3.0, "b": 5.0}


def _series_ctx() -> dict[str, pd.Series]:
    return {"a": pd.Series([3.0, 7.0, 5.0]), "b": pd.Series([5.0, 5.0, 5.0])}


# --------------------------------------------------------------------------- #
# Unary operators (USub / UAdd)
# --------------------------------------------------------------------------- #
def test_unary_minus_scalar() -> None:
    assert evaluate_expression("-a < b", {"a": 3.0, "b": 1.0}) is True


def test_unary_plus_scalar() -> None:
    # ``+a`` exercises the UAdd branch; bare arithmetic returns the number untouched.
    assert evaluate_expression("+a", {"a": 4.0}) == pytest.approx(4.0)


def test_unary_minus_series() -> None:
    result = evaluate_expression("-a < b", {"a": pd.Series([1.0, 2.0]), "b": pd.Series([0.0, 0.0])})
    assert list(result) == [True, True]


# --------------------------------------------------------------------------- #
# Addition (Add) -- scalar and Series
# --------------------------------------------------------------------------- #
def test_add_scalar() -> None:
    assert evaluate_expression("a + b > 7", _scalar_ctx()) is True


def test_add_series() -> None:
    result = evaluate_expression("a + b > 9", _series_ctx())
    assert list(result) == [False, True, True]


# --------------------------------------------------------------------------- #
# Comparison operators <= / >= / != (scalar + Series)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("a <= b", True),
        ("b <= a", False),
        ("a >= b", False),
        ("b >= a", True),
        ("a != b", True),
        ("a != a", False),
    ],
)
def test_comparisons_scalar(expr: str, expected: bool) -> None:
    assert evaluate_expression(expr, _scalar_ctx()) is expected


def test_le_series() -> None:
    assert list(evaluate_expression("a <= b", _series_ctx())) == [True, False, True]


def test_ge_series() -> None:
    assert list(evaluate_expression("a >= b", _series_ctx())) == [False, True, True]


def test_ne_series() -> None:
    assert list(evaluate_expression("a != b", _series_ctx())) == [True, True, False]


# --------------------------------------------------------------------------- #
# min / max helpers -- scalar, Series-first, and scalar-first/Series-second
# --------------------------------------------------------------------------- #
def test_min_max_scalar() -> None:
    ctx = _scalar_ctx()
    assert evaluate_expression("min(a, b) == 3", ctx) is True
    assert evaluate_expression("max(a, b) == 5", ctx) is True


def test_min_max_series_first() -> None:
    ctx = _series_ctx()
    assert list(evaluate_expression("min(a, b) <= 5", ctx)) == [True, True, True]
    assert list(evaluate_expression("max(a, b) >= 5", ctx)) == [True, True, True]


def test_min_max_scalar_first_series_second() -> None:
    # First arg scalar, second a Series -> exercises the ``where_b`` fallthrough in
    # ``_emin`` / ``_emax`` (the scalar has no ``.where`` so the Series drives it).
    ctx = {"b": pd.Series([3.0, 7.0, 5.0])}
    assert list(evaluate_expression("min(4, b) == 3", ctx)) == [True, False, False]
    assert list(evaluate_expression("max(4, b) == 7", ctx)) == [False, True, False]


# --------------------------------------------------------------------------- #
# Parity: the scalar path and the per-element Series path agree
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "expr",
    [
        "a <= b",
        "a >= b",
        "a != b",
        "min(a, b) >= 4",
        "max(a, b) <= 6",
        "-a + b > 0",
    ],
)
def test_scalar_matches_series_elementwise(expr: str) -> None:
    """Evaluating bar-by-bar (scalars) equals the vectorized Series evaluation."""
    a = [3.0, 7.0, 5.0]
    b = [5.0, 5.0, 5.0]
    series_result = list(evaluate_expression(expr, {"a": pd.Series(a), "b": pd.Series(b)}))
    scalar_result = [
        bool(evaluate_expression(expr, {"a": av, "b": bv})) for av, bv in zip(a, b, strict=True)
    ]
    assert [bool(x) for x in series_result] == scalar_result
