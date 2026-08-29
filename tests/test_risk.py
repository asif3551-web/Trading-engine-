"""Risk management tests.

These cover the rules that keep an account alive: size derived from the stop,
the reward:risk floor, and every circuit breaker. A regression here is more
expensive than a regression anywhere else in the codebase, because it is only
discovered with real money.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_engine.config import Config, RiskConfig, StrategyConfig
from trading_engine.core.types import Position, Side, Signal, TakeProfit, Trade
from trading_engine.risk.manager import (
    PositionSizer, RiskManager, RiskState, breakeven_win_rate, expectancy_r,
    kelly_fraction, risk_of_ruin,
)

NOW = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)


def make_signal(
    entry: float = 100.0, stop: float = 98.0, side: Side = Side.LONG,
    ladder=((1.0, 0.4), (2.0, 0.35), (3.0, 0.25)), atr: float = 2.0,
) -> Signal:
    risk = abs(entry - stop)
    tps = [
        TakeProfit(entry + side.sign * r * risk, r, size, f"TP{i}")
        for i, (r, size) in enumerate(ladder, 1)
    ]
    return Signal(
        timestamp=NOW, symbol="BTC/USDT", timeframe="15m", side=side,
        entry=entry, stop_loss=stop, take_profits=tps, confidence=70.0, atr=atr,
    )


# --------------------------------------------------------------------------- #
# Signal geometry
# --------------------------------------------------------------------------- #

def test_signal_rejects_stop_on_the_wrong_side():
    with pytest.raises(ValueError, match="must be below entry"):
        Signal(NOW, "X", "15m", Side.LONG, entry=100.0, stop_loss=102.0)
    with pytest.raises(ValueError, match="must be above entry"):
        Signal(NOW, "X", "15m", Side.SHORT, entry=100.0, stop_loss=98.0)


def test_signal_rejects_target_on_the_wrong_side():
    with pytest.raises(ValueError, match="must be above entry"):
        Signal(NOW, "X", "15m", Side.LONG, 100.0, 98.0,
               [TakeProfit(95.0, 1.0, 1.0)])


def test_r_multiples_and_weighted_reward_risk():
    sig = make_signal()
    assert sig.risk_per_unit == pytest.approx(2.0)
    assert sig.r_multiple_at(104.0) == pytest.approx(2.0)
    assert sig.max_r == pytest.approx(3.0)
    # 0.4*1 + 0.35*2 + 0.25*3 = 1.85
    assert sig.reward_risk == pytest.approx(1.85)


def test_short_r_multiples_mirror_longs():
    sig = make_signal(entry=100.0, stop=102.0, side=Side.SHORT)
    assert sig.risk_per_unit == pytest.approx(2.0)
    assert sig.r_multiple_at(96.0) == pytest.approx(2.0)
    assert sig.max_r == pytest.approx(3.0)


def test_breakeven_win_rate_matches_the_formula():
    assert breakeven_win_rate(1.0) == pytest.approx(0.50)
    assert breakeven_win_rate(2.0) == pytest.approx(1 / 3)
    assert breakeven_win_rate(3.0) == pytest.approx(0.25)


def test_expectancy_maths():
    # The core argument for 2-3R: profitable while losing most of the time.
    assert expectancy_r(0.35, 2.5) == pytest.approx(0.225)
    assert expectancy_r(0.25, 3.0) == pytest.approx(0.0)


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #

def test_size_is_derived_from_the_stop_distance():
    sizer = PositionSizer(RiskConfig(risk_per_trade=0.01))
    tight = sizer.size_for(make_signal(100.0, 99.0), equity=10_000)
    wide = sizer.size_for(make_signal(100.0, 95.0), equity=10_000)

    assert tight.risk_amount == pytest.approx(100.0)
    assert wide.risk_amount == pytest.approx(100.0)
    # Same dollar risk, so a 5x wider stop must give a 5x smaller position.
    assert tight.size == pytest.approx(wide.size * 5, rel=1e-6)


def test_risk_amount_tracks_the_configured_fraction():
    sizer = PositionSizer(RiskConfig(risk_per_trade=0.005))
    result = sizer.size_for(make_signal(), equity=20_000)
    assert result.risk_amount == pytest.approx(100.0)


def test_size_never_exceeds_the_per_trade_ceiling():
    cfg = RiskConfig(risk_per_trade=0.10, max_risk_per_trade=0.02)
    result = PositionSizer(cfg).size_for(make_signal(), equity=10_000)
    assert result.risk_amount <= 10_000 * 0.02 + 1e-6
    assert result.capped_by in ("max_risk_per_trade", "max_notional")


def test_liquidity_cap_limits_size():
    cfg = RiskConfig(risk_per_trade=0.02, max_leverage=10)
    result = PositionSizer(cfg).size_for(
        make_signal(), equity=1_000_000, avg_volume=1000.0
    )
    assert result.size <= 10.0            # 1% of average volume
    assert result.capped_by == "liquidity"


def test_zero_stop_distance_is_not_tradeable():
    sizer = PositionSizer(RiskConfig())
    sig = make_signal()
    sig.stop_loss = sig.entry             # bypass validation deliberately
    assert not sizer.size_for(sig, 10_000).is_tradeable


def test_kelly_is_fractional_and_never_negative():
    assert kelly_fraction(0.5, 2.0) == pytest.approx(0.25)
    assert kelly_fraction(0.2, 1.0) == 0.0     # negative edge floors at zero

    cfg = RiskConfig(sizing_model="kelly", kelly_fraction=0.25)
    result = PositionSizer(cfg).size_for(make_signal(), 10_000, win_rate=0.5)
    assert result.risk_amount <= 10_000 * cfg.max_risk_per_trade + 1e-6


def test_risk_of_ruin_grows_with_position_size():
    small = risk_of_ruin(0.4, 2.5, 0.01)
    large = risk_of_ruin(0.4, 2.5, 0.10)
    assert large > small
    assert risk_of_ruin(0.2, 1.0, 0.02) == pytest.approx(1.0)   # no edge -> ruin


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def manager(**overrides) -> RiskManager:
    cfg = RiskConfig(**overrides)
    return RiskManager(cfg, RiskState(equity=10_000.0))


def test_approves_a_sound_signal():
    decision = manager().evaluate(make_signal(), [], NOW)
    assert decision.approved, decision.reason
    assert decision.sizing is not None and decision.sizing.size > 0


def test_rejects_when_the_furthest_target_is_too_close():
    sig = make_signal(ladder=((0.5, 0.5), (1.0, 0.5)))
    decision = manager().evaluate(sig, [], NOW)
    assert not decision.approved
    assert "furthest target" in decision.reason


def test_rejects_a_ladder_that_banks_too_early():
    """Reaches 3R but exits 90% at 0.5R — not a 3R trade in any real sense."""
    sig = make_signal(ladder=((0.5, 0.9), (3.0, 0.1)))
    decision = manager().evaluate(sig, [], NOW)
    assert not decision.approved
    assert "weighted" in decision.reason


def test_rejects_a_stop_inside_the_noise_band():
    sig = make_signal(entry=100.0, stop=99.9, atr=5.0)
    decision = manager().evaluate(sig, [], NOW)
    assert not decision.approved
    assert "noise" in decision.reason


def test_rejects_an_excessively_wide_stop():
    sig = make_signal(entry=100.0, stop=80.0, atr=1.0)
    decision = manager().evaluate(sig, [], NOW)
    assert not decision.approved
    assert "too wide" in decision.reason


def test_enforces_the_position_count_limit():
    mgr = manager(max_positions=2)
    positions = [
        Position("ETH/USDT", Side.LONG, 1.0, 100.0, NOW, stop_loss=99.0),
        Position("SOL/USDT", Side.LONG, 1.0, 100.0, NOW, stop_loss=99.0),
    ]
    decision = mgr.evaluate(make_signal(), positions, NOW)
    assert not decision.approved
    assert "position limit" in decision.reason


def test_blocks_a_second_position_in_the_same_symbol():
    mgr = manager()
    held = [Position("BTC/USDT", Side.LONG, 1.0, 100.0, NOW, stop_loss=99.0)]
    decision = mgr.evaluate(make_signal(), held, NOW)
    assert not decision.approved
    assert "already holding" in decision.reason


def test_blocks_an_opposing_position_in_the_same_symbol():
    mgr = manager(max_positions_per_symbol=2)
    held = [Position("BTC/USDT", Side.SHORT, 1.0, 100.0, NOW, stop_loss=101.0)]
    decision = mgr.evaluate(make_signal(side=Side.LONG), held, NOW)
    assert not decision.approved
    assert "opposing" in decision.reason


def test_correlated_positions_count_as_one():
    mgr = manager(max_correlated_positions=1, correlation_threshold=0.7)
    held = [Position("ETH/USDT", Side.LONG, 1.0, 100.0, NOW, stop_loss=99.0)]
    decision = mgr.evaluate(
        make_signal(), held, NOW, correlations={"ETH/USDT": 0.95}
    )
    assert not decision.approved
    assert "correlated" in decision.reason


def test_portfolio_heat_caps_total_open_risk():
    mgr = manager(max_portfolio_heat=0.02, risk_per_trade=0.01)
    # An open position already risking 1.9% of a 10k account.
    held = [Position("ETH/USDT", Side.LONG, 190.0, 100.0, NOW, stop_loss=99.0)]
    assert mgr.portfolio_heat(held) == pytest.approx(0.019)
    decision = mgr.evaluate(make_signal(), held, NOW)
    assert decision.approved
    assert decision.risk_fraction < 0.01          # trimmed to fit under the cap
    assert any("heat" in w for w in decision.warnings)


def test_position_without_a_stop_counts_full_notional_as_risk():
    mgr = manager()
    naked = [Position("ETH/USDT", Side.LONG, 10.0, 100.0, NOW, stop_loss=0.0)]
    assert mgr.portfolio_heat(naked) == pytest.approx(0.1)   # 1000/10000


def test_daily_loss_limit_stops_new_risk():
    mgr = manager(daily_loss_limit=0.03)
    mgr.state.roll_periods(NOW)
    mgr.state.update_equity(9_600.0)              # -4% on the day
    decision = mgr.evaluate(make_signal(), [], NOW)
    assert not decision.approved
    assert "daily loss limit" in decision.reason


def test_consecutive_losses_trigger_a_cooloff():
    mgr = manager(consecutive_loss_limit=3)
    mgr.state.consecutive_losses = 3
    decision = mgr.evaluate(make_signal(), [], NOW)
    assert not decision.approved
    assert "consecutive losses" in decision.reason


def test_drawdown_throttle_scales_size_down():
    mgr = manager(drawdown_throttle_start=0.10, max_drawdown_stop=0.20)
    mgr.state.peak_equity = 10_000.0
    mgr.state.update_equity(8_500.0)              # 15% drawdown, mid-throttle
    multiplier = mgr.drawdown_risk_multiplier()
    assert 0.25 <= multiplier < 1.0

    decision = mgr.evaluate(make_signal(), [], NOW)
    assert decision.approved
    assert decision.risk_fraction < mgr.config.risk_per_trade
    assert any("drawdown" in w for w in decision.warnings)


def test_max_drawdown_halts_the_system():
    mgr = manager(max_drawdown_stop=0.20)
    mgr.state.peak_equity = 10_000.0
    mgr.state.update_equity(7_000.0)              # 30% drawdown
    decision = mgr.evaluate(make_signal(), [], NOW)
    assert not decision.approved
    assert mgr.state.halted

    # Stays halted until explicitly resumed.
    mgr.state.update_equity(10_000.0)
    assert not mgr.evaluate(make_signal(), [], NOW).approved
    mgr.resume()
    assert mgr.evaluate(make_signal(), [], NOW).approved


def test_daily_baseline_rolls_over_at_midnight():
    mgr = manager()
    mgr.state.roll_periods(NOW)
    mgr.state.update_equity(9_000.0)
    assert mgr.state.daily_pnl_pct < 0

    mgr.state.roll_periods(NOW + timedelta(days=1))
    assert mgr.state.daily_pnl_pct == pytest.approx(0.0)


def test_trade_outcomes_update_the_loss_streak():
    mgr = manager()
    loser = Trade("BTC/USDT", Side.LONG, 100, 98, 1, NOW, NOW, -100.0, -1.0)
    winner = Trade("BTC/USDT", Side.LONG, 100, 106, 1, NOW, NOW, 300.0, 3.0)

    mgr.on_trade_closed(loser, 9_900.0)
    mgr.on_trade_closed(loser, 9_800.0)
    assert mgr.state.consecutive_losses == 2
    mgr.on_trade_closed(winner, 10_100.0)
    assert mgr.state.consecutive_losses == 0


# --------------------------------------------------------------------------- #
# Config validation
# --------------------------------------------------------------------------- #

def test_default_config_is_valid():
    Config().raise_on_invalid()


def test_rejects_unsurvivable_per_trade_risk():
    errors = RiskConfig(risk_per_trade=0.10, max_risk_per_trade=0.10).validate()
    assert any("not survivable" in e for e in errors)


def test_rejects_a_ladder_that_can_never_pass():
    """The exact bug this guard exists for: a (1,2,3)R ladder weighted
    (40,35,25)% averages 1.85R, so a 2.0 weighted floor rejects everything."""
    config = Config()
    config.risk.min_expected_r = 2.5
    errors = config.validate()
    assert any("every signal would be rejected" in e for e in errors)


def test_rejects_tp_sizes_that_do_not_sum_to_one():
    errors = StrategyConfig(tp_ladder=(1.0, 2.0), tp_sizes=(0.5, 0.9)).validate()
    assert any("sum to 1.0" in e for e in errors)


def test_rejects_a_descending_ladder():
    errors = StrategyConfig(tp_ladder=(3.0, 1.0), tp_sizes=(0.5, 0.5)).validate()
    assert any("ascending" in e for e in errors)


def test_rejects_live_mode_with_the_paper_broker():
    config = Config()
    config.live.mode = "live"
    config.live.broker = "paper"
    assert any("misconfiguration" in e for e in config.validate())


def test_config_rejects_unknown_keys():
    with pytest.raises(ValueError, match="unknown keys"):
        Config.from_dict({"risk": {"not_a_real_setting": 1}})
