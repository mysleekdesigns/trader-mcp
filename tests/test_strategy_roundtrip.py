"""Cross-layer lifecycle tests: templates <-> spec validation <-> persistence.

``qa-parity-engineer`` owns these. Wave 1 unit-tested each layer in isolation
(``tests/strategy/**``) and Wave 2a tested the MCP tool wrappers
(``tests/test_strategy_*.py``). This suite proves the three PUBLIC layers AGREE
end-to-end through ``trader_mcp.strategy``'s public API only:

    build_from_template -> create_strategy/validate_strategy -> StrategyStore

For every shipped template it drives the full create/save/load/list/delete
lifecycle against a REAL on-disk store (a tmp data dir), asserting the spec
round-trips byte-equivalently and that the store's created/updated timestamp
semantics hold across a re-save. No network; no indicator computation (so this
runs on the 3.11 CI leg too).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trader_mcp.errors import ValidationError
from trader_mcp.strategy import (
    StrategySpec,
    StrategyStore,
    build_from_template,
    create_strategy,
    list_templates,
    validate_strategy,
)


@pytest.fixture
def store(tmp_path: Path) -> StrategyStore:
    """A real file-backed store rooted at an isolated tmp data dir."""
    return StrategyStore(tmp_path)


def _template_ids() -> list[str]:
    return [t.id for t in list_templates()]


# --------------------------------------------------------------------------- #
# list_templates advertises a stable, complete set
# --------------------------------------------------------------------------- #
def test_list_templates_exposes_seven_known_ids() -> None:
    """The seven curated templates are all present and uniquely identified."""
    ids = _template_ids()
    assert len(ids) == 7
    assert len(set(ids)) == 7
    assert set(ids) == {
        "ma_cross",
        "rsi_reversion",
        "donchian_break",
        "macd",
        "bollinger",
        "grid",
        "dca",
    }


# --------------------------------------------------------------------------- #
# Full lifecycle through the PUBLIC surface, for EVERY template
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("template_id", _template_ids())
def test_template_full_lifecycle_roundtrips(template_id: str, store: StrategyStore) -> None:
    """build_from_template -> create_strategy -> save -> load -> list -> delete.

    Asserts the three layers agree: a template is a valid spec, that spec
    survives the rich validation path, persists, and reloads byte-equivalent.
    """
    # 1. Template builds a fully-valid spec.
    spec = build_from_template(template_id)
    assert isinstance(spec, StrategySpec)

    # 2. The validator agrees the template is clean.
    report = validate_strategy(spec)
    assert report.ok is True
    assert report.issues == []

    # 3. The rich create path accepts the template's own dump unchanged.
    recreated = create_strategy(spec.model_dump())
    assert recreated == spec
    assert recreated.model_dump() == spec.model_dump()

    # 4. Persist; the listing summary mirrors the spec's identity.
    info = store.save(spec)
    assert info.name == spec.name
    assert info.exchange == spec.exchange
    assert info.symbol == spec.symbol
    assert info.timeframe == spec.timeframe
    assert info.strategy_type == spec.strategy_type
    assert info.schema_version == spec.schema_version

    # 5. Load round-trips byte-equivalent (the on-disk JSON re-validates to the
    #    same model dump -- persistence preserves every declared field).
    loaded = store.load(spec.name)
    assert loaded == spec
    assert loaded.model_dump() == spec.model_dump()

    # 6. The strategy shows up in the listing and reports existence.
    assert store.exists(spec.name) is True
    names = {i.name for i in store.list()}
    assert spec.name in names

    # 7. Delete removes it; a second delete is a no-op (returns False).
    assert store.delete(spec.name) is True
    assert store.exists(spec.name) is False
    assert store.delete(spec.name) is False
    assert spec.name not in {i.name for i in store.list()}


# --------------------------------------------------------------------------- #
# All seven templates coexist in one store and list deterministically
# --------------------------------------------------------------------------- #
def test_all_templates_persist_and_list_sorted(store: StrategyStore) -> None:
    """Saving every template yields a complete, name-sorted listing."""
    specs = [build_from_template(tid) for tid in _template_ids()]
    for spec in specs:
        store.save(spec)

    listing = store.list()
    assert len(listing) == len(specs)
    listed_names = [i.name for i in listing]
    assert listed_names == sorted(listed_names)
    assert set(listed_names) == {s.name for s in specs}


# --------------------------------------------------------------------------- #
# Timestamp semantics across a re-save: created preserved, updated advances
# --------------------------------------------------------------------------- #
def test_resave_preserves_created_advances_updated(store: StrategyStore) -> None:
    """On overwrite the store keeps ``created`` but refreshes ``updated``."""
    spec = build_from_template("ma_cross")

    first = store.save(spec)
    assert first.created is not None
    assert first.updated is not None
    # First save stamps created == updated (same instant).
    assert first.created == first.updated

    # Re-save the same strategy: created is preserved, updated moves forward.
    second = store.save(spec)
    assert second.created is not None
    assert second.updated is not None
    assert second.created == first.created
    assert second.updated >= first.updated
    assert second.updated >= second.created

    # The reloaded listing reflects the preserved created stamp.
    reloaded_info = next(i for i in store.list() if i.name == spec.name)
    assert reloaded_info.created == first.created


# --------------------------------------------------------------------------- #
# An override that changes identity persists under the NEW name/slug
# --------------------------------------------------------------------------- #
def test_template_override_persists_under_new_identity(store: StrategyStore) -> None:
    """A renamed template builds, validates and persists at its own slug."""
    spec = build_from_template(
        "rsi_reversion",
        overrides={"name": "My RSI Bot", "symbol": "ETH/USD"},
    )
    assert spec.name == "My RSI Bot"
    assert spec.symbol == "ETH/USD"

    store.save(spec)
    assert store.path_for("My RSI Bot").name == "my-rsi-bot.json"
    loaded = store.load("My RSI Bot")
    assert loaded == spec


# --------------------------------------------------------------------------- #
# load() of an absent strategy is a typed, AI-actionable error
# --------------------------------------------------------------------------- #
def test_load_missing_strategy_raises_typed_error(store: StrategyStore) -> None:
    """Loading a non-existent strategy raises a structured ValidationError."""
    with pytest.raises(ValidationError) as excinfo:
        store.load("does-not-exist")
    assert excinfo.value.details["kind"] == "strategy_not_found"
