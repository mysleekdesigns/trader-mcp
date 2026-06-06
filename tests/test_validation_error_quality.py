"""AI-friendliness of strategy validation errors (PRD §6 Phase 3 exit evidence).

The PRD requires "rich validation errors (what's wrong + how to fix)". This suite
asserts that property across the two error families ``validate_strategy`` /
``create_strategy`` surface:

    * Field-level (Pydantic) failures -> a PRECISE leaf ``location`` (e.g.
      ``indicators.0.kind``, ``grid.levels``, ``name``, ``exchange``) plus a
      tailored ``fix`` hint.
    * Cross-field (model-validator) failures -> ``location == "(root)"`` with the
      specifics carried in ``problem`` (unsupported timeframe, undefined indicator,
      non-whitelisted call, duplicate ids, missing grid block, rule type with no
      rules).

For every broken spec it asserts ``report.ok is False`` and that the relevant
:class:`ValidationIssue` has a non-empty ``location``, ``problem`` AND ``fix``. It
also asserts ``create_strategy`` raises ``trader_mcp.errors.ValidationError`` whose
``details["kind"] == "invalid_strategy_spec"`` carrying the same structured issues.
No network, no indicator computation -- pure validation, so this runs on 3.11 too.
"""

from __future__ import annotations

from typing import Any

import pytest

from trader_mcp.errors import ValidationError
from trader_mcp.strategy import (
    ValidationIssue,
    create_strategy,
    validate_strategy,
)

# A minimal valid skeleton; each case mutates it to break exactly one thing.
_BASE: dict[str, Any] = {
    "name": "probe",
    "symbol": "BTC/USD",
    "timeframe": "1h",
    "entry": {"long": "close > 0"},
}


def _spec(**overrides: Any) -> dict[str, Any]:
    return {**_BASE, **overrides}


def _assert_issue_actionable(issue: ValidationIssue) -> None:
    """Every issue must carry a non-empty location, problem AND fix."""
    assert issue.location, "issue.location must be non-empty"
    assert issue.problem, "issue.problem must be non-empty"
    assert issue.fix, "issue.fix must be non-empty"


# --------------------------------------------------------------------------- #
# Each broken spec: report.ok False + a matching, actionable issue.
#
# ``substr`` must appear in some issue's ``problem``; ``location`` (when given)
# must be that issue's exact location. ``location=None`` means a cross-field
# error reported at the model root.
# --------------------------------------------------------------------------- #
_CASES: list[tuple[str, dict[str, Any], str, str | None]] = [
    (
        "unsupported_timeframe",
        _spec(timeframe="3h"),
        "unsupported timeframe",
        "(root)",
    ),
    (
        "undefined_indicator_reference",
        _spec(entry={"long": "rsi < 30"}),  # no rsi indicator defined
        "unknown name 'rsi'",
        # A failing rule expression is reported at its precise side location (the
        # spec lifts the side out of the folded model-validator message).
        "entry.long",
    ),
    (
        "non_whitelisted_call",
        _spec(entry={"long": "eval(close)"}),
        "non-whitelisted function 'eval'",
        "entry.long",
    ),
    (
        "non_whitelisted_operator",
        _spec(entry={"long": "close ** 2 > 0"}),  # '**' is rejected
        "unsupported binary operator",
        "entry.long",
    ),
    (
        "duplicate_indicator_ids",
        _spec(
            indicators=[{"id": "x", "kind": "rsi"}, {"id": "x", "kind": "ema"}],
            entry={"long": "x < 30"},
        ),
        "duplicate indicator id",
        "(root)",
    ),
    (
        "grid_missing_block",
        _spec(strategy_type="grid", entry={"long": None}),
        "requires a 'grid' block",
        "(root)",
    ),
    (
        "rule_type_no_rules",
        _spec(strategy_type="rule", entry={"long": None}),
        "requires at least one entry/exit rule",
        "(root)",
    ),
    (
        "bad_indicator_kind",
        _spec(indicators=[{"id": "x", "kind": "not_a_real_kind"}]),
        "",  # pydantic literal message varies; location is the assertion here
        "indicators.0.kind",
    ),
    (
        "percent_equity_over_100",
        _spec(position_sizing={"mode": "percent_equity", "value": 150}),
        "",
        "position_sizing",
    ),
    (
        "empty_name",
        _spec(name=""),
        "",
        "name",
    ),
    (
        "unsupported_exchange",
        _spec(exchange="binance"),
        "",
        "exchange",
    ),
]


@pytest.mark.parametrize(
    ("case_id", "data", "substr", "location"), _CASES, ids=[c[0] for c in _CASES]
)
def test_validate_strategy_reports_actionable_issue(
    case_id: str, data: dict[str, Any], substr: str, location: str | None
) -> None:
    """validate_strategy never raises and returns an actionable failure report."""
    report = validate_strategy(data)
    assert report.ok is False, f"{case_id} should be invalid"
    assert report.issues, f"{case_id} must yield at least one issue"

    for issue in report.issues:
        _assert_issue_actionable(issue)

    # The expected problem text appears in some issue.
    if substr:
        assert any(substr in i.problem for i in report.issues), (
            f"{case_id}: no issue.problem contains {substr!r}; "
            f"got {[i.problem for i in report.issues]}"
        )

    # The error is reported at the expected location.
    if location is not None:
        locations = {i.location for i in report.issues}
        assert location in locations, (
            f"{case_id}: expected an issue at {location!r}; got {sorted(locations)}"
        )


@pytest.mark.parametrize(
    ("case_id", "data", "substr", "location"), _CASES, ids=[c[0] for c in _CASES]
)
def test_create_strategy_raises_with_same_structured_issues(
    case_id: str, data: dict[str, Any], substr: str, location: str | None
) -> None:
    """create_strategy raises a typed ValidationError carrying the same issues."""
    with pytest.raises(ValidationError) as excinfo:
        create_strategy(data)

    err = excinfo.value
    assert err.details["kind"] == "invalid_strategy_spec"

    issues = err.details["issues"]
    assert isinstance(issues, list), f"{case_id} issues must be a list"
    assert issues, f"{case_id} must carry issues"
    for issue in issues:
        assert issue["location"], f"{case_id}: empty location in {issue}"
        assert issue["problem"], f"{case_id}: empty problem in {issue}"
        assert issue["fix"], f"{case_id}: empty fix in {issue}"

    # The structured issues mirror the report from validate_strategy exactly.
    report = validate_strategy(data)
    assert issues == [i.model_dump() for i in report.issues]


def test_valid_spec_yields_clean_report_and_no_raise() -> None:
    """A well-formed spec validates ok and create_strategy returns the model."""
    report = validate_strategy(_BASE)
    assert report.ok is True
    assert report.issues == []
    spec = create_strategy(_BASE)
    assert spec.name == "probe"
