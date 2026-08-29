"""Risk management: position sizing and the portfolio guardrails.

Two responsibilities, kept separate:

  `PositionSizer`  — how big, given a stop. Size is always *derived* from the
                     stop distance; a code path that returns a size without
                     consulting the stop is a bug, not a shortcut.

  `RiskManager`    — may this trade be taken at all, given everything already
                     open and everything already lost today. This is where the
                     circuit breakers live.

The layered limits exist because per-trade risk alone does not bound the damage:
five uncorrelated 1% positions is 5% at risk, and five *correlated* ones is
effectively a single 5% position that can gap through every stop at once.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone, date

from ..config import RiskConfig
from ..core.types import Position, Side, Signal, Trade, safe_div


@dataclass(slots=True)
class SizingResult:
    size: float
    risk_amount: float
    notional: float
    leverage: float
    capped_by: str = ""      # which constraint bound the size, "" if none

    @property
    def is_tradeable(self) -> bool:
        return self.size > 0


class PositionSizer:
    """Turns a stop distance into a position size."""

    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def size_for(
        self,
        signal: Signal,
        equity: float,
        risk_fraction: float | None = None,
        avg_volume: float | None = None,
        asset_volatility: float | None = None,
        win_rate: float | None = None,
    ) -> SizingResult:
        cfg = self.config
        risk_fraction = (
            cfg.risk_per_trade if risk_fraction is None else risk_fraction
        )
        # Record the clamp, so a caller asking for more than the ceiling sees
        # why it did not get it rather than an empty `capped_by`.
        clamped_by = (
            "max_risk_per_trade" if risk_fraction > cfg.max_risk_per_trade else ""
        )
        risk_fraction = min(risk_fraction, cfg.max_risk_per_trade)

        stop_distance = signal.risk_per_unit
        if stop_distance <= 0 or equity <= 0:
            return SizingResult(0.0, 0.0, 0.0, 1.0, "invalid stop or equity")

        risk_amount = equity * risk_fraction

        # -- base size from the chosen model ------------------------------- #
        model = cfg.sizing_model
        if model == "fixed_fractional":
            size = risk_amount / stop_distance
        elif model == "atr_normalised":
            # Normalise so a 1-ATR adverse move costs the same everywhere.
            atr_unit = signal.atr if signal.atr > 0 else stop_distance
            size = risk_amount / atr_unit
        elif model == "vol_target":
            vol = asset_volatility if asset_volatility and asset_volatility > 0 else None
            if vol is None:
                size = risk_amount / stop_distance
            else:
                size = (equity * cfg.target_volatility) / (vol * signal.entry)
        elif model == "kelly":
            size = self._kelly_size(signal, equity, stop_distance, win_rate)
        else:
            raise ValueError(f"unknown sizing_model {model!r}")

        capped_by = clamped_by

        # -- caps ----------------------------------------------------------- #
        # Never let a model exceed the per-trade risk budget.
        max_by_risk = (equity * cfg.max_risk_per_trade) / stop_distance
        if size > max_by_risk:
            size, capped_by = max_by_risk, "max_risk_per_trade"

        max_notional = equity * cfg.max_notional_pct * cfg.max_leverage
        max_by_notional = max_notional / signal.entry
        if size > max_by_notional:
            size, capped_by = max_by_notional, "max_notional"

        # Liquidity cap: never take more than ~1% of average volume, or the
        # slippage on entry and (worse) on the stop exit destroys the edge.
        if avg_volume and avg_volume > 0:
            max_by_liquidity = avg_volume * 0.01
            if size > max_by_liquidity:
                size, capped_by = max_by_liquidity, "liquidity"

        if size <= 0:
            return SizingResult(0.0, 0.0, 0.0, 1.0, capped_by or "size rounded to zero")

        notional = size * signal.entry
        leverage = safe_div(notional, equity, 1.0)
        actual_risk = size * stop_distance

        return SizingResult(
            size=size,
            risk_amount=actual_risk,
            notional=notional,
            leverage=max(1.0, leverage),
            capped_by=capped_by,
        )

    def _kelly_size(
        self, signal: Signal, equity: float, stop_distance: float,
        win_rate: float | None,
    ) -> float:
        """Fractional Kelly. Full Kelly is never correct here — W and R are
        estimates, and Kelly is brutally sensitive to overestimating either."""
        cfg = self.config
        w = win_rate if win_rate is not None else 0.40
        rr = signal.reward_risk if signal.reward_risk > 0 else 1.0
        edge = w - (1.0 - w) / rr
        if edge <= 0:
            return 0.0
        fraction = edge * cfg.kelly_fraction
        fraction = min(fraction, cfg.max_risk_per_trade)
        return (equity * fraction) / stop_distance


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    reason: str = ""
    risk_fraction: float = 0.0
    sizing: SizingResult | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "risk_fraction": round(self.risk_fraction, 5),
            "size": self.sizing.size if self.sizing else 0.0,
            "risk_amount": round(self.sizing.risk_amount, 2) if self.sizing else 0.0,
            "capped_by": self.sizing.capped_by if self.sizing else "",
            "warnings": list(self.warnings),
        }


@dataclass
class RiskState:
    """Mutable account risk state. Persisted so limits survive a restart —
    a daily loss limit that resets when the process crashes is not a limit."""

    equity: float
    peak_equity: float = 0.0
    starting_equity: float = 0.0
    day_start_equity: float = 0.0
    week_start_equity: float = 0.0
    current_day: date | None = None
    current_week: int | None = None
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: str = ""

    def __post_init__(self) -> None:
        if not self.peak_equity:
            self.peak_equity = self.equity
        if not self.starting_equity:
            self.starting_equity = self.equity
        if not self.day_start_equity:
            self.day_start_equity = self.equity
        if not self.week_start_equity:
            self.week_start_equity = self.equity

    @property
    def drawdown(self) -> float:
        """Current peak-to-trough drawdown as a positive fraction."""
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, 1.0 - self.equity / self.peak_equity)

    @property
    def daily_pnl_pct(self) -> float:
        return safe_div(self.equity - self.day_start_equity, self.day_start_equity)

    @property
    def weekly_pnl_pct(self) -> float:
        return safe_div(self.equity - self.week_start_equity, self.week_start_equity)

    def roll_periods(self, now: datetime) -> None:
        """Reset the daily/weekly baselines when the calendar turns over."""
        today = now.date()
        week = now.isocalendar()[1]
        if self.current_day != today:
            self.current_day = today
            self.day_start_equity = self.equity
        if self.current_week != week:
            self.current_week = week
            self.week_start_equity = self.equity

    def update_equity(self, equity: float) -> None:
        self.equity = equity
        self.peak_equity = max(self.peak_equity, equity)

    def record_trade(self, trade: Trade) -> None:
        if trade.pnl > 0:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1

    def to_dict(self) -> dict:
        return {
            "equity": round(self.equity, 2),
            "peak_equity": round(self.peak_equity, 2),
            "starting_equity": round(self.starting_equity, 2),
            "drawdown": round(self.drawdown, 4),
            "daily_pnl_pct": round(self.daily_pnl_pct, 4),
            "weekly_pnl_pct": round(self.weekly_pnl_pct, 4),
            "consecutive_losses": self.consecutive_losses,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
        }


class RiskManager:
    """The gatekeeper. Every signal passes through `evaluate()` before it can
    become an order."""

    def __init__(self, config: RiskConfig, state: RiskState) -> None:
        self.config = config
        self.state = state
        self.sizer = PositionSizer(config)

    # -- portfolio measures ------------------------------------------------ #

    def portfolio_heat(self, positions: list[Position]) -> float:
        """Total open risk as a fraction of equity — what a simultaneous
        stop-out of everything would cost."""
        if self.state.equity <= 0:
            return 0.0
        total = 0.0
        for p in positions:
            if p.stop_loss <= 0:
                # No stop = the whole notional is at risk, and it should be
                # counted that way rather than as zero.
                total += p.size * p.entry_price
            else:
                total += abs(p.entry_price - p.stop_loss) * p.size
        return total / self.state.equity

    def drawdown_risk_multiplier(self) -> float:
        """Scale risk down as drawdown deepens.

        Linear from 1.0x at the throttle threshold to 0.25x at the hard stop.
        Cutting size in a drawdown is what turns a survivable losing streak into
        a recoverable one.
        """
        cfg = self.config
        dd = self.state.drawdown
        if dd < cfg.drawdown_throttle_start:
            return 1.0
        if dd >= cfg.max_drawdown_stop:
            return 0.0
        span = cfg.max_drawdown_stop - cfg.drawdown_throttle_start
        progress = (dd - cfg.drawdown_throttle_start) / span
        return max(0.25, 1.0 - 0.75 * progress)

    # -- the gate ---------------------------------------------------------- #

    def evaluate(
        self,
        signal: Signal,
        positions: list[Position],
        now: datetime | None = None,
        correlations: dict[str, float] | None = None,
        avg_volume: float | None = None,
        asset_volatility: float | None = None,
        win_rate: float | None = None,
    ) -> RiskDecision:
        cfg = self.config
        now = now or datetime.now(timezone.utc)
        self.state.roll_periods(now)
        warnings: list[str] = []

        def reject(reason: str) -> RiskDecision:
            return RiskDecision(False, reason, warnings=warnings)

        # --- hard halts ---
        if self.state.halted:
            return reject(f"system halted: {self.state.halt_reason}")

        if self.state.drawdown >= cfg.max_drawdown_stop:
            self.halt(
                f"max drawdown {self.state.drawdown:.1%} >= "
                f"{cfg.max_drawdown_stop:.1%}"
            )
            return reject(self.state.halt_reason)

        if self.state.daily_pnl_pct <= -cfg.daily_loss_limit:
            return reject(
                f"daily loss limit hit ({self.state.daily_pnl_pct:.2%} <= "
                f"-{cfg.daily_loss_limit:.2%}) — no new risk until tomorrow"
            )

        if self.state.weekly_pnl_pct <= -cfg.weekly_loss_limit:
            return reject(
                f"weekly loss limit hit ({self.state.weekly_pnl_pct:.2%})"
            )

        if self.state.consecutive_losses >= cfg.consecutive_loss_limit:
            return reject(
                f"{self.state.consecutive_losses} consecutive losses — "
                f"cooling off (limit {cfg.consecutive_loss_limit})"
            )

        # --- reward:risk, the core mandate ---
        # Both thresholds, for the reasons documented on RiskConfig: the
        # furthest target proves the trade can pay 2-3x the risk, the weighted
        # average proves the ladder does not give that away by scaling out too
        # early.
        if signal.max_r < cfg.min_reward_risk:
            return reject(
                f"furthest target is {signal.max_r:.2f}R, below the "
                f"{cfg.min_reward_risk:.2f}R minimum — the setup does not pay "
                f"enough for the risk"
            )
        if signal.reward_risk < cfg.min_expected_r:
            return reject(
                f"size-weighted expectancy {signal.reward_risk:.2f}R is below "
                f"the {cfg.min_expected_r:.2f}R minimum"
            )

        # --- stop sanity ---
        if signal.atr > 0:
            stop_atr = signal.risk_per_unit / signal.atr
            if stop_atr < cfg.min_stop_distance_atr:
                return reject(
                    f"stop is {stop_atr:.2f} ATR away — inside the noise band "
                    f"(minimum {cfg.min_stop_distance_atr})"
                )
            if stop_atr > cfg.max_stop_distance_atr:
                return reject(
                    f"stop is {stop_atr:.2f} ATR away — too wide "
                    f"(maximum {cfg.max_stop_distance_atr})"
                )

        # --- exposure limits ---
        if len(positions) >= cfg.max_positions:
            return reject(
                f"already at the {cfg.max_positions}-position limit"
            )

        same_symbol = [p for p in positions if p.symbol == signal.symbol]
        if len(same_symbol) >= cfg.max_positions_per_symbol:
            return reject(f"already holding {signal.symbol}")

        # Opposing exposure in the same symbol is a hedge, not a trade.
        if any(p.side is not signal.side for p in same_symbol):
            return reject(f"an opposing position in {signal.symbol} is already open")

        if correlations:
            correlated = [
                p for p in positions
                if p.side is signal.side
                and abs(correlations.get(p.symbol, 0.0)) >= cfg.correlation_threshold
            ]
            if len(correlated) >= cfg.max_correlated_positions:
                names = ", ".join(sorted({p.symbol for p in correlated}))
                return reject(
                    f"{len(correlated)} correlated positions already open ({names}) "
                    f"— these behave as one position"
                )

        # --- risk fraction after throttling ---
        multiplier = self.drawdown_risk_multiplier()
        if multiplier <= 0:
            return reject("drawdown throttle has reduced size to zero")
        if multiplier < 1.0:
            warnings.append(
                f"size reduced to {multiplier:.0%} — in a "
                f"{self.state.drawdown:.1%} drawdown"
            )

        risk_fraction = cfg.risk_per_trade * multiplier

        # --- portfolio heat ---
        heat = self.portfolio_heat(positions)
        remaining = cfg.max_portfolio_heat - heat
        if remaining <= 0:
            return reject(
                f"portfolio heat {heat:.2%} is at the "
                f"{cfg.max_portfolio_heat:.2%} cap"
            )
        if risk_fraction > remaining:
            risk_fraction = remaining
            warnings.append(
                f"risk trimmed to {risk_fraction:.2%} to stay under the "
                f"{cfg.max_portfolio_heat:.2%} heat cap"
            )

        # --- size it ---
        sizing = self.sizer.size_for(
            signal, self.state.equity, risk_fraction,
            avg_volume=avg_volume, asset_volatility=asset_volatility,
            win_rate=win_rate,
        )
        if not sizing.is_tradeable:
            return reject(f"position size resolved to zero ({sizing.capped_by})")

        if sizing.leverage > cfg.max_leverage:
            return reject(
                f"required leverage {sizing.leverage:.2f}x exceeds the "
                f"{cfg.max_leverage:.2f}x limit"
            )
        if sizing.capped_by:
            warnings.append(f"size capped by {sizing.capped_by}")

        return RiskDecision(
            approved=True,
            reason="approved",
            risk_fraction=risk_fraction,
            sizing=sizing,
            warnings=warnings,
        )

    # -- lifecycle --------------------------------------------------------- #

    def apply(self, signal: Signal, decision: RiskDecision) -> Signal:
        """Write the approved sizing back onto the signal."""
        if not decision.approved or decision.sizing is None:
            raise ValueError("cannot apply a rejected risk decision")
        s = decision.sizing
        signal.position_size = s.size
        signal.risk_amount = s.risk_amount
        signal.notional = s.notional
        signal.leverage = s.leverage
        return signal

    def halt(self, reason: str) -> None:
        self.state.halted = True
        self.state.halt_reason = reason

    def resume(self) -> None:
        self.state.halted = False
        self.state.halt_reason = ""

    def on_trade_closed(self, trade: Trade, new_equity: float) -> None:
        self.state.record_trade(trade)
        self.state.update_equity(new_equity)
        if self.state.drawdown >= self.config.max_drawdown_stop:
            self.halt(
                f"max drawdown breached ({self.state.drawdown:.1%})"
            )


def kelly_fraction(win_rate: float, reward_risk: float) -> float:
    """f* = W - (1-W)/R. Returns 0 when the edge is negative."""
    if reward_risk <= 0:
        return 0.0
    f = win_rate - (1.0 - win_rate) / reward_risk
    return max(0.0, f)


def expectancy_r(win_rate: float, avg_win_r: float, avg_loss_r: float = 1.0) -> float:
    """Expected R per trade. The only performance number that matters."""
    return win_rate * avg_win_r - (1.0 - win_rate) * abs(avg_loss_r)


def breakeven_win_rate(reward_risk: float) -> float:
    """Hit rate needed to break even at a given reward:risk."""
    return 1.0 / (1.0 + reward_risk) if reward_risk > 0 else 1.0


def risk_of_ruin(
    win_rate: float, reward_risk: float, risk_per_trade: float,
    ruin_fraction: float = 0.5,
) -> float:
    """Approximate probability of losing `ruin_fraction` of the account.

    Uses the standard gambler's-ruin approximation. It is an estimate, not a
    guarantee, but it is the right order of magnitude and it makes the cost of
    oversizing immediately obvious: at 2% risk the number is small, at 10% it is
    not.
    """
    if risk_per_trade <= 0 or win_rate <= 0 or win_rate >= 1:
        return 0.0
    edge = win_rate * reward_risk - (1.0 - win_rate)
    if edge <= 0:
        return 1.0
    # Units of risk between here and ruin.
    units = ruin_fraction / risk_per_trade
    a = (1.0 - win_rate) / (win_rate * reward_risk)
    if a >= 1.0:
        return 1.0
    try:
        return float(min(1.0, math.pow(a, units)))
    except (OverflowError, ValueError):
        return 0.0
