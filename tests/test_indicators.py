"""Indicator tests.

The most important test in this file is `test_all_indicators_are_causal`: it
asserts that no indicator's value at bar `i` changes when future bars are
appended. That single property is what makes every backtest number meaningful,
and it is the property that silently breaks when someone reaches for a
centred rolling window or a whole-series normalisation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_engine.indicators.core import (
    adx, atr, add_all, bollinger, choppiness, cmf, donchian, ema, rma, rsi,
    sma, true_range, volume_zscore, vwap,
)


@pytest.fixture
def bars() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    n = 300
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    spread = np.abs(rng.normal(0, 0.006, n)) * close
    open_ = np.concatenate([[100.0], close[:-1]])
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    return pd.DataFrame(
        {
            "open": open_, "high": high, "low": low, "close": close,
            "volume": rng.uniform(100, 1000, n),
        },
        index=pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC"),
    )


# --------------------------------------------------------------------------- #
# Known values
# --------------------------------------------------------------------------- #

def test_sma_matches_hand_calculation():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert sma(s, 3).tolist()[2:] == [2.0, 3.0, 4.0]
    assert pd.isna(sma(s, 3).iloc[1])       # not enough history yet


def test_ema_seeds_and_converges():
    s = pd.Series([10.0] * 50)
    out = ema(s, 10)
    assert pd.isna(out.iloc[8])
    assert out.iloc[-1] == pytest.approx(10.0)


def test_rma_is_not_ema():
    """Wilder smoothing uses alpha=1/n, EMA uses 2/(n+1). Substituting one for
    the other shifts RSI and ADX enough to change signals."""
    s = pd.Series(np.arange(1.0, 61.0))
    assert rma(s, 14).iloc[-1] != pytest.approx(ema(s, 14).iloc[-1], rel=1e-6)


def test_true_range_captures_gaps():
    df = pd.DataFrame(
        {"high": [10.0, 20.0], "low": [9.0, 19.0], "close": [9.5, 19.5],
         "open": [9.5, 19.5], "volume": [1.0, 1.0]}
    )
    # Bar 2 gapped up from 9.5; range is 19 to 20, but true range spans the gap.
    assert true_range(df).iloc[1] == pytest.approx(20.0 - 9.5)


def test_atr_is_positive_and_finite(bars):
    values = atr(bars, 14).dropna()
    assert len(values) > 200
    assert (values > 0).all()
    assert np.isfinite(values).all()


def test_rsi_bounds_and_extremes():
    rising = pd.Series(np.arange(1.0, 60.0))
    assert rsi(rising, 14).iloc[-1] == pytest.approx(100.0)

    falling = pd.Series(np.arange(60.0, 1.0, -1.0))
    assert rsi(falling, 14).iloc[-1] == pytest.approx(0.0, abs=1e-6)


def test_rsi_stays_within_bounds(bars):
    values = rsi(bars["close"], 14).dropna()
    assert values.between(0, 100).all()


def test_adx_components_bounded(bars):
    out = adx(bars, 14).dropna()
    assert (out["adx"] >= 0).all() and (out["adx"] <= 100).all()
    assert (out["plus_di"] >= 0).all()


def test_bollinger_ordering(bars):
    bb = bollinger(bars["close"], 20, 2.0).dropna()
    assert (bb["upper"] >= bb["middle"]).all()
    assert (bb["middle"] >= bb["lower"]).all()


def test_donchian_excludes_current_bar():
    """Without the shift, a bar making a new high looks like it already broke
    out — a textbook lookahead bug."""
    df = pd.DataFrame(
        {
            "high": [1.0, 2, 3, 4, 100],
            "low": [1.0, 1, 1, 1, 1],
            "close": [1.0, 2, 3, 4, 100],
            "open": [1.0, 1, 1, 1, 1],
            "volume": [1.0] * 5,
        }
    )
    out = donchian(df, 3)
    assert out["upper"].iloc[4] == 4.0        # not 100


def test_vwap_within_price_range(bars):
    values = vwap(bars, 50).dropna()
    assert (values > bars["low"].min()).all()
    assert (values < bars["high"].max()).all()


def test_choppiness_range(bars):
    values = choppiness(bars, 14).dropna()
    assert (values >= 0).all() and (values <= 100).all()


def test_cmf_bounded(bars):
    values = cmf(bars, 20).dropna()
    assert values.between(-1, 1).all()


def test_volume_zscore_is_centred(bars):
    values = volume_zscore(bars, 20).dropna()
    assert abs(values.mean()) < 1.0


# --------------------------------------------------------------------------- #
# Causality — the property the whole backtest rests on
# --------------------------------------------------------------------------- #

INDICATORS = {
    "sma": lambda d: sma(d["close"], 20),
    "ema": lambda d: ema(d["close"], 20),
    "rma": lambda d: rma(d["close"], 14),
    "atr": lambda d: atr(d, 14),
    "rsi": lambda d: rsi(d["close"], 14),
    "adx": lambda d: adx(d, 14)["adx"],
    "bb_upper": lambda d: bollinger(d["close"], 20)["upper"],
    "donchian_upper": lambda d: donchian(d, 20)["upper"],
    "vwap": lambda d: vwap(d, 50),
    "cmf": lambda d: cmf(d, 20),
    "choppiness": lambda d: choppiness(d, 14),
    "volume_z": lambda d: volume_zscore(d, 20),
}


@pytest.mark.parametrize("name", sorted(INDICATORS))
def test_all_indicators_are_causal(bars, name):
    """Appending future bars must not change any past value.

    If this fails, the indicator peeks at the future and every backtest metric
    derived from it is fiction.
    """
    fn = INDICATORS[name]
    cut = 200
    truncated = fn(bars.iloc[:cut]).to_numpy()
    full = fn(bars).to_numpy()[:cut]

    both_nan = np.isnan(truncated) & np.isnan(full)
    comparable = ~both_nan
    np.testing.assert_allclose(
        truncated[comparable], full[comparable], rtol=1e-9, atol=1e-9,
        err_msg=f"{name} is not causal: past values changed when future bars "
                f"were appended",
    )


def test_add_all_does_not_mutate_input(bars):
    before = list(bars.columns)
    add_all(bars)
    assert list(bars.columns) == before


def test_add_all_attaches_expected_columns(bars):
    out = add_all(bars)
    for column in ("atr", "ema_20", "ema_50", "rsi", "adx", "vwap",
                   "volume_z", "cmf", "choppiness"):
        assert column in out.columns, f"{column} missing"
    assert out["atr"].notna().sum() > 200
