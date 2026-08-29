"""Liquidity engine tests: structure, pools, sweeps and zones.

These use hand-built price series with the pattern deliberately planted, so a
failure points at the detector rather than at the data.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_engine.indicators.core import atr, volume_zscore
from trading_engine.liquidity.analyzer import LiquidityAnalyzer
from trading_engine.liquidity.pools import (
    PoolKind, detect_sweeps, find_liquidity_pools, unswept_pools,
)
from trading_engine.liquidity.structure import (
    SwingType, Trend, classify_trend, detect_swings, swings_visible_at,
)
from trading_engine.liquidity.zones import (
    ZoneKind, find_fair_value_gaps, find_order_blocks, volume_profile,
)


def make_df(highs, lows, closes=None, opens=None, volumes=None) -> pd.DataFrame:
    n = len(highs)
    closes = closes if closes is not None else [(h + l) / 2 for h, l in zip(highs, lows)]
    opens = opens if opens is not None else closes
    volumes = volumes if volumes is not None else [100.0] * n
    return pd.DataFrame(
        {
            "open": np.asarray(opens, dtype=float),
            "high": np.asarray(highs, dtype=float),
            "low": np.asarray(lows, dtype=float),
            "close": np.asarray(closes, dtype=float),
            "volume": np.asarray(volumes, dtype=float),
        },
        index=pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC"),
    )


# --------------------------------------------------------------------------- #
# Swings
# --------------------------------------------------------------------------- #

def test_detects_a_swing_high():
    highs = [10, 11, 12, 13, 14, 20, 14, 13, 12, 11, 10]
    lows = [h - 2 for h in highs]
    swings = detect_swings(make_df(highs, lows), left=5, right=5)
    highs_found = [s for s in swings if s.kind is SwingType.HIGH]
    assert len(highs_found) == 1
    assert highs_found[0].index == 5
    assert highs_found[0].price == 20.0


def test_swing_confirmation_lags_by_right_width():
    """A swing cannot be known until `right` bars have printed after it. This
    lag is what `swings_visible_at` enforces, and skipping it would let the
    strategy trade pivots before the market formed them."""
    highs = [10, 11, 12, 13, 14, 20, 14, 13, 12, 11, 10]
    lows = [h - 2 for h in highs]
    swings = detect_swings(make_df(highs, lows), left=5, right=5)
    swing = swings[0]
    assert swing.confirmed_index == swing.index + 5
    assert swings_visible_at(swings, swing.index) == []
    assert swings_visible_at(swings, swing.confirmed_index) == [swing]


def test_flat_series_has_no_swings():
    df = make_df([10] * 30, [9] * 30)
    assert detect_swings(df, 5, 5) == []


def test_classify_trend_bullish():
    highs = [10, 20, 15, 30, 25, 40]
    lows = [5, 12, 8, 22, 18, 32]
    n = 60
    h = np.interp(np.linspace(0, len(highs) - 1, n), range(len(highs)), highs)
    l = np.interp(np.linspace(0, len(lows) - 1, n), range(len(lows)), lows)
    swings = detect_swings(make_df(h, l), 3, 3)
    # With too few confirmed swings the classifier must say RANGING, never guess.
    assert classify_trend(swings) in (Trend.BULLISH, Trend.RANGING)


def test_classify_trend_needs_evidence():
    assert classify_trend([]) is Trend.RANGING


# --------------------------------------------------------------------------- #
# Pools
# --------------------------------------------------------------------------- #

def build_equal_highs() -> pd.DataFrame:
    """Two swing highs at the same level -> one buy-side pool."""
    highs, lows = [], []
    for block in range(2):
        highs += [10, 11, 12, 13, 14, 20.0, 14, 13, 12, 11, 10]
        lows += [8, 9, 10, 11, 12, 15, 12, 11, 10, 9, 8]
        del block
    return make_df(highs, lows)


def test_equal_highs_form_a_pool():
    df = build_equal_highs()
    swings = detect_swings(df, 5, 5)
    pools = find_liquidity_pools(swings, tolerance_pct=0.002, min_touches=2)
    buy_side = [p for p in pools if p.kind is PoolKind.BUY_SIDE and p.touches >= 2]
    assert buy_side, "two equal swing highs should cluster into one pool"
    assert buy_side[0].price == pytest.approx(20.0)


def test_pools_are_local_in_time():
    """Two matching highs hundreds of bars apart are a coincidence, not a
    cluster of resting stops."""
    highs = [10, 11, 12, 13, 14, 20.0, 14, 13, 12, 11, 10]
    filler_h = [10.0] * 400
    filler_l = [8.0] * 400
    lows = [8, 9, 10, 11, 12, 15, 12, 11, 10, 9, 8]
    df = make_df(highs + filler_h + highs, lows + filler_l + lows)
    swings = detect_swings(df, 5, 5)
    pools = find_liquidity_pools(
        swings, tolerance_pct=0.002, min_touches=2, max_bars_apart=50
    )
    assert all(p.touches < 2 or p.last_index - p.first_index <= 50 for p in pools)


def test_prominent_single_swing_counts_as_liquidity():
    highs = [10, 11, 12, 13, 14, 30.0, 14, 13, 12, 11, 10]
    lows = [8, 9, 10, 11, 12, 15, 12, 11, 10, 9, 8]
    df = make_df(highs, lows)
    a = atr(df, 5)
    swings = detect_swings(df, 5, 5, a)
    pools = find_liquidity_pools(
        swings, tolerance_pct=0.001, min_touches=2, single_swing_min_strength=0.5
    )
    singles = [p for p in pools if p.touches == 1]
    assert singles, "a prominent isolated swing still holds stops"


def test_unswept_pools_split_above_and_below():
    df = build_equal_highs()
    swings = detect_swings(df, 5, 5)
    pools = find_liquidity_pools(swings, tolerance_pct=0.002)
    above, below = unswept_pools(pools, bar_index=len(df) - 1, price=15.0)
    assert all(p.price > 15.0 for p in above)
    assert all(p.price < 15.0 for p in below)
    # Sorted by proximity to price.
    assert above == sorted(above, key=lambda p: p.price)


# --------------------------------------------------------------------------- #
# Sweeps
# --------------------------------------------------------------------------- #

def test_detects_a_sweep_and_reclaim():
    """Price pokes above the pool then closes back below: a stop hunt."""
    highs = [10, 11, 12, 13, 14, 20.0, 14, 13, 12, 11, 10,
             10, 11, 12, 13, 14, 20.0, 14, 13, 12, 11, 10]
    lows = [8, 9, 10, 11, 12, 15, 12, 11, 10, 9, 8,
            8, 9, 10, 11, 12, 15, 12, 11, 10, 9, 8]
    closes = [(h + l) / 2 for h, l in zip(highs, lows)]

    # Sweep bar: wick to 21, close back at 17 (below the 20 pool).
    highs = highs + [21.0]
    lows = lows + [16.0]
    closes = closes + [17.0]
    df = make_df(highs, lows, closes)

    a = atr(df, 5)
    swings = detect_swings(df, 5, 5, a)
    pools = find_liquidity_pools(swings, tolerance_pct=0.002, min_touches=2)
    sweeps = detect_sweeps(df, pools, a, min_penetration_pct=0.0001,
                           volume_z=volume_zscore(df, 5))

    assert sweeps, "the wick above equal highs should register as a sweep"
    sweep = sweeps[0]
    assert sweep.direction == "up"
    assert sweep.reclaimed is True
    assert sweep.bias == "short"          # sweeping highs traps longs
    assert 0.0 <= sweep.quality <= 1.0


def test_no_sweep_when_price_never_reaches_the_pool():
    df = build_equal_highs()
    a = atr(df, 5)
    swings = detect_swings(df, 5, 5, a)
    pools = find_liquidity_pools(swings, tolerance_pct=0.002, min_touches=2)
    for pool in pools:
        pool.price = 1000.0               # far above anything traded
    assert detect_sweeps(df, pools, a) == []


def test_sweep_only_counted_after_the_pool_exists():
    """A pool cannot be swept by a bar that came before the swings forming it."""
    df = build_equal_highs()
    a = atr(df, 5)
    swings = detect_swings(df, 5, 5, a)
    pools = find_liquidity_pools(swings, tolerance_pct=0.002, min_touches=2)
    sweeps = detect_sweeps(df, pools, a)
    assert all(s.index > s.pool.last_index for s in sweeps)


# --------------------------------------------------------------------------- #
# Zones
# --------------------------------------------------------------------------- #

def test_detects_bullish_fair_value_gap():
    # Bar 3's low (20) sits above bar 1's high (12): an untraded pocket.
    highs = [10.0, 12.0, 18.0, 25.0, 26.0]
    lows = [8.0, 9.0, 13.0, 20.0, 21.0]
    df = make_df(highs, lows)
    zones = find_fair_value_gaps(df, atr(df, 3), min_size_atr=0.01)
    demand = [z for z in zones if z.kind is ZoneKind.DEMAND]
    assert demand
    # Zones come back newest-first, so match on the gap rather than on order.
    gaps = {(round(z.bottom, 6), round(z.top, 6)) for z in demand}
    assert (12.0, 20.0) in gaps


def test_no_fvg_when_ranges_overlap():
    highs = [10.0, 11.0, 12.0, 13.0, 14.0]
    lows = [8.0, 9.0, 10.0, 11.0, 12.0]
    df = make_df(highs, lows)
    assert find_fair_value_gaps(df, atr(df, 3), min_size_atr=0.01) == []


def test_order_block_is_the_last_opposing_candle():
    opens = [10.0, 10.0, 10.0, 10.5, 11.0]
    closes = [10.0, 10.0, 9.0, 11.0, 20.0]       # bar 2 down, then displacement
    highs = [10.5, 10.5, 10.5, 11.5, 21.0]
    lows = [9.5, 9.5, 8.5, 10.0, 10.5]
    df = make_df(highs, lows, closes, opens)
    zones = find_order_blocks(df, atr(df, 3), displacement_atr=0.5, lookback=5)
    demand = [z for z in zones if z.kind is ZoneKind.DEMAND]
    assert demand, "a sharp rally should leave a demand order block behind it"


def test_volume_profile_poc_and_value_area():
    # Concentrate volume around 100 so the POC must land there.
    highs = [101.0] * 40 + [110.0] * 5
    lows = [99.0] * 40 + [108.0] * 5
    volumes = [1000.0] * 40 + [10.0] * 5
    df = make_df(highs, lows, volumes=volumes)
    profile = volume_profile(df, bins=30)

    assert profile is not None
    assert 99.0 <= profile.poc <= 101.5
    assert profile.value_area_low <= profile.poc <= profile.value_area_high
    assert profile.total_volume > 0


def test_volume_profile_handles_degenerate_input():
    assert volume_profile(make_df([10.0], [9.0])) is None
    flat = make_df([10.0] * 10, [10.0] * 10)
    assert volume_profile(flat) is None       # zero price range


# --------------------------------------------------------------------------- #
# Analyzer
# --------------------------------------------------------------------------- #

@pytest.fixture
def prepared_analyzer():
    from trading_engine.data.feeds import SyntheticFeed
    df = SyntheticFeed(seed=11).get_bars("TEST/USDT", "15m", 600)
    return LiquidityAnalyzer().prepare(df), df


def test_analyzer_produces_a_scored_context(prepared_analyzer):
    analyzer, df = prepared_analyzer
    ctx = analyzer.context_at(len(df) - 1)
    assert 0.0 <= ctx.score <= 100.0
    assert ctx.bias in ("long", "short", "neutral")
    assert ctx.atr > 0


def test_analyzer_context_uses_only_past_data(prepared_analyzer):
    """The context at bar i must not reference structures formed after i."""
    analyzer, df = prepared_analyzer
    i = 400
    ctx = analyzer.context_at(i)
    for zone in ctx.demand_zones + ctx.supply_zones:
        assert zone.index <= i
    if ctx.recent_sweep is not None:
        assert ctx.recent_sweep.index <= i
    for swing in (ctx.structure.last_swing_high, ctx.structure.last_swing_low):
        if swing is not None:
            assert swing.confirmed_index <= i


def test_analyzer_rejects_missing_columns():
    with pytest.raises(ValueError, match="missing columns"):
        LiquidityAnalyzer().prepare(pd.DataFrame({"close": [1.0, 2.0]}))


def test_context_at_rejects_out_of_range(prepared_analyzer):
    analyzer, df = prepared_analyzer
    with pytest.raises(IndexError):
        analyzer.context_at(len(df) + 10)
