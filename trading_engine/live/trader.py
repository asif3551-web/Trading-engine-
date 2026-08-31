"""The autotrader — the live/paper trading loop.

Design principles, in priority order:

  1. **Fail closed.** Stale data, an unknown position state or an error storm
     stops new risk immediately, and flattens if protective orders cannot be
     confirmed. There is no path where uncertainty results in more exposure.
  2. **The stop goes on with the entry.** A filled position without a resting
     stop is naked risk for as long as that gap lasts, so the bracket is
     submitted in the same step, and a position whose stop cannot be placed is
     closed rather than left hanging.
  3. **Paper and live share this code.** The only difference is which Broker is
     injected.
  4. **Idempotent.** Every order carries a client ID, and state is persisted, so
     a crash and restart reconciles rather than duplicating.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ..config import Config
from ..core.types import (
    AssetClass, ExitReason, Order, OrderType, Position, Side, Signal,
)
from ..data.feeds import DataFeed, DataError, get_feed, timeframe_seconds
from ..fundamentals.context import (
    CryptoFundamentals, FundamentalAnalyzer, FundamentalContext,
)
from ..risk.manager import RiskManager, RiskState
from ..strategy.liquidity_sweep import LiquiditySweepStrategy
from .broker import Broker, BrokerError, PaperBroker, new_client_id

log = logging.getLogger("trading_engine.trader")


@dataclass
class TraderStatus:
    running: bool = False
    mode: str = "paper"
    broker: str = "paper"
    equity: float = 0.0
    starting_equity: float = 0.0
    open_positions: int = 0
    signals_today: int = 0
    last_update: datetime | None = None
    last_error: str = ""
    halted: bool = False
    halt_reason: str = ""
    data_stale: bool = False
    feed_names: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "mode": self.mode,
            "broker": self.broker,
            "equity": round(self.equity, 2),
            "starting_equity": round(self.starting_equity, 2),
            "pnl_pct": round(
                (self.equity / self.starting_equity - 1.0) * 100.0, 3
            ) if self.starting_equity else 0.0,
            "open_positions": self.open_positions,
            "signals_today": self.signals_today,
            "last_update": self.last_update.isoformat() if self.last_update else None,
            "last_error": self.last_error,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "data_stale": self.data_stale,
            "feed_names": dict(self.feed_names),
        }


class AutoTrader:
    """Polls the market, generates signals, and manages positions end to end."""

    def __init__(
        self,
        config: Config,
        broker: Broker | None = None,
        feed: DataFeed | None = None,
    ) -> None:
        self.config = config
        self.broker = broker or PaperBroker(
            starting_equity=config.live.starting_equity,
            taker_fee=config.execution.taker_fee,
            maker_fee=config.execution.maker_fee,
            slippage_bps=config.execution.slippage_bps,
        )
        self.feed = feed
        self._feeds: dict[str, DataFeed] = {}

        equity = config.live.starting_equity
        self.risk_state = RiskState(equity=equity)
        self.risk = RiskManager(config.risk, self.risk_state)
        self.fundamentals = FundamentalAnalyzer(
            blackout_before_min=config.fundamentals.event_blackout_minutes_before,
            blackout_after_min=config.fundamentals.event_blackout_minutes_after,
            high_impact_only=config.fundamentals.high_impact_only,
            funding_extreme_bps=config.fundamentals.funding_extreme_bps,
            oi_surge_pct=config.fundamentals.oi_surge_pct,
        )

        self.status = TraderStatus(
            mode=config.live.mode,
            broker=self.broker.name,
            equity=equity,
            starting_equity=equity,
        )
        self.active_signals: list[Signal] = []
        self.recent_evaluations: list[dict] = []
        self._pending_stops: dict[str, Order] = {}
        self._error_count = 0
        self._running = False

    # -- feeds ------------------------------------------------------------- #

    def _feed_for(self, symbol: str) -> DataFeed:
        if self.feed is not None:
            return self.feed
        if symbol not in self._feeds:
            self._feeds[symbol] = get_feed(
                self.config.data.provider,
                symbol,
                cache_dir=self.config.data.cache_dir,
                cache_enabled=self.config.data.cache_enabled,
            )
            self.status.feed_names[symbol] = self._feeds[symbol].name
        return self._feeds[symbol]

    # -- one iteration ----------------------------------------------------- #

    def tick(self) -> None:
        """One full cycle across every configured symbol."""
        if self._check_kill_switch():
            return

        now = datetime.now(timezone.utc)
        self.risk_state.roll_periods(now)

        for symbol in self.config.data.symbols:
            try:
                self._process_symbol(symbol, now)
                self._error_count = 0
            except (DataError, BrokerError) as exc:
                self._on_error(f"{symbol}: {exc}")
            except Exception as exc:                      # noqa: BLE001
                self._on_error(f"{symbol}: unexpected error: {exc}")
                log.exception("unexpected error processing %s", symbol)

        equity = self._current_equity()
        self.risk_state.update_equity(equity)
        self.status.equity = equity
        self.status.open_positions = len(self.broker.get_positions())
        self.status.last_update = now
        self.status.halted = self.risk_state.halted
        self.status.halt_reason = self.risk_state.halt_reason
        self._save_state()

    def _process_symbol(self, symbol: str, now: datetime) -> None:
        cfg = self.config
        feed = self._feed_for(symbol)

        df = feed.get_bars(symbol, cfg.strategy.timeframe, cfg.strategy.lookback_bars)
        if df.empty:
            raise DataError(f"empty frame for {symbol}")

        # --- staleness: fail closed --------------------------------------- #
        last_bar = df.index[-1].to_pydatetime()
        bar_seconds = timeframe_seconds(cfg.strategy.timeframe)
        age = (now - last_bar).total_seconds()
        # Allow one bar of lag plus the configured tolerance.
        if age > bar_seconds + cfg.data.max_staleness_sec:
            self.status.data_stale = True
            log.warning(
                "%s data is %.0fs old (last bar %s) — no new risk",
                symbol, age, last_bar.isoformat(),
            )
            self._manage_open_position(symbol, df, allow_new=False)
            self._note(symbol, f"data is {age:.0f}s stale — no new risk")
            return
        self.status.data_stale = False

        price = float(df["close"].iloc[-1])
        if isinstance(self.broker, PaperBroker):
            self.broker.set_price(symbol, price)

        # Existing position takes priority over looking for a new one.
        self._manage_open_position(symbol, df, allow_new=True)
        if self.broker_position(symbol) is not None:
            self._note(symbol, "already holding a position in this symbol")
            return

        # --- fundamentals --------------------------------------------------#
        asset_class = feed.asset_class(symbol)
        crypto = None
        if cfg.fundamentals.enabled and asset_class is AssetClass.CRYPTO:
            crypto = self._crypto_fundamentals(feed, symbol)
        fund_ctx = (
            self.fundamentals.analyse(now, symbol, asset_class, crypto=crypto)
            if cfg.fundamentals.enabled else None
        )

        # --- signal ---------------------------------------------------------#
        htf_df = None
        if cfg.strategy.require_htf_alignment:
            try:
                htf_df = feed.get_bars(symbol, cfg.strategy.htf_timeframe, 200)
            except DataError:
                htf_df = None   # missing HTF disables the filter, not the engine

        strategy = LiquiditySweepStrategy(
            cfg.strategy,
            min_reward_risk=cfg.risk.min_reward_risk,
            min_expected_r=cfg.risk.min_expected_r,
            min_stop_atr=cfg.risk.min_stop_distance_atr,
            max_stop_atr=cfg.risk.max_stop_distance_atr,
        )
        strategy.prepare(df, htf_df)

        book = None
        if asset_class is AssetClass.CRYPTO:
            book = feed.get_orderbook(symbol, cfg.data.orderbook_depth)

        last = len(df) - 1
        ev = strategy.evaluate(
            last, fund_ctx, book, asset_class, symbol, feed.tick_size(symbol)
        )
        self._record_evaluation(ev)

        if ev.signal is None:
            return

        decision = self.risk.evaluate(
            ev.signal, self.broker.get_positions(), now,
            avg_volume=float(df["volume"].tail(20).mean()),
        )
        if not decision.approved:
            log.info("signal on %s rejected by risk: %s", symbol, decision.reason)
            self._note(symbol, f"risk manager: {decision.reason}")
            return

        signal = self.risk.apply(ev.signal, decision)
        self.active_signals.append(signal)
        self.status.signals_today += 1
        self._open_position(signal, price)

    # -- position lifecycle ------------------------------------------------ #

    def broker_position(self, symbol: str) -> Position | None:
        return next(
            (p for p in self.broker.get_positions() if p.symbol == symbol), None
        )

    def _open_position(self, signal: Signal, market_price: float) -> None:
        """Enter, then immediately attach the protective stop.

        If the stop cannot be placed, the position is closed straight away. An
        unprotected position is a worse outcome than a missed trade.
        """
        entry = Order(
            symbol=signal.symbol,
            side=signal.side,
            size=signal.position_size,
            order_type=OrderType.MARKET,
            client_order_id=new_client_id("entry"),
            tag="entry",
        )
        filled = self.broker.submit(entry)
        if filled.status.value not in ("filled", "partially_filled"):
            log.warning("entry order for %s was not filled: %s",
                        signal.symbol, filled.status.value)
            return

        fill_price = filled.avg_fill_price or market_price

        # Slippage can push the fill past the stop, which would mean entering an
        # already-invalidated trade.
        if (signal.side is Side.LONG and fill_price <= signal.stop_loss) or (
            signal.side is Side.SHORT and fill_price >= signal.stop_loss
        ):
            log.warning(
                "%s filled at %.8g, already through the %.8g stop — closing",
                signal.symbol, fill_price, signal.stop_loss,
            )
            self._flatten(signal.symbol, ExitReason.RISK_HALT)
            return

        position = self.broker_position(signal.symbol)
        if position is None:
            log.error("no position found after a filled entry on %s", signal.symbol)
            return

        position.stop_loss = signal.stop_loss
        position.initial_stop = signal.stop_loss
        position.take_profits = list(signal.take_profits)
        position.signal = signal
        position.entry_price = fill_price

        try:
            self._place_stop(position)
        except BrokerError as exc:
            log.error(
                "could not place the protective stop on %s (%s) — flattening",
                signal.symbol, exc,
            )
            self._flatten(signal.symbol, ExitReason.RISK_HALT)
            return

        log.info(
            "opened %s %s size=%.8g entry=%.8g stop=%.8g targets=%s (R:R %.2f)",
            signal.side.value, signal.symbol, position.size, fill_price,
            signal.stop_loss, [round(t.price, 8) for t in signal.take_profits],
            signal.reward_risk,
        )

    def _place_stop(self, position: Position) -> None:
        """Rest a stop order at the broker.

        The paper broker fills stops synchronously in `_manage_open_position`,
        so a resting order there would double-close; for live brokers the
        resting order is what protects the position if this process dies.
        """
        if isinstance(self.broker, PaperBroker):
            return
        stop = Order(
            symbol=position.symbol,
            side=position.side.opposite,
            size=position.size,
            order_type=OrderType.STOP,
            stop_price=position.stop_loss,
            reduce_only=True,
            client_order_id=new_client_id("stop"),
            tag="stop_loss",
        )
        self._pending_stops[position.symbol] = self.broker.submit(stop)

    def _manage_open_position(
        self, symbol: str, df: pd.DataFrame, allow_new: bool
    ) -> None:
        """Check stops, targets, break-even and trailing against the latest bar."""
        position = self.broker_position(symbol)
        if position is None:
            return

        cfg = self.config.strategy
        bar = df.iloc[-1]
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        position.update_excursions(high, low)

        # --- stop ---
        if position.stop_hit(high, low):
            from ..backtest.engine import _stop_exit_reason
            reason = _stop_exit_reason(position)
            log.info("%s stop hit at %.8g", symbol, position.stop_loss)
            self._flatten(symbol, reason)
            return

        # --- targets ---
        long = position.side is Side.LONG
        for tp in position.take_profits:
            if tp.hit:
                continue
            reached = high >= tp.price if long else low <= tp.price
            if not reached:
                continue

            size = min(position.initial_size * tp.size_pct, position.size)
            if size <= 0:
                continue
            order = Order(
                symbol=symbol,
                side=position.side.opposite,
                size=size,
                order_type=OrderType.MARKET,
                reduce_only=True,
                client_order_id=new_client_id("tp"),
                tag="take_profit",
            )
            self.broker.submit(order)
            tp.hit = True
            log.info("%s %s filled at %.8g (%.2fR)", symbol, tp.label, tp.price,
                     tp.r_multiple)

            filled_count = sum(1 for t in position.take_profits if t.hit)
            if (
                not position.breakeven_moved
                and filled_count >= cfg.move_to_breakeven_after_tp
            ):
                position.stop_loss = position.entry_price
                position.breakeven_moved = True
                log.info("%s stop moved to break-even at %.8g", symbol,
                         position.entry_price)
                self._replace_stop(position)

        if self.broker_position(symbol) is None:
            return

        # --- trailing ---
        from ..indicators.core import atr as atr_fn
        atr_series = atr_fn(df, cfg.atr_period)
        atr_val = float(atr_series.iloc[-1]) if pd.notna(atr_series.iloc[-1]) else 0.0
        if atr_val > 0 and cfg.trail_after_r > 0:
            r_now = position.unrealised_r(close)
            if r_now >= cfg.trail_after_r:
                trail = (
                    close - atr_val * cfg.trail_atr_mult if long
                    else close + atr_val * cfg.trail_atr_mult
                )
                if (long and trail > position.stop_loss) or (
                    not long and trail < position.stop_loss
                ):
                    position.stop_loss = trail
                    self._replace_stop(position)

        # --- time stop ---
        position.bars_held += 1
        if cfg.time_stop_bars > 0 and position.bars_held >= cfg.time_stop_bars:
            log.info("%s hit the time stop after %d bars", symbol, position.bars_held)
            self._flatten(symbol, ExitReason.TIME_STOP)

    def _replace_stop(self, position: Position) -> None:
        if isinstance(self.broker, PaperBroker):
            return
        old = self._pending_stops.get(position.symbol)
        if old is not None:
            try:
                self.broker.cancel(old)
            except BrokerError as exc:
                log.warning("could not cancel the old stop on %s: %s",
                            position.symbol, exc)
                return   # never leave the position with two live stops
        try:
            self._place_stop(position)
        except BrokerError as exc:
            log.error("failed to re-place the stop on %s: %s", position.symbol, exc)

    def _flatten(self, symbol: str, reason: ExitReason) -> None:
        position = self.broker_position(symbol)
        if position is None:
            return
        old = self._pending_stops.pop(symbol, None)
        if old is not None and not isinstance(self.broker, PaperBroker):
            try:
                self.broker.cancel(old)
            except BrokerError:
                pass
        order = Order(
            symbol=symbol,
            side=position.side.opposite,
            size=position.size,
            order_type=OrderType.MARKET,
            reduce_only=True,
            client_order_id=new_client_id("flat"),
            tag=reason.value,
        )
        self.broker.submit(order)

        if isinstance(self.broker, PaperBroker) and self.broker.trades:
            trade = self.broker.trades[-1]
            self.risk.on_trade_closed(trade, self._current_equity())

    def flatten_all(self, reason: ExitReason = ExitReason.MANUAL) -> None:
        for position in list(self.broker.get_positions()):
            self._flatten(position.symbol, reason)

    # -- support ----------------------------------------------------------- #

    def _crypto_fundamentals(
        self, feed: DataFeed, symbol: str
    ) -> CryptoFundamentals | None:
        getter = getattr(feed, "get_funding", None)
        if getter is None:
            return None
        try:
            data = getter(symbol)
        except Exception:                                  # noqa: BLE001
            return None
        if not data:
            return None
        return CryptoFundamentals(
            funding_rate_bps=data.get("funding_rate_bps"),
            open_interest=data.get("open_interest"),
            basis_bps=data.get("basis_bps"),
        )

    def _current_equity(self) -> float:
        try:
            return self.broker.get_equity()
        except BrokerError as exc:
            self._on_error(f"equity lookup failed: {exc}")
            return self.risk_state.equity

    def _record_evaluation(self, ev) -> None:
        self.recent_evaluations.append(ev.to_dict())
        del self.recent_evaluations[:-50]

    def _note(self, symbol: str, reason: str) -> None:
        """Publish a gate reason for a path that never reached the strategy.

        Without this the dashboard cannot distinguish "the engine is working and
        declining setups" from "the engine is dead", which are very different
        things to a person deciding whether to trust it.
        """
        self.recent_evaluations.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "signal": None,
                "rejected": reason,
                "confidence": 0.0,
                "liquidity_score": 0.0,
                "technical_score": 0.0,
                "fundamental_score": 0.0,
                "reasons": [],
            }
        )
        del self.recent_evaluations[:-50]

    def _on_error(self, message: str) -> None:
        self._error_count += 1
        self.status.last_error = message
        log.error(message)
        # An error storm means we no longer know the true state. Fail closed.
        if self._error_count >= 5:
            self.risk.halt(
                f"{self._error_count} consecutive errors — halting to avoid "
                f"trading on unknown state"
            )
            self.status.halted = True
            self.status.halt_reason = self.risk_state.halt_reason

    def _check_kill_switch(self) -> bool:
        """A file on disk that flattens everything and halts. Deliberately the
        simplest possible mechanism — it must work when nothing else does."""
        path = Path(self.config.live.kill_switch_file)
        if not path.exists():
            return False
        if not self.risk_state.halted:
            log.critical("kill switch file found at %s — flattening and halting", path)
            self.flatten_all(ExitReason.RISK_HALT)
            self.risk.halt(f"kill switch file present at {path}")
            self.status.halted = True
            self.status.halt_reason = self.risk_state.halt_reason
        return True

    def _save_state(self) -> None:
        path = Path(self.config.live.state_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "status": self.status.to_dict(),
                "risk": self.risk_state.to_dict(),
                "positions": [p.to_dict() for p in self.broker.get_positions()],
                "active_signals": [s.to_dict() for s in self.active_signals[-20:]],
            }
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("could not persist state: %s", exc)

    def load_state(self) -> dict | None:
        path = Path(self.config.live.state_file)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read the state file: %s", exc)
            return None

    # -- loop -------------------------------------------------------------- #

    def run(self, max_iterations: int | None = None) -> None:
        """Blocking poll loop. `max_iterations` bounds it for tests."""
        self._running = True
        self.status.running = True
        if self.config.live.reconcile_on_start:
            self.broker.reconcile()

        log.info(
            "autotrader starting: mode=%s broker=%s symbols=%s timeframe=%s",
            self.config.live.mode, self.broker.name,
            self.config.data.symbols, self.config.strategy.timeframe,
        )
        if self.config.live.mode == "live" and self.broker.is_live:
            log.warning("LIVE TRADING IS ARMED — real orders will be sent")

        count = 0
        try:
            while self._running:
                self.tick()
                count += 1
                if max_iterations is not None and count >= max_iterations:
                    break
                if self.risk_state.halted:
                    log.critical("halted: %s", self.risk_state.halt_reason)
                    break
                time.sleep(self.config.live.poll_interval_sec)
        except KeyboardInterrupt:
            log.info("interrupted — shutting down")
        finally:
            self._running = False
            self.status.running = False
            self._save_state()

    def stop(self) -> None:
        self._running = False
        self.status.running = False
