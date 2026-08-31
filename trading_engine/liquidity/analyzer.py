"""The liquidity analyzer — fuses structure, pools, zones and depth into one
per-bar read that the strategy layer consumes.

This is the module that answers, for a given bar: where is the resting
liquidity, was any of it just taken, which zones are live, and what does the
order book say right now. Everything it returns is restricted to information
knowable at that bar.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..core.types import OrderBook
from ..indicators.core import atr, volume_zscore
from .pools import (
    LiquidityPool, LiquiditySweep, detect_sweeps, find_liquidity_pools,
    unswept_pools,
)
from .structure import (
    StructureBreak, StructureState, SwingPoint, Trend, detect_structure_breaks,
    detect_swings, structure_state,
)
from .zones import (
    Zone, ZoneKind, VolumeProfile, active_zones, find_fair_value_gaps,
    find_order_blocks, nearest_zone, volume_profile,
)


@dataclass(slots=True)
class LiquidityContext:
    """Everything the strategy needs to know about liquidity at one bar."""

    bar_index: int
    timestamp: pd.Timestamp
    price: float
    atr: float

    structure: StructureState
    recent_sweep: LiquiditySweep | None
    pools_above: list[LiquidityPool] = field(default_factory=list)
    pools_below: list[LiquidityPool] = field(default_factory=list)
    demand_zones: list[Zone] = field(default_factory=list)
    supply_zones: list[Zone] = field(default_factory=list)
    profile: VolumeProfile | None = None
    depth_imbalance: float = 0.0
    spread_bps: float = 0.0

    # 0-100, how strongly liquidity conditions favour a trade
    score: float = 0.0
    bias: str = "neutral"          # "long" | "short" | "neutral"
    reasons: list[str] = field(default_factory=list)

    @property
    def nearest_pool_above(self) -> LiquidityPool | None:
        return self.pools_above[0] if self.pools_above else None

    @property
    def nearest_pool_below(self) -> LiquidityPool | None:
        return self.pools_below[0] if self.pools_below else None

    def to_dict(self) -> dict:
        return {
            "bar_index": self.bar_index,
            "timestamp": self.timestamp.isoformat(),
            "price": self.price,
            "atr": self.atr,
            "structure": self.structure.to_dict(),
            "recent_sweep": self.recent_sweep.to_dict() if self.recent_sweep else None,
            "pools_above": [p.to_dict() for p in self.pools_above[:5]],
            "pools_below": [p.to_dict() for p in self.pools_below[:5]],
            "demand_zones": [z.to_dict() for z in self.demand_zones[:5]],
            "supply_zones": [z.to_dict() for z in self.supply_zones[:5]],
            "profile": self.profile.to_dict() if self.profile else None,
            "depth_imbalance": round(self.depth_imbalance, 4),
            "spread_bps": round(self.spread_bps, 3),
            "score": round(self.score, 2),
            "bias": self.bias,
            "reasons": list(self.reasons),
        }


class LiquidityAnalyzer:
    """Computes structure/pool/zone state once, then serves per-bar contexts.

    The expensive detection runs once per dataset in `prepare()`; `context_at()`
    is then cheap enough to call on every bar of a backtest. The split exists
    for speed, and `context_at` is careful to expose only what was knowable at
    the requested bar.
    """

    def __init__(
        self,
        swing_lookback: int = 5,
        tolerance_pct: float = 0.001,
        min_penetration_pct: float = 0.0005,
        fvg_min_size_atr: float = 0.15,
        displacement_atr: float = 1.2,
        order_block_lookback: int = 30,
        atr_period: int = 14,
        sweep_recency_bars: int = 5,
        profile_bars: int = 200,
        zone_max_age_bars: int = 500,
        profile_refresh_bars: int = 10,
    ) -> None:
        self.swing_lookback = swing_lookback
        self.tolerance_pct = tolerance_pct
        self.min_penetration_pct = min_penetration_pct
        self.fvg_min_size_atr = fvg_min_size_atr
        self.displacement_atr = displacement_atr
        self.order_block_lookback = order_block_lookback
        self.atr_period = atr_period
        self.sweep_recency_bars = sweep_recency_bars
        self.profile_bars = profile_bars
        # Zones older than this are treated as expired. This is a correctness
        # rule first and a performance win second: an unmitigated gap from
        # hundreds of bars ago is no longer live structure, and scanning every
        # zone ever detected makes per-bar cost grow with dataset length.
        self.zone_max_age_bars = zone_max_age_bars
        self.profile_refresh_bars = profile_refresh_bars

        self._has_orderbook = False
        self._zones_by_index: dict[int, list[Zone]] = {}
        self._profile_cache: tuple[int, VolumeProfile | None] = (-1, None)
        self._df: pd.DataFrame | None = None
        self._atr: pd.Series | None = None
        self._swings: list[SwingPoint] = []
        self._breaks: list[StructureBreak] = []
        self._pools: list[LiquidityPool] = []
        self._sweeps: list[LiquiditySweep] = []
        self._fvgs: list[Zone] = []
        self._blocks: list[Zone] = []

    def prepare(self, df: pd.DataFrame) -> "LiquidityAnalyzer":
        """Run all detection over the dataset. Call once per symbol/timeframe."""
        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"dataframe missing columns: {sorted(missing)}")

        self._df = df
        self._atr = atr(df, self.atr_period)
        vol_z = volume_zscore(df, 20)

        self._swings = detect_swings(
            df, self.swing_lookback, self.swing_lookback, self._atr
        )
        self._breaks = detect_structure_breaks(df, self._swings)
        self._pools = find_liquidity_pools(self._swings, self.tolerance_pct)
        self._sweeps = detect_sweeps(
            df, self._pools, self._atr, self.min_penetration_pct, vol_z
        )
        self._fvgs = find_fair_value_gaps(df, self._atr, self.fvg_min_size_atr)
        self._blocks = find_order_blocks(
            df, self._atr, self.displacement_atr, self.order_block_lookback
        )

        # Bucket zones by creation bar so a per-bar lookup touches only the
        # recent window instead of the whole history.
        self._zones_by_index = {}
        for zone in self._fvgs + self._blocks:
            self._zones_by_index.setdefault(zone.index, []).append(zone)
        self._profile_cache = (-1, None)
        return self

    # -- per-bar ----------------------------------------------------------- #

    def context_at(
        self, bar_index: int, book: OrderBook | None = None
    ) -> LiquidityContext:
        if self._df is None or self._atr is None:
            raise RuntimeError("call prepare() before context_at()")
        df = self._df
        if not 0 <= bar_index < len(df):
            raise IndexError(f"bar_index {bar_index} out of range for {len(df)} bars")

        price = float(df["close"].iloc[bar_index])
        atr_val = float(self._atr.iloc[bar_index]) if pd.notna(
            self._atr.iloc[bar_index]
        ) else 0.0

        state = structure_state(df, self._swings, self._breaks, bar_index)

        recent = next(
            (
                s for s in reversed(self._sweeps)
                if bar_index - self.sweep_recency_bars <= s.index <= bar_index
            ),
            None,
        )

        above, below = unswept_pools(self._pools, bar_index, price)

        # Only zones created within the age window are live.
        oldest = max(0, bar_index - self.zone_max_age_bars)
        recent_zones: list[Zone] = []
        for idx in range(oldest, bar_index + 1):
            recent_zones.extend(self._zones_by_index.get(idx, ()))
        demand = active_zones(recent_zones, bar_index, ZoneKind.DEMAND)
        supply = active_zones(recent_zones, bar_index, ZoneKind.SUPPLY)

        # The profile shifts slowly, so recomputing it on every bar is wasted
        # work; refresh it on a stride and reuse it in between.
        cached_at, cached = self._profile_cache
        if cached_at >= 0 and 0 <= bar_index - cached_at < self.profile_refresh_bars:
            profile = cached
        else:
            start = max(0, bar_index - self.profile_bars + 1)
            profile = volume_profile(df.iloc[start: bar_index + 1])
            self._profile_cache = (bar_index, profile)

        self._has_orderbook = book is not None
        ctx = LiquidityContext(
            bar_index=bar_index,
            timestamp=df.index[bar_index],
            price=price,
            atr=atr_val,
            structure=state,
            recent_sweep=recent,
            pools_above=above,
            pools_below=below,
            demand_zones=demand,
            supply_zones=supply,
            profile=profile,
            depth_imbalance=book.depth_imbalance() if book else 0.0,
            spread_bps=(book.spread_bps or 0.0) if book else 0.0,
        )
        self._score(ctx)
        return ctx

    def _score(self, ctx: LiquidityContext) -> None:
        """Turn the raw structures into a 0-100 score and a directional bias.

        Weights are deliberately dominated by the sweep: it is the only
        component that is simultaneously a trigger, a direction and a stop
        location. The rest confirm.
        """
        long_score = 0.0
        short_score = 0.0
        reasons: list[str] = []

        # 1. Sweep — the primary trigger (up to 35 points)
        sweep = ctx.recent_sweep
        if sweep is not None:
            points = 35.0 * sweep.quality
            if sweep.bias == "long":
                long_score += points
                reasons.append(
                    f"swept sell-side liquidity at {sweep.pool.price:.6g} "
                    f"({sweep.pool.touches} touches, quality {sweep.quality:.2f})"
                )
            else:
                short_score += points
                reasons.append(
                    f"swept buy-side liquidity at {sweep.pool.price:.6g} "
                    f"({sweep.pool.touches} touches, quality {sweep.quality:.2f})"
                )
            if sweep.reclaimed:
                reasons.append("price reclaimed the swept level (rejection confirmed)")

        # 2. Structure (up to 20)
        if ctx.structure.trend is Trend.BULLISH:
            long_score += 20.0
            reasons.append("bullish structure: higher highs and higher lows")
        elif ctx.structure.trend is Trend.BEARISH:
            short_score += 20.0
            reasons.append("bearish structure: lower highs and lower lows")

        last_break = ctx.structure.last_break
        if last_break is not None and ctx.bar_index - last_break.index <= 10:
            bonus = 12.0 if last_break.kind.value == "choch" else 8.0
            if last_break.direction == "up":
                long_score += bonus
            else:
                short_score += bonus
            reasons.append(
                f"{last_break.kind.value.upper()} to the {last_break.direction} "
                f"at {last_break.price:.6g}"
            )

        # 3. Zone confluence (up to 18)
        dz = nearest_zone(ctx.demand_zones, ctx.price, ZoneKind.DEMAND)
        sz = nearest_zone(ctx.supply_zones, ctx.price, ZoneKind.SUPPLY)
        near = ctx.atr * 0.5 if ctx.atr > 0 else ctx.price * 0.002

        if dz is not None and dz.distance_from(ctx.price) <= near:
            long_score += 18.0 * max(0.35, dz.strength)
            reasons.append(
                f"price at {dz.origin.replace('_', ' ')} demand zone "
                f"{dz.bottom:.6g}-{dz.top:.6g}"
            )
        if sz is not None and sz.distance_from(ctx.price) <= near:
            short_score += 18.0 * max(0.35, sz.strength)
            reasons.append(
                f"price at {sz.origin.replace('_', ' ')} supply zone "
                f"{sz.bottom:.6g}-{sz.top:.6g}"
            )

        # 4. Draw on liquidity — is there room to a target? (up to 12)
        # A trade toward an untouched pool has a real destination; one into
        # empty space has to invent its targets.
        if ctx.pools_above and ctx.atr > 0:
            room = (ctx.pools_above[0].price - ctx.price) / ctx.atr
            if room >= 2.0:
                long_score += min(12.0, room * 2.0)
                reasons.append(
                    f"unswept buy-side liquidity {room:.1f} ATR above at "
                    f"{ctx.pools_above[0].price:.6g}"
                )
        if ctx.pools_below and ctx.atr > 0:
            room = (ctx.price - ctx.pools_below[0].price) / ctx.atr
            if room >= 2.0:
                short_score += min(12.0, room * 2.0)
                reasons.append(
                    f"unswept sell-side liquidity {room:.1f} ATR below at "
                    f"{ctx.pools_below[0].price:.6g}"
                )

        # Headroom actually available on this market. Volume profile needs
        # volume and depth imbalance needs an order book; neither exists for
        # Yahoo FX or spot metals. Unreachable points are removed from the
        # denominator instead of quietly counting against those markets.
        available = 100.0
        if ctx.profile is None:
            available -= 8.0
        if not self._has_orderbook:
            available -= 7.0

        # 5. Volume profile position (up to 8)
        if ctx.profile is not None:
            if ctx.price < ctx.profile.value_area_low:
                long_score += 8.0
                reasons.append("price below the value area (discount, POC above)")
            elif ctx.price > ctx.profile.value_area_high:
                short_score += 8.0
                reasons.append("price above the value area (premium, POC below)")

        # 6. Order book depth — confirmation only, never a trigger (up to 7)
        if abs(ctx.depth_imbalance) > 0.15:
            if ctx.depth_imbalance > 0:
                long_score += min(7.0, ctx.depth_imbalance * 14.0)
                reasons.append(
                    f"order book bid-heavy ({ctx.depth_imbalance:+.2f} imbalance)"
                )
            else:
                short_score += min(7.0, abs(ctx.depth_imbalance) * 14.0)
                reasons.append(
                    f"order book ask-heavy ({ctx.depth_imbalance:+.2f} imbalance)"
                )

        def normalise(raw: float) -> float:
            capped = min(available, raw)
            return min(100.0, capped * 100.0 / available) if available > 0 else 0.0

        if long_score > short_score:
            ctx.bias, ctx.score = "long", normalise(long_score)
        elif short_score > long_score:
            ctx.bias, ctx.score = "short", normalise(short_score)
        else:
            ctx.bias, ctx.score = "neutral", normalise(long_score)

        # Only surface the reasons that support the chosen side.
        ctx.reasons = _filter_reasons(reasons, ctx.bias)


_LONG_MARKERS = (
    "sell-side", "bullish", "demand", "discount", "bid-heavy", "to the up",
)
_SHORT_MARKERS = (
    "buy-side", "bearish", "supply", "premium", "ask-heavy", "to the down",
)


def _filter_reasons(reasons: list[str], bias: str) -> list[str]:
    """Drop reasons that argue for the other side, so the signal's rationale
    reads as an argument rather than a dump of every observation."""
    if bias == "neutral":
        return reasons
    wanted = _LONG_MARKERS if bias == "long" else _SHORT_MARKERS
    unwanted = _SHORT_MARKERS if bias == "long" else _LONG_MARKERS
    out = []
    for r in reasons:
        low = r.lower()
        if any(m in low for m in unwanted) and not any(m in low for m in wanted):
            continue
        out.append(r)
    return out
