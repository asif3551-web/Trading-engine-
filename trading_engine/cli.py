"""Command-line interface.

    python -m trading_engine scan     --symbol BTC/USDT
    python -m trading_engine backtest --symbol BTC/USDT --bars 3000
    python -m trading_engine walkforward --symbol BTC/USDT
    python -m trading_engine trade    --symbol BTC/USDT      # paper by default
    python -m trading_engine serve    --port 8000
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .backtest.engine import Backtester
from .backtest.metrics import compute_metrics, split_walk_forward
from .config import Config
from .core.types import Side
from .data.feeds import DataError, get_feed
from .live.broker import BrokerError, get_broker
from .live.trader import AutoTrader
from .strategy.liquidity_sweep import LiquiditySweepStrategy


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_config(args) -> Config:
    config = Config.load(args.config)
    if getattr(args, "symbol", None):
        # Accept "BTC/USDT,ETH/USDT" so several markets can be watched at once.
        config.data.symbols = [
            s.strip() for s in args.symbol.split(",") if s.strip()
        ]
    if getattr(args, "timeframe", None):
        config.strategy.timeframe = args.timeframe
    if getattr(args, "provider", None):
        config.data.provider = args.provider
    if getattr(args, "risk", None) is not None:
        config.risk.risk_per_trade = args.risk
    if getattr(args, "equity", None) is not None:
        config.backtest.starting_equity = args.equity
        config.live.starting_equity = args.equity
    config.raise_on_invalid()
    return config


def _feed_for(config: Config, symbol: str, provider: str | None):
    return get_feed(
        provider or config.data.provider,
        symbol,
        cache_dir=config.data.cache_dir,
        cache_enabled=config.data.cache_enabled,
    )


def _warn_if_synthetic(feed) -> None:
    if feed.name == "synthetic":
        print(
            "\n  NOTE: using the SYNTHETIC feed — these are generated prices, not\n"
            "  a real market. Results show whether the engine runs, and say\n"
            "  nothing about whether the strategy is profitable.\n",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------- #

def cmd_scan(args) -> int:
    config = _load_config(args)
    symbol = config.data.symbols[0]
    feed = _feed_for(config, symbol, args.provider)
    _warn_if_synthetic(feed)

    df = feed.get_bars(symbol, config.strategy.timeframe, config.strategy.lookback_bars)
    htf = None
    if config.strategy.require_htf_alignment:
        try:
            htf = feed.get_bars(symbol, config.strategy.htf_timeframe, 200)
        except DataError:
            htf = None

    strategy = LiquiditySweepStrategy(
        config.strategy,
        min_reward_risk=config.risk.min_reward_risk,
        min_expected_r=config.risk.min_expected_r,
        min_stop_atr=config.risk.min_stop_distance_atr,
        max_stop_atr=config.risk.max_stop_distance_atr,
    )
    strategy.prepare(df, htf)
    ev = strategy.evaluate(
        len(df) - 1,
        book=feed.get_orderbook(symbol, config.data.orderbook_depth),
        asset_class=feed.asset_class(symbol),
        symbol=symbol,
        tick_size=feed.tick_size(symbol),
    )

    if args.json:
        print(json.dumps(ev.to_dict(), indent=2, default=str))
        return 0

    price = float(df["close"].iloc[-1])
    print(f"\n{symbol} · {config.strategy.timeframe} · last {price:,.8g}")
    print(f"feed: {feed.name}   bars: {len(df)}\n")

    if ev.signal is None:
        print(f"  NO SIGNAL — {ev.rejected}")
        print(
            f"\n  scores: liquidity {ev.liquidity_score:.1f} · "
            f"technical {ev.technical_score:.1f} · confidence {ev.confidence:.1f}"
        )
        return 0

    s = ev.signal
    arrow = "LONG" if s.side is Side.LONG else "SHORT"
    print(f"  {arrow}  confidence {s.confidence:.0f}/100\n")
    print(f"  Entry     {s.entry:>16,.8g}")
    print(
        f"  Stop      {s.stop_loss:>16,.8g}   "
        f"({s.stop_distance_pct:.2f}% away, 1R)"
    )
    for i, tp in enumerate(s.take_profits, 1):
        move = (tp.price - s.entry) / s.entry * 100 * s.side.sign
        print(
            f"  TP{i}       {tp.price:>16,.8g}   "
            f"({move:+.2f}%, {tp.r_multiple:.2f}R, exit {tp.size_pct:.0%})"
        )
    print(
        f"\n  Reward:risk {s.reward_risk:.2f} weighted · {s.max_r:.2f}R at the "
        f"furthest target"
    )
    print(f"  Break-even win rate needed: {s.breakeven_win_rate:.1%}")
    print("\n  Why:")
    for reason in s.reasons:
        print(f"    - {reason}")
    print()
    return 0


def cmd_backtest(args) -> int:
    config = _load_config(args)
    symbol = config.data.symbols[0]
    feed = _feed_for(config, symbol, args.provider)
    _warn_if_synthetic(feed)

    df = feed.get_bars(symbol, config.strategy.timeframe, args.bars)
    print(f"backtesting {symbol} {config.strategy.timeframe}: {len(df)} bars "
          f"({df.index[0]} to {df.index[-1]})")

    result = Backtester(config).run(
        df, symbol=symbol, asset_class=feed.asset_class(symbol)
    )
    metrics = compute_metrics(
        result.trades, result.equity_curve, result.starting_equity,
        periods_per_year=config.backtest.annualisation_periods,
        exposure=result.exposure,
    )

    if args.json:
        print(json.dumps(
            {
                "metrics": metrics.to_dict(),
                "trades": [t.to_dict() for t in result.trades],
                "rejections": result.rejections,
            },
            indent=2, default=str,
        ))
        return 0

    print(metrics.summary())
    if result.rejections:
        print("\nWhy setups were rejected:")
        for reason, count in sorted(result.rejections.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {reason:<28} {count:>6d}")
    return 0


def cmd_walkforward(args) -> int:
    config = _load_config(args)
    symbol = config.data.symbols[0]
    feed = _feed_for(config, symbol, args.provider)
    _warn_if_synthetic(feed)

    df = feed.get_bars(symbol, config.strategy.timeframe, args.bars)
    try:
        splits = split_walk_forward(
            df, config.backtest.walk_forward_splits, config.backtest.in_sample_ratio
        )
    except ValueError as exc:
        print(f"cannot split the data: {exc}", file=sys.stderr)
        return 1

    backtester = Backtester(config)
    print(f"\nwalk-forward over {len(splits)} windows ({len(df)} bars)\n")
    header = f"{'window':<8}{'IS trades':>10}{'IS exp':>10}{'OOS trades':>12}{'OOS exp':>10}{'degrade':>10}"
    print(header)
    print("-" * len(header))

    oos_expectancies = []
    for i, (is_df, oos_df) in enumerate(splits, 1):
        is_res = backtester.run(is_df, symbol=symbol)
        oos_res = backtester.run(oos_df, symbol=symbol)
        is_m = compute_metrics(
            is_res.trades, is_res.equity_curve, is_res.starting_equity,
            config.backtest.annualisation_periods, is_res.exposure,
        )
        oos_m = compute_metrics(
            oos_res.trades, oos_res.equity_curve, oos_res.starting_equity,
            config.backtest.annualisation_periods, oos_res.exposure,
        )
        degrade = (
            1.0 - oos_m.expectancy_r / is_m.expectancy_r
            if is_m.expectancy_r else 0.0
        )
        oos_expectancies.append(oos_m.expectancy_r)
        print(
            f"{i:<8}{is_m.total_trades:>10}{is_m.expectancy_r:>10.3f}"
            f"{oos_m.total_trades:>12}{oos_m.expectancy_r:>10.3f}{degrade:>10.1%}"
        )

    if oos_expectancies:
        avg = sum(oos_expectancies) / len(oos_expectancies)
        print(f"\naverage out-of-sample expectancy: {avg:+.3f}R")
        print(
            "\nA strategy that is positive in-sample and negative out-of-sample "
            "is fitted,\nnot predictive. Consistency across windows matters more "
            "than the average."
        )
    return 0


def cmd_trade(args) -> int:
    config = _load_config(args)
    if args.live:
        config.live.mode = "live"
        config.live.broker = args.broker
    try:
        broker = get_broker(
            config.live.broker,
            starting_equity=config.live.starting_equity,
            armed=args.live,
        )
    except BrokerError as exc:
        print(f"broker error: {exc}", file=sys.stderr)
        return 1

    if args.live:
        print("\n" + "!" * 62)
        print("  LIVE TRADING — REAL ORDERS WILL BE SENT WITH REAL MONEY")
        print(f"  broker={config.live.broker}  symbols={config.data.symbols}")
        print(f"  risk/trade={config.risk.risk_per_trade:.2%}  "
              f"daily loss limit={config.risk.daily_loss_limit:.2%}")
        print("!" * 62)
        if input("\nType 'I ACCEPT THE RISK' to continue: ") != "I ACCEPT THE RISK":
            print("aborted")
            return 1

    trader = AutoTrader(config, broker=broker)
    trader.run(max_iterations=args.iterations)

    print(f"\nfinal equity: {trader.status.equity:,.2f}")
    if getattr(broker, "trades", None):
        metrics = compute_metrics(
            broker.trades, None, config.live.starting_equity
        )
        print(metrics.summary())
    return 0


def cmd_symbols(args) -> int:
    """List the markets the engine knows how to fetch."""
    from .data.symbols import catalogue, describe, resolve

    if args.query:
        for name in args.query.split(","):
            name = name.strip()
            if not name:
                continue
            market = resolve(name)
            print(f"\n  {name}")
            print(f"    provider   {market.provider}:{market.provider_symbol}")
            print(f"    market     {market.description}")
            print(f"    class      {market.asset_class.value}")
            print(f"    timing     "
                  f"{'real time' if market.is_realtime else f'~{market.delay_sec // 60} min delayed'}")
            print(f"    volume     {'yes' if market.has_volume else 'NO — volume-based confluence abstains'}")
        print()
        return 0

    for group, entries in catalogue().items():
        print(f"\n{group}")
        print("-" * len(group))
        for name, market in entries:
            flag = "" if market.has_volume else "  (no volume)"
            print(f"  {name:<14} {market.provider_symbol:<12} {market.description}{flag}")
    print(
        "\nAny of these spellings work: XAUUSD, XAU/USD, GOLD, xau.\n"
        "Unlisted tickers are passed to Yahoo verbatim, so AAPL, ^VIX and\n"
        "ES=F all work too.\n"
    )
    return 0


def cmd_tune(args) -> int:
    """Walk-forward parameter sweep on real data."""
    from .backtest.tuner import format_report, tune

    config = _load_config(args)
    symbol = config.data.symbols[0]
    feed = _feed_for(config, symbol, args.provider)
    _warn_if_synthetic(feed)
    if feed.name == "synthetic":
        print(
            "  Tuning on synthetic data is meaningless: it is a random walk, so\n"
            "  every setting must lose after costs. Use a real feed.\n",
            file=sys.stderr,
        )

    df = feed.get_bars(symbol, config.strategy.timeframe, args.bars)
    print(f"tuning {symbol} {config.strategy.timeframe} over {len(df)} bars "
          f"({df.index[0].date()} to {df.index[-1].date()})")

    def show(n: int, total: int, label: str) -> None:
        print(f"\r  [{n:>3}/{total}] {label:<52}", end="", flush=True)

    candidates = tune(
        df, base_config=config, symbol=symbol,
        n_splits=config.backtest.walk_forward_splits,
        in_sample_ratio=config.backtest.in_sample_ratio,
        min_out_sample_trades=args.min_trades,
        max_candidates=args.max_candidates,
        progress=None if args.json else show,
    )
    if not args.json:
        print("\r" + " " * 70)

    if args.json:
        print(json.dumps([c.to_dict() for c in candidates], indent=2, default=str))
        return 0

    print(format_report(candidates, top=args.top))
    return 0


def cmd_vendor_chart(args) -> int:
    """Download the charting library into frontend/vendor/ for offline use.

    The dashboard tries this local copy first, then jsdelivr and unpkg. Running
    this is optional — the CDN path works — but it makes the chart independent
    of the network, which is worth having on a restricted or offline machine.
    """
    import urllib.error
    import urllib.request
    from pathlib import Path

    version = args.chart_version
    filename = "lightweight-charts.standalone.production.js"
    # jsdelivr and unpkg both mirror the npm package. cdnjs does not host
    # lightweight-charts, so it is not tried at all — listing it only produced
    # a confusing 404 on the first attempt.
    sources = [
        f"https://cdn.jsdelivr.net/npm/lightweight-charts@{version}/dist/{filename}",
        f"https://unpkg.com/lightweight-charts@{version}/dist/{filename}",
    ]

    target_dir = Path(__file__).resolve().parent.parent / "frontend" / "vendor"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename

    for url in sources:
        print(f"trying {url}")
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "trading-engine/1.0"}
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  failed: {exc}")
            continue

        # Sanity-check the payload rather than trusting a 200: a captive portal
        # or proxy error page would otherwise be written out as "the library".
        if len(body) < 50_000 or b"createChart" not in body:
            print(f"  rejected: {len(body)} bytes and no createChart symbol "
                  f"— probably a proxy error page, not the library")
            continue

        target.write_bytes(body)
        print(f"\nsaved {len(body):,} bytes to {target}")
        print("the dashboard will now load its chart with no network at all")
        return 0

    print(
        "\ncould not download the chart library from any source.\n"
        "Download it manually on a machine with access and place it at:\n"
        f"  {target}\n"
        f"Source: https://unpkg.com/lightweight-charts@{version}/dist/{filename}",
        file=sys.stderr,
    )
    return 1


def cmd_serve(args) -> int:
    from .api.server import serve

    config = _load_config(args)
    serve(config, host=args.host, port=args.port, run_trader=args.trade)
    return 0


# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trading_engine",
        description="Liquidity-driven trading engine: signals, backtesting, "
                    "and paper/live auto-trading.",
    )
    parser.add_argument("-c", "--config", help="path to a YAML config file")
    parser.add_argument("-v", "--verbose", action="store_true")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-s", "--symbol",
        help="one symbol or a comma-separated list, e.g. BTC/USDT,ETH/USDT "
             "(also AAPL, EURUSD=X, ^GSPC, GC=F)",
    )
    common.add_argument("-t", "--timeframe", help="1m 5m 15m 1h 4h 1d")
    common.add_argument(
        "-p", "--provider",
        help="auto | binance | yfinance | synthetic | csv:<dir>",
    )
    common.add_argument("--risk", type=float, help="fraction of equity per trade")
    common.add_argument("--equity", type=float, help="starting equity")
    common.add_argument("--json", action="store_true", help="machine-readable output")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "scan", parents=[common], help="evaluate the latest bar for a signal",
    ).set_defaults(func=cmd_scan)

    bt = sub.add_parser("backtest", parents=[common], help="run a backtest")
    bt.add_argument("--bars", type=int, default=3000)
    bt.set_defaults(func=cmd_backtest)

    wf = sub.add_parser(
        "walkforward", parents=[common],
        help="walk-forward analysis across in/out-of-sample windows",
    )
    wf.add_argument("--bars", type=int, default=8000)
    wf.set_defaults(func=cmd_walkforward)

    tr = sub.add_parser("trade", parents=[common], help="run the autotrader")
    tr.add_argument(
        "--live", action="store_true",
        help="ARM REAL MONEY TRADING (paper is the default)",
    )
    tr.add_argument("--broker", default="binance", help="broker for --live")
    tr.add_argument(
        "--iterations", type=int, default=None, help="stop after N cycles",
    )
    tr.set_defaults(func=cmd_trade)

    sy = sub.add_parser(
        "symbols", help="list supported markets, or resolve specific symbols",
    )
    sy.add_argument(
        "query", nargs="?", default="",
        help="comma-separated symbols to resolve, e.g. XAUUSD,EURUSD",
    )
    sy.set_defaults(func=cmd_symbols, config=None, verbose=False)

    tu = sub.add_parser(
        "tune", parents=[common],
        help="walk-forward sweep of exit/confluence settings on real data",
    )
    tu.add_argument("--bars", type=int, default=8000)
    tu.add_argument(
        "--min-trades", type=int, default=20,
        help="disqualify settings with fewer out-of-sample trades",
    )
    tu.add_argument("--max-candidates", type=int, default=60)
    tu.add_argument("--top", type=int, default=10)
    tu.set_defaults(func=cmd_tune)

    vc = sub.add_parser(
        "vendor-chart",
        help="download the charting library for offline dashboard use",
    )
    vc.add_argument("--chart-version", default="4.2.0")
    vc.set_defaults(func=cmd_vendor_chart, config=None, verbose=False)

    sv = sub.add_parser("serve", parents=[common], help="run the dashboard")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument(
        "--no-trade", dest="trade", action="store_false",
        help="serve the dashboard only, without the paper autotrader",
    )
    sv.set_defaults(trade=True)
    sv.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except DataError as exc:
        print(f"data error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
