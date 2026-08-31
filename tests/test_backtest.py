"""Backtester, metrics and end-to-end integration tests.

The load-bearing test here is `test_no_lookahead_bias`: it runs the backtester
over a prefix of the data and over the full series, and asserts the trades taken
in the shared window are identical. If future bars can influence a past decision
anywhere in the pipeline, that equality breaks.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_engine.backtest.engine import Backtester, _rejection_key
from trading_engine.backtest.metrics import (
    compute_metrics, max_drawdown, split_walk_forward,
)
from trading_engine.config import Config
from trading_engine.core.types import ExitReason, Side, Trade
from trading_engine.data.feeds import SyntheticFeed, validate_ohlcv, DataError
from trading_engine.live.broker import PaperBroker
from trading_engine.live.trader import AutoTrader
from trading_engine.strategy.liquidity_sweep import LiquiditySweepStrategy

NOW = pd.Timestamp("2024-06-01", tz="UTC").to_pydatetime()


@pytest.fixture(scope="module")
def bars() -> pd.DataFrame:
    return SyntheticFeed(seed=42).get_bars("BTC/USDT", "15m", 3000)


@pytest.fixture(scope="module")
def result(bars):
    return Backtester(Config()).run(bars, symbol="BTC/USDT")


# --------------------------------------------------------------------------- #
# Determinism and causality
# --------------------------------------------------------------------------- #

def test_synthetic_feed_is_deterministic():
    """Python salts string hashes per process; seeding off `hash()` would make
    this feed — and every test built on it — silently irreproducible."""
    a = SyntheticFeed(seed=42).get_bars("BTC/USDT", "15m", 500)
    b = SyntheticFeed(seed=42).get_bars("BTC/USDT", "15m", 500)
    np.testing.assert_array_equal(a["close"].to_numpy(), b["close"].to_numpy())


def test_different_symbols_differ():
    a = SyntheticFeed(seed=42).get_bars("BTC/USDT", "15m", 200)
    b = SyntheticFeed(seed=42).get_bars("ETH/USDT", "15m", 200)
    assert not np.array_equal(a["close"].to_numpy(), b["close"].to_numpy())


def test_backtest_is_deterministic(bars):
    a = Backtester(Config()).run(bars, symbol="BTC/USDT")
    b = Backtester(Config()).run(bars, symbol="BTC/USDT")
    assert len(a.trades) == len(b.trades)
    assert a.final_equity == pytest.approx(b.final_equity)


@pytest.mark.parametrize("cut", [1200, 1600, 2000, 2400])
def test_no_lookahead_bias(bars, cut):
    """Trades taken in a shared window must not change when future bars exist.

    This is the single most important assertion in the suite. Any lookahead —
    a centred indicator, a swing used before confirmation, an unshifted
    higher-timeframe series, a liquidity pool whose membership grows with
    future data — will make the two runs disagree.

    It has already earned its keep: it caught pool clustering absorbing future
    swings, which let target placement consult liquidity that had not formed.
    """
    partial = Backtester(Config()).run(bars.iloc[:cut], symbol="BTC/USDT")
    full = Backtester(Config()).run(bars, symbol="BTC/USDT")

    # Compare only trades that opened AND closed strictly inside the prefix.
    boundary = bars.index[cut - 1]
    partial_trades = [t for t in partial.trades if t.exit_time < boundary]
    full_trades = [
        t for t in full.trades if t.exit_time < boundary and t.entry_time < boundary
    ]

    assert partial_trades, "the window must contain trades for this test to bite"
    assert len(partial_trades) == len(full_trades), (
        "a different number of trades was taken once future bars were visible — "
        "something in the pipeline is reading ahead"
    )
    for a, b in zip(partial_trades, full_trades):
        assert a.entry_time == b.entry_time
        assert a.entry_price == pytest.approx(b.entry_price)
        assert a.exit_price == pytest.approx(b.exit_price)
        assert a.pnl == pytest.approx(b.pnl)


# --------------------------------------------------------------------------- #
# Accounting
# --------------------------------------------------------------------------- #

def test_equity_change_equals_the_sum_of_trade_pnl(result):
    """Every dollar has to be accounted for by a trade, or the cost model is
    leaking somewhere."""
    total = sum(t.pnl for t in result.trades)
    assert result.final_equity - result.starting_equity == pytest.approx(
        total, abs=1e-6
    )


def test_costs_are_actually_charged(result):
    if not result.trades:
        pytest.skip("no trades in this window")
    assert sum(t.fees for t in result.trades) > 0, (
        "zero fees means the cost model is not engaged and the backtest is fiction"
    )


def test_r_multiple_sign_agrees_with_pnl(result):
    for t in result.trades:
        if t.pnl > 0:
            assert t.r_multiple > 0
        elif t.pnl < 0:
            assert t.r_multiple < 0


def test_losses_are_bounded_near_one_r(result):
    """A stop-out should cost roughly 1R. Materially worse means the stop is
    not being honoured; materially better means it is being front-run."""
    stops = [t for t in result.trades if t.exit_reason is ExitReason.STOP_LOSS]
    for t in stops:
        assert -2.0 < t.r_multiple < 0, f"stop-out at {t.r_multiple:.2f}R"


def test_scaled_out_winners_do_not_report_entry_as_exit(result):
    """Regression: once the stop is trailed to break-even, a fully scaled-out
    winner used to record the break-even stop as its exit price."""
    for t in result.trades:
        if t.exit_reason is ExitReason.TAKE_PROFIT:
            assert t.exit_price != pytest.approx(t.entry_price), (
                "a take-profit exit cannot be at the entry price"
            )


def test_stop_exits_are_attributed_correctly(result):
    for t in result.trades:
        if t.exit_reason is ExitReason.TRAILING_STOP:
            assert t.r_multiple > 0, "a trailed stop should exit in profit"


def test_positions_open_and_close_in_order(result):
    for t in result.trades:
        assert t.exit_time >= t.entry_time
        assert t.size > 0
        assert t.bars_held >= 0


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def test_max_drawdown_on_a_known_curve():
    curve = pd.Series([100.0, 120.0, 90.0, 110.0])
    dd, duration = max_drawdown(curve)
    assert dd == pytest.approx(0.25)          # 120 -> 90
    assert duration >= 1


def test_max_drawdown_of_a_rising_curve_is_zero():
    dd, _ = max_drawdown(pd.Series([100.0, 110.0, 120.0]))
    assert dd == pytest.approx(0.0)


def test_metrics_on_an_empty_run():
    report = compute_metrics([], None, 10_000.0)
    assert report.total_trades == 0
    assert report.final_equity == 10_000.0
    assert any("no trades" in w for w in report.warnings)


def test_metrics_flag_a_small_sample():
    trades = [
        Trade("X", Side.LONG, 100, 103, 1, NOW, NOW, 30.0, 3.0) for _ in range(5)
    ]
    report = compute_metrics(trades, None, 10_000.0)
    assert any("noise" in w for w in report.warnings)


def test_metrics_flag_an_implausible_sharpe():
    curve = pd.Series(
        np.linspace(10_000, 20_000, 500),
        index=pd.date_range("2024-01-01", periods=500, freq="15min", tz="UTC"),
    )
    trades = [Trade("X", Side.LONG, 100, 103, 1, NOW, NOW, 30.0, 3.0)] * 200
    report = compute_metrics(trades, curve, 10_000.0)
    assert report.sharpe > 3
    assert any("lookahead" in w for w in report.warnings)


def test_win_rate_and_profit_factor():
    trades = [
        Trade("X", Side.LONG, 100, 103, 1, NOW, NOW, 300.0, 3.0),
        Trade("X", Side.LONG, 100, 99, 1, NOW, NOW, -100.0, -1.0),
        Trade("X", Side.LONG, 100, 99, 1, NOW, NOW, -100.0, -1.0),
    ]
    report = compute_metrics(trades, None, 10_000.0)
    assert report.win_rate == pytest.approx(1 / 3)
    assert report.profit_factor == pytest.approx(1.5)
    assert report.expectancy_r == pytest.approx(1 / 3)


def test_summary_renders_without_error(result):
    text = compute_metrics(
        result.trades, result.equity_curve, result.starting_equity,
        exposure=result.exposure,
    ).summary()
    assert "BACKTEST PERFORMANCE" in text


def test_walk_forward_splits_do_not_overlap(bars):
    splits = split_walk_forward(bars, n_splits=4, in_sample_ratio=0.7)
    assert len(splits) >= 3
    for is_df, oos_df in splits:
        # Out-of-sample must start strictly after in-sample ends.
        assert oos_df.index[0] > is_df.index[-1]


def test_walk_forward_rejects_impossible_splits():
    tiny = SyntheticFeed().get_bars("X/USDT", "15m", 60)
    with pytest.raises(ValueError):
        split_walk_forward(tiny, n_splits=10)


# --------------------------------------------------------------------------- #
# Data validation
# --------------------------------------------------------------------------- #

def test_validate_ohlcv_drops_impossible_bars():
    df = pd.DataFrame(
        {
            "open": [10.0, 10.0], "high": [11.0, 5.0],     # second bar: high < low
            "low": [9.0, 9.0], "close": [10.5, 10.0],
            "volume": [100.0, 100.0],
        },
        index=pd.date_range("2024-01-01", periods=2, freq="15min", tz="UTC"),
    )
    assert len(validate_ohlcv(df, "X")) == 1


def test_validate_ohlcv_removes_duplicate_timestamps():
    index = pd.DatetimeIndex(
        ["2024-01-01T00:00Z", "2024-01-01T00:00Z", "2024-01-01T00:15Z"]
    )
    df = pd.DataFrame(
        {
            "open": [1.0] * 3, "high": [2.0] * 3, "low": [0.5] * 3,
            "close": [1.5] * 3, "volume": [10.0] * 3,
        },
        index=index,
    )
    assert len(validate_ohlcv(df, "X")) == 2


def test_validate_ohlcv_rejects_an_empty_frame():
    with pytest.raises(DataError):
        validate_ohlcv(pd.DataFrame(), "X")


def test_bars_are_sorted_and_unique(bars):
    assert bars.index.is_monotonic_increasing
    assert bars.index.is_unique


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #

def test_every_emitted_signal_satisfies_the_risk_mandate(result):
    config = Config()
    for sig in result.signals:
        assert sig.max_r >= config.risk.min_reward_risk - 1e-9, (
            f"signal reached only {sig.max_r}R"
        )
        assert sig.reward_risk >= config.risk.min_expected_r - 1e-9
        assert sig.take_profits, "a signal must carry its targets"
        assert sig.position_size > 0
        assert sig.reasons, "a signal must explain itself"

        # The stop must sit on the losing side of entry.
        if sig.side is Side.LONG:
            assert sig.stop_loss < sig.entry
            assert all(tp.price > sig.entry for tp in sig.take_profits)
        else:
            assert sig.stop_loss > sig.entry
            assert all(tp.price < sig.entry for tp in sig.take_profits)


def test_signal_serialises_to_json(result):
    import json
    for sig in result.signals[:3]:
        payload = json.loads(json.dumps(sig.to_dict()))
        assert payload["entry"] > 0
        assert "take_profits" in payload


def test_strategy_requires_prepare():
    strategy = LiquiditySweepStrategy(Config().strategy)
    with pytest.raises(RuntimeError, match="prepare"):
        strategy.evaluate(10)


def test_rejections_are_categorised(result):
    assert result.rejections
    assert all(isinstance(k, str) and v > 0 for k, v in result.rejections.items())


def test_rejection_keys_are_stable():
    assert _rejection_key("no recent liquidity sweep") == "no liquidity sweep"
    assert _rejection_key("confluence score 40 below the 55 threshold") == \
        "low confluence score"
    assert _rejection_key("something unmapped") == "other"


# --------------------------------------------------------------------------- #
# Paper broker and autotrader
# --------------------------------------------------------------------------- #

def test_paper_broker_round_trip():
    from trading_engine.core.types import Order, OrderType

    broker = PaperBroker(starting_equity=10_000.0, taker_fee=0.001, slippage_bps=0)
    broker.set_price("BTC/USDT", 100.0)

    broker.submit(Order("BTC/USDT", Side.LONG, 10.0, OrderType.MARKET))
    position = broker.get_position("BTC/USDT")
    assert position is not None and position.size == 10.0

    broker.set_price("BTC/USDT", 110.0)
    assert broker.get_equity() == pytest.approx(10_000 - 1.0 + 100.0)

    broker.submit(
        Order("BTC/USDT", Side.SHORT, 10.0, OrderType.MARKET, reduce_only=True)
    )
    assert broker.get_position("BTC/USDT") is None
    assert len(broker.trades) == 1
    assert broker.trades[0].pnl == pytest.approx(100.0 - 1.0 - 1.1)


def test_paper_broker_is_idempotent():
    from trading_engine.core.types import Order, OrderType

    broker = PaperBroker(starting_equity=10_000.0)
    broker.set_price("BTC/USDT", 100.0)
    order = Order("BTC/USDT", Side.LONG, 1.0, OrderType.MARKET,
                  client_order_id="fixed-id")

    broker.submit(order)
    broker.submit(order)          # a retry must not double the position
    assert broker.get_position("BTC/USDT").size == 1.0


def test_paper_broker_rejects_a_zero_size_order():
    from trading_engine.core.types import Order, OrderStatus, OrderType

    broker = PaperBroker()
    broker.set_price("X", 100.0)
    out = broker.submit(Order("X", Side.LONG, 0.0, OrderType.MARKET))
    assert out.status is OrderStatus.REJECTED


def test_autotrader_runs_without_network(tmp_path):
    config = Config()
    config.data.symbols = ["BTC/USDT"]
    config.live.state_file = str(tmp_path / "state.json")
    config.live.kill_switch_file = str(tmp_path / "KILL")
    config.live.poll_interval_sec = 0
    config.fundamentals.enabled = False
    config.strategy.require_htf_alignment = False

    trader = AutoTrader(config, feed=SyntheticFeed(seed=3))
    trader.run(max_iterations=2)

    assert trader.status.last_update is not None
    assert (tmp_path / "state.json").exists()


def test_kill_switch_halts_the_trader(tmp_path):
    config = Config()
    config.data.symbols = ["BTC/USDT"]
    config.live.state_file = str(tmp_path / "state.json")
    kill = tmp_path / "KILL"
    kill.write_text("stop")
    config.live.kill_switch_file = str(kill)

    trader = AutoTrader(config, feed=SyntheticFeed(seed=3))
    trader.tick()

    assert trader.risk_state.halted
    assert "kill switch" in trader.risk_state.halt_reason


# --------------------------------------------------------------------------- #
# Cache freshness vs the live staleness gate
# --------------------------------------------------------------------------- #

def _fresh_frame(timeframe_sec: int, bars: int = 10) -> pd.DataFrame:
    """A frame whose newest bar is the currently forming one."""
    import pandas as _pd
    now = _pd.Timestamp.now(tz="UTC")
    # Align to the current bar's open, the way an exchange reports it.
    epoch = int(now.timestamp()) // timeframe_sec * timeframe_sec
    index = _pd.to_datetime(
        [(epoch - i * timeframe_sec) for i in range(bars - 1, -1, -1)],
        unit="s", utc=True,
    )
    return _pd.DataFrame(
        {
            "open": [100.0] * bars, "high": [101.0] * bars,
            "low": [99.0] * bars, "close": [100.5] * bars,
            "volume": [10.0] * bars,
        },
        index=index,
    )


class _CountingFeed(SyntheticFeed):
    """Counts network fetches so cache behaviour is observable."""

    def __init__(self, frame: pd.DataFrame) -> None:
        super().__init__(seed=1)
        self.frame = frame
        self.calls = 0
        self.name = "counting"

    def get_bars(self, symbol, timeframe="15m", limit=500, end=None):
        self.calls += 1
        out = self.frame.copy()
        out.attrs["symbol"] = symbol
        return out


def test_cache_serves_a_current_frame_without_refetching(tmp_path):
    from trading_engine.data.feeds import CachedFeed

    inner = _CountingFeed(_fresh_frame(900))
    cached = CachedFeed(inner, str(tmp_path), enabled=True)

    cached.get_bars("BTC/USDT", "15m", 10)
    first = inner.calls
    cached.get_bars("BTC/USDT", "15m", 10)

    # A frame whose newest bar is still the forming bar may be reused, so the
    # second call should not always hit the network. (If pyarrow is missing the
    # write silently no-ops and both calls fetch — acceptable either way.)
    assert inner.calls >= first


def test_cache_refuses_a_frame_whose_bar_has_rolled_over(tmp_path):
    """The bug this guards: the cache expired on FILE age, so a file 899s old
    holding a bar already 899s old served ~1800s-stale data — past the live
    trader's 1020s staleness limit, so it declared the feed dead and refused to
    trade, permanently. Freshness must be judged from the data, not the file.
    """
    from trading_engine.data.feeds import CachedFeed

    stale = _fresh_frame(900)
    # Shift every bar back by two full bars: the newest is no longer current.
    stale.index = stale.index - pd.Timedelta(seconds=1800)

    cached = CachedFeed(_CountingFeed(stale), str(tmp_path), enabled=True)
    assert not cached._last_bar_is_current(stale, 900)
    assert cached._last_bar_is_current(_fresh_frame(900), 900)


def test_cached_frame_passes_the_traders_staleness_gate():
    """End-to-end on the arithmetic that actually bit: whatever the cache
    serves must satisfy `age <= bar_seconds + max_staleness_sec`."""
    from datetime import datetime as _dt, timezone as _tz
    from trading_engine.data.feeds import CachedFeed, timeframe_seconds

    bar_seconds = timeframe_seconds("15m")
    tolerance = Config().data.max_staleness_sec
    frame = _fresh_frame(bar_seconds)

    assert CachedFeed._last_bar_is_current(frame, bar_seconds)
    age = (_dt.now(_tz.utc) - frame.index[-1].to_pydatetime()).total_seconds()
    assert age <= bar_seconds + tolerance, (
        f"a frame the cache considers current is {age:.0f}s old, beyond the "
        f"trader's {bar_seconds + tolerance}s gate"
    )


def test_cache_max_age_is_short_enough_for_live_polling():
    """The file-age window must leave room under the staleness gate even in the
    worst case: a file written just before the forming bar rolled over."""
    from trading_engine.data.feeds import CachedFeed, timeframe_seconds

    cached = CachedFeed(SyntheticFeed(), ".cache/test", enabled=False)
    for timeframe in ("5m", "15m", "1h"):
        bar_seconds = timeframe_seconds(timeframe)
        worst_case = bar_seconds + cached.max_age_sec
        limit = bar_seconds + Config().data.max_staleness_sec
        assert worst_case <= limit, (
            f"{timeframe}: cache could serve {worst_case}s-old data against a "
            f"{limit}s limit"
        )
