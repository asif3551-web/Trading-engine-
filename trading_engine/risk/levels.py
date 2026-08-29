"""Stop and target placement.

This is where the 2-3R mandate is actually enforced, and the honest part of it
is worth stating plainly: the engine does not *make* a trade return 3R. What it
controls is geometry. It places the stop at the structural invalidation point —
the price at which the trade idea is simply wrong — and then checks whether the
real liquidity above/below leaves room to reach 2R and 3R before price runs into
an opposing pool. If it does not, the setup is rejected rather than stretched.

That ordering matters. Picking a target first and then working backwards to a
stop that makes the ratio look good is the most common way a system produces
beautiful backtested R:R and terrible live results: the stop ends up inside the
noise band and gets hit constantly.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.types import Side, TakeProfit
from ..liquidity.analyzer import LiquidityContext
from ..liquidity.zones import Zone, ZoneKind


@dataclass(slots=True)
class LevelPlan:
    """The full geometry of a proposed trade."""

    entry: float
    stop_loss: float
    take_profits: list[TakeProfit]
    stop_basis: str          # what determined the stop
    target_basis: str        # what determined the final target
    reward_risk: float
    rejected: str = ""       # non-empty means the geometry does not qualify

    @property
    def ok(self) -> bool:
        return not self.rejected

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop_loss)


def _round_away(price: float, side: Side, is_stop: bool, tick: float) -> float:
    """Round a level in the conservative direction.

    A stop always rounds further away from entry and a target always rounds
    further out, so tick rounding can never quietly improve the reported R.
    """
    if tick <= 0:
        return price
    import math
    if is_stop:
        # Long stop is below entry -> round down; short stop is above -> round up.
        fn = math.floor if side is Side.LONG else math.ceil
    else:
        fn = math.ceil if side is Side.LONG else math.floor
    return fn(price / tick) * tick


def build_stop(
    side: Side,
    entry: float,
    ctx: LiquidityContext,
    atr_mult: float = 1.5,
    buffer_atr: float = 0.25,
    entry_zone: Zone | None = None,
) -> tuple[float, str]:
    """Place the stop at structural invalidation, with a volatility buffer.

    Priority order, best first:
      1. Beyond the swept liquidity wick — the cleanest invalidation there is.
         If price returns through the level it just swept, the sweep failed.
      2. Beyond the entry zone's far edge.
      3. Beyond the framing swing point.
      4. Pure ATR fallback.

    The buffer exists because stops resting exactly on an obvious level are
    themselves liquidity, and get taken first.
    """
    atr = ctx.atr if ctx.atr > 0 else entry * 0.005
    buffer = atr * buffer_atr

    sweep = ctx.recent_sweep
    if sweep is not None and sweep.bias == ("long" if side is Side.LONG else "short"):
        # The extreme of the sweep is the invalidation point.
        if side is Side.LONG:
            level = sweep.pool.price - sweep.penetration
            return level - buffer, "beyond the swept low"
        level = sweep.pool.price + sweep.penetration
        return level + buffer, "beyond the swept high"

    if entry_zone is not None:
        if side is Side.LONG:
            return entry_zone.bottom - buffer, f"below the {entry_zone.origin} zone"
        return entry_zone.top + buffer, f"above the {entry_zone.origin} zone"

    swing = (
        ctx.structure.last_swing_low if side is Side.LONG
        else ctx.structure.last_swing_high
    )
    if swing is not None:
        candidate = (
            swing.price - buffer if side is Side.LONG else swing.price + buffer
        )
        # Only use the swing if it is actually on the losing side of entry.
        if (side is Side.LONG and candidate < entry) or (
            side is Side.SHORT and candidate > entry
        ):
            return candidate, "beyond the last swing point"

    fallback = atr * atr_mult
    return (
        (entry - fallback, "ATR volatility stop") if side is Side.LONG
        else (entry + fallback, "ATR volatility stop")
    )


def build_targets(
    side: Side,
    entry: float,
    stop_loss: float,
    ctx: LiquidityContext,
    ladder: tuple[float, ...] = (1.0, 2.0, 3.0),
    sizes: tuple[float, ...] = (0.40, 0.35, 0.25),
    snap_to_liquidity: bool = True,
) -> tuple[list[TakeProfit], str]:
    """Build the take-profit ladder at fixed R multiples.

    When `snap_to_liquidity` is on, the furthest target is pulled back to just
    short of the nearest opposing liquidity pool if that pool sits between the
    R-multiple target and entry. Resting liquidity is where price is being
    drawn, so it is a far more realistic exit than an arbitrary multiple — and
    trying to trade *through* a pool is how runners give back their gains.
    """
    if len(ladder) != len(sizes):
        raise ValueError("ladder and sizes must be the same length")

    risk = abs(entry - stop_loss)
    if risk <= 0:
        return [], "invalid risk"

    sign = side.sign
    basis = "fixed R multiples"

    # The realistic ceiling: the first opposing pool in the trade's direction.
    barrier: float | None = None
    if snap_to_liquidity:
        pools = ctx.pools_above if side is Side.LONG else ctx.pools_below
        if pools:
            barrier = pools[0].price

    targets: list[TakeProfit] = []
    for r, size_pct in zip(ladder, sizes):
        price = entry + sign * r * risk
        label = f"TP{len(targets) + 1}"

        if barrier is not None:
            beyond = (
                price > barrier if side is Side.LONG else price < barrier
            )
            if beyond:
                # Stop just short of the pool — front-run the crowd rather than
                # queueing behind it.
                pullback = risk * 0.1
                adjusted = barrier - sign * pullback
                adjusted_r = (adjusted - entry) * sign / risk
                if adjusted_r > 0:
                    price = adjusted
                    r = adjusted_r
                    basis = "capped at the opposing liquidity pool"

        targets.append(
            TakeProfit(price=price, r_multiple=round(r, 3), size_pct=size_pct,
                       label=label)
        )

    # Snapping can flatten the ladder; drop rungs that no longer advance.
    deduped: list[TakeProfit] = []
    for tp in targets:
        if deduped and tp.r_multiple <= deduped[-1].r_multiple + 0.05:
            # Merge into the previous rung rather than exiting twice at one price.
            deduped[-1].size_pct += tp.size_pct
            continue
        deduped.append(tp)

    return deduped, basis


def plan_levels(
    side: Side,
    entry: float,
    ctx: LiquidityContext,
    min_reward_risk: float = 2.0,
    min_expected_r: float = 1.5,
    ladder: tuple[float, ...] = (1.0, 2.0, 3.0),
    sizes: tuple[float, ...] = (0.40, 0.35, 0.25),
    atr_stop_mult: float = 1.5,
    min_stop_atr: float = 0.5,
    max_stop_atr: float = 3.0,
    entry_zone: Zone | None = None,
    tick_size: float = 0.0,
) -> LevelPlan:
    """Produce the complete stop/target geometry, or explain why it fails."""
    stop, stop_basis = build_stop(side, entry, ctx, atr_stop_mult, entry_zone=entry_zone)

    if tick_size > 0:
        stop = _round_away(stop, side, is_stop=True, tick=tick_size)

    # The stop must be on the losing side of entry or risk is undefined.
    if side is Side.LONG and stop >= entry:
        return LevelPlan(entry, stop, [], stop_basis, "", 0.0,
                         rejected="computed stop sits above entry for a long")
    if side is Side.SHORT and stop <= entry:
        return LevelPlan(entry, stop, [], stop_basis, "", 0.0,
                         rejected="computed stop sits below entry for a short")

    risk = abs(entry - stop)
    atr = ctx.atr if ctx.atr > 0 else 0.0

    if atr > 0:
        stop_atr = risk / atr
        if stop_atr < min_stop_atr:
            return LevelPlan(
                entry, stop, [], stop_basis, "", 0.0,
                rejected=(
                    f"stop only {stop_atr:.2f} ATR from entry — inside the noise "
                    f"band, it would be hit at random"
                ),
            )
        if stop_atr > max_stop_atr:
            return LevelPlan(
                entry, stop, [], stop_basis, "", 0.0,
                rejected=(
                    f"stop {stop_atr:.2f} ATR from entry — too wide to reach "
                    f"{min_reward_risk:.1f}R within a realistic move"
                ),
            )

    targets, target_basis = build_targets(side, entry, stop, ctx, ladder, sizes)
    if not targets:
        return LevelPlan(entry, stop, [], stop_basis, target_basis, 0.0,
                         rejected="no valid targets could be constructed")

    if tick_size > 0:
        for tp in targets:
            tp.price = _round_away(tp.price, side, is_stop=False, tick=tick_size)
            tp.r_multiple = round((tp.price - entry) * side.sign / risk, 3)

    total = sum(tp.size_pct for tp in targets)
    weighted_rr = (
        sum(tp.r_multiple * tp.size_pct for tp in targets) / total if total else 0.0
    )
    max_r = max(tp.r_multiple for tp in targets)

    # The furthest target is the "can this pay 2-3x the risk?" test. It is the
    # binding constraint, and it usually fails because the nearest opposing
    # liquidity pool sits too close for the trade to run.
    if max_r < min_reward_risk:
        return LevelPlan(
            entry, stop, targets, stop_basis, target_basis, weighted_rr,
            rejected=(
                f"furthest target only reaches {max_r:.2f}R, below the "
                f"{min_reward_risk:.2f} minimum — opposing liquidity is too "
                f"close to pay for a stop this wide"
            ),
        )

    # The weighted average catches the opposite failure: a ladder that reaches
    # 3R but banks almost everything at 0.5R is not a 3R trade.
    if weighted_rr < min_expected_r:
        return LevelPlan(
            entry, stop, targets, stop_basis, target_basis, weighted_rr,
            rejected=(
                f"size-weighted expectancy is only {weighted_rr:.2f}R "
                f"(minimum {min_expected_r:.2f}R) — too much size exits too early"
            ),
        )

    return LevelPlan(
        entry=entry,
        stop_loss=stop,
        take_profits=targets,
        stop_basis=stop_basis,
        target_basis=target_basis,
        reward_risk=weighted_rr,
    )


def select_entry(
    side: Side, ctx: LiquidityContext, prefer_zone: bool = True
) -> tuple[float, Zone | None, str]:
    """Choose the entry price.

    A limit entry at the near edge of a fresh zone beats a market entry: same
    idea, shorter stop distance, so the same target is a higher R. The trade-off
    is fill risk — the zone may never be revisited — which the backtester models
    honestly by requiring price to actually trade there.
    """
    price = ctx.price
    if not prefer_zone:
        return price, None, "market at close"

    zones = ctx.demand_zones if side is Side.LONG else ctx.supply_zones
    if not zones:
        return price, None, "market at close"

    tolerance = ctx.atr * 1.5 if ctx.atr > 0 else price * 0.01
    # Only consider zones price has not already passed through.
    viable = [
        z for z in zones
        if (side is Side.LONG and z.top <= price and price - z.top <= tolerance)
        or (side is Side.SHORT and z.bottom >= price and z.bottom - price <= tolerance)
    ]
    if not viable:
        return price, None, "market at close"

    zone = max(viable, key=lambda z: z.strength)
    entry = zone.top if side is Side.LONG else zone.bottom
    return entry, zone, f"limit at the {zone.origin.replace('_', ' ')} edge"
