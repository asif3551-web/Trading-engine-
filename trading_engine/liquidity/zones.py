"""Fair value gaps, order blocks and volume profile — the zone layer.

These give the engine *where* to enter after a trigger fires, and where the
structural invalidation sits. Entering at a zone edge rather than at market is
what compresses the stop distance, and a compressed stop is what makes 3R
attainable within a realistic price excursion.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd


class ZoneKind(str, Enum):
    DEMAND = "demand"    # expect buyers here
    SUPPLY = "supply"    # expect sellers here


@dataclass(slots=True)
class Zone:
    """A price band expected to produce a reaction."""

    top: float
    bottom: float
    kind: ZoneKind
    index: int              # bar that created it
    origin: str             # "fvg" | "order_block" | "breaker"
    strength: float = 0.0
    mitigated: bool = False
    mitigated_index: int | None = None

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def height(self) -> float:
        return self.top - self.bottom

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def distance_from(self, price: float) -> float:
        """0 inside the zone, otherwise the gap to its nearest edge."""
        if self.contains(price):
            return 0.0
        return self.bottom - price if price < self.bottom else price - self.top

    def to_dict(self) -> dict:
        return {
            "top": self.top,
            "bottom": self.bottom,
            "mid": self.mid,
            "kind": self.kind.value,
            "index": self.index,
            "origin": self.origin,
            "strength": round(self.strength, 3),
            "mitigated": self.mitigated,
            "mitigated_index": self.mitigated_index,
        }


# --------------------------------------------------------------------------- #
# Fair value gaps
# --------------------------------------------------------------------------- #

def find_fair_value_gaps(
    df: pd.DataFrame,
    atr_series: pd.Series,
    min_size_atr: float = 0.15,
    max_gaps: int | None = None,
) -> list[Zone]:
    """Three-bar imbalances where bar 1 and bar 3 ranges do not overlap.

    A bullish FVG is `low[i] > high[i-2]`: price moved so fast that the range
    between them never traded. Those untraded pockets tend to get revisited,
    which makes them good limit-entry zones.

    `max_gaps=None` returns every gap found. A cap keeps only the most *recent*
    gaps, which is right for a live window but wrong for a backtest — capping
    leaves early bars with no zones while later ones have plenty, which silently
    biases results toward the end of the sample.
    """
    highs = df["high"].to_numpy(dtype="float64")
    lows = df["low"].to_numpy(dtype="float64")
    closes = df["close"].to_numpy(dtype="float64")
    atr_v = atr_series.to_numpy(dtype="float64")
    zones: list[Zone] = []

    for i in range(2, len(df)):
        a = atr_v[i]
        if not np.isfinite(a) or a <= 0:
            continue

        # Bullish FVG
        if lows[i] > highs[i - 2]:
            size = lows[i] - highs[i - 2]
            if size / a >= min_size_atr:
                zones.append(
                    Zone(
                        top=float(lows[i]), bottom=float(highs[i - 2]),
                        kind=ZoneKind.DEMAND, index=i, origin="fvg",
                        strength=float(min(1.0, size / a / 2.0)),
                    )
                )
        # Bearish FVG
        elif highs[i] < lows[i - 2]:
            size = lows[i - 2] - highs[i]
            if size / a >= min_size_atr:
                zones.append(
                    Zone(
                        top=float(lows[i - 2]), bottom=float(highs[i]),
                        kind=ZoneKind.SUPPLY, index=i, origin="fvg",
                        strength=float(min(1.0, size / a / 2.0)),
                    )
                )

    _mark_mitigated(zones, closes, highs, lows)
    zones.sort(key=lambda z: z.index, reverse=True)
    return zones if max_gaps is None else zones[:max_gaps]


# --------------------------------------------------------------------------- #
# Order blocks
# --------------------------------------------------------------------------- #

def find_order_blocks(
    df: pd.DataFrame,
    atr_series: pd.Series,
    displacement_atr: float = 1.2,
    lookback: int = 30,
    max_blocks: int | None = None,
) -> list[Zone]:
    """The last opposing candle before an impulsive displacement move.

    The logic: a large move away from a level implies unfilled institutional
    interest at that level. The last down-candle before a sharp rally is where
    the buying started, so its range becomes a demand zone.
    """
    opens = df["open"].to_numpy(dtype="float64")
    highs = df["high"].to_numpy(dtype="float64")
    lows = df["low"].to_numpy(dtype="float64")
    closes = df["close"].to_numpy(dtype="float64")
    atr_v = atr_series.to_numpy(dtype="float64")
    zones: list[Zone] = []

    for i in range(1, len(df)):
        a = atr_v[i]
        if not np.isfinite(a) or a <= 0:
            continue

        displacement = (closes[i] - opens[i]) / a
        if abs(displacement) < displacement_atr:
            continue

        bullish_move = displacement > 0
        # Walk back to the last candle opposing the impulse.
        start = max(0, i - lookback)
        ob_idx = None
        for j in range(i - 1, start - 1, -1):
            is_down = closes[j] < opens[j]
            if bullish_move and is_down:
                ob_idx = j
                break
            if not bullish_move and not is_down:
                ob_idx = j
                break
        if ob_idx is None:
            continue

        zones.append(
            Zone(
                top=float(highs[ob_idx]),
                bottom=float(lows[ob_idx]),
                kind=ZoneKind.DEMAND if bullish_move else ZoneKind.SUPPLY,
                index=ob_idx,
                origin="order_block",
                strength=float(min(1.0, abs(displacement) / 3.0)),
            )
        )

    _mark_mitigated(zones, closes, highs, lows)
    # Deduplicate overlapping blocks at the same origin bar, keeping the strongest.
    seen: dict[tuple[int, str], Zone] = {}
    for z in zones:
        key = (z.index, z.kind.value)
        if key not in seen or z.strength > seen[key].strength:
            seen[key] = z
    out = sorted(seen.values(), key=lambda z: z.index, reverse=True)
    return out if max_blocks is None else out[:max_blocks]


def _mark_mitigated(
    zones: list[Zone], closes: np.ndarray, highs: np.ndarray, lows: np.ndarray
) -> None:
    """A zone is mitigated once price trades back through it.

    Mitigated zones are kept (they can become breakers) but are scored lower —
    a fresh, untouched zone reacts far more reliably than a used one.
    """
    n = len(closes)
    for z in zones:
        for k in range(z.index + 1, n):
            touched = (
                lows[k] <= z.top if z.kind is ZoneKind.DEMAND else highs[k] >= z.bottom
            )
            if touched:
                z.mitigated = True
                z.mitigated_index = k
                break


def active_zones(
    zones: list[Zone], bar_index: int, kind: ZoneKind | None = None,
    include_mitigated: bool = False,
) -> list[Zone]:
    """Zones formed at or before `bar_index` and not yet consumed."""
    out = []
    for z in zones:
        if z.index > bar_index:
            continue
        if not include_mitigated and z.mitigated and (z.mitigated_index or 0) <= bar_index:
            continue
        if kind is not None and z.kind is not kind:
            continue
        out.append(z)
    return sorted(out, key=lambda z: z.index, reverse=True)


def nearest_zone(zones: list[Zone], price: float, kind: ZoneKind) -> Zone | None:
    candidates = [z for z in zones if z.kind is kind]
    if not candidates:
        return None
    return min(candidates, key=lambda z: z.distance_from(price))


# --------------------------------------------------------------------------- #
# Volume profile
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class VolumeProfile:
    """Volume distributed across price bins.

    POC and the value area describe where the market agreed on price. Price
    tends to stall at high-volume nodes and travel quickly through low-volume
    ones, which makes the profile useful for both targets and stop placement.
    """

    poc: float                    # price of maximum traded volume
    value_area_high: float
    value_area_low: float
    bins: list[float]
    volumes: list[float]
    total_volume: float

    @property
    def value_area_width(self) -> float:
        return self.value_area_high - self.value_area_low

    def in_value_area(self, price: float) -> bool:
        return self.value_area_low <= price <= self.value_area_high

    def low_volume_nodes(self, threshold: float = 0.3) -> list[float]:
        """Bins well below average volume — price traverses these quickly, so
        they make poor targets and good places to expect acceleration."""
        if not self.volumes:
            return []
        avg = float(np.mean(self.volumes))
        if avg <= 0:
            return []
        return [
            self.bins[i] for i, v in enumerate(self.volumes) if v < avg * threshold
        ]

    def high_volume_nodes(self, threshold: float = 1.5) -> list[float]:
        """Bins well above average — magnets and stalling points."""
        if not self.volumes:
            return []
        avg = float(np.mean(self.volumes))
        if avg <= 0:
            return []
        return [
            self.bins[i] for i, v in enumerate(self.volumes) if v > avg * threshold
        ]

    def to_dict(self) -> dict:
        return {
            "poc": self.poc,
            "value_area_high": self.value_area_high,
            "value_area_low": self.value_area_low,
            "total_volume": self.total_volume,
        }


def volume_profile(
    df: pd.DataFrame, bins: int = 50, value_area_pct: float = 0.70
) -> VolumeProfile | None:
    """Build a volume profile over the given bars.

    Each bar's volume is spread evenly across the price bins its range covers.
    That is an approximation — true volume-at-price needs tick data — but it is
    far closer than assigning the whole bar's volume to its close.
    """
    if df.empty or len(df) < 2:
        return None

    lo = float(df["low"].min())
    hi = float(df["high"].max())
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return None

    edges = np.linspace(lo, hi, bins + 1)
    centres = (edges[:-1] + edges[1:]) / 2.0
    vol_at_price = np.zeros(bins)

    lows = df["low"].to_numpy(dtype="float64")
    highs = df["high"].to_numpy(dtype="float64")
    vols = df["volume"].to_numpy(dtype="float64")

    for low, high, vol in zip(lows, highs, vols):
        if not np.isfinite(vol) or vol <= 0:
            continue
        lo_bin = int(np.searchsorted(edges, low, side="right") - 1)
        hi_bin = int(np.searchsorted(edges, high, side="right") - 1)
        lo_bin = max(0, min(bins - 1, lo_bin))
        hi_bin = max(0, min(bins - 1, hi_bin))
        span = hi_bin - lo_bin + 1
        vol_at_price[lo_bin: hi_bin + 1] += vol / span

    total = float(vol_at_price.sum())
    if total <= 0:
        return None

    poc_idx = int(np.argmax(vol_at_price))
    poc = float(centres[poc_idx])

    # Expand from the POC, always taking the richer neighbour, until the value
    # area holds the target share of volume.
    target = total * value_area_pct
    lo_i = hi_i = poc_idx
    acc = float(vol_at_price[poc_idx])
    while acc < target and (lo_i > 0 or hi_i < bins - 1):
        below = vol_at_price[lo_i - 1] if lo_i > 0 else -1.0
        above = vol_at_price[hi_i + 1] if hi_i < bins - 1 else -1.0
        if above >= below:
            hi_i += 1
            acc += float(vol_at_price[hi_i])
        else:
            lo_i -= 1
            acc += float(vol_at_price[lo_i])

    return VolumeProfile(
        poc=poc,
        value_area_high=float(centres[hi_i]),
        value_area_low=float(centres[lo_i]),
        bins=[float(c) for c in centres],
        volumes=[float(v) for v in vol_at_price],
        total_volume=total,
    )
