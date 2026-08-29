"""Trading engine — a liquidity-driven, multi-asset systematic trading system.

Signal generation from market liquidity structure, risk-first position sizing
with an enforced 2-3R reward:risk floor, an event-driven backtester with a
realistic cost model, and a paper/live autotrader sharing one code path.

Quick start::

    from trading_engine import Config, Backtester, get_feed

    config = Config()
    feed = get_feed("auto", "BTC/USDT")
    bars = feed.get_bars("BTC/USDT", "15m", 2000)
    result = Backtester(config).run(bars, symbol="BTC/USDT")

    from trading_engine import compute_metrics
    print(compute_metrics(result.trades, result.equity_curve,
                          result.starting_equity, exposure=result.exposure).summary())

Nothing in this package is investment advice, and no configuration of it can
guarantee a return. What it enforces is the *geometry* of each trade — the ratio
between what is risked and what is targeted. Hit rate is a property of the
market, not of this code.
"""

from .backtest import (
    Backtester, BacktestResult, PerformanceReport, compute_metrics,
    split_walk_forward,
)
from .config import (
    BacktestConfig, Config, DataConfig, ExecutionConfig, FundamentalsConfig,
    LiveConfig, RiskConfig, StrategyConfig,
)
from .core import (
    AssetClass, Bar, ExitReason, Order, OrderBook, Position, Side, Signal,
    TakeProfit, Trade,
)
from .data import DataFeed, SyntheticFeed, get_feed
from .fundamentals import (
    CryptoFundamentals, EconomicEvent, FundamentalAnalyzer, MacroSnapshot,
)
from .liquidity import LiquidityAnalyzer, LiquidityContext
from .live import AutoTrader, Broker, PaperBroker, get_broker
from .risk import RiskManager, RiskState, plan_levels
from .strategy import LiquiditySweepStrategy

__version__ = "1.0.0"

__all__ = [
    "__version__",
    # config
    "Config", "RiskConfig", "StrategyConfig", "ExecutionConfig", "DataConfig",
    "FundamentalsConfig", "LiveConfig", "BacktestConfig",
    # core
    "Signal", "TakeProfit", "Side", "Order", "Position", "Trade", "Bar",
    "OrderBook", "AssetClass", "ExitReason",
    # data
    "get_feed", "DataFeed", "SyntheticFeed",
    # analysis
    "LiquidityAnalyzer", "LiquidityContext", "LiquiditySweepStrategy",
    "FundamentalAnalyzer", "EconomicEvent", "CryptoFundamentals",
    "MacroSnapshot",
    # risk
    "RiskManager", "RiskState", "plan_levels",
    # backtest
    "Backtester", "BacktestResult", "compute_metrics", "PerformanceReport",
    "split_walk_forward",
    # live
    "AutoTrader", "Broker", "PaperBroker", "get_broker",
]
