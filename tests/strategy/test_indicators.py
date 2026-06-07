"""Indicator registry + computation correctness tests.

Correctness is checked against direct ``pandas-ta`` calls (the engine of record)
and against the output-naming convention the spec validator and Phase 4
interpreter both rely on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

# pandas-ta is 3.12-only (see pyproject marker); on the 3.11 CI leg the indicator
# engine is absent, so the whole module (which eagerly imports the pandas-ta shim
# and computes indicators) skips cleanly rather than erroring at collection.
pytest.importorskip("pandas_ta")

from trader_mcp.exchanges.models import OHLCVBar
from trader_mcp.indicators import (
    INDICATOR_REGISTRY,
    OHLCV_COLUMNS,
    allowed_names_for,
    compute_indicators,
    df_from_bars,
    list_indicators,
    output_names_for,
)
from trader_mcp.indicators import _ta as ta
from trader_mcp.strategy.spec import IndicatorSpec

_BASE = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
def bars() -> list[OHLCVBar]:
    rng = np.random.default_rng(7)
    closes = np.cumsum(rng.standard_normal(200)) + 100
    return [
        OHLCVBar(
            timestamp=_BASE + timedelta(hours=i),
            open=float(c),
            high=float(c) + 1.5,
            low=float(c) - 1.5,
            close=float(c),
            volume=10.0 + i,
        )
        for i, c in enumerate(closes)
    ]


def test_df_from_bars_shape(bars: list[OHLCVBar]) -> None:
    df = df_from_bars(bars)
    assert list(df.columns) == list(OHLCV_COLUMNS)
    assert len(df) == len(bars)
    assert df.index.name == "timestamp"
    assert isinstance(df.index, pd.DatetimeIndex)
    assert str(df.index.tz) == "UTC"


def test_df_from_empty_bars() -> None:
    df = df_from_bars([])
    assert list(df.columns) == list(OHLCV_COLUMNS)
    assert len(df) == 0


def test_list_indicators_covers_registry() -> None:
    kinds = {i.kind for i in list_indicators()}
    assert kinds == set(INDICATOR_REGISTRY)
    assert {"sma", "ema", "rsi", "macd", "bbands", "donchian", "atr", "stoch", "adx"} <= kinds


def test_output_names_single_and_multi() -> None:
    assert output_names_for("s", "sma") == ["s"]
    assert output_names_for("m", "macd") == ["m", "m_signal", "m_hist"]
    assert output_names_for("bb", "bbands") == ["bb", "bb_upper", "bb_lower"]
    assert output_names_for("dc", "donchian") == ["dc", "dc_upper", "dc_lower"]
    assert output_names_for("st", "stoch") == ["st", "st_d"]
    assert output_names_for("ax", "adx") == ["ax", "ax_plus_di", "ax_minus_di"]


def test_allowed_names_for_includes_ohlcv_and_outputs() -> None:
    inds = [IndicatorSpec(id="r", kind="rsi"), IndicatorSpec(id="m", kind="macd")]
    names = allowed_names_for(inds)
    assert set(OHLCV_COLUMNS) <= names
    assert {"r", "m", "m_signal", "m_hist"} <= names


def test_sma_matches_pandas_ta(bars: list[OHLCVBar]) -> None:
    df = df_from_bars(bars)
    out = compute_indicators([IndicatorSpec(id="s", kind="sma", params={"length": 10})], df)
    expected = ta.sma(df["close"], length=10).to_numpy()
    np.testing.assert_allclose(out["s"].to_numpy(), expected, equal_nan=True)


def test_rsi_matches_pandas_ta(bars: list[OHLCVBar]) -> None:
    df = df_from_bars(bars)
    out = compute_indicators([IndicatorSpec(id="r", kind="rsi", params={"length": 14})], df)
    expected = ta.rsi(df["close"], length=14).to_numpy()
    np.testing.assert_allclose(out["r"].to_numpy(), expected, equal_nan=True)


def test_macd_maps_lines_correctly(bars: list[OHLCVBar]) -> None:
    df = df_from_bars(bars)
    out = compute_indicators([IndicatorSpec(id="m", kind="macd")], df)
    raw = ta.macd(df["close"], fast=12, slow=26, signal=9)
    # column order in raw: MACD, MACDh, MACDs
    np.testing.assert_allclose(out["m"].to_numpy(), raw.iloc[:, 0].to_numpy(), equal_nan=True)
    np.testing.assert_allclose(
        out["m_signal"].to_numpy(), raw.iloc[:, 2].to_numpy(), equal_nan=True
    )
    np.testing.assert_allclose(out["m_hist"].to_numpy(), raw.iloc[:, 1].to_numpy(), equal_nan=True)


def test_bbands_maps_bands_correctly(bars: list[OHLCVBar]) -> None:
    df = df_from_bars(bars)
    out = compute_indicators([IndicatorSpec(id="bb", kind="bbands")], df)
    raw = ta.bbands(df["close"], length=20, std=2.0)
    # raw order: BBL (lower), BBM (mid), BBU (upper)
    np.testing.assert_allclose(out["bb"].to_numpy(), raw.iloc[:, 1].to_numpy(), equal_nan=True)
    np.testing.assert_allclose(
        out["bb_upper"].to_numpy(), raw.iloc[:, 2].to_numpy(), equal_nan=True
    )
    np.testing.assert_allclose(
        out["bb_lower"].to_numpy(), raw.iloc[:, 0].to_numpy(), equal_nan=True
    )
    # Sanity: upper >= mid >= lower where defined.
    valid = out[["bb", "bb_upper", "bb_lower"]].dropna()
    assert (valid["bb_upper"] >= valid["bb"]).all()
    assert (valid["bb"] >= valid["bb_lower"]).all()


def test_donchian_bands_ordering(bars: list[OHLCVBar]) -> None:
    df = df_from_bars(bars)
    out = compute_indicators([IndicatorSpec(id="dc", kind="donchian")], df)
    valid = out[["dc", "dc_upper", "dc_lower"]].dropna()
    assert (valid["dc_upper"] >= valid["dc_lower"]).all()


def test_hand_computed_sma() -> None:
    # SMA of a known short series: deterministic check independent of pandas-ta.
    bars = [
        OHLCVBar(
            timestamp=_BASE + timedelta(hours=i),
            open=p,
            high=p,
            low=p,
            close=p,
            volume=1.0,
        )
        for i, p in enumerate([10.0, 20.0, 30.0, 40.0])
    ]
    df = df_from_bars(bars)
    out = compute_indicators([IndicatorSpec(id="s", kind="sma", params={"length": 2})], df)
    # SMA(2): NaN, 15, 25, 35
    vals = out["s"].to_numpy()
    assert pd.isna(vals[0])
    np.testing.assert_allclose(vals[1:], [15.0, 25.0, 35.0])


def test_compute_preserves_ohlcv(bars: list[OHLCVBar]) -> None:
    df = df_from_bars(bars)
    out = compute_indicators([IndicatorSpec(id="r", kind="rsi")], df)
    for col in OHLCV_COLUMNS:
        assert col in out.columns
    # Original frame is not mutated.
    assert "r" not in df.columns


def test_compute_deterministic(bars: list[OHLCVBar]) -> None:
    df = df_from_bars(bars)
    a = compute_indicators([IndicatorSpec(id="r", kind="rsi")], df)
    b = compute_indicators([IndicatorSpec(id="r", kind="rsi")], df)
    np.testing.assert_array_equal(a["r"].to_numpy(), b["r"].to_numpy())


@pytest.mark.parametrize("kind", sorted(INDICATOR_REGISTRY))
def test_every_kind_computes_its_output_columns(kind: str, bars: list[OHLCVBar]) -> None:
    df = df_from_bars(bars)
    out = compute_indicators([IndicatorSpec(id="x", kind=kind)], df)  # type: ignore[arg-type]
    for name in output_names_for("x", kind):
        assert name in out.columns
        # Each indicator produces at least some non-NaN values past its warmup.
        assert bool(out[name].notna().any())


def test_unknown_kind_rejected_in_output_names() -> None:
    from trader_mcp.errors import ValidationError

    with pytest.raises(ValidationError, match="Unknown indicator kind"):
        output_names_for("x", "bogus")


# --------------------------------------------------------------------------- #
# Phase 5 robustness: insufficient-data warm-up must not crash.
#
# pandas-ta returns ``None`` (not a NaN-filled frame) when a composite indicator
# gets fewer rows than its minimum lookback. The live/paper StrategyRuntime drives
# the interpreter over a growing trailing prefix, so it computes indicators over
# SHORT windows during warm-up. The adapters must return NaN-filled Series of the
# correct length/order rather than raising on ``None.iloc``.
# --------------------------------------------------------------------------- #
def _short_bars(n: int) -> list[OHLCVBar]:
    rng = np.random.default_rng(11)
    closes = np.cumsum(rng.standard_normal(n)) + 100
    return [
        OHLCVBar(
            timestamp=_BASE + timedelta(hours=i),
            open=float(c),
            high=float(c) + 1.5,
            low=float(c) - 1.5,
            close=float(c),
            volume=10.0 + i,
        )
        for i, c in enumerate(closes)
    ]


@pytest.mark.parametrize("kind", sorted(INDICATOR_REGISTRY))
@pytest.mark.parametrize("n", [1, 5, 10])
def test_short_window_returns_nan_columns_not_crash(kind: str, n: int) -> None:
    # Every indicator over a too-short window yields NaN-filled output columns of
    # the right length, in the right order -- no AttributeError on a None result.
    df = df_from_bars(_short_bars(n))
    out = compute_indicators([IndicatorSpec(id="x", kind=kind)], df)  # type: ignore[arg-type]
    names = output_names_for("x", kind)
    for name in names:
        assert name in out.columns
        col = out[name].to_numpy()
        assert len(col) == n
        # During deep warm-up these are NaN (the interpreter maps NaN -> False).
        assert np.isnan(col).all()


def test_macd_short_window_does_not_crash() -> None:
    # Direct regression for the reported crash: MACD over 10 rows (needs ~35).
    df = df_from_bars(_short_bars(10))
    out = compute_indicators([IndicatorSpec(id="m", kind="macd")], df)
    for name in ("m", "m_signal", "m_hist"):
        vals = out[name].to_numpy()
        assert len(vals) == 10
        assert np.isnan(vals).all()


def test_bbands_short_window_does_not_crash() -> None:
    df = df_from_bars(_short_bars(10))
    out = compute_indicators([IndicatorSpec(id="bb", kind="bbands")], df)
    for name in ("bb", "bb_upper", "bb_lower"):
        vals = out[name].to_numpy()
        assert len(vals) == 10
        assert np.isnan(vals).all()


def test_macd_happy_path_unchanged_after_robustness(bars: list[OHLCVBar]) -> None:
    # Regression guard: a sufficiently long window must yield the exact same values
    # as a direct pandas-ta call (the None-fallback must never touch this path).
    df = df_from_bars(bars)
    out = compute_indicators([IndicatorSpec(id="m", kind="macd")], df)
    raw = ta.macd(df["close"], fast=12, slow=26, signal=9)
    np.testing.assert_allclose(out["m"].to_numpy(), raw.iloc[:, 0].to_numpy(), equal_nan=True)
    np.testing.assert_allclose(
        out["m_signal"].to_numpy(), raw.iloc[:, 2].to_numpy(), equal_nan=True
    )
    np.testing.assert_allclose(out["m_hist"].to_numpy(), raw.iloc[:, 1].to_numpy(), equal_nan=True)


def test_bbands_happy_path_unchanged_after_robustness(bars: list[OHLCVBar]) -> None:
    df = df_from_bars(bars)
    out = compute_indicators([IndicatorSpec(id="bb", kind="bbands")], df)
    raw = ta.bbands(df["close"], length=20, std=2.0)
    np.testing.assert_allclose(out["bb"].to_numpy(), raw.iloc[:, 1].to_numpy(), equal_nan=True)
    np.testing.assert_allclose(
        out["bb_upper"].to_numpy(), raw.iloc[:, 2].to_numpy(), equal_nan=True
    )
    np.testing.assert_allclose(
        out["bb_lower"].to_numpy(), raw.iloc[:, 0].to_numpy(), equal_nan=True
    )


def test_per_bar_prefix_matches_full_window_macd(bars: list[OHLCVBar]) -> None:
    # Parity proof for the live path: once pandas-ta returns a frame for a prefix,
    # the last value of that prefix equals the full-window value at the same index
    # (MACD/EWM is causal). When the prefix is too short pandas-ta returns ``None``
    # and our fix yields NaN -> the interpreter maps that to False (no signal),
    # which is the only safe, crash-free behavior in that warm-up band.
    df = df_from_bars(bars)
    full = compute_indicators([IndicatorSpec(id="m", kind="macd")], df)
    # n=10/27 land in pandas-ta's None band (NaN expected); n>=34 returns a frame.
    saw_nan_band = False
    saw_value_band = False
    for n in (10, 27, 34, 40, 80, len(df)):
        prefix = compute_indicators([IndicatorSpec(id="m", kind="macd")], df.iloc[:n])
        for name in ("m", "m_signal", "m_hist"):
            last_prefix = prefix[name].to_numpy()[-1]
            if np.isnan(last_prefix):
                # Warm-up: safe NaN -> False; never a crash.
                saw_nan_band = True
            else:
                # Steady state: byte-for-byte parity with the full-window value.
                np.testing.assert_allclose(last_prefix, full[name].to_numpy()[n - 1])
                saw_value_band = True
    # The test exercises both the NaN warm-up band and the parity band.
    assert saw_nan_band
    assert saw_value_band
