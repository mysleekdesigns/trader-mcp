"""Spec model + cross-field validation tests (good specs build, bad specs fail)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from trader_mcp.strategy.spec import (
    SCHEMA_VERSION,
    DCAConfig,
    EntryRules,
    ExitRules,
    GridConfig,
    IndicatorSpec,
    PositionSizing,
    StrategySpec,
)


def _rule_spec(**overrides: object) -> StrategySpec:
    base: dict[str, object] = {
        "name": "test",
        "symbol": "BTC/USD",
        "indicators": [IndicatorSpec(id="rsi", kind="rsi")],
        "entry": EntryRules(long="rsi < 30"),
        "exit": ExitRules(long="rsi > 50"),
    }
    base.update(overrides)
    return StrategySpec(**base)  # type: ignore[arg-type]


def test_minimal_rule_spec_builds() -> None:
    spec = _rule_spec()
    assert spec.schema_version == SCHEMA_VERSION
    assert spec.exchange == "coinbase"
    assert spec.timeframe == "1h"
    assert spec.strategy_type == "rule"


def test_spec_is_frozen_and_forbids_extra() -> None:
    spec = _rule_spec()
    with pytest.raises(PydanticValidationError):
        spec.name = "x"  # type: ignore[misc]
    with pytest.raises(PydanticValidationError):
        StrategySpec.model_validate(
            {"name": "t", "symbol": "BTC/USD", "entry": {"long": "close > 0"}, "bogus": 1}
        )


def test_unsupported_timeframe_rejected() -> None:
    with pytest.raises(PydanticValidationError, match="unsupported timeframe"):
        _rule_spec(timeframe="2h")


def test_duplicate_indicator_ids_rejected() -> None:
    with pytest.raises(PydanticValidationError, match="duplicate indicator id"):
        StrategySpec(
            name="dup",
            symbol="BTC/USD",
            indicators=[IndicatorSpec(id="x", kind="sma"), IndicatorSpec(id="x", kind="ema")],
            entry=EntryRules(long="x > 0"),
        )


def test_rule_referencing_unknown_name_rejected() -> None:
    with pytest.raises(PydanticValidationError, match="unknown name"):
        _rule_spec(entry=EntryRules(long="not_a_thing < 30"))


def test_rule_with_unsafe_construct_rejected() -> None:
    with pytest.raises(PydanticValidationError, match="attribute access"):
        _rule_spec(entry=EntryRules(long="rsi.real < 30"))


def test_rule_references_indicator_output_names() -> None:
    # macd contributes m, m_signal, m_hist.
    spec = StrategySpec(
        name="macd",
        symbol="BTC/USD",
        indicators=[IndicatorSpec(id="m", kind="macd")],
        entry=EntryRules(long="crossover(m, m_signal)"),
        exit=ExitRules(long="m_hist < 0"),
    )
    assert any(i.id == "m" for i in spec.indicators)


def test_rule_can_reference_ohlcv_columns() -> None:
    spec = StrategySpec(
        name="px",
        symbol="BTC/USD",
        indicators=[IndicatorSpec(id="dc", kind="donchian")],
        entry=EntryRules(long="close > dc_upper"),
    )
    assert spec.entry.long == "close > dc_upper"


def test_rule_type_requires_a_rule() -> None:
    with pytest.raises(PydanticValidationError, match="requires at least one"):
        StrategySpec(
            name="empty", symbol="BTC/USD", indicators=[IndicatorSpec(id="rsi", kind="rsi")]
        )


def test_unknown_indicator_param_rejected() -> None:
    with pytest.raises(PydanticValidationError, match="unknown param"):
        IndicatorSpec(id="r", kind="rsi", params={"nope": 5})


def test_non_integer_length_param_rejected() -> None:
    with pytest.raises(PydanticValidationError, match="positive integer"):
        IndicatorSpec(id="r", kind="rsi", params={"length": 14.5})


def test_indicator_id_must_be_identifier() -> None:
    with pytest.raises(PydanticValidationError, match="valid identifier"):
        IndicatorSpec(id="bad id", kind="rsi")


def test_resolved_params_merges_defaults() -> None:
    ind = IndicatorSpec(id="m", kind="macd", params={"fast": 8})
    resolved = ind.resolved_params()
    assert resolved == {"fast": 8.0, "slow": 26.0, "signal": 9.0}


def test_position_sizing_percent_cap() -> None:
    with pytest.raises(PydanticValidationError, match="<= 100"):
        PositionSizing(mode="percent_equity", value=150)


# --------------------------------------------------------------------------- #
# Grid / DCA discriminator
# --------------------------------------------------------------------------- #
def test_grid_spec_builds_without_rules() -> None:
    spec = StrategySpec(
        name="grid",
        symbol="BTC/USD",
        strategy_type="grid",
        grid=GridConfig(lower=10.0, upper=20.0, levels=5),
    )
    assert spec.grid is not None
    assert spec.grid.levels == 5


def test_grid_type_requires_grid_block() -> None:
    with pytest.raises(PydanticValidationError, match="requires a 'grid' block"):
        StrategySpec(name="g", symbol="BTC/USD", strategy_type="grid")


def test_grid_must_not_have_dca_block() -> None:
    with pytest.raises(PydanticValidationError, match="must not include a 'dca'"):
        StrategySpec(
            name="g",
            symbol="BTC/USD",
            strategy_type="grid",
            grid=GridConfig(lower=1.0, upper=2.0, levels=3),
            dca=DCAConfig(amount_quote=10.0, interval_bars=1),
        )


def test_grid_bounds_validated() -> None:
    with pytest.raises(PydanticValidationError, match="greater than 'lower'"):
        GridConfig(lower=20.0, upper=10.0, levels=5)


def test_dca_spec_builds() -> None:
    spec = StrategySpec(
        name="dca",
        symbol="BTC/USD",
        strategy_type="dca",
        dca=DCAConfig(amount_quote=50.0, interval_bars=24),
    )
    assert spec.dca is not None
    assert spec.dca.interval_bars == 24


def test_dca_type_requires_dca_block() -> None:
    with pytest.raises(PydanticValidationError, match="requires a 'dca' block"):
        StrategySpec(name="d", symbol="BTC/USD", strategy_type="dca")


def test_rule_type_must_not_have_grid_block() -> None:
    with pytest.raises(PydanticValidationError, match="must not include a 'grid'/'dca'"):
        StrategySpec(
            name="r",
            symbol="BTC/USD",
            strategy_type="rule",
            entry=EntryRules(long="close > 0"),
            grid=GridConfig(lower=1.0, upper=2.0, levels=3),
        )


def test_json_schema_generation() -> None:
    from trader_mcp.strategy.spec import spec_json_schema

    schema = spec_json_schema()
    assert schema["type"] == "object"
    assert "strategy_type" in schema["properties"]
