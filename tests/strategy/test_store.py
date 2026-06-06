"""Persistence round-trip tests for the file-based StrategyStore.

Uses a tmp data dir via ``TRADER_MCP_DATA_DIR`` + ``get_settings.cache_clear()``
(the same pattern as ``tests/test_dataset_resources.py``), plus a direct
``data_dir`` construction to mirror ``tests/data/test_store.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trader_mcp.config import get_settings
from trader_mcp.strategy import StrategyStore, build_from_template
from trader_mcp.strategy.store import StrategyInfo, slugify


@pytest.fixture
def store(tmp_path: Path) -> StrategyStore:
    return StrategyStore(tmp_path)


def test_construction_is_cheap_no_filesystem(tmp_path: Path) -> None:
    sub = tmp_path / "nope"
    StrategyStore(sub)
    assert not (sub / "strategies").exists()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("RSI Mean Reversion", "rsi-mean-reversion"),
        ("ma_cross/btc", "ma-cross-btc"),
        ("  Spaced  Name  ", "spaced-name"),
    ],
)
def test_slugify(name: str, expected: str) -> None:
    assert slugify(name) == expected


def test_slugify_empty_rejected() -> None:
    from trader_mcp.errors import ValidationError

    with pytest.raises(ValidationError):
        slugify("///")


def test_save_and_load_roundtrip(store: StrategyStore) -> None:
    spec = build_from_template("macd")
    info = store.save(spec)
    assert isinstance(info, StrategyInfo)
    assert info.name == spec.name
    assert info.strategy_type == "rule"
    assert info.created is not None
    assert info.updated is not None
    assert store.exists(spec.name)

    loaded = store.load(spec.name)
    assert loaded == spec


def test_load_missing_raises(store: StrategyStore) -> None:
    from trader_mcp.errors import ValidationError

    with pytest.raises(ValidationError, match="No saved strategy"):
        store.load("does-not-exist")


def test_list_sorted(store: StrategyStore) -> None:
    store.save(build_from_template("rsi_reversion"))
    store.save(build_from_template("macd"))
    store.save(build_from_template("grid"))
    infos = store.list()
    names = [i.name for i in infos]
    assert names == sorted(names)
    assert len(infos) == 3


def test_list_empty(store: StrategyStore) -> None:
    assert store.list() == []


def test_delete(store: StrategyStore) -> None:
    spec = build_from_template("dca")
    store.save(spec)
    assert store.delete(spec.name) is True
    assert store.exists(spec.name) is False
    assert store.delete(spec.name) is False


def test_overwrite_preserves_created_refreshes_updated(store: StrategyStore) -> None:
    spec = build_from_template("rsi_reversion")
    first = store.save(spec)
    # Re-save with a tweak (same name -> overwrite).
    spec2 = build_from_template("rsi_reversion", {"description": "tweaked"})
    second = store.save(spec2)
    assert second.created == first.created
    assert second.updated is not None
    assert first.updated is not None
    assert second.updated >= first.updated


def test_grid_and_dca_persist(store: StrategyStore) -> None:
    for tid in ("grid", "dca"):
        spec = build_from_template(tid)
        store.save(spec)
        loaded = store.load(spec.name)
        assert loaded == spec


def test_store_uses_settings_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADER_MCP_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        store = StrategyStore()  # no explicit data_dir -> from settings
        spec = build_from_template("ma_cross")
        store.save(spec)
        assert (tmp_path / "strategies").is_dir()
        assert store.load(spec.name) == spec
    finally:
        get_settings.cache_clear()
