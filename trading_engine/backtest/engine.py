"""Event-driven backtester.

Bar by bar, no vectorisation, no peeking. The design choices here are all in
service of one goal: producing a number you can actually believe.

  - A signal generated from bar `t`'s close fills at bar `t+1`'s open. You could
    not have acted on a close before it happened.
  - When a bar's range touches both the stop and a target, the **stop** is
    assumed to fill first. Without intrabar data you cannot know the order, and
    the optimistic assumption is precisely what makes losing systems backtest
    profitably.
  - Fees, spread and volatility-scaled slippage are charged on every fill.
  - Limit entries only fill if price actually traded through the limit.

The result is a backtest that reports *worse* numbers than a naive one. That is
the intended behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from ..config import BacktestConfig, Config, ExecutionConfig
from ..core.types import (
    AssetClass, ExitReason, Position, Side, Signal, Trade, safe_div,
)
from ..fundamentals.context import FundamentalContext
from ..risk.manager import RiskManager, RiskState
from ..strategy.liquidity_sweep import LiquiditySweepStrategy


@dataclass(slots=True)
class PendingEntry:
    """An approved signal waiting to fill on a later bar."""

    signal: Signal
    created_index: int
    expires_index: int
    is_limit: bool


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series | None = None
    signals: list[Signal] = field(default_factory=list)
    rejections: dict[str, int] = field(default_factory=dict)
    starting_equity: float = 0.0
    final_equity: float = 0.0
    bars_processed: int = 0
    bars_in_market: int = 0

    @property
    def total_return(self) -> float:
        return safe_div(
            self.final_equity - self.starting_equity, self.starting_equity
        )

    @property
    def exposure(self) -> float:
        return safe_div(self.bars_in_market, self.bars_processed)


class Backtester:
    """Runs a strategy over historical bars with a realistic cost model."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.exec_cfg: ExecutionConfig = config.execution
        self.bt_cfg: BacktestConfig = config.backtest

    # -- cost model -------------------------------------------------------- #

    def _slippage(self, price: float, atr: float) -> float:
        """Base slippage plus a volatility component.

        Flat tick slippage understates cost exactly when it matters most —
        stop-outs happen in fast markets, which is where slippage is worst.
        """
        base = price * self.exec_cfg.slippage_bps / 10_000.0
        vol = atr * self.exec_cfg.slippage_atr_factor if atr > 0 else 0.0
        return base + vol

    def _fill_price(
        self, price: float, side: Side, atr: float, is_maker: bool = False
    ) -> float:
        """Apply spread and slippage in the direction that hurts."""
        if is_maker:
            return price
        half_spread = price * self.exec_cfg.spread_bps / 10_000.0 / 2.0
        slip = self._slippage(price, atr)
        return price + side.sign * (half_spread + slip)

    def _fee(self, notional: float, is_maker: bool = False) -> float:
        rate = self.exec_cfg.maker_fee if is_maker else self.exec_cfg.taker_fee
        return abs(notional) * rate

    # -- main loop --------------------------------------------------------- #

    def run(
        self,
        df: pd.DataFrame,
        symbol: str = "",
        htf_df: pd.DataFrame | None = None,
        fundamentals: FundamentalContext | None = None,
        asset_class: AssetClass = AssetClass.CRYPTO,
        strategy: LiquiditySweepStrategy | None = None,
    ) -> BacktestResult:
        cfg = self.config
        strategy = strategy or LiquiditySweepStrategy(
            cfg.strategy,
            min_reward_risk=cfg.risk.min_reward_risk,
            min_expected_r=cfg.risk.min_expected_r,
            min_stop_atr=cfg.risk.min_stop_distance_atr,
            max_stop_atr=cfg.risk.max_stop_distance_atr,
        )
        strategy.prepare(df, htf_df)
        data = strategy._df                     # indicators already attached
        assert data is not None

        equity = self.bt_cfg.starting_equity
        risk_state = RiskState(equity=equity)
        risk = RiskManager(cfg.risk, risk_state)

        result = BacktestResult(starting_equity=equity)
        position: Position | None = None
        pending: PendingEntry | None = None
        equity_points: list[float] = []
        equity_index: list[pd.Timestamp] = []

        atr_col = data["atr"].to_numpy(dtype="float64")
        opens = data["open"].to_numpy(dtype="float64")
        highs = data["high"].to_numpy(dtype="float64")
        lows = data["low"].to_numpy(dtype="float64")
        closes = data["close"].to_numpy(dtype="float64")
        volumes = data["volume"].to_numpy(dtype="float64")

        warmup = max(self.bt_cfg.warmup_bars, cfg.strategy.atr_period * 3)

        for i in range(len(data)):
            ts = data.index[i]
            atr_now = atr_col[i] if np.isfinite(atr_col[i]) else 0.0

            # 1. Fill any pending entry at THIS bar (decided on a previous close).
            if pending is not None and position is None:
                position, fill_cost = self._try_fill(
                    pending, i, opens, highs, lows, atr_now, ts
                )
                if position is not None:
                    equity -= fill_cost
                    result.signals.append(pending.signal)
                    pending = None
                elif i >= pending.expires_index:
                    pending = None

            # 2. Manage an open position against this bar's range.
            if position is not None:
                position.bars_held += 1
                position.update_excursions(highs[i], lows[i])
                trade, realised = self._manage(
                    position, i, opens, highs, lows, closes, atr_now, ts,
                    is_last=(i == len(data) - 1),
                )
                equity += realised
                result.bars_in_market += 1
                if trade is not None:
                    result.trades.append(trade)
                    risk.on_trade_closed(trade, equity)
                    position = None

            risk_state.update_equity(equity)
            risk_state.roll_periods(ts.to_pydatetime())
            equity_points.append(equity)
            equity_index.append(ts)

            # 3. Look for a new signal on this bar's close (fills next bar).
            if position is None and pending is None and i >= warmup and i < len(data) - 1:
                ev = strategy.evaluate(
                    i, fundamentals, symbol=symbol, asset_class=asset_class
                )
                if ev.rejected:
                    key = _rejection_key(ev.rejected)
                    result.rejections[key] = result.rejections.get(key, 0) + 1
                elif ev.signal is not None:
                    lookback = slice(max(0, i - 20), i + 1)
                    avg_vol = float(np.nanmean(volumes[lookback])) or None
                    decision = risk.evaluate(
                        ev.signal, [], ts.to_pydatetime(), avg_volume=avg_vol
                    )
                    if decision.approved:
                        signal = risk.apply(ev.signal, decision)
                        is_limit = signal.entry != closes[i]
                        pending = PendingEntry(
                            signal=signal,
                            created_index=i,
                            expires_index=i + (5 if is_limit else 1),
                            is_limit=is_limit,
                        )
                    else:
                        key = _rejection_key(decision.reason)
                        result.rejections[key] = result.rejections.get(key, 0) + 1

        result.final_equity = equity
        result.bars_processed = len(data)
        result.equity_curve = pd.Series(equity_points, index=pd.Index(equity_index))
        return result

    # -- entry ------------------------------------------------------------- #

    def _try_fill(
        self,
        pending: PendingEntry,
        i: int,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        atr: float,
        ts: pd.Timestamp,
    ) -> tuple[Position | None, float]:
        """Attempt to fill the pending entry on bar `i`."""
        sig = pending.signal

        if pending.is_limit:
            # A limit only fills if price actually traded through it.
            touched = (
                lows[i] <= sig.entry if sig.side is Side.LONG
                else highs[i] >= sig.entry
            )
            if not touched:
                return None, 0.0
            # Gap through the limit fills at the open, in our favour.
            if sig.side is Side.LONG:
                fill = min(sig.entry, opens[i])
            else:
                fill = max(sig.entry, opens[i])
            fill = self._fill_price(fill, sig.side, atr, is_maker=True)
            fee = self._fee(fill * sig.position_size, is_maker=True)
        else:
            fill = self._fill_price(opens[i], sig.side, atr, is_maker=False)
            fee = self._fee(fill * sig.position_size, is_maker=False)

        # Slippage can push the fill past the stop; that trade is dead on arrival.
        if sig.side is Side.LONG and fill <= sig.stop_loss:
            return None, 0.0
        if sig.side is Side.SHORT and fill >= sig.stop_loss:
            return None, 0.0

        position = Position(
            symbol=sig.symbol,
            side=sig.side,
            size=sig.position_size,
            entry_price=fill,
            opened_at=ts.to_pydatetime(),
            stop_loss=sig.stop_loss,
            take_profits=[
                type(tp)(tp.price, tp.r_multiple, tp.size_pct, tp.label, False)
                for tp in sig.take_profits
            ],
            initial_size=sig.position_size,
            initial_stop=sig.stop_loss,
            fees_paid=fee,
            signal=sig,
        )
        return position, fee

    # -- management -------------------------------------------------------- #

    def _manage(
        self,
        pos: Position,
        i: int,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        atr: float,
        ts: pd.Timestamp,
        is_last: bool,
    ) -> tuple[Trade | None, float]:
        """Process one bar against an open position.

        Returns (closed_trade_or_None, realised_pnl_delta_this_bar).
        """
        cfg = self.config.strategy
        realised_delta = 0.0
        long = pos.side is Side.LONG

        stop_touched = pos.stop_hit(highs[i], lows[i])
        # Which targets could have been reached this bar.
        hit_targets = [
            tp for tp in pos.take_profits
            if not tp.hit and (
                highs[i] >= tp.price if long else lows[i] <= tp.price
            )
        ]

        # Pessimistic ordering: if both the stop and a target are inside this
        # bar's range, assume the stop went first.
        if stop_touched and (hit_targets and self.exec_cfg.stop_first_on_ambiguous_bar):
            hit_targets = []

        # -- partial take-profits --
        for tp in hit_targets:
            close_size = pos.initial_size * tp.size_pct
            close_size = min(close_size, pos.size)
            if close_size <= 0:
                continue
            fill = self._fill_price(tp.price, pos.side.opposite, atr, is_maker=True)
            pnl = (fill - pos.entry_price) * pos.side.sign * close_size
            fee = self._fee(fill * close_size, is_maker=True)
            pos.realised_pnl += pnl
            pos.fees_paid += fee
            pos.size -= close_size
            tp.hit = True
            realised_delta += pnl - fee

            # Move to break-even once the configured rung has filled — from
            # here the trade cannot lose, which is what lets the runner ride.
            filled_count = sum(1 for t in pos.take_profits if t.hit)
            if (
                not pos.breakeven_moved
                and filled_count >= cfg.move_to_breakeven_after_tp
            ):
                pos.stop_loss = pos.entry_price
                pos.breakeven_moved = True

        if pos.size <= 1e-12:
            # Fully scaled out. Record the exit at the LAST target filled — by
            # this point the stop has been moved to break-even, so reporting it
            # would show a winning trade exiting at its own entry price.
            filled = [tp for tp in pos.take_profits if tp.hit]
            exit_price = (
                max(filled, key=lambda t: t.r_multiple).price if filled
                else closes[i]
            )
            trade = self._close_trade(pos, exit_price, ts, ExitReason.TAKE_PROFIT)
            return trade, realised_delta

        # -- stop --
        if stop_touched:
            # A gap through the stop fills at the open, not at the stop price.
            raw = pos.stop_loss
            if long and opens[i] < pos.stop_loss:
                raw = opens[i]
            elif not long and opens[i] > pos.stop_loss:
                raw = opens[i]
            fill = self._fill_price(raw, pos.side.opposite, atr, is_maker=False)
            pnl = (fill - pos.entry_price) * pos.side.sign * pos.size
            fee = self._fee(fill * pos.size, is_maker=False)
            pos.realised_pnl += pnl
            pos.fees_paid += fee
            realised_delta += pnl - fee
            # Attribute the exit precisely: a stop that was trailed into
            # profit is a trailing-stop exit, not a break-even one. Lumping
            # them together hides whether the trailing logic is earning its
            # keep or cutting runners short.
            reason = _stop_exit_reason(pos)
            trade = self._close_trade(pos, fill, ts, reason)
            return trade, realised_delta

        # -- trailing stop, once the trade is meaningfully in profit --
        if atr > 0 and cfg.trail_after_r > 0:
            r_now = pos.unrealised_r(closes[i])
            if r_now >= cfg.trail_after_r:
                trail = (
                    closes[i] - atr * cfg.trail_atr_mult if long
                    else closes[i] + atr * cfg.trail_atr_mult
                )
                # Only ever tighten.
                if (long and trail > pos.stop_loss) or (
                    not long and trail < pos.stop_loss
                ):
                    pos.stop_loss = trail

        # -- time stop --
        if cfg.time_stop_bars > 0 and pos.bars_held >= cfg.time_stop_bars:
            fill = self._fill_price(closes[i], pos.side.opposite, atr)
            pnl = (fill - pos.entry_price) * pos.side.sign * pos.size
            fee = self._fee(fill * pos.size)
            pos.realised_pnl += pnl
            pos.fees_paid += fee
            realised_delta += pnl - fee
            return self._close_trade(pos, fill, ts, ExitReason.TIME_STOP), realised_delta

        # -- end of data --
        if is_last:
            fill = self._fill_price(closes[i], pos.side.opposite, atr)
            pnl = (fill - pos.entry_price) * pos.side.sign * pos.size
            fee = self._fee(fill * pos.size)
            pos.realised_pnl += pnl
            pos.fees_paid += fee
            realised_delta += pnl - fee
            return (
                self._close_trade(pos, fill, ts, ExitReason.END_OF_DATA),
                realised_delta,
            )

        return None, realised_delta

    def _close_trade(
        self, pos: Position, exit_price: float, ts: pd.Timestamp,
        reason: ExitReason,
    ) -> Trade:
        net_pnl = pos.realised_pnl - pos.fees_paid
        risk = pos.risk_per_unit * pos.initial_size
        r_multiple = safe_div(net_pnl, risk)
        return Trade(
            symbol=pos.symbol,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size=pos.initial_size,
            entry_time=pos.opened_at,
            exit_time=ts.to_pydatetime(),
            pnl=net_pnl,
            r_multiple=r_multiple,
            fees=pos.fees_paid,
            exit_reason=reason,
            bars_held=pos.bars_held,
            mae_r=pos.max_adverse,
            mfe_r=pos.max_favourable,
            confidence=pos.signal.confidence if pos.signal else 0.0,
            reasons=pos.signal.reasons[:5] if pos.signal else [],
        )


