"""Core domain types shared by every layer of the engine.

Everything here is a plain dataclass so the objects serialise cleanly to JSON for
the API and persist without an ORM.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        """+1 for long, -1 for short. Lets price math be written once."""
        return 1 if self is Side.LONG else -1

    @property
    def opposite(self) -> "Side":
        return Side.SHORT if self is Side.LONG else Side.LONG


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class AssetClass(str, Enum):
    CRYPTO = "crypto"
    EQUITY = "equity"
    FX = "fx"
    FUTURES = "futures"
    INDEX = "index"
    COMMODITY = "commodity"


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    BREAK_EVEN = "break_even"
    TIME_STOP = "time_stop"
    SIGNAL_FLIP = "signal_flip"
    RISK_HALT = "risk_halt"
    MANUAL = "manual"
    END_OF_DATA = "end_of_data"


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Bar:
    """A single OHLCV candle. `timestamp` is the bar's OPEN time, always UTC."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    symbol: str = ""
    timeframe: str = ""

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=timezone.utc)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        return d


@dataclass(slots=True)
class OrderBookLevel:
    price: float
    size: float


@dataclass(slots=True)
class OrderBook:
    """Top-of-book depth snapshot, best price first on both sides."""

    timestamp: datetime
    symbol: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def spread_bps(self) -> float | None:
        s, m = self.spread, self.mid
        if s is None or not m:
            return None
        return s / m * 10_000.0

    def depth_imbalance(self, levels: int = 10) -> float:
        """(bid - ask) / (bid + ask) over the top N levels, in [-1, 1].

        Positive means resting bid size dominates. Spoofable, so this is a
        confirming filter only — never a standalone entry trigger.
        """
        bid = sum(l.size for l in self.bids[:levels])
        ask = sum(l.size for l in self.asks[:levels])
        total = bid + ask
        return 0.0 if total <= 0 else (bid - ask) / total


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class TakeProfit:
    """One rung of the take-profit ladder."""

    price: float
    r_multiple: float
    size_pct: float          # fraction of the position closed here, 0-1
    label: str = ""
    hit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Signal:
    """A complete, actionable trade instruction.

    A signal without a stop is not a signal — the stop is what defines 1R, and
    every other number here is derived from it.
    """

    timestamp: datetime
    symbol: str
    timeframe: str
    side: Side
    entry: float
    stop_loss: float
    take_profits: list[TakeProfit] = field(default_factory=list)

    confidence: float = 0.0                    # 0-100 confluence score
    reasons: list[str] = field(default_factory=list)
    asset_class: AssetClass = AssetClass.CRYPTO

    # Sizing, filled in by the risk manager
    position_size: float = 0.0
    risk_amount: float = 0.0
    notional: float = 0.0
    leverage: float = 1.0

    # Diagnostics
    atr: float = 0.0
    liquidity_score: float = 0.0
    fundamental_score: float = 0.0
    technical_score: float = 0.0
    regime: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            self.timestamp = self.timestamp.replace(tzinfo=timezone.utc)
        self._validate()

    def _validate(self) -> None:
        if self.entry <= 0:
            raise ValueError(f"entry must be positive, got {self.entry}")
        if self.stop_loss <= 0:
            raise ValueError(f"stop_loss must be positive, got {self.stop_loss}")
        # The stop must sit on the losing side of entry, or risk is undefined.
        if self.side is Side.LONG and self.stop_loss >= self.entry:
            raise ValueError(
                f"long stop {self.stop_loss} must be below entry {self.entry}"
            )
        if self.side is Side.SHORT and self.stop_loss <= self.entry:
            raise ValueError(
                f"short stop {self.stop_loss} must be above entry {self.entry}"
            )
        for tp in self.take_profits:
            if self.side is Side.LONG and tp.price <= self.entry:
                raise ValueError(f"long target {tp.price} must be above entry")
            if self.side is Side.SHORT and tp.price >= self.entry:
                raise ValueError(f"short target {tp.price} must be below entry")

    # -- risk geometry ----------------------------------------------------- #

    @property
    def risk_per_unit(self) -> float:
        """1R expressed in price. The denominator of every R calculation."""
        return abs(self.entry - self.stop_loss)

    @property
    def stop_distance_pct(self) -> float:
        return self.risk_per_unit / self.entry * 100.0

    def r_multiple_at(self, price: float) -> float:
        r = self.risk_per_unit
        if r <= 0:
            return 0.0
        return (price - self.entry) * self.side.sign / r

    @property
    def reward_risk(self) -> float:
        """Size-weighted expected R if every rung of the ladder fills."""
        if not self.take_profits:
            return 0.0
        total_pct = sum(tp.size_pct for tp in self.take_profits)
        if total_pct <= 0:
            return 0.0
        return sum(tp.r_multiple * tp.size_pct for tp in self.take_profits) / total_pct

    @property
    def max_r(self) -> float:
        """R at the furthest target — the best case for a full runner."""
        return max((tp.r_multiple for tp in self.take_profits), default=0.0)

    @property
    def final_target(self) -> float:
        if not self.take_profits:
            return self.entry
        return max(self.take_profits, key=lambda t: t.r_multiple).price

    def expectancy_r(self, win_rate: float) -> float:
        """E[R] per trade at an assumed hit rate, losses at a full -1R."""
        return win_rate * self.reward_risk - (1.0 - win_rate) * 1.0

    @property
    def breakeven_win_rate(self) -> float:
        """Hit rate needed to break even at this reward:risk."""
        rr = self.reward_risk
        return 1.0 / (1.0 + rr) if rr > 0 else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side.value,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profits": [tp.to_dict() for tp in self.take_profits],
            "confidence": round(self.confidence, 2),
            "reasons": list(self.reasons),
            "asset_class": self.asset_class.value,
            "position_size": self.position_size,
            "risk_amount": round(self.risk_amount, 2),
            "notional": round(self.notional, 2),
            "leverage": self.leverage,
            "risk_per_unit": self.risk_per_unit,
            "stop_distance_pct": round(self.stop_distance_pct, 4),
            "reward_risk": round(self.reward_risk, 3),
            "max_r": round(self.max_r, 3),
            "breakeven_win_rate": round(self.breakeven_win_rate, 4),
            "atr": self.atr,
            "liquidity_score": round(self.liquidity_score, 2),
            "fundamental_score": round(self.fundamental_score, 2),
            "technical_score": round(self.technical_score, 2),
            "regime": self.regime,
            "meta": self.meta,
        }


