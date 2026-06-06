"""Template library tests: all seven build, validate, and accept overrides."""

from __future__ import annotations

import pytest

from trader_mcp.errors import ValidationError
from trader_mcp.strategy import (
    build_from_template,
    list_templates,
    validate_strategy,
)

_EXPECTED_IDS = {
    "ma_cross",
    "rsi_reversion",
    "donchian_break",
    "macd",
    "bollinger",
    "grid",
    "dca",
}


def test_seven_templates_listed() -> None:
    infos = list_templates()
    assert {t.id for t in infos} == _EXPECTED_IDS
    assert len(infos) == 7
    for t in infos:
        assert t.title
        assert t.summary
        assert t.overridable


@pytest.mark.parametrize("template_id", sorted(_EXPECTED_IDS))
def test_each_template_builds_and_validates(template_id: str) -> None:
    spec = build_from_template(template_id)
    report = validate_strategy(spec)
    assert report.ok, report.issues


def test_grid_and_dca_have_correct_type() -> None:
    assert build_from_template("grid").strategy_type == "grid"
    assert build_from_template("dca").strategy_type == "dca"
    assert build_from_template("rsi_reversion").strategy_type == "rule"


def test_unknown_template_lists_valid_ids() -> None:
    with pytest.raises(ValidationError) as exc:
        build_from_template("nope")
    assert set(exc.value.details["valid"]) == _EXPECTED_IDS


def test_override_top_level_field() -> None:
    spec = build_from_template("rsi_reversion", {"symbol": "ETH/USD", "timeframe": "4h"})
    assert spec.symbol == "ETH/USD"
    assert spec.timeframe == "4h"


def test_override_revalidates_and_rejects_bad_value() -> None:
    with pytest.raises(ValidationError, match="invalid"):
        build_from_template("rsi_reversion", {"timeframe": "2h"})


def test_override_rejects_unsafe_rule() -> None:
    with pytest.raises(ValidationError):
        build_from_template("rsi_reversion", {"entry": {"long": "rsi.__class__ < 1"}})


def test_override_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError, match="Unknown override"):
        build_from_template("rsi_reversion", {"bogus": 1})


def test_advertised_overridable_matches_accepted_set() -> None:
    # NIT #5: every advertised override field must actually be accepted (no drift).
    advertised = set(list_templates()[0].overridable)
    assert "strategy_type" in advertised  # now both advertised AND accepted
    assert "schema_version" not in advertised  # owned by the model; never overridable
    # A field that is advertised must not be rejected as unknown. Re-applying each
    # field's own current value is a no-op that proves it is accepted.
    base = build_from_template("rsi_reversion")
    payload = base.model_dump()
    for field in advertised:
        build_from_template("rsi_reversion", {field: payload[field]})


def test_schema_version_override_rejected() -> None:
    with pytest.raises(ValidationError, match="Unknown override"):
        build_from_template("rsi_reversion", {"schema_version": "9.9"})
