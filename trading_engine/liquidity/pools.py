"""Liquidity pools and sweeps.

The central premise of the strategy: price is drawn to resting orders. Clusters
of equal highs and lows are where stop-loss orders accumulate, and a *sweep* —
penetrating that cluster then closing back inside the prior range — is the
engine's highest-quality entry trigger.

Why it earns its place as the primary trigger: a sweep both supplies the
liquidity that fuels the reversal *and* leaves a precise invalidation point just
beyond the wick. That tight, structural stop is what makes a 3R target reachable
without inventing a target far outside the day's realistic range.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd

from .structure import SwingPoint, SwingType


class PoolKind(str, Enum):
    BUY_SIDE = "buy_side"    # above price: resting buy stops (shorts' stops)
    SELL_SIDE = "sell_side"  # below price: resting sell stops (longs' stops)


@dataclass(slots=True)
class LiquidityPool:
    """A cluster of swing points at a similar price — where stops rest."""

    price: float
    kind: PoolKind
    touches: int
    first_index: int
    last_index: int
    # The bar at which this pool became *knowable*. A pool is built from swing
    # points, and a swing is only confirmed `right` bars after it prints, so the
    # pool cannot be acted on before every member has confirmed. Consumers must
    # filter on this or they are reading liquidity that had not formed yet.
    confirmed_index: int = 0
    swept: bool = False
    swept_index: int | None = None
    strength: float = 0.0        # touches x recency x tightness
    member_prices: list[float] = field(default_factory=list)

    @property
    def is_equal_level(self) -> bool:
        """Two or more touches is what makes it a magnet rather than a level."""
        return self.touches >= 2

    def to_dict(self) -> dict:
        return {
            "price": self.price,
            "kind": self.kind.value,
            "touches": self.touches,
            "first_index": self.first_index,
            "last_index": self.last_index,
            "confirmed_index": self.confirmed_index,
            "swept": self.swept,
            "swept_index": self.swept_index,
            "strength": round(self.strength, 3),
        }


@dataclass(slots=True)
class LiquiditySweep:
    """A pool taken out, with price rejecting back inside the range."""

    index: int
    timestamp: pd.Timestamp
    pool: LiquidityPool
    penetration: float        # how far beyond the pool price went
    penetration_atr: float
    reclaimed: bool           # closed back inside -> a true sweep, not a break
    direction: str            # "up" = swept highs (bearish), "down" = swept lows
    volume_z: float = 0.0
    wick_ratio: float = 0.0   # rejection wick as a fraction of the bar's range

    @property
    def bias(self) -> str:
        """A sweep of highs traps longs and points down, and vice versa."""
        return "short" if self.direction == "up" else "long"

    @property
    def quality(self) -> float:
        """0-1 quality score. Drives the confluence weighting."""
        score = 0.0
        score += 0.30 if self.reclaimed else 0.0
        score += 0.20 * min(1.0, self.wick_ratio / 0.6)
        score += 0.20 * min(1.0, max(0.0, self.volume_z) / 2.0)
        score += 0.15 * min(1.0, self.pool.touches / 4.0)
        # A sliver of penetration is a clean sweep; a deep one is a real break.
        score += 0.15 * (1.0 - min(1.0, self.penetration_atr / 1.5))
        return round(min(1.0, score), 4)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "timestamp": self.timestamp.isoformat(),
            "pool": self.pool.to_dict(),
            "penetration": self.penetration,
            "penetration_atr": round(self.penetration_atr, 4),
            "reclaimed": self.reclaimed,
            "direction": self.direction,
            "bias": self.bias,
            "volume_z": round(self.volume_z, 3),
            "wick_ratio": round(self.wick_ratio, 4),
            "quality": self.quality,
        }


def find_liquidity_pools(
    swings: list[SwingPoint],
    tolerance_pct: float = 0.001,
    min_touches: int = 2,
    max_pools: int | None = None,
    max_bars_apart: int = 250,
    single_swing_min_strength: float = 1.0,
) -> list[LiquidityPool]:
    """Cluster swing points into pools of "equal" highs/lows.

    Clustering is local in **both** price and time. `tolerance_pct` is relative,
    not absolute, so one setting works on a $0.50 altcoin and on a $100k BTC;
    `max_bars_apart` stops two swings that merely happen to share a price from
    being treated as one pool when they are hundreds of bars apart. Stops rest
    at a level because traders placed them around a *recent* structure, so a
    match separated by months is a coincidence, not a cluster.

    A *prominent* single swing also counts. Stops rest above the recent high
    whether or not that high was tested twice, so requiring two touches would
    miss most real liquidity. Singles must clear `single_swing_min_strength`
    (measured in ATR of prominence, set when the swing was detected) and are
    recorded with `touches=1`, which feeds through to a lower sweep quality —
    so they are treated as the weaker signal they are rather than being
    silently equated with a genuine double top.

    `max_pools=None` keeps every pool found. Callers working on a live window
    can cap it; a backtest should not, or later bars are left with no liquidity
    map at all.
    """
    pools: list[LiquidityPool] = []

    for kind, swing_type in (
        (PoolKind.BUY_SIDE, SwingType.HIGH),
        (PoolKind.SELL_SIDE, SwingType.LOW),
    ):
        points = sorted(
            (s for s in swings if s.kind is swing_type), key=lambda s: s.index
        )

        # Every swing anchors pools, and a pool is emitted at each *prefix* of
        # its cluster: {A}, {A,B}, {A,B,C}, ...
        #
        # This shape is forced by causality. If a pool were emitted only for the
        # final cluster, then appending future bars could add a member to a
        # cluster that had already formed — changing that pool's price, its
        # touch count and its confirmation bar retroactively, and with them a
        # decision the engine had already made at an earlier bar. Emitting
        # prefixes means each pool's content is fixed the moment it confirms:
        # {A,B} appearing later never alters {A}. It mirrors what a trader
        # actually sees — first a single high, and only later a double top.
        #
        # The cost is overlapping pools at the same level, which
        # `unswept_pools` deduplicates at consumption time, where the caller
        # knows which bar it is standing on.
        for i, anchor in enumerate(points):
            cluster = [anchor]

            if (
                anchor.strength >= single_swing_min_strength
                and min_touches > 1
            ):
                # A swing standing well clear of its neighbourhood holds stops
                # even before it is tested a second time.
                pools.append(
                    LiquidityPool(
                        price=float(anchor.price),
                        kind=kind,
                        touches=1,
                        first_index=anchor.index,
                        last_index=anchor.index,
                        confirmed_index=anchor.confirmed_index,
                        strength=min(1.5, anchor.strength * 0.5),
                        member_prices=[anchor.price],
                    )
                )

            for j in range(i + 1, len(points)):
                candidate = points[j]
                if candidate.index - anchor.index > max_bars_apart:
                    break
                # Compare against the cluster mean so a long chain of small
                # steps cannot drift into one absurdly wide "pool".
                mean_price = sum(p.price for p in cluster) / len(cluster)
                if abs(candidate.price - mean_price) / mean_price > tolerance_pct:
                    continue
                cluster.append(candidate)

                if len(cluster) < min_touches:
                    continue

                prices = [p.price for p in cluster]
                idxs = [p.index for p in cluster]
                # Use the extreme, not the mean: stops sit beyond the furthest
                # touch, so that is where the liquidity actually rests.
                price = max(prices) if kind is PoolKind.BUY_SIDE else min(prices)
                spread = (
                    (max(prices) - min(prices))
                    / max(1e-12, float(np.mean(prices)))
                )
                tightness = 1.0 - min(1.0, spread / max(tolerance_pct, 1e-9))
                pools.append(
                    LiquidityPool(
                        price=float(price),
                        kind=kind,
                        touches=len(cluster),
                        first_index=min(idxs),
                        last_index=max(idxs),
                        confirmed_index=max(p.confirmed_index for p in cluster),
                        strength=len(cluster) * (0.5 + 0.5 * tightness),
                        member_prices=list(prices),
                    )
                )

    if max_pools is not None and len(pools) > max_pools:
        # Keep the strongest, then restore chronological order.
        pools.sort(key=lambda p: (p.strength, p.last_index), reverse=True)
        pools = pools[:max_pools]

    pools.sort(key=lambda p: p.last_index)
    return pools


def detect_sweeps(
    df: pd.DataFrame,
    pools: list[LiquidityPool],
    atr_series: pd.Series,
    min_penetration_pct: float = 0.0005,
    volume_z: pd.Series | None = None,
    max_lookahead: int = 3,
) -> list[LiquiditySweep]:
    """Find bars that swept a pool and rejected.

    A sweep is: wick beyond the pool, then a close back on the original side —
    either on the sweep bar itself or within `max_lookahead` bars. That
    reclaim is what separates a stop hunt from a genuine breakout.
    """
    highs = df["high"].to_numpy(dtype="float64")
    lows = df["low"].to_numpy(dtype="float64")
    closes = df["close"].to_numpy(dtype="float64")
    opens = df["open"].to_numpy(dtype="float64")
    atr_v = atr_series.to_numpy(dtype="float64")
    vz = (
        volume_z.to_numpy(dtype="float64")
        if volume_z is not None
        else np.zeros(len(df))
    )

    sweeps: list[LiquiditySweep] = []

    for pool in pools:
        # Only look after the pool was knowable. Using last_index alone is not
        # enough: the final swing in the cluster is not confirmed until `right`
        # bars later, so a "sweep" in between would be detected before the
        # system could have known the level was there.
        start = max(pool.last_index, pool.confirmed_index) + 1
        for i in range(start, len(df)):
            a = atr_v[i]
            if not np.isfinite(a) or a <= 0:
                continue

            if pool.kind is PoolKind.BUY_SIDE:
                if highs[i] <= pool.price:
                    continue
                penetration = highs[i] - pool.price
                if penetration / pool.price < min_penetration_pct:
                    continue
                reclaimed = closes[i] < pool.price or any(
                    closes[k] < pool.price
                    for k in range(i + 1, min(i + 1 + max_lookahead, len(df)))
                )
                bar_range = max(1e-12, highs[i] - lows[i])
                wick = (highs[i] - max(opens[i], closes[i])) / bar_range
                direction = "up"
            else:
                if lows[i] >= pool.price:
                    continue
                penetration = pool.price - lows[i]
                if penetration / pool.price < min_penetration_pct:
                    continue
                reclaimed = closes[i] > pool.price or any(
                    closes[k] > pool.price
                    for k in range(i + 1, min(i + 1 + max_lookahead, len(df)))
                )
                bar_range = max(1e-12, highs[i] - lows[i])
                wick = (min(opens[i], closes[i]) - lows[i]) / bar_range
                direction = "down"

            pool.swept = True
            pool.swept_index = i
            sweeps.append(
                LiquiditySweep(
                    index=i,
                    timestamp=df.index[i],
                    pool=pool,
                    penetration=float(penetration),
                    penetration_atr=float(penetration / a),
                    reclaimed=bool(reclaimed),
                    direction=direction,
                    volume_z=float(vz[i]) if np.isfinite(vz[i]) else 0.0,
                    wick_ratio=float(wick),
                )
            )
            break  # a pool is only swept once

    sweeps.sort(key=lambda s: s.index)
    return sweeps


def unswept_pools(
    pools: list[LiquidityPool], bar_index: int, price: float
) -> tuple[list[LiquidityPool], list[LiquidityPool]]:
    """Pools still resting above and below price — the realistic draw-on-liquidity.

    These are the engine's target candidates: an untouched pool is where price is
    *trying* to go, which makes it a far better target than an arbitrary R
    multiple projected into empty space.
    """
    def live(p: LiquidityPool) -> bool:
        # Knowable by now, and not already taken out.
        if p.confirmed_index > bar_index:
            return False
        return not (p.swept and (p.swept_index or 0) <= bar_index)

    def dedupe(candidates: list[LiquidityPool]) -> list[LiquidityPool]:
        """Collapse pools describing the same price level, keeping the
        strongest. Overlapping pools are emitted deliberately (see
        find_liquidity_pools) so that detection stays causal; this is where
        they are resolved."""
        best: dict[int, LiquidityPool] = {}
        for p in candidates:
            # Bucket by relative price so the key works at any price scale.
            key = int(round(p.price / max(price, 1e-12) / 1e-4))
            current = best.get(key)
            if current is None or (p.touches, p.strength) > (
                current.touches, current.strength
            ):
                best[key] = p
        return list(best.values())

    above = dedupe([p for p in pools if p.price > price and live(p)])
    below = dedupe([p for p in pools if p.price < price and live(p)])
    above.sort(key=lambda p: p.price)
    below.sort(key=lambda p: p.price, reverse=True)
    return above, below
