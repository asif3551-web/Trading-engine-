"""Market structure: swing points, trend state, BOS and CHoCH.

Everything else in the liquidity package is built on the swing points detected
here, so this module's causality guarantee matters most: a swing point at index
`i` is only *confirmed* at index `i + right`, because you cannot know a bar was a
local high until enough bars have printed after it. `detect_swings` therefore
returns the confirmation index alongside the swing, and consumers must filter on
it. Getting this wrong makes a backtest see pivots before the market did, which
is the single most flattering bug a structure-based system can have.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd


class SwingType(str, Enum):
    HIGH = "high"
    LOW = "low"


class Trend(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    RANGING = "ranging"


class BreakType(str, Enum):
    BOS = "bos"        # break of structure: continuation
    CHOCH = "choch"    # change of character: first break against the trend


@dataclass(slots=True)
class SwingPoint:
    index: int              # bar where the extreme printed
    confirmed_index: int    # bar at which it became knowable
    timestamp: pd.Timestamp
    price: float
    kind: SwingType
    strength: float = 0.0   # how far it stands out, in ATR

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "confirmed_index": self.confirmed_index,
            "timestamp": self.timestamp.isoformat(),
            "price": self.price,
            "kind": self.kind.value,
            "strength": round(self.strength, 3),
        }


@dataclass(slots=True)
class StructureBreak:
    index: int
    timestamp: pd.Timestamp
    price: float            # the level that was broken
    kind: BreakType
    direction: str          # "up" | "down"
    broken_swing: SwingPoint

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "timestamp": self.timestamp.isoformat(),
            "price": self.price,
            "kind": self.kind.value,
            "direction": self.direction,
        }


def detect_swings(
    df: pd.DataFrame, left: int = 5, right: int = 5, atr_series: pd.Series | None = None
) -> list[SwingPoint]:
    """Fractal swing highs and lows.

    A swing high at `i` has no higher high within `left` bars before or `right`
    bars after. It is confirmed at `i + right`.
    """
    highs = df["high"].to_numpy(dtype="float64")
    lows = df["low"].to_numpy(dtype="float64")
    n = len(df)
    atr_v = (
        atr_series.to_numpy(dtype="float64")
        if atr_series is not None
        else np.full(n, np.nan)
    )
    swings: list[SwingPoint] = []

    for i in range(left, n - right):
        window_hi = highs[i - left: i + right + 1]
        window_lo = lows[i - left: i + right + 1]

        if highs[i] == window_hi.max() and (window_hi == highs[i]).sum() == 1:
            neighbourhood = float(np.nanmin(window_lo))
            a = atr_v[i]
            strength = (
                (highs[i] - neighbourhood) / a if a and np.isfinite(a) and a > 0 else 0.0
            )
            swings.append(
                SwingPoint(i, i + right, df.index[i], float(highs[i]),
                           SwingType.HIGH, strength)
            )

        if lows[i] == window_lo.min() and (window_lo == lows[i]).sum() == 1:
            neighbourhood = float(np.nanmax(window_hi))
            a = atr_v[i]
            strength = (
                (neighbourhood - lows[i]) / a if a and np.isfinite(a) and a > 0 else 0.0
            )
            swings.append(
                SwingPoint(i, i + right, df.index[i], float(lows[i]),
                           SwingType.LOW, strength)
            )

    swings.sort(key=lambda s: s.index)
    return swings


def swings_visible_at(swings: list[SwingPoint], bar_index: int) -> list[SwingPoint]:
    """Only the swings already confirmed at `bar_index`.

    Every consumer that makes a decision on bar `i` must go through this, or it
    is reading the future.
    """
    return [s for s in swings if s.confirmed_index <= bar_index]


def classify_trend(swings: list[SwingPoint], lookback: int = 4) -> Trend:
    """Trend from the sequence of swing points.

    Higher highs and higher lows = bullish; lower highs and lower lows = bearish;
    anything mixed is ranging. Deliberately strict — treating a mixed structure
    as trending is how continuation setups get taken into a range.
    """
    highs = [s for s in swings if s.kind is SwingType.HIGH][-lookback:]
    lows = [s for s in swings if s.kind is SwingType.LOW][-lookback:]
    if len(highs) < 2 or len(lows) < 2:
        return Trend.RANGING

    hh = highs[-1].price > highs[-2].price
    hl = lows[-1].price > lows[-2].price
    lh = highs[-1].price < highs[-2].price
    ll = lows[-1].price < lows[-2].price

    if hh and hl:
        return Trend.BULLISH
    if lh and ll:
        return Trend.BEARISH
    return Trend.RANGING


def detect_structure_breaks(
    df: pd.DataFrame, swings: list[SwingPoint], confirm_on_close: bool = True
) -> list[StructureBreak]:
    """Find BOS and CHoCH events.

    A break requires a *close* beyond the swing by default. Wick-only breaks are
    usually liquidity sweeps, not structural breaks, and conflating the two is
    what makes a structure model fire on every stop hunt.
    """
    closes = df["close"].to_numpy(dtype="float64")
    highs = df["high"].to_numpy(dtype="float64")
    lows = df["low"].to_numpy(dtype="float64")
    breaks: list[StructureBreak] = []
    trend = Trend.RANGING

    for i in range(len(df)):
        visible = swings_visible_at(swings, i)
        if len(visible) < 4:
            continue

        last_high = next(
            (s for s in reversed(visible) if s.kind is SwingType.HIGH and s.index < i),
            None,
        )
        last_low = next(
            (s for s in reversed(visible) if s.kind is SwingType.LOW and s.index < i),
            None,
        )

        up_ref = closes[i] if confirm_on_close else highs[i]
        down_ref = closes[i] if confirm_on_close else lows[i]

        if last_high and up_ref > last_high.price:
            kind = BreakType.CHOCH if trend is Trend.BEARISH else BreakType.BOS
            breaks.append(
                StructureBreak(i, df.index[i], last_high.price, kind, "up", last_high)
            )
            trend = Trend.BULLISH
        elif last_low and down_ref < last_low.price:
            kind = BreakType.CHOCH if trend is Trend.BULLISH else BreakType.BOS
            breaks.append(
                StructureBreak(i, df.index[i], last_low.price, kind, "down", last_low)
            )
            trend = Trend.BEARISH

    return breaks


@dataclass(slots=True)
class StructureState:
    """The structural picture as of one bar. What the strategy actually consumes."""

    trend: Trend
    last_swing_high: SwingPoint | None
    last_swing_low: SwingPoint | None
    last_break: StructureBreak | None
    swing_range: float          # distance between the framing swings
    position_in_range: float    # 0 = at the low, 1 = at the high

    def to_dict(self) -> dict:
        return {
            "trend": self.trend.value,
            "last_swing_high": self.last_swing_high.price if self.last_swing_high else None,
            "last_swing_low": self.last_swing_low.price if self.last_swing_low else None,
            "last_break": self.last_break.to_dict() if self.last_break else None,
            "swing_range": round(self.swing_range, 8),
            "position_in_range": round(self.position_in_range, 4),
        }


def structure_state(
    df: pd.DataFrame,
    swings: list[SwingPoint],
    breaks: list[StructureBreak],
    bar_index: int,
) -> StructureState:
    """Assemble the structural state visible at `bar_index`."""
    visible = swings_visible_at(swings, bar_index)
    last_high = next(
        (s for s in reversed(visible) if s.kind is SwingType.HIGH), None
    )
    last_low = next((s for s in reversed(visible) if s.kind is SwingType.LOW), None)
    last_break = next((b for b in reversed(breaks) if b.index <= bar_index), None)

    swing_range = 0.0
    position = 0.5
    if last_high and last_low:
        swing_range = abs(last_high.price - last_low.price)
        if swing_range > 0:
            close = float(df["close"].iloc[bar_index])
            position = (close - last_low.price) / swing_range
            position = float(np.clip(position, -1.0, 2.0))

    return StructureState(
        trend=classify_trend(visible),
        last_swing_high=last_high,
        last_swing_low=last_low,
        last_break=last_break,
        swing_range=swing_range,
        position_in_range=position,
    )