def _stop_exit_reason(pos: Position) -> ExitReason:
    """Classify a stop-out by where the stop had been moved to."""
    if not pos.breakeven_moved:
        return ExitReason.STOP_LOSS
    beyond_entry = (
        pos.stop_loss > pos.entry_price if pos.side is Side.LONG
        else pos.stop_loss < pos.entry_price
    )
    return ExitReason.TRAILING_STOP if beyond_entry else ExitReason.BREAK_EVEN


def _rejection_key(reason: str) -> str:
    """Collapse a specific rejection message into a countable category."""
    low = reason.lower()
    for key, marker in (
        ("no liquidity sweep", "no recent liquidity sweep"),
        ("sweep not reclaimed", "did not reclaim"),
        ("sweep direction mismatch", "points the other way"),
        ("no directional bias", "no directional bias"),
        ("low confluence score", "confluence score"),
        ("htf conflict", "higher timeframe"),
        ("reward:risk too low", "reward:risk"),
        ("stop too tight", "inside the noise"),
        ("stop too wide", "too wide"),
        ("event blackout", "blackout"),
        ("insufficient history", "insufficient history"),
        ("daily loss limit", "daily loss limit"),
        ("drawdown halt", "drawdown"),
        ("position limit", "position limit"),
        ("portfolio heat", "heat"),
        ("consecutive losses", "consecutive losses"),
        ("no valid targets", "no valid targets"),
    ):
        if marker in low:
            return key
    return "other"
