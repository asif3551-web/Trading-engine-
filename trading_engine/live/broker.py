"""Broker adapters.

`PaperBroker` is the default and is a first-class implementation, not a stub.
The autotrader drives paper and live through exactly the same interface and the
same code path — if paper and live were different code, neither would be tested.

`BinanceBroker` is included as a real-money adapter. It refuses to place an
order unless the caller has explicitly armed live trading, because the failure
mode of a mis-wired config here is unbounded.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from ..core.types import (
    Order, OrderStatus, OrderType, Position, Side, Trade, ExitReason, safe_div,
)


class BrokerError(RuntimeError):
    pass


class Broker(ABC):
    """Minimal order/position interface.

    Deliberately small. Anything a strategy needs beyond this belongs in the
    engine, not spread across every venue adapter.
    """

    name = "base"
    is_live = False

    @abstractmethod
    def get_equity(self) -> float: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def submit(self, order: Order) -> Order: ...

    @abstractmethod
    def cancel(self, order: Order) -> Order: ...

    def get_open_orders(self) -> list[Order]:
        return []

    def close_position(self, position: Position, price: float | None = None) -> Order:
        """Flatten a position with a reduce-only market order."""
        return self.submit(
            Order(
                symbol=position.symbol,
                side=position.side.opposite,
                size=position.size,
                order_type=OrderType.MARKET,
                reduce_only=True,
                client_order_id=new_client_id("close"),
                tag="close",
            )
        )

    def reconcile(self) -> None:
        """Re-sync local state from the broker. The broker is the source of
        truth, always — local state is a cache that can be wrong."""
        return None


def new_client_id(prefix: str = "te") -> str:
    """Idempotency key. Every order carries one so a retry after a timeout
    cannot silently double the position."""
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


# --------------------------------------------------------------------------- #
# Paper broker
# --------------------------------------------------------------------------- #

class PaperBroker(Broker):
    """Simulated broker with the same cost model as the backtester.

    Holds positions in memory, marks them to the last price it was given, and
    applies fees and slippage on every fill.
    """

    name = "paper"
    is_live = False

    def __init__(
        self,
        starting_equity: float = 10_000.0,
        taker_fee: float = 0.0005,
        maker_fee: float = 0.0002,
        slippage_bps: float = 2.0,
    ) -> None:
        self.cash = starting_equity
        self.starting_equity = starting_equity
        self.taker_fee = taker_fee
        self.maker_fee = maker_fee
        self.slippage_bps = slippage_bps

        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._prices: dict[str, float] = {}
        self.trades: list[Trade] = []

    # -- marking ----------------------------------------------------------- #

    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    def mark_price(self, symbol: str) -> float:
        return self._prices.get(symbol, 0.0)

    def get_equity(self) -> float:
        """Cash plus the mark-to-market value of open positions."""
        unrealised = sum(
            p.unrealised_pnl(self.mark_price(p.symbol))
            for p in self._positions.values()
            if self.mark_price(p.symbol) > 0
        )
        return self.cash + unrealised

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_position(self, symbol: str) -> Position | None:
        return self._positions.get(symbol)

    def get_open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.is_open]

    # -- execution --------------------------------------------------------- #

    def _fill_price(self, symbol: str, side: Side, requested: float | None) -> float:
        base = requested or self.mark_price(symbol)
        if base <= 0:
            raise BrokerError(f"no price available for {symbol}")
        slip = base * self.slippage_bps / 10_000.0
        return base + side.sign * slip

    def submit(self, order: Order) -> Order:
        if not order.client_order_id:
            order.client_order_id = new_client_id("paper")
        if order.client_order_id in self._orders:
            # Idempotency: a retried submit returns the original order.
            return self._orders[order.client_order_id]

        if order.size <= 0:
            order.status = OrderStatus.REJECTED
            self._orders[order.client_order_id] = order
            return order

        requested = (
            order.limit_price if order.order_type is OrderType.LIMIT
            else order.stop_price if order.order_type in (
                OrderType.STOP, OrderType.STOP_LIMIT
            )
            else None
        )
        try:
            fill = self._fill_price(order.symbol, order.side, requested)
        except BrokerError:
            order.status = OrderStatus.REJECTED
            self._orders[order.client_order_id] = order
            return order

        is_maker = order.order_type is OrderType.LIMIT
        fee = fill * order.size * (self.maker_fee if is_maker else self.taker_fee)

        order.filled_size = order.size
        order.avg_fill_price = fill
        order.fee = fee
        order.status = OrderStatus.FILLED
        order.broker_order_id = order.client_order_id
        order.updated_at = datetime.now(timezone.utc)
        self._orders[order.client_order_id] = order

        self._apply_fill(order, fill, fee)
        return order

    def _apply_fill(self, order: Order, fill: float, fee: float) -> None:
        self.cash -= fee
        existing = self._positions.get(order.symbol)

        if existing is None:
            if order.reduce_only:
                return       # nothing to reduce
            self._positions[order.symbol] = Position(
                symbol=order.symbol,
                side=order.side,
                size=order.size,
                entry_price=fill,
                opened_at=datetime.now(timezone.utc),
                fees_paid=fee,
            )
            return

        if existing.side is order.side and not order.reduce_only:
            # Scale in: weighted-average the entry.
            total = existing.size + order.size
            existing.entry_price = safe_div(
                existing.entry_price * existing.size + fill * order.size, total,
                existing.entry_price,
            )
            existing.size = total
            existing.initial_size = max(existing.initial_size, total)
            existing.fees_paid += fee
            return

        # Reduce or close.
        closing = min(order.size, existing.size)
        pnl = (fill - existing.entry_price) * existing.side.sign * closing
        existing.realised_pnl += pnl
        existing.fees_paid += fee
        existing.size -= closing
        self.cash += pnl

        if existing.size <= 1e-12:
            self.trades.append(
                Trade(
                    symbol=existing.symbol,
                    side=existing.side,
                    entry_price=existing.entry_price,
                    exit_price=fill,
                    size=existing.initial_size,
                    entry_time=existing.opened_at,
                    exit_time=datetime.now(timezone.utc),
                    pnl=existing.realised_pnl - existing.fees_paid,
                    r_multiple=safe_div(
                        existing.realised_pnl - existing.fees_paid,
                        existing.risk_per_unit * existing.initial_size,
                    ),
                    fees=existing.fees_paid,
                    exit_reason=ExitReason(order.tag) if order.tag in
                    {e.value for e in ExitReason} else ExitReason.MANUAL,
                    bars_held=existing.bars_held,
                    mae_r=existing.max_adverse,
                    mfe_r=existing.max_favourable,
                    confidence=existing.signal.confidence if existing.signal else 0.0,
                )
            )
            del self._positions[order.symbol]

    def cancel(self, order: Order) -> Order:
        stored = self._orders.get(order.client_order_id)
        if stored and stored.is_open:
            stored.status = OrderStatus.CANCELLED
            stored.updated_at = datetime.now(timezone.utc)
            return stored
        return order

    def register_position(self, position: Position) -> None:
        """Attach protective levels to a position the broker just opened."""
        self._positions[position.symbol] = position


# --------------------------------------------------------------------------- #
# Binance
# --------------------------------------------------------------------------- #

class BinanceBroker(Broker):
    """Live Binance spot adapter.

    Requires BINANCE_API_KEY / BINANCE_API_SECRET in the environment. Keys are
    never read from config files or written to logs.

    `armed` must be explicitly True before any order is sent. That flag exists
    so that a config typo, a stale environment variable or a copy-pasted example
    cannot by itself put real money at risk.
    """

    name = "binance"
    is_live = True
    BASE = "https://api.binance.com"

    def __init__(self, armed: bool = False, timeout: int = 15) -> None:
        self.api_key = os.environ.get("BINANCE_API_KEY", "")
        self.api_secret = os.environ.get("BINANCE_API_SECRET", "")
        if not self.api_key or not self.api_secret:
            raise BrokerError(
                "BINANCE_API_KEY and BINANCE_API_SECRET must be set in the "
                "environment for live trading"
            )
        self.armed = armed
        self.timeout = timeout
        self._equity_cache: tuple[float, float] = (0.0, 0.0)

    def _sign(self, params: dict) -> str:
        query = urllib.parse.urlencode(params)
        signature = hmac.new(
            self.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        return f"{query}&signature={signature}"

    def _request(self, method: str, path: str, params: dict) -> dict:
        params = {**params, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        body = self._sign(params)
        url = f"{self.BASE}{path}"
        if method == "GET":
            url = f"{url}?{body}"
            data = None
        else:
            data = body.encode()
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={
                "X-MBX-APIKEY": self.api_key,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            raise BrokerError(f"binance {method} {path} failed: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BrokerError(f"binance request failed: {exc}") from exc

    def get_equity(self) -> float:
        now = time.time()
        cached_at, value = self._equity_cache
        if now - cached_at < 10.0 and value > 0:
            return value
        account = self._request("GET", "/api/v3/account", {})
        total = 0.0
        for balance in account.get("balances", []):
            free = float(balance.get("free", 0) or 0)
            locked = float(balance.get("locked", 0) or 0)
            amount = free + locked
            if amount <= 0:
                continue
            asset = balance.get("asset", "")
            if asset in ("USDT", "USDC", "BUSD"):
                total += amount
            # Non-stable balances need a price lookup; the autotrader marks
            # those through its own feed rather than duplicating it here.
        self._equity_cache = (now, total)
        return total

    def get_positions(self) -> list[Position]:
        # Spot has no position concept; the engine tracks its own from fills.
        return []

    def submit(self, order: Order) -> Order:
        if not self.armed:
            raise BrokerError(
                "live broker is not armed — refusing to submit a real order. "
                "Arm explicitly only when you intend to trade real money."
            )
        params = {
            "symbol": order.symbol.replace("/", "").upper(),
            "side": "BUY" if order.side is Side.LONG else "SELL",
            "type": order.order_type.value.upper(),
            "quantity": order.size,
            "newClientOrderId": order.client_order_id or new_client_id("bnb"),
        }
        if order.order_type is OrderType.LIMIT:
            params["price"] = order.limit_price
            params["timeInForce"] = "GTC"
        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            params["stopPrice"] = order.stop_price

        response = self._request("POST", "/api/v3/order", params)
        order.broker_order_id = str(response.get("orderId", ""))
        order.status = (
            OrderStatus.FILLED if response.get("status") == "FILLED"
            else OrderStatus.SUBMITTED
        )
        executed = float(response.get("executedQty", 0) or 0)
        order.filled_size = executed
        quote = float(response.get("cummulativeQuoteQty", 0) or 0)
        if executed > 0:
            order.avg_fill_price = quote / executed
        order.updated_at = datetime.now(timezone.utc)
        return order

    def cancel(self, order: Order) -> Order:
        if not order.broker_order_id:
            return order
        self._request(
            "DELETE", "/api/v3/order",
            {
                "symbol": order.symbol.replace("/", "").upper(),
                "orderId": order.broker_order_id,
            },
        )
        order.status = OrderStatus.CANCELLED
        return order


def get_broker(name: str, starting_equity: float = 10_000.0, armed: bool = False,
               **kwargs) -> Broker:
    if name == "paper":
        return PaperBroker(starting_equity=starting_equity, **kwargs)
    if name == "binance":
        return BinanceBroker(armed=armed)
    raise BrokerError(f"unknown broker {name!r}")
