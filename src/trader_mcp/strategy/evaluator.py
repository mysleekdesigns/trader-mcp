"""Safe, sandboxed expression evaluator for strategy rules (the SECURITY boundary).

INVARIANT: strategy specs are **data, never code**. A rule string like
``"rsi < 30 and crossover(close, sma)"`` is parsed with :func:`ast.parse` in
``eval`` mode and then walked node-by-node against a strict whitelist. Anything not
explicitly allowed is hard-rejected with an actionable message. This is a
**default-deny** allowlist: an unrecognized AST node type is rejected, not ignored.

What is allowed (and nothing else):
    * boolean ops:        ``and``, ``or``
    * unary ops:          ``not``, unary ``+``, unary ``-``
    * binary arithmetic:  ``+ - * / %`` (``/`` and ``%`` by zero -> NaN -> False,
                          in BOTH scalar and Series mode -- no parity divergence)
    * comparisons:        ``< <= > >= == !=`` (incl. chained, e.g. ``30 < rsi < 70``)
    * names:              ONLY those in the caller-supplied ``allowed_names`` set
                          (OHLCV columns + the spec's indicator outputs)
    * constants:          numbers and booleans (``True``/``False``/``None``)
    * calls:              ONLY the whitelisted helpers below, each with its EXACT
                          arity: ``crossover(a, b)``, ``crossunder(a, b)``,
                          ``abs(x)``, ``min(a, b)``, ``max(a, b)``. ``min``/``max``
                          are the ELEMENT-WISE BINARY forms only -- a single-arg
                          ``min(close)`` is rejected (it would reduce over the whole
                          column, leaking future bars and breaking scalar/Series
                          parity).

Hard-rejected (each with a clear "what + how to fix" message):
    attribute access (``x.y``), subscripting (``x[0]``), comprehensions, lambdas,
    assignments / walrus (``:=``), f-strings / joined strings, starred args,
    keyword args, dunder names, imports, unknown function calls, and any name not
    in the whitelist. There is no ``eval``/``exec``, no ``__builtins__``, no
    attribute traversal -- so there is no escape to arbitrary Python.

Evaluation model (one expression, two execution modes -- SAME code):
    The same :func:`evaluate_expression` call works whether the context maps names
    to scalars (a single bar -- the **live** path) or to equal-length pandas
    Series (a vectorized column -- the **backtest** path). Operators and the
    ``crossover``/``crossunder`` helpers are written to behave identically in both:
    scalars yield a ``bool``; Series yield a boolean Series. The Phase 4
    interpreter will use the **Series** form to precompute signals over a cached
    window, and may use the **scalar** form per-bar on a streaming feed; because
    the arithmetic is identical, backtest and live agree.

``crossover(a, b)`` / ``crossunder(a, b)`` semantics (precise):
    * ``crossover(a, b)`` is True on bar *t* when ``a`` was ``<= b`` on bar *t-1*
      and ``a`` is ``> b`` on bar *t`` (a crosses strictly above b *this* bar).
    * ``crossunder(a, b)`` is True on bar *t* when ``a`` was ``>= b`` on bar *t-1*
      and ``a`` is ``< b`` on bar *t`` (a crosses strictly below b *this* bar).
    The first bar is never a crossover/crossunder (no prior bar). In Series mode
    the result is False at the first index. In scalar mode the helper needs the
    previous value; the live interpreter supplies it by evaluating over a small
    trailing window (the documented contract), so a bare scalar context that lacks
    history yields False for the first bar -- never a spurious signal.

``pandas`` is imported lazily inside the runtime evaluator so static validation
(the part the spec validator always runs) never imports it.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Any

from trader_mcp.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Whitelisted helper functions -> their EXACT required positional arity. ``min``
#: and ``max`` are the element-wise BINARY forms only (``min(a, b)`` / ``max(a, b)``);
#: a single-arg ``min(close)`` is rejected because it would reduce over the whole
#: column -- leaking future bars (lookahead) and diverging from scalar mode (where
#: ``min`` of one scalar raises). This map is the single source of truth for both
#: the whitelist membership and the arity check.
_HELPER_ARITY: dict[str, int] = {
    "crossover": 2,
    "crossunder": 2,
    "abs": 1,
    "min": 2,
    "max": 2,
}

#: Whitelisted helper function names callable from a rule expression. These are
#: the ONLY callables; any other ``Call`` node is rejected.
ALLOWED_FUNCTIONS: frozenset[str] = frozenset(_HELPER_ARITY)

#: AST binary operators we permit (arithmetic only -- no bitwise/shift/pow).
_ALLOWED_BINOPS: tuple[type[ast.operator], ...] = (
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
)

#: AST unary operators we permit (logical not + numeric sign; no bitwise invert).
_ALLOWED_UNARYOPS: tuple[type[ast.unaryop], ...] = (
    ast.Not,
    ast.UAdd,
    ast.USub,
)

#: AST comparison operators we permit.
_ALLOWED_COMPARES: tuple[type[ast.cmpop], ...] = (
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Eq,
    ast.NotEq,
)


def _reject(problem: str, fix: str, *, expr: str) -> ValidationError:
    """Build a rich :class:`ValidationError` for a rejected expression construct."""
    return ValidationError(
        f"Rejected rule expression: {problem} Fix: {fix}",
        details={
            "kind": "unsafe_expression",
            "problem": problem,
            "fix": fix,
            "expression": expr,
        },
    )


def _validate_node(node: ast.AST, allowed_names: set[str], expr: str) -> None:
    """Recursively assert ``node`` is on the whitelist; raise otherwise.

    Default-deny: the final ``else`` rejects any node type not explicitly handled,
    so new/exotic Python syntax cannot slip through.
    """
    if isinstance(node, ast.Expression):
        _validate_node(node.body, allowed_names, expr)
        return

    if isinstance(node, ast.BoolOp):
        if not isinstance(node.op, (ast.And, ast.Or)):  # pragma: no cover - defensive
            raise _reject("an unsupported boolean operator.", "use only 'and'/'or'.", expr=expr)
        for value in node.values:
            _validate_node(value, allowed_names, expr)
        return

    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _ALLOWED_UNARYOPS):
            raise _reject(
                "an unsupported unary operator.",
                "use only 'not', unary '+' or unary '-'.",
                expr=expr,
            )
        _validate_node(node.operand, allowed_names, expr)
        return

    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _ALLOWED_BINOPS):
            raise _reject(
                "an unsupported binary operator (e.g. '**', '//', bitwise/shift).",
                "use only '+', '-', '*', '/', '%'.",
                expr=expr,
            )
        _validate_node(node.left, allowed_names, expr)
        _validate_node(node.right, allowed_names, expr)
        return

    if isinstance(node, ast.Compare):
        for op in node.ops:
            if not isinstance(op, _ALLOWED_COMPARES):
                raise _reject(
                    "an unsupported comparison operator (e.g. 'is', 'in').",
                    "use only <, <=, >, >=, ==, !=.",
                    expr=expr,
                )
        _validate_node(node.left, allowed_names, expr)
        for comparator in node.comparators:
            _validate_node(comparator, allowed_names, expr)
        return

    if isinstance(node, ast.Name):
        name = node.id
        if name.startswith("__"):
            raise _reject(
                f"a dunder name ({name!r}) is forbidden.",
                "reference only indicator outputs and OHLCV columns.",
                expr=expr,
            )
        if name not in allowed_names:
            available = ", ".join(sorted(allowed_names)) or "(none)"
            raise _reject(
                f"unknown name {name!r}.",
                f"reference only a defined indicator output or OHLCV column "
                f"(available: {available}).",
                expr=expr,
            )
        return

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, bool)) or node.value is None:
            return
        raise _reject(
            f"an unsupported constant of type {type(node.value).__name__} "
            "(strings/bytes are not allowed).",
            "use only numbers and booleans.",
            expr=expr,
        )

    if isinstance(node, ast.Call):
        _validate_call(node, allowed_names, expr)
        return

    # ---- everything below is an explicit, named rejection (better messages) ----
    if isinstance(node, ast.Attribute):
        raise _reject(
            "attribute access (e.g. 'x.y') is forbidden.",
            "reference indicator outputs directly by name, no dotted access.",
            expr=expr,
        )
    if isinstance(node, ast.Subscript):
        raise _reject(
            "subscripting (e.g. 'x[0]') is forbidden.",
            "use indicator outputs and the crossover()/crossunder() helpers.",
            expr=expr,
        )
    if isinstance(node, (ast.Lambda, ast.FunctionDef)):
        raise _reject(
            "defining functions/lambdas is forbidden.",
            "compose rules from whitelisted operators and helpers only.",
            expr=expr,
        )
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        raise _reject(
            "comprehensions/generators are forbidden.",
            "express the rule as a flat boolean expression.",
            expr=expr,
        )
    if isinstance(node, ast.NamedExpr):
        raise _reject(
            "the walrus operator ':=' is forbidden.",
            "rules are expressions only; no assignment.",
            expr=expr,
        )
    if isinstance(node, ast.JoinedStr):
        raise _reject(
            "f-strings are forbidden.",
            "rules contain no strings; compare numbers/indicators.",
            expr=expr,
        )
    if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
        raise _reject(
            "collection literals are forbidden.",
            "compare scalar indicator values, not collections.",
            expr=expr,
        )
    if isinstance(node, ast.Starred):
        raise _reject(
            "starred/unpacking arguments are forbidden.",
            "pass exactly the positional args a helper expects.",
            expr=expr,
        )
    if isinstance(node, (ast.IfExp,)):
        raise _reject(
            "conditional expressions ('a if c else b') are forbidden.",
            "use boolean operators (and/or/not) to combine conditions.",
            expr=expr,
        )

    raise _reject(
        f"an unsupported syntax element ({type(node).__name__}).",
        "use only whitelisted operators, names, numbers and helper calls.",
        expr=expr,
    )


def _validate_call(node: ast.Call, allowed_names: set[str], expr: str) -> None:
    """Validate a function call: only whitelisted helpers, positional args only."""
    func = node.func
    if not isinstance(func, ast.Name):
        raise _reject(
            "an indirect/computed call target is forbidden.",
            "call only the bare helpers: crossover, crossunder, abs, min, max.",
            expr=expr,
        )
    if func.id not in ALLOWED_FUNCTIONS:
        raise _reject(
            f"call to non-whitelisted function {func.id!r}.",
            f"call only: {', '.join(sorted(ALLOWED_FUNCTIONS))}.",
            expr=expr,
        )
    if node.keywords:
        raise _reject(
            "keyword arguments are forbidden in helper calls.",
            "pass positional arguments only.",
            expr=expr,
        )
    expected = _HELPER_ARITY[func.id]
    if len(node.args) != expected:
        if expected == 1:
            raise _reject(
                f"{func.id}() takes exactly 1 argument.",
                f"call {func.id}(x) with a single series/value.",
                expr=expr,
            )
        raise _reject(
            f"{func.id}() takes exactly 2 arguments.",
            f"call {func.id}(a, b) -- it is the element-wise binary form only "
            f"(e.g. {func.id}(close, sma)); reducing over a whole column is not "
            "allowed (it would leak future bars and break scalar/Series parity).",
            expr=expr,
        )
    for arg in node.args:
        _validate_node(arg, allowed_names, expr)


def validate_expression(expr: str, allowed_names: set[str]) -> None:
    """Statically validate ``expr`` against the whitelist; raise if unsafe/invalid.

    This is a pure static check (no execution, no pandas import). It is the gate
    the strategy-spec validator runs on every entry/exit rule before a spec is
    accepted. An empty/blank expression is rejected (use ``None`` to mean "no
    rule" at the spec level, not an empty string).

    Args:
        expr: The rule expression source, e.g. ``"rsi < 30"``.
        allowed_names: The set of names the expression may reference (OHLCV columns
            plus the spec's indicator output names).

    Raises:
        trader_mcp.errors.ValidationError: with a structured ``details`` payload
            (``problem`` + ``fix``) describing exactly what is wrong.
    """
    if not expr or not expr.strip():
        raise _reject(
            "the expression is empty.",
            "provide a boolean rule, or omit the rule entirely (set it to null).",
            expr=expr,
        )
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise _reject(
            f"it is not valid syntax ({exc.msg}).",
            "write a single boolean expression, e.g. 'rsi < 30 and close > sma'.",
            expr=expr,
        ) from None
    _validate_node(tree, allowed_names, expr)


# --------------------------------------------------------------------------- #
# Runtime helpers (work on scalars AND pandas Series, identically)
# --------------------------------------------------------------------------- #
def _shift_prev(value: Any) -> Any:
    """Return the previous-bar view of ``value`` for crossover detection.

    For a pandas Series, this is ``.shift(1)`` (previous bar aligned to current
    index, NaN at the first bar). For a scalar there is no history available in a
    bare context, so the previous value is treated as equal to the current value
    -- which makes ``crossover``/``crossunder`` False (no transition). The live
    interpreter supplies real history by evaluating over a trailing window.
    """
    shift = getattr(value, "shift", None)
    if callable(shift):
        return shift(1)
    return value


def crossover(a: Any, b: Any) -> Any:
    """True when ``a`` crosses strictly above ``b`` on the current bar.

    Defined as ``a_prev <= b_prev`` and ``a > b``. Works for scalars (returns
    ``bool``) and for aligned pandas Series (returns a boolean Series, False at the
    first bar). See module docstring for the precise contract.
    """
    a_prev = _shift_prev(a)
    b_prev = _shift_prev(b)
    result = (a_prev <= b_prev) & (a > b)
    return _coerce_bool(result, a, b)


def crossunder(a: Any, b: Any) -> Any:
    """True when ``a`` crosses strictly below ``b`` on the current bar.

    Defined as ``a_prev >= b_prev`` and ``a < b``. Scalar -> ``bool``; Series ->
    boolean Series (False at the first bar).
    """
    a_prev = _shift_prev(a)
    b_prev = _shift_prev(b)
    result = (a_prev >= b_prev) & (a < b)
    return _coerce_bool(result, a, b)


def _coerce_bool(result: Any, a: Any, b: Any) -> Any:
    """Normalize a crossover result: fill NaN (first bar / Series mode) with False.

    In Series mode the shifted first element is NaN, so the comparison yields NaN;
    we fill it with False so the first bar is never a signal. In scalar mode the
    result is already a plain bool.
    """
    fillna = getattr(result, "fillna", None)
    if callable(fillna):
        filled: Any = fillna(False)
        astype = getattr(filled, "astype", None)
        return astype(bool) if callable(astype) else filled
    return bool(result)


def _logical_not(operand: Any) -> Any:
    """Apply logical NOT identically in scalar (live) and Series (backtest) modes.

    PARITY INVARIANT: a Python ``bool`` is an ``int`` subclass and therefore has
    ``__invert__``, so naively using ``~`` would turn scalar ``not (rsi > 50)``
    into ``~False == -1`` (an int) while the Series path returns a proper boolean
    -- a backtest<->live divergence. We invert with pandas' elementwise ``~`` ONLY
    for a real :class:`pandas.Series`; every scalar uses Python ``not`` and returns
    a genuine ``bool``. The pandas import stays lazy: a plain scalar never imports
    it, and a Series operand means pandas is already loaded.
    """
    if not isinstance(operand, (bool, int, float)):
        import pandas as pd

        if isinstance(operand, pd.Series):
            return ~operand
    return not operand


def _eval_node(node: ast.AST, context: Mapping[str, Any]) -> Any:
    """Evaluate a pre-validated AST node against ``context`` (scalars or Series).

    Assumes :func:`validate_expression` has already run, so only whitelisted node
    types appear. Kept tiny and explicit -- there is no fallthrough to ``eval``.
    """
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, context)
    if isinstance(node, ast.BoolOp):
        values = [_eval_node(v, context) for v in node.values]
        result = values[0]
        for nxt in values[1:]:
            result = (result & nxt) if isinstance(node.op, ast.And) else (result | nxt)
        return result
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, context)
        if isinstance(node.op, ast.Not):
            return _logical_not(operand)
        if isinstance(node.op, ast.USub):
            return -operand
        return +operand
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, context)
        right = _eval_node(node.right, context)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return _safe_div(left, right)
        return _safe_mod(left, right)
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, context)
        result: Any = None
        for op, comparator_node in zip(node.ops, node.comparators, strict=True):
            right = _eval_node(comparator_node, context)
            cmp = _apply_compare(op, left, right)
            result = cmp if result is None else (result & cmp)
            left = right
        return result
    if isinstance(node, ast.Name):
        return context[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Call):
        return _eval_call(node, context)
    # Unreachable after validation; defensive guard keeps the boundary closed.
    raise _reject(  # pragma: no cover - defensive
        f"an unsupported syntax element ({type(node).__name__}).",
        "this should have been caught by validation.",
        expr=ast.dump(node),
    )


def _is_series(value: Any) -> bool:
    """True if ``value`` is a pandas Series (without forcing pandas import scalars)."""
    if isinstance(value, (bool, int, float)):
        return False
    import pandas as pd

    return isinstance(value, pd.Series)


def _safe_div(left: Any, right: Any) -> Any:
    """Division with identical scalar/Series semantics: divide-by-zero -> NaN.

    PARITY INVARIANT: scalar ``x / 0`` raises ``ZeroDivisionError`` while a pandas
    Series yields ``inf``; either way a downstream comparison would diverge. We
    normalize BOTH paths to NaN on a zero (or NaN) divisor, so the eventual
    comparison is NaN -> ``False`` in scalar and Series mode alike (no signal on an
    undefined ratio).
    """
    import math

    if _is_series(left) or _is_series(right):
        import numpy as np
        import pandas as pd

        right_s = (
            right.replace(0, np.nan) if _is_series(right) else (np.nan if right == 0 else right)
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            result = left / right_s
        return result if isinstance(result, pd.Series) else pd.Series(result)
    if right == 0 or (isinstance(right, float) and math.isnan(right)):
        return math.nan
    return left / right


def _safe_mod(left: Any, right: Any) -> Any:
    """Modulo with identical scalar/Series semantics: mod-by-zero -> NaN (see _safe_div)."""
    import math

    if _is_series(left) or _is_series(right):
        import numpy as np
        import pandas as pd

        right_s = (
            right.replace(0, np.nan) if _is_series(right) else (np.nan if right == 0 else right)
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            result = left % right_s
        return result if isinstance(result, pd.Series) else pd.Series(result)
    if right == 0 or (isinstance(right, float) and math.isnan(right)):
        return math.nan
    return left % right


def _apply_compare(op: ast.cmpop, left: Any, right: Any) -> Any:
    if isinstance(op, ast.Lt):
        return left < right
    if isinstance(op, ast.LtE):
        return left <= right
    if isinstance(op, ast.Gt):
        return left > right
    if isinstance(op, ast.GtE):
        return left >= right
    if isinstance(op, ast.Eq):
        return left == right
    return left != right


def _emin(a: Any, b: Any) -> Any:
    """Element-wise minimum of two scalars/Series (parity-safe binary form).

    For pandas Series this is element-wise (``a.where(a <= b, b)``); for scalars it
    is the ordinary ``min(a, b)``. Never reduces a column, so there is no lookahead.
    """
    where = getattr(a, "where", None)
    if callable(where):
        return where(a <= b, b)
    where_b = getattr(b, "where", None)
    if callable(where_b):
        return where_b(b <= a, a)
    return min(a, b)


def _emax(a: Any, b: Any) -> Any:
    """Element-wise maximum of two scalars/Series (parity-safe binary form)."""
    where = getattr(a, "where", None)
    if callable(where):
        return where(a >= b, b)
    where_b = getattr(b, "where", None)
    if callable(where_b):
        return where_b(b >= a, a)
    return max(a, b)


_HELPER_IMPLS = {
    "crossover": crossover,
    "crossunder": crossunder,
    "abs": abs,
    "min": _emin,
    "max": _emax,
}


def _eval_call(node: ast.Call, context: Mapping[str, Any]) -> Any:
    func = node.func
    assert isinstance(func, ast.Name)  # guaranteed by validation
    impl = _HELPER_IMPLS[func.id]
    args = [_eval_node(arg, context) for arg in node.args]
    return impl(*args)


def evaluate_expression(expr: str, context: Mapping[str, Any]) -> Any:
    """Evaluate a rule ``expr`` against ``context`` and return the result.

    The result is a ``bool`` when ``context`` holds scalars (live, single bar) or a
    boolean ``pandas.Series`` when ``context`` holds Series (backtest, vectorized).
    The expression is validated first (so this is always safe to call), using the
    context keys as the allowed-name set.

    Args:
        expr: The rule expression source.
        context: Mapping of allowed names -> scalar values or equal-length Series.

    Returns:
        The evaluated value (boolean for a comparison/boolean rule).

    Raises:
        trader_mcp.errors.ValidationError: if ``expr`` is unsafe or references a
            name absent from ``context``.
    """
    validate_expression(expr, set(context.keys()))
    tree = ast.parse(expr, mode="eval")
    result = _eval_node(tree, context)
    return _finalize_scalar(result)


def _finalize_scalar(result: Any) -> Any:
    """Coerce a scalar (non-Series) boolean result to a real Python ``bool``.

    The Phase 4 live interpreter feeds per-bar values via ``df.iloc[i]["close"]``,
    which are ``numpy`` scalars (``np.float64``), so a comparison returns
    ``numpy.bool_`` rather than ``bool``. Series results are returned untouched
    (the backtest path expects a boolean ``pandas.Series``); any non-boolean scalar
    (e.g. a bare arithmetic value) is also returned untouched.
    """
    if isinstance(result, bool):
        return result
    if isinstance(result, (int, float)):
        # A plain Python number from a bare arithmetic expression; leave as-is.
        return result
    import numpy as np

    if isinstance(result, np.bool_):
        return bool(result)
    return result
