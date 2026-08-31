"""The liquidity sweep strategy — the engine's primary signal generator.

The thesis, in one paragraph: markets move to where the orders are. Clusters of
equal highs and lows hold stop-loss orders; price is repeatedly drawn into them,
takes them out, and then reverses because that liquidity is exactly what large
participants needed to fill against. The tradeable event is therefore not the
breakout but the *failed* breakout — the sweep and reclaim.

Why that specific event, rather than a moving-average cross or an RSI level: it
is the only common setup that hands you a precise, structural invalidation point
(just beyond the sweep wick) at the same moment it gives you a direction. A tight
stop is not a cosmetic detail — it is the entire reason a 3R target can sit
inside a normal day's range instead of requiring an exceptional move.

The strategy takes a trade only when several independent things agree:
  - liquidity was swept and reclaimed (the trigger),
  - market structure supports the direction,
  - price is at a fresh demand/supply zone (the entry),
  - the higher timeframe is not fighting it,
  - fundamentals do not veto it,
  - and the resulting geometry clears the minimum reward:risk.

Each of those alone is weak. Requiring confluence cuts the number of trades hard,
which is the point: the edge is in selectivity, not frequency.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..config import StrategyConfig
from ..core.types import AssetClass, Side, Signal
from ..fundamentals.context import FundamentalContext
from ..indicators.core import add_all
from ..liquidity.analyzer import LiquidityAnalyzer, LiquidityContext
from ..liquidity.structure import Trend
from ..risk.levels import LevelPlan, plan_levels, select_entry


@dataclass(slots=True)
class Evaluation:
    """The full reasoning for one bar, whether or not it produced a signal."""

    bar_index: int
    timestamp: pd.Timestamp
    signal: Signal | None = None
    rejected: str = ""
    confidence: float = 0.0
    liquidity_score: float = 0.0
    technical_score: float = 0.0
    fundamental_score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def has_signal(self) -> bool:
        return self.signal is not None

    def to_dict(self) -> dict:
        return {
            "bar_index": self.bar_index,
            "timestamp": self.timestamp.isoformat(),
            "signal": self.signal.to_dict() if self.signal else None,
            "rejected": self.rejected,
            "confidence": round(self.confidence, 2),
            "liquidity_score": round(self.liquidity_score, 2),
            "technical_score": round(self.technical_score, 2),
            "fundamental_score": round(self.fundamental_score, 2),
            "reasons": list(self.reasons),
        }


class LiquiditySweepStrategy:
    """Generates signals from liquidity sweeps confirmed by structure and zones."""

    name = "liquidity_sweep"

    def __init__(
        self,
        config: StrategyConfig,
        min_reward_risk: float = 2.0,
        min_expected_r: float = 1.5,
        min_stop_atr: float = 0.5,
        max_stop_atr: float = 3.0,
    ) -> None:
        self.config = config
        self.min_reward_risk = min_reward_risk
        self.min_expected_r = min_expected_r
        self.min_stop_atr = min_stop_atr
        self.max_stop_atr = max_stop_atr

        self.analyzer = LiquidityAnalyzer(
            swing_lookback=config.swing_lookback,
            tolerance_pct=config.liquidity_tolerance_pct,
            min_penetration_pct=config.min_sweep_penetration_pct,
            fvg_min_size_atr=config.fvg_min_size_atr,
            displacement_atr=config.displacement_atr,
            order_block_lookback=config.order_block_lookback,
            atr_period=config.atr_period,
        )
        self._df: pd.DataFrame | None = None
        self._htf_bias: pd.Series | None = None

    # -- preparation ------------------------------------------------------- #

    def prepare(
        self, df: pd.DataFrame, htf_df: pd.DataFrame | None = None
    ) -> "LiquiditySweepStrategy":
        """Compute indicators and liquidity structures once for the dataset."""
        self._df = add_all(df, self.config.atr_period)
        self.analyzer.prepare(self._df)
        self._htf_bias = (
            self._compute_htf_bias(self._df, htf_df) if htf_df is not None else None
        )
        return self

    def _compute_htf_bias(
        self, df: pd.DataFrame, htf_df: pd.DataFrame
    ) -> pd.Series:
        """Map higher-timeframe trend onto the trading timeframe's index.

        The `shift(1)` and `reindex(method='ffill')` together are what keep this
        causal: a 4h bar's trend is only usable once that bar has *closed*, so
        each LTF bar sees the last fully-closed HTF bar and never the one still
        forming around it.
        """
        htf = add_all(htf_df, self.config.atr_period)
        bias = pd.Series(0.0, index=htf.index)
        bias[(htf["close"] > htf["ema_50"]) & (htf["ema_20"] > htf["ema_50"])] = 1.0
        bias[(htf["close"] < htf["ema_50"]) & (htf["ema_20"] < htf["ema_50"])] = -1.0
        closed = bias.shift(1)
        return closed.reindex(df.index, method="ffill").fillna(0.0)

    # -- evaluation -------------------------------------------------------- #

    def evaluate(
        self,
        bar_index: int,
        fundamentals: FundamentalContext | None = None,
        book=None,
        asset_class: AssetClass = AssetClass.CRYPTO,
        symbol: str = "",
        tick_size: float = 0.0,
    ) -> Evaluation:
        if self._df is None:
            raise RuntimeError("call prepare() before evaluate()")

        df = self._df
        ts = df.index[bar_index]
        ev = Evaluation(bar_index=bar_index, timestamp=ts)

        if bar_index < self.config.lookback_bars // 10:
            ev.rejected = "insufficient history"
            return ev

        ctx = self.analyzer.context_at(bar_index, book)
        ev.liquidity_score = ctx.score

        # --- fundamental veto comes first: it is a hard gate, and evaluating
        # --- the rest would waste work and produce misleading diagnostics.
        if fundamentals is not None and not fundamentals.tradeable:
            ev.rejected = fundamentals.block_reason
            return ev

        # --- direction ---
        side = self._direction(ctx, fundamentals)
        if side is None:
            ev.rejected = "no directional bias"
            return ev

        # --- trigger ---
        if self.config.require_liquidity_sweep:
            sweep = ctx.recent_sweep
            if sweep is None:
                ev.rejected = "no recent liquidity sweep"
                return ev
            if sweep.bias != side.value:
                ev.rejected = "the recent sweep points the other way"
                return ev
            if not sweep.reclaimed:
                ev.rejected = (
                    "liquidity was swept but price did not reclaim the level — "
                    "this is a break, not a sweep"
                )
                return ev

        # --- higher timeframe ---
        if self.config.require_htf_alignment and self._htf_bias is not None:
            htf = float(self._htf_bias.iloc[bar_index])
            if htf != 0.0 and ((htf > 0) != (side is Side.LONG)):
                ev.rejected = (
                    f"higher timeframe ({self.config.htf_timeframe}) is "
                    f"{'bullish' if htf > 0 else 'bearish'}, against this setup"
                )
                return ev

        # --- scores ---
        tech_score, tech_reasons = self._technical_score(bar_index, side)
        ev.technical_score = tech_score

        cfg = self.config
        components = [
            (ctx.score, cfg.weight_liquidity),
            (tech_score, cfg.weight_technical),
        ]
        fund_reasons: list[str] = []

        if fundamentals is not None:
            raw = fundamentals.bias_score
            aligned = raw if side is Side.LONG else -raw
            fund_score = max(0.0, min(100.0, 50.0 + aligned * 0.5))
            fund_reasons = list(fundamentals.reasons)
            components.append((fund_score, cfg.weight_fundamental))
        else:
            # With no fundamental data connected, the component *abstains*
            # rather than voting a neutral 50. Injecting a synthetic midpoint
            # would drag every score toward the middle and make the confidence
            # threshold behave differently depending on whether a data source
            # happened to be reachable.
            fund_score = 0.0
        ev.fundamental_score = fund_score

        total_weight = sum(w for _, w in components)
        confidence = (
            sum(score * w for score, w in components) / total_weight
            if total_weight > 0 else 0.0
        )
        ev.confidence = confidence
        ev.reasons = ctx.reasons + tech_reasons + fund_reasons

        if confidence < cfg.min_confidence:
            ev.rejected = (
                f"confluence score {confidence:.1f} below the "
                f"{cfg.min_confidence:.1f} threshold"
            )
            return ev

        # --- geometry ---
        entry, zone, entry_basis = select_entry(side, ctx)
        plan = plan_levels(
            side=side,
            entry=entry,
            ctx=ctx,
            min_reward_risk=self.min_reward_risk,
            min_expected_r=self.min_expected_r,
            ladder=cfg.tp_ladder,
            sizes=cfg.tp_sizes,
            atr_stop_mult=cfg.atr_stop_mult,
            min_stop_atr=self.min_stop_atr,
            max_stop_atr=self.max_stop_atr,
            entry_zone=zone,
            tick_size=tick_size,
        )
        if not plan.ok:
            ev.rejected = plan.rejected
            return ev

        ev.signal = self._build_signal(
            ts, symbol or str(df.attrs.get("symbol", "")), side, plan, ctx,
            confidence, ev, entry_basis, asset_class, fundamentals,
        )
        return ev

    # -- helpers ----------------------------------------------------------- #

    def _direction(
        self, ctx: LiquidityContext, fundamentals: FundamentalContext | None
    ) -> Side | None:
        """Liquidity sets the direction; fundamentals can only veto it.

        Fundamentals are deliberately not allowed to *choose* the side. They are
        slow and coarse relative to a 15m sweep, and letting them pick direction
        produces trades with no structural stop.
        """
        if ctx.bias == "long":
            side = Side.LONG
        elif ctx.bias == "short":
            side = Side.SHORT
        else:
            return None

        if fundamentals is not None:
            fb = fundamentals.bias
            if fb != "neutral" and fb != side.value:
                # Only a strong fundamental disagreement vetoes.
                if abs(fundamentals.bias_score) >= 40:
                    return None
        return side

    def _technical_score(
        self, bar_index: int, side: Side
    ) -> tuple[float, list[str]]:
        """0-100 from classic indicators. Confirmation only — none of these
        trigger a trade by themselves."""
        assert self._df is not None
        row = self._df.iloc[bar_index]
        score = 50.0
        reasons: list[str] = []
        long = side is Side.LONG

        def val(name: str) -> float | None:
            v = row.get(name)
            return None if v is None or pd.isna(v) else float(v)

        ema20, ema50 = val("ema_20"), val("ema_50")
        close = val("close")
        if ema20 is not None and ema50 is not None and close is not None:
            if long and ema20 > ema50 and close > ema50:
                score += 12.0
                reasons.append("price above a rising 20/50 EMA stack")
            elif not long and ema20 < ema50 and close < ema50:
                score += 12.0
                reasons.append("price below a falling 20/50 EMA stack")
            else:
                score -= 8.0

        adx = val("adx")
        if adx is not None:
            if adx > 25:
                score += 10.0
                reasons.append(f"ADX {adx:.0f} — trending conditions")
            elif adx < 15:
                score -= 10.0

        chop = val("choppiness")
        if chop is not None:
            if chop > 61:
                score -= 12.0
                reasons.append(f"choppiness {chop:.0f} — ranging, lower conviction")
            elif chop < 38:
                score += 8.0

        rsi = val("rsi")
        if rsi is not None:
            # After a sweep, an oversold/overbought reading supports the
            # reversal rather than warning against it.
            if long and rsi < 35:
                score += 10.0
                reasons.append(f"RSI {rsi:.0f} — oversold into the sweep")
            elif not long and rsi > 65:
                score += 10.0
                reasons.append(f"RSI {rsi:.0f} — overbought into the sweep")
            elif (long and rsi > 75) or (not long and rsi < 25):
                score -= 12.0
                reasons.append(f"RSI {rsi:.0f} — stretched, poor entry location")

        # Volume components. Spot FX and spot metals report no volume at all,
        # so these must ABSTAIN rather than simply fail to score: a component
        # that can only add points silently penalises every market that cannot
        # supply it, and the fixed confidence threshold then rejects FX and
        # metals setups that would have passed on crypto. `available` tracks
        # the headroom actually on offer so the score can be renormalised.
        available = 100.0
        vz = val("volume_z")
        cmf_value = val("cmf")
        volume_present = vz is not None or cmf_value is not None

        if volume_present:
            if vz is not None and vz > 1.0:
                score += 10.0
                reasons.append(
                    f"volume {vz:.1f}σ above average — real participation"
                )
            if cmf_value is not None:
                if (long and cmf_value > 0.05) or (not long and cmf_value < -0.05):
                    score += 8.0
                    reasons.append(f"money flow confirms ({cmf_value:+.2f})")
        else:
            # 18 points of upside are unreachable on this market.
            available -= 18.0

        score = max(0.0, min(available, score))
        if available < 100.0:
            score = score * 100.0 / available
        return max(0.0, min(100.0, score)), reasons

    def _build_signal(
        self,
        ts: pd.Timestamp,
        symbol: str,
        side: Side,
        plan: LevelPlan,
        ctx: LiquidityContext,
        confidence: float,
        ev: Evaluation,
        entry_basis: str,
        asset_class: AssetClass,
        fundamentals: FundamentalContext | None,
    ) -> Signal:
        reasons = list(ev.reasons)
        reasons.append(f"entry: {entry_basis}")
        reasons.append(f"stop: {plan.stop_basis}")
        reasons.append(f"targets: {plan.target_basis}")

        return Signal(
            timestamp=ts.to_pydatetime(),
            symbol=symbol,
            timeframe=self.config.timeframe,
            side=side,
            entry=plan.entry,
            stop_loss=plan.stop_loss,
            take_profits=plan.take_profits,
            confidence=confidence,
            reasons=reasons,
            asset_class=asset_class,
            atr=ctx.atr,
            liquidity_score=ev.liquidity_score,
            fundamental_score=ev.fundamental_score,
            technical_score=ev.technical_score,
            regime=(
                fundamentals.regime.value if fundamentals else
                ctx.structure.trend.value
            ),
            meta={
                "structure": ctx.structure.to_dict(),
                "sweep": ctx.recent_sweep.to_dict() if ctx.recent_sweep else None,
                "entry_basis": entry_basis,
                "stop_basis": plan.stop_basis,
                "target_basis": plan.target_basis,
                "depth_imbalance": round(ctx.depth_imbalance, 4),
                "profile": ctx.profile.to_dict() if ctx.profile else None,
            },
        )

    # -- convenience ------------------------------------------------------- #

    def scan(
        self,
        start: int | None = None,
        end: int | None = None,
        fundamentals: FundamentalContext | None = None,
        symbol: str = "",
        asset_class: AssetClass = AssetClass.CRYPTO,
    ) -> list[Evaluation]:
        """Evaluate a range of bars. Used by the backtester and the CLI scan."""
        if self._df is None:
            raise RuntimeError("call prepare() before scan()")
        start = start if start is not None else 0
        end = end if end is not None else len(self._df)
        return [
            self.evaluate(i, fundamentals, symbol=symbol, asset_class=asset_class)
            for i in range(max(0, start), min(end, len(self._df)))
        ]
