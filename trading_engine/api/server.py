"""HTTP API and dashboard server.

Uses only the standard library's `http.server` so the engine has no web
framework dependency. It is a single-threaded-per-request server intended for
local use by one operator watching their own system — it is not hardened for
public exposure, and `serve()` binds to localhost by default for that reason.

Endpoints
---------
GET /                       the dashboard
GET /api/status             engine + risk state
GET /api/signals            recent signals
GET /api/chart?symbol=&tf=  OHLCV plus liquidity overlays for the chart
GET /api/positions          open positions
GET /api/backtest?symbol=   run a backtest and return metrics
GET /api/config             the effective configuration
POST /api/flatten           close every open position
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import Config
from ..core.types import AssetClass, ExitReason
from ..data.feeds import DataError, get_feed
from ..indicators.core import add_all
from ..liquidity.analyzer import LiquidityAnalyzer
from ..live.trader import AutoTrader

log = logging.getLogger("trading_engine.api")

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"


class EngineState:
    """Shared state between the HTTP handlers and the trading loop."""

    def __init__(self, config: Config, trader: AutoTrader | None = None) -> None:
        self.config = config
        self.trader = trader or AutoTrader(config)
        self.lock = threading.Lock()
        self.started_at = datetime.now(timezone.utc)


class Handler(BaseHTTPRequestHandler):
    state: EngineState                      # injected by make_server
    server_version = "trading-engine/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------- #

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send({"error": f"{path.name} not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400) -> None:
        self._send({"error": message}, status)

    # -- routing ----------------------------------------------------------- #

    def do_GET(self) -> None:                                   # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if route == "/":
                self._send_file(FRONTEND_DIR / "index.html", "text/html; charset=utf-8")
            elif route == "/app.js":
                self._send_file(
                    FRONTEND_DIR / "app.js", "application/javascript; charset=utf-8"
                )
            elif route == "/styles.css":
                self._send_file(FRONTEND_DIR / "styles.css", "text/css; charset=utf-8")
            elif route == "/favicon.ico":
                self.send_response(204)      # no icon, and no 404 noise either
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif route == "/api/status":
                self._status()
            elif route == "/api/signals":
                self._signals()
            elif route == "/api/positions":
                self._positions()
            elif route == "/api/chart":
                self._chart(query)
            elif route == "/api/backtest":
                self._backtest(query)
            elif route == "/api/config":
                self._send(self.state.config.to_dict())
            else:
                self._error("no such route", 404)
        except DataError as exc:
            self._error(f"data error: {exc}", 503)
        except Exception as exc:                                # noqa: BLE001
            log.exception("error handling %s", self.path)
            self._error(f"internal error: {exc}", 500)

    def do_POST(self) -> None:                                  # noqa: N802
        route = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        try:
            if route == "/api/flatten":
                with self.state.lock:
                    self.state.trader.flatten_all(ExitReason.MANUAL)
                self._send({"ok": True, "message": "all positions flattened"})
            else:
                self._error("no such route", 404)
        except Exception as exc:                                # noqa: BLE001
            log.exception("error handling POST %s", self.path)
            self._error(f"internal error: {exc}", 500)

    # -- handlers ---------------------------------------------------------- #

    def _status(self) -> None:
        trader = self.state.trader
        with self.state.lock:
            payload = {
                "status": trader.status.to_dict(),
                "risk": trader.risk_state.to_dict(),
                "limits": {
                    "risk_per_trade": self.state.config.risk.risk_per_trade,
                    "max_portfolio_heat": self.state.config.risk.max_portfolio_heat,
                    "daily_loss_limit": self.state.config.risk.daily_loss_limit,
                    "max_drawdown_stop": self.state.config.risk.max_drawdown_stop,
                    "min_reward_risk": self.state.config.risk.min_reward_risk,
                    "max_positions": self.state.config.risk.max_positions,
                },
                "portfolio_heat": trader.risk.portfolio_heat(
                    trader.broker.get_positions()
                ),
                "server_time": datetime.now(timezone.utc).isoformat(),
                "started_at": self.state.started_at.isoformat(),
            }
        self._send(payload)

    def _signals(self) -> None:
        with self.state.lock:
            signals = [s.to_dict() for s in self.state.trader.active_signals[-25:]]
            evaluations = list(self.state.trader.recent_evaluations[-25:])
        self._send({"signals": list(reversed(signals)), "evaluations": evaluations})

    def _positions(self) -> None:
        with self.state.lock:
            positions = self.state.trader.broker.get_positions()
            payload = []
            for p in positions:
                mark = getattr(self.state.trader.broker, "mark_price", lambda _s: 0.0)(
                    p.symbol
                )
                d = p.to_dict()
                d["mark_price"] = mark
                d["unrealised_pnl"] = round(p.unrealised_pnl(mark), 2) if mark else 0.0
                d["unrealised_r"] = round(p.unrealised_r(mark), 3) if mark else 0.0
                payload.append(d)
        self._send({"positions": payload})

    def _chart(self, query: dict) -> None:
        cfg = self.state.config
        symbol = query.get("symbol", [cfg.data.symbols[0]])[0]
        timeframe = query.get("tf", [cfg.strategy.timeframe])[0]
        limit = min(int(query.get("limit", ["500"])[0]), 2000)

        feed = get_feed(
            cfg.data.provider, symbol,
            cache_dir=cfg.data.cache_dir, cache_enabled=cfg.data.cache_enabled,
        )
        df = feed.get_bars(symbol, timeframe, limit)
        data = add_all(df, cfg.strategy.atr_period)

        analyzer = LiquidityAnalyzer(
            swing_lookback=cfg.strategy.swing_lookback,
            tolerance_pct=cfg.strategy.liquidity_tolerance_pct,
            min_penetration_pct=cfg.strategy.min_sweep_penetration_pct,
            fvg_min_size_atr=cfg.strategy.fvg_min_size_atr,
            displacement_atr=cfg.strategy.displacement_atr,
            order_block_lookback=cfg.strategy.order_block_lookback,
            atr_period=cfg.strategy.atr_period,
        ).prepare(data)
        ctx = analyzer.context_at(len(data) - 1, feed.get_orderbook(symbol, 20))

        bars = [
            {
                "time": int(ts.timestamp()),
                "open": float(r.open), "high": float(r.high),
                "low": float(r.low), "close": float(r.close),
                "volume": float(r.volume),
            }
            for ts, r in zip(data.index, data.itertuples())
        ]

        # Zones are sent with the bar time they formed at so the chart can draw
        # them from their origin forward rather than across the whole history.
        def zone_payload(zones, kind):
            out = []
            for z in zones[:8]:
                if z.index >= len(data):
                    continue
                out.append(
                    {
                        **z.to_dict(),
                        "from_time": int(data.index[z.index].timestamp()),
                        "to_time": int(data.index[-1].timestamp()),
                        "zone_kind": kind,
                    }
                )
            return out

        self._send(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "feed": feed.name,
                "is_synthetic": feed.name == "synthetic",
                "bars": bars,
                "liquidity": ctx.to_dict(),
                "zones": (
                    zone_payload(ctx.demand_zones, "demand")
                    + zone_payload(ctx.supply_zones, "supply")
                ),
                "pools": [
                    {**p.to_dict(), "side": "above"} for p in ctx.pools_above[:5]
                ] + [
                    {**p.to_dict(), "side": "below"} for p in ctx.pools_below[:5]
                ],
            }
        )

    def _backtest(self, query: dict) -> None:
        from ..backtest.engine import Backtester
        from ..backtest.metrics import compute_metrics

        cfg = self.state.config
        symbol = query.get("symbol", [cfg.data.symbols[0]])[0]
        timeframe = query.get("tf", [cfg.strategy.timeframe])[0]
        limit = min(int(query.get("limit", ["2000"])[0]), 10_000)

        feed = get_feed(
            cfg.data.provider, symbol,
            cache_dir=cfg.data.cache_dir, cache_enabled=cfg.data.cache_enabled,
        )
        df = feed.get_bars(symbol, timeframe, limit)
        result = Backtester(cfg).run(
            df, symbol=symbol, asset_class=feed.asset_class(symbol)
        )
        metrics = compute_metrics(
            result.trades, result.equity_curve, result.starting_equity,
            periods_per_year=cfg.backtest.annualisation_periods,
            exposure=result.exposure,
        )

        curve = []
        if result.equity_curve is not None:
            # Thin the curve so the payload stays small on long backtests.
            step = max(1, len(result.equity_curve) // 500)
            sampled = result.equity_curve.iloc[::step]
            curve = [
                {"time": int(ts.timestamp()), "value": float(v)}
                for ts, v in sampled.items()
            ]

        self._send(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "feed": feed.name,
                "is_synthetic": feed.name == "synthetic",
                "bars_tested": result.bars_processed,
                "metrics": metrics.to_dict(),
                "equity_curve": curve,
                "trades": [t.to_dict() for t in result.trades[-100:]],
                "rejections": dict(
                    sorted(result.rejections.items(), key=lambda kv: -kv[1])
                ),
            }
        )


def make_server(
    config: Config, host: str = "127.0.0.1", port: int = 8000,
    trader: AutoTrader | None = None,
) -> ThreadingHTTPServer:
    state = EngineState(config, trader)
    handler = type("BoundHandler", (Handler,), {"state": state})
    return ThreadingHTTPServer((host, port), handler)


def serve(
    config: Config | None = None, host: str = "127.0.0.1", port: int = 8000,
    trader: AutoTrader | None = None, run_trader: bool = False,
) -> None:
    """Start the dashboard, optionally with the trading loop in a background
    thread.

    Binds to localhost by default: this server has no authentication, and an
    endpoint that can flatten positions should not be reachable from the
    network.
    """
    config = config or Config()
    server = make_server(config, host, port, trader)
    engine_state: EngineState = server.RequestHandlerClass.state  # type: ignore[attr-defined]

    if run_trader:
        thread = threading.Thread(
            target=engine_state.trader.run, daemon=True, name="autotrader"
        )
        thread.start()
        log.info("autotrader thread started")

    log.info("dashboard on http://%s:%d", host, port)
    if host not in ("127.0.0.1", "localhost"):
        log.warning(
            "bound to %s — this API is unauthenticated and can close positions; "
            "do not expose it to an untrusted network", host
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        engine_state.trader.stop()
        server.server_close()
