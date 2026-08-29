"""Fundamental and macro context.

Fundamentals rarely time an entry well, but they veto trades reliably. This
module produces two things:

  1. A hard **tradeable / not tradeable** verdict — chiefly the event blackout.
     Opening new risk into a scheduled high-impact release means widened
     spreads, skipped stops and unbounded slippage. No technical setup is good
     enough to pay for that.
  2. A soft **directional bias score** in [-100, +100] that nudges the
     confluence score but never triggers a trade on its own.

Every input is optional. With no data connected the module returns a neutral,
permissive context rather than blocking everything, so the engine still runs
offline — but `has_calendar_data` tells the caller the blackout check was
vacuous, and the live trader treats that as a reason for caution rather than
silently assuming the calendar was clear.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from ..core.types import AssetClass


class Impact(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Regime(str, Enum):
    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    NEUTRAL = "neutral"


@dataclass(slots=True)
class EconomicEvent:
    """A scheduled release. `actual` is None until it prints."""

    timestamp: datetime
    name: str
    currency: str
    impact: Impact = Impact.MEDIUM
    forecast: float | None = None
    previous: float | None = None
    actual: float | None = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=timezone.utc)

    @property
    def surprise(self) -> float | None:
        """Actual vs forecast, normalised. The market trades the surprise, not
        the number, so this is what carries directional information."""
        if self.actual is None or self.forecast is None:
            return None
        denom = abs(self.forecast) if abs(self.forecast) > 1e-9 else 1.0
        return (self.actual - self.forecast) / denom

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "name": self.name,
            "currency": self.currency,
            "impact": self.impact.value,
            "forecast": self.forecast,
            "previous": self.previous,
            "actual": self.actual,
            "surprise": self.surprise,
        }


@dataclass(slots=True)
class CryptoFundamentals:
    """Positioning data from perpetual futures venues.

    These are the closest thing crypto has to fundamentals, and they are mostly
    useful as a crowding warning: extreme funding means one side is paying to
    hold, and that side is the one that gets squeezed.
    """

    funding_rate_bps: float | None = None     # per 8h period
    open_interest: float | None = None
    open_interest_change_pct: float | None = None
    long_short_ratio: float | None = None
    basis_bps: float | None = None            # perp vs spot

    def to_dict(self) -> dict:
        return {
            "funding_rate_bps": self.funding_rate_bps,
            "open_interest": self.open_interest,
            "open_interest_change_pct": self.open_interest_change_pct,
            "long_short_ratio": self.long_short_ratio,
            "basis_bps": self.basis_bps,
        }


@dataclass(slots=True)
class MacroSnapshot:
    """Cross-asset risk read."""

    dxy_change_pct: float | None = None        # dollar strength
    yield_10y_change_bps: float | None = None
    vix: float | None = None
    spx_change_pct: float | None = None

    def to_dict(self) -> dict:
        return {
            "dxy_change_pct": self.dxy_change_pct,
            "yield_10y_change_bps": self.yield_10y_change_bps,
            "vix": self.vix,
            "spx_change_pct": self.spx_change_pct,
        }


@dataclass(slots=True)
class FundamentalContext:
    """The verdict handed to the strategy."""

    timestamp: datetime
    symbol: str
    tradeable: bool = True
    block_reason: str = ""
    bias_score: float = 0.0          # -100 (bearish) .. +100 (bullish)
    regime: Regime = Regime.NEUTRAL
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    upcoming_events: list[EconomicEvent] = field(default_factory=list)
    crypto: CryptoFundamentals | None = None
    macro: MacroSnapshot | None = None
    has_calendar_data: bool = False

    @property
    def bias(self) -> str:
        if self.bias_score >= 15:
            return "long"
        if self.bias_score <= -15:
            return "short"
        return "neutral"

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "symbol": self.symbol,
            "tradeable": self.tradeable,
            "block_reason": self.block_reason,
            "bias_score": round(self.bias_score, 2),
            "bias": self.bias,
            "regime": self.regime.value,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "upcoming_events": [e.to_dict() for e in self.upcoming_events[:5]],
            "crypto": self.crypto.to_dict() if self.crypto else None,
            "macro": self.macro.to_dict() if self.macro else None,
            "has_calendar_data": self.has_calendar_data,
        }


# Currencies whose events move each asset class.
_RELEVANT_CURRENCIES = {
    AssetClass.CRYPTO: {"USD"},
    AssetClass.EQUITY: {"USD"},
    AssetClass.INDEX: {"USD"},
    AssetClass.COMMODITY: {"USD"},
    AssetClass.FUTURES: {"USD"},
}


class FundamentalAnalyzer:
    """Builds a `FundamentalContext` from whatever data is available."""

    def __init__(
        self,
        blackout_before_min: int = 60,
        blackout_after_min: int = 30,
        high_impact_only: bool = True,
        funding_extreme_bps: float = 5.0,
        oi_surge_pct: float = 10.0,
        earnings_blackout_days: int = 2,
    ) -> None:
        self.blackout_before = timedelta(minutes=blackout_before_min)
        self.blackout_after = timedelta(minutes=blackout_after_min)
        self.high_impact_only = high_impact_only
        self.funding_extreme_bps = funding_extreme_bps
        self.oi_surge_pct = oi_surge_pct
        self.earnings_blackout_days = earnings_blackout_days

        self._events: list[EconomicEvent] = []
        self._earnings: dict[str, datetime] = {}

    # -- data loading ------------------------------------------------------ #

    def load_events(self, events: list[EconomicEvent]) -> None:
        self._events = sorted(events, key=lambda e: e.timestamp)

    def set_earnings_date(self, symbol: str, when: datetime) -> None:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        self._earnings[symbol.upper()] = when

    # -- analysis ---------------------------------------------------------- #

    def analyse(
        self,
        now: datetime,
        symbol: str,
        asset_class: AssetClass = AssetClass.CRYPTO,
        crypto: CryptoFundamentals | None = None,
        macro: MacroSnapshot | None = None,
    ) -> FundamentalContext:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        ctx = FundamentalContext(
            timestamp=now, symbol=symbol, crypto=crypto, macro=macro,
            has_calendar_data=bool(self._events),
        )

        self._apply_event_blackout(ctx, now, asset_class)
        self._apply_earnings_blackout(ctx, now, symbol, asset_class)
        if macro is not None:
            self._apply_macro(ctx, macro, asset_class)
        if crypto is not None and asset_class is AssetClass.CRYPTO:
            self._apply_crypto(ctx, crypto)

        if not ctx.has_calendar_data:
            ctx.warnings.append(
                "no economic calendar loaded — the event blackout check did not run"
            )

        ctx.bias_score = max(-100.0, min(100.0, ctx.bias_score))
        return ctx

    def _relevant(self, event: EconomicEvent, asset_class: AssetClass) -> bool:
        if self.high_impact_only and event.impact is not Impact.HIGH:
            return False
        currencies = _RELEVANT_CURRENCIES.get(asset_class)
        if currencies is None:      # FX: every currency matters
            return True
        return event.currency.upper() in currencies

    def _apply_event_blackout(
        self, ctx: FundamentalContext, now: datetime, asset_class: AssetClass
    ) -> None:
        window_start = now - self.blackout_after
        window_end = now + self.blackout_before

        for event in self._events:
            if not self._relevant(event, asset_class):
                continue
            if window_start <= event.timestamp <= window_end:
                ctx.tradeable = False
                delta_min = (event.timestamp - now).total_seconds() / 60.0
                when = (
                    f"in {delta_min:.0f}m" if delta_min >= 0
                    else f"{abs(delta_min):.0f}m ago"
                )
                ctx.block_reason = (
                    f"{event.impact.value}-impact event blackout: "
                    f"{event.name} ({event.currency}) {when}"
                )
                break

        ctx.upcoming_events = [
            e for e in self._events
            if now <= e.timestamp <= now + timedelta(hours=24)
            and self._relevant(e, asset_class)
        ][:5]

        # A release that already printed carries direction for a short window.
        recent_cutoff = now - timedelta(hours=4)
        for event in reversed(self._events):
            if event.timestamp > now or event.timestamp < recent_cutoff:
                continue
            surprise = event.surprise
            if surprise is None or not self._relevant(event, asset_class):
                continue
            contribution = max(-20.0, min(20.0, surprise * 40.0))
            # A strong USD print is a headwind for USD-quoted risk assets.
            if asset_class in (AssetClass.CRYPTO, AssetClass.EQUITY, AssetClass.INDEX):
                contribution = -contribution
            ctx.bias_score += contribution
            ctx.reasons.append(
                f"{event.name} printed {surprise:+.1%} vs forecast"
            )
            break

    def _apply_earnings_blackout(
        self,
        ctx: FundamentalContext,
        now: datetime,
        symbol: str,
        asset_class: AssetClass,
    ) -> None:
        if asset_class is not AssetClass.EQUITY:
            return
        when = self._earnings.get(symbol.upper())
        if when is None:
            return
        days = abs((when - now).total_seconds()) / 86400.0
        if days <= self.earnings_blackout_days:
            ctx.tradeable = False
            ctx.block_reason = (
                f"earnings blackout: {symbol} reports "
                f"{when.date().isoformat()} ({days:.1f}d away)"
            )

    def _apply_macro(
        self, ctx: FundamentalContext, macro: MacroSnapshot, asset_class: AssetClass
    ) -> None:
        risk_score = 0.0

        if macro.vix is not None:
            if macro.vix > 30:
                risk_score -= 30.0
                ctx.reasons.append(f"VIX at {macro.vix:.1f} — stressed conditions")
            elif macro.vix > 20:
                risk_score -= 12.0
                ctx.reasons.append(f"VIX at {macro.vix:.1f} — elevated volatility")
            elif macro.vix < 15:
                risk_score += 10.0
                ctx.reasons.append(f"VIX at {macro.vix:.1f} — calm conditions")

        if macro.dxy_change_pct is not None and abs(macro.dxy_change_pct) > 0.3:
            # A rising dollar drains liquidity from risk assets.
            contribution = -macro.dxy_change_pct * 15.0
            risk_score += max(-20.0, min(20.0, contribution))
            direction = "strengthening" if macro.dxy_change_pct > 0 else "weakening"
            ctx.reasons.append(f"DXY {direction} {macro.dxy_change_pct:+.2f}%")

        if macro.yield_10y_change_bps is not None and abs(macro.yield_10y_change_bps) > 5:
            contribution = -macro.yield_10y_change_bps * 0.8
            risk_score += max(-15.0, min(15.0, contribution))
            ctx.reasons.append(
                f"10y yield {macro.yield_10y_change_bps:+.0f}bps"
            )

        if macro.spx_change_pct is not None and abs(macro.spx_change_pct) > 0.5:
            contribution = macro.spx_change_pct * 8.0
            risk_score += max(-15.0, min(15.0, contribution))
            ctx.reasons.append(f"S&P {macro.spx_change_pct:+.2f}%")

        if risk_score >= 15:
            ctx.regime = Regime.RISK_ON
        elif risk_score <= -15:
            ctx.regime = Regime.RISK_OFF
        else:
            ctx.regime = Regime.NEUTRAL

        # Risk assets follow the regime; defensive assets invert it.
        if asset_class in (
            AssetClass.CRYPTO, AssetClass.EQUITY, AssetClass.INDEX
        ):
            ctx.bias_score += risk_score * 0.6

    def _apply_crypto(
        self, ctx: FundamentalContext, cf: CryptoFundamentals
    ) -> None:
        if cf.funding_rate_bps is not None:
            f = cf.funding_rate_bps
            if abs(f) >= self.funding_extreme_bps:
                # Crowded positioning is a fade signal: the paying side is the
                # one that gets liquidated.
                ctx.bias_score += -25.0 if f > 0 else 25.0
                crowd = "longs" if f > 0 else "shorts"
                ctx.reasons.append(
                    f"funding at {f:+.2f}bps — {crowd} crowded and paying, "
                    f"squeeze risk against them"
                )
                ctx.warnings.append(f"extreme funding ({f:+.2f}bps)")
            elif abs(f) >= self.funding_extreme_bps * 0.5:
                ctx.bias_score += -8.0 if f > 0 else 8.0

        if cf.open_interest_change_pct is not None:
            oi = cf.open_interest_change_pct
            if oi >= self.oi_surge_pct:
                ctx.reasons.append(
                    f"open interest +{oi:.1f}% — new positioning, moves have fuel"
                )
                ctx.warnings.append("rapid OI build — expect violent liquidation moves")
            elif oi <= -self.oi_surge_pct:
                ctx.reasons.append(
                    f"open interest {oi:.1f}% — positions unwinding"
                )

        if cf.long_short_ratio is not None:
            lsr = cf.long_short_ratio
            if lsr > 2.5:
                ctx.bias_score -= 12.0
                ctx.reasons.append(f"long/short ratio {lsr:.2f} — one-sided long")
            elif lsr < 0.5:
                ctx.bias_score += 12.0
                ctx.reasons.append(f"long/short ratio {lsr:.2f} — one-sided short")

        if cf.basis_bps is not None and abs(cf.basis_bps) > 50:
            ctx.reasons.append(f"perp basis {cf.basis_bps:+.0f}bps vs spot")