# --------------------------------------------------------------------------- #
# Orders, positions, trades
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Order:
    symbol: str
    side: Side
    size: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    client_order_id: str = ""
    broker_order_id: str = ""
    status: OrderStatus = OrderStatus.PENDING
    filled_size: float = 0.0
    avg_fill_price: float = 0.0
    fee: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reduce_only: bool = False
    tag: str = ""

    @property
    def is_open(self) -> bool:
        return self.status in (
            OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL,
        )

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled_size)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["order_type"] = self.order_type.value
        d["status"] = self.status.value
        d["created_at"] = self.created_at.isoformat()
        d["updated_at"] = self.updated_at.isoformat()
        return d


@dataclass(slots=True)
class Position:
    """An open position with its live protective levels."""

    symbol: str
    side: Side
    size: float
    entry_price: float
    opened_at: datetime
    stop_loss: float = 0.0
    take_profits: list[TakeProfit] = field(default_factory=list)
    initial_size: float = 0.0
    initial_stop: float = 0.0
    realised_pnl: float = 0.0
    fees_paid: float = 0.0
    max_favourable: float = 0.0   # MFE in R
    max_adverse: float = 0.0      # MAE in R
    breakeven_moved: bool = False
    bars_held: int = 0
    signal: Signal | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.initial_size:
            self.initial_size = self.size
        if not self.initial_stop:
            self.initial_stop = self.stop_loss

    @property
    def risk_per_unit(self) -> float:
        """1R from the ORIGINAL stop — R must stay anchored even after the
        stop is trailed, or realised R becomes meaningless."""
        return abs(self.entry_price - self.initial_stop)

    def unrealised_pnl(self, price: float) -> float:
        return (price - self.entry_price) * self.side.sign * self.size

    def unrealised_r(self, price: float) -> float:
        r = self.risk_per_unit
        if r <= 0:
            return 0.0
        return (price - self.entry_price) * self.side.sign / r

    def total_pnl(self, price: float) -> float:
        return self.realised_pnl + self.unrealised_pnl(price) - self.fees_paid

    def update_excursions(self, high: float, low: float) -> None:
        best = high if self.side is Side.LONG else low
        worst = low if self.side is Side.LONG else high
        self.max_favourable = max(self.max_favourable, self.unrealised_r(best))
        self.max_adverse = min(self.max_adverse, self.unrealised_r(worst))

    def stop_hit(self, high: float, low: float) -> bool:
        if self.stop_loss <= 0:
            return False
        return low <= self.stop_loss if self.side is Side.LONG else high >= self.stop_loss

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "size": self.size,
            "initial_size": self.initial_size,
            "entry_price": self.entry_price,
            "opened_at": self.opened_at.isoformat(),
            "stop_loss": self.stop_loss,
            "initial_stop": self.initial_stop,
            "take_profits": [tp.to_dict() for tp in self.take_profits],
            "realised_pnl": round(self.realised_pnl, 2),
            "fees_paid": round(self.fees_paid, 4),
            "max_favourable_r": round(self.max_favourable, 3),
            "max_adverse_r": round(self.max_adverse, 3),
            "breakeven_moved": self.breakeven_moved,
            "bars_held": self.bars_held,
        }


@dataclass(slots=True)
class Trade:
    """A closed round-trip, the unit of performance measurement."""

    symbol: str
    side: Side
    entry_price: float
    exit_price: float
    size: float
    entry_time: datetime
    exit_time: datetime
    pnl: float
    r_multiple: float
    fees: float = 0.0
    exit_reason: ExitReason = ExitReason.MANUAL
    bars_held: int = 0
    mae_r: float = 0.0
    mfe_r: float = 0.0
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def is_win(self) -> bool:
        return self.pnl > 0

    @property
    def return_pct(self) -> float:
        notional = self.entry_price * self.size
        return 0.0 if notional == 0 else self.pnl / notional * 100.0

    @property
    def duration_hours(self) -> float:
        return (self.exit_time - self.entry_time).total_seconds() / 3600.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "size": self.size,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat(),
            "pnl": round(self.pnl, 2),
            "r_multiple": round(self.r_multiple, 3),
            "fees": round(self.fees, 4),
            "exit_reason": self.exit_reason.value,
            "bars_held": self.bars_held,
            "mae_r": round(self.mae_r, 3),
            "mfe_r": round(self.mfe_r, 3),
            "confidence": round(self.confidence, 2),
            "duration_hours": round(self.duration_hours, 2),
            "reasons": list(self.reasons),
        }


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    """Division that returns `default` instead of raising or producing nan/inf."""
    if b == 0 or not math.isfinite(b):
        return default
    out = a / b
    return out if math.isfinite(out) else default
