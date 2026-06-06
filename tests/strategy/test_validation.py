"""Tests for the rich ValidationReport / create_strategy surface."""

from __future__ import annotations

import pytest

from trader_mcp.errors import ValidationError
from trader_mcp.strategy import (
    create_strategy,
    validate_strategy,
)
from trader_mcp.strategy.validation import ValidationIssue, ValidationReport

_GOOD = {
    "name": "rsi-btc",
    "symbol": "BTC/USD",
    "indicators": [{"id": "rsi", "kind": "rsi", "params": {"length": 14}}],
    "entry": {"long": "rsi < 30"},
    "exit": {"long": "rsi > 50"},
}


def test_validate_good_spec_ok() -> None:
    report = validate_strategy(_GOOD)
    assert isinstance(report, ValidationReport)
    assert report.ok is True
    assert report.issues == []


def test_validate_never_raises_on_bad_spec() -> None:
    report = validate_strategy({"name": "", "symbol": "", "timeframe": "2h"})
    assert report.ok is False
    assert len(report.issues) >= 1
    for issue in report.issues:
        assert isinstance(issue, ValidationIssue)
        assert issue.location
        assert issue.problem
        assert issue.fix


def test_validate_accepts_built_spec_roundtrip() -> None:
    spec = create_strategy(_GOOD)
    report = validate_strategy(spec)
    assert report.ok is True


def test_issue_for_bad_timeframe_has_actionable_fix() -> None:
    report = validate_strategy({**_GOOD, "timeframe": "2h"})
    assert report.ok is False
    tf_issues = [
        i
        for i in report.issues
        if "timeframe" in i.location.lower() or "timeframe" in i.problem.lower()
    ]
    assert tf_issues
    assert any("1h" in i.fix or "supported" in i.problem for i in tf_issues)


def test_bad_rule_reports_precise_side_location() -> None:
    # NIT #6: a failing entry/exit rule reports a precise location (entry.long /
    # exit.short), not "(root)" with the side folded into the message.
    report = validate_strategy(
        {
            "name": "x",
            "symbol": "BTC/USD",
            "indicators": [{"id": "rsi", "kind": "rsi"}],
            "entry": {"long": "rsi < 30"},
            "exit": {"short": "missing_name > 5"},
        }
    )
    assert report.ok is False
    rule_issues = [i for i in report.issues if i.location == "exit.short"]
    assert rule_issues, [i.location for i in report.issues]
    issue = rule_issues[0]
    assert "exit.short" not in issue.problem  # side lifted out of the message
    assert "missing_name" in issue.problem
    assert issue.fix


def test_bad_entry_long_rule_location() -> None:
    report = validate_strategy(
        {
            "name": "x",
            "symbol": "BTC/USD",
            "indicators": [{"id": "rsi", "kind": "rsi"}],
            "entry": {"long": "rsi.__class__"},
        }
    )
    assert report.ok is False
    assert any(i.location == "entry.long" for i in report.issues)


def test_create_strategy_returns_spec() -> None:
    spec = create_strategy(_GOOD)
    assert spec.name == "rsi-btc"
    assert spec.symbol == "BTC/USD"


def test_create_strategy_raises_rich_validation_error() -> None:
    with pytest.raises(ValidationError) as exc:
        create_strategy(
            {
                "name": "x",
                "symbol": "BTC/USD",
                "timeframe": "2h",
                "indicators": [{"id": "rsi", "kind": "rsi"}],
                "entry": {"long": "rsi < 30"},
            }
        )
    details = exc.value.details
    assert details["kind"] == "invalid_strategy_spec"
    assert isinstance(details["issues"], list)
    assert details["issues"]
    first = details["issues"][0]
    assert {"location", "problem", "fix"} <= set(first)


def test_create_strategy_reports_unsafe_rule() -> None:
    with pytest.raises(ValidationError) as exc:
        create_strategy(
            {
                "name": "x",
                "symbol": "BTC/USD",
                "indicators": [{"id": "rsi", "kind": "rsi"}],
                "entry": {"long": "rsi.__class__"},
            }
        )
    issues = exc.value.details["issues"]
    assert any(
        "attribute" in i["problem"].lower() or "rejected" in i["problem"].lower() for i in issues
    )
