"""Configuration for the whole engine.

Plain dataclasses with a YAML/dict loader — no pydantic dependency so the engine
installs anywhere. Every default here is deliberately conservative; the risk
numbers in particular are the ones that keep an account alive.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict, fields
from typing import Any

try:  # optional
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


@dataclass
class RiskConfig:
    """Risk limits. These are the guardrails, not suggestions."""

    # Per-trade
    risk_per_trade: float = 0.005        # 0.5% of equity at risk per position
    max_risk_per_trade: float = 0.02     # hard ceiling; above this a losing run is fatal

    # Portfolio
    max_portfolio_heat: float = 0.06     # total open risk across all positions
    max_positions: int = 5
    max_positions_per_symbol: int = 1
    max_correlated_positions: int = 2    # correlated positions are one position
    correlation_threshold: float = 0.7

    # Exposure
    max_leverage: float = 3.0
    max_notional_pct: float = 1.0        # notional cap as a multiple of equity

    # Circuit breakers
    daily_loss_limit: float = 0.03       # stop trading for the day at -3%
    weekly_loss_limit: float = 0.07
    max_drawdown_stop: float = 0.20      # stop the system entirely
    drawdown_throttle_start: float = 0.10  # start halving size here
    consecutive_loss_limit: int = 5      # cool off after this many losses

    # Reward:risk enforcement — the core of the 2-3R mandate.
    #
    # Two distinct thresholds, because "2-3R" means two different things
    # depending on which number you mean:
    #   min_reward_risk  — the FURTHEST target must sit at least this many R
    #                      away. This is the "can this trade pay 2-3x the risk?"
    #                      test, and it is what rejects setups with no room.
    #   min_expected_r   — the SIZE-WEIGHTED average across the whole ladder,
    #                      i.e. what the trade actually returns if every rung
    #                      fills. Scaling out necessarily makes this lower than
    #                      the furthest target: the default (1,2,3)R ladder at
    #                      (40,35,25)% weights averages 1.85R.
    # Gating only on the weighted number would reject every laddered exit;
    # gating only on the furthest would let a trade that banks 90% at 0.5R
    # advertise itself as 3R. Both are enforced.
    min_reward_risk: float = 2.0         # furthest target, in R
    min_expected_r: float = 1.5          # size-weighted ladder average, in R
    target_reward_risk: float = 3.0      # what the furthest target aims for
    min_stop_distance_atr: float = 0.5   # stops closer than this are noise-stops
    max_stop_distance_atr: float = 3.0   # wider than this and size becomes tiny

    # Sizing model: fixed_fractional | atr_normalised | vol_target | kelly
    sizing_model: str = "fixed_fractional"
    kelly_fraction: float = 0.25         # never full Kelly
    target_volatility: float = 0.15      # annualised, for vol_target sizing

    def validate(self) -> list[str]:
        errs: list[str] = []
        if not 0 < self.risk_per_trade <= self.max_risk_per_trade:
            errs.append(
                f"risk_per_trade {self.risk_per_trade} must be in "
                f"(0, {self.max_risk_per_trade}]"
            )
        if self.max_risk_per_trade > 0.05:
            errs.append("max_risk_per_trade above 5% is not survivable")
        if self.max_portfolio_heat < self.risk_per_trade:
            errs.append("max_portfolio_heat is below a single trade's risk")
        if self.min_reward_risk < 1.0:
            errs.append("min_reward_risk below 1.0 defeats the purpose of the system")
        if self.min_expected_r > self.min_reward_risk:
            errs.append(
                "min_expected_r cannot exceed min_reward_risk — the weighted "
                "average of a ladder is always below its furthest target"
            )
        if self.max_drawdown_stop <= self.drawdown_throttle_start:
            errs.append("max_drawdown_stop must exceed drawdown_throttle_start")
        if self.max_leverage < 1.0:
            errs.append("max_leverage must be >= 1")
        return errs


@dataclass
class ExecutionConfig:
    """Cost model. An untaxed backtest is fiction."""

    maker_fee: float = 0.0002            # 2 bps
    taker_fee: float = 0.0005            # 5 bps
    slippage_bps: float = 2.0            # base slippage
    slippage_atr_factor: float = 0.05    # extra slippage scaled by volatility
    spread_bps: float = 1.0
    use_limit_entries: bool = False
    fill_on_next_open: bool = True       # decide on close of t, fill at open of t+1
    stop_first_on_ambiguous_bar: bool = True  # pessimistic: stop wins ties

    # Live
    max_slippage_bps: float = 25.0       # abort a fill worse than this
    order_timeout_sec: int = 30
    max_retries: int = 3


@dataclass
class StrategyConfig:
    """Strategy behaviour and the confluence thresholds."""

    name: str = "liquidity_sweep"
    timeframe: str = "15m"
    htf_timeframe: str = "4h"            # higher timeframe for bias
    lookback_bars: int = 500

    # Structure detection
    swing_lookback: int = 5              # fractal width
    liquidity_tolerance_pct: float = 0.001   # how close counts as "equal" highs/lows
    min_sweep_penetration_pct: float = 0.0005
    fvg_min_size_atr: float = 0.15
    order_block_lookback: int = 30
    displacement_atr: float = 1.2        # what counts as an impulsive move

    # Confluence scoring
    min_confidence: float = 55.0         # reject setups below this
    require_htf_alignment: bool = True
    require_liquidity_sweep: bool = True

    # Component weights (normalised at use)
    weight_liquidity: float = 0.40
    weight_technical: float = 0.35
    weight_fundamental: float = 0.25

    # Exits
    tp_ladder: tuple[float, ...] = (1.0, 2.0, 3.0)      # R multiples
    tp_sizes: tuple[float, ...] = (0.40, 0.35, 0.25)    # fraction closed at each
    move_to_breakeven_after_tp: int = 1                 # after TP1 fills
    trail_after_r: float = 2.0                          # start trailing at +2R
    trail_atr_mult: float = 1.5
    time_stop_bars: int = 96                            # bail if it goes nowhere
    atr_period: int = 14
    atr_stop_mult: float = 1.5

    def __post_init__(self) -> None:
        # YAML gives lists where the defaults are tuples. Coerce so a config
        # loaded from a file is byte-identical to one built in code.
        self.tp_ladder = tuple(float(r) for r in self.tp_ladder)
        self.tp_sizes = tuple(float(s) for s in self.tp_sizes)

    def validate(self) -> list[str]:
        errs: list[str] = []
        if len(self.tp_ladder) != len(self.tp_sizes):
            errs.append("tp_ladder and tp_sizes must be the same length")
        total = sum(self.tp_sizes)
        if abs(total - 1.0) > 1e-6:
            errs.append(f"tp_sizes must sum to 1.0, got {total}")
        if any(r <= 0 for r in self.tp_ladder):
            errs.append("tp_ladder entries must be positive R multiples")
        if list(self.tp_ladder) != sorted(self.tp_ladder):
            errs.append("tp_ladder must be ascending")
        if not 0 <= self.min_confidence <= 100:
            errs.append("min_confidence must be within 0-100")
        return errs


@dataclass
class DataConfig:
    provider: str = "auto"               # auto | binance | yfinance | csv | synthetic
    # A few liquid defaults so the dashboard's symbol picker is useful out of
    # the box. Override in config, or with a comma-separated --symbol.
    symbols: list[str] = field(
        default_factory=lambda: ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    )
    cache_dir: str = ".cache/market_data"
    cache_enabled: bool = True
    max_staleness_sec: int = 120         # beyond this the feed is stale -> fail closed
    orderbook_depth: int = 20
    request_timeout_sec: int = 15


@dataclass
class FundamentalsConfig:
    enabled: bool = True
    event_blackout_minutes_before: int = 60   # no new risk into a high-impact print
    event_blackout_minutes_after: int = 30
    flatten_before_event: bool = False
    high_impact_only: bool = True
    # Crypto
    funding_extreme_bps: float = 5.0     # 8h funding beyond this = crowded
    oi_surge_pct: float = 10.0
    # Equity
    earnings_blackout_days: int = 2


@dataclass
class LiveConfig:
    mode: str = "paper"                  # paper | live — paper shares the live code path
    broker: str = "paper"                # paper | binance | alpaca
    poll_interval_sec: int = 15
    starting_equity: float = 10_000.0
    state_file: str = ".state/engine_state.json"
    heartbeat_sec: int = 60
    reconcile_on_start: bool = True
    kill_switch_file: str = ".state/KILL"  # touch this file to flatten and halt

    def __post_init__(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got {self.mode!r}")


@dataclass
class BacktestConfig:
    starting_equity: float = 10_000.0
    warmup_bars: int = 200               # bars consumed before signals are allowed
    walk_forward_splits: int = 4
    in_sample_ratio: float = 0.7
    annualisation_periods: int = 365 * 24 * 4   # 15m bars in a 24/7 year


@dataclass
class Config:
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    data: DataConfig = field(default_factory=DataConfig)
    fundamentals: FundamentalsConfig = field(default_factory=FundamentalsConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)

    # -- construction ------------------------------------------------------ #

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        sections = {f.name: f.type for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        section_types = {
            "risk": RiskConfig, "execution": ExecutionConfig,
            "strategy": StrategyConfig, "data": DataConfig,
            "fundamentals": FundamentalsConfig, "live": LiveConfig,
            "backtest": BacktestConfig,
        }
        for name, typ in section_types.items():
            if name not in sections:
                continue
            raw = data.get(name) or {}
            valid = {f.name for f in fields(typ)}
            unknown = set(raw) - valid
            if unknown:
                raise ValueError(f"unknown keys in [{name}]: {sorted(unknown)}")
            kwargs[name] = typ(**raw)
        return cls(**kwargs)

    @classmethod
    def from_yaml(cls, path: str) -> "Config":
        if yaml is None:
            raise RuntimeError("PyYAML is not installed; use Config.from_dict()")
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh) or {})

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        """Load from an explicit path, then $TRADING_ENGINE_CONFIG, else defaults."""
        path = path or os.environ.get("TRADING_ENGINE_CONFIG")
        if path and os.path.exists(path):
            cfg = cls.from_yaml(path)
        else:
            cfg = cls()
        cfg.raise_on_invalid()
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # -- validation -------------------------------------------------------- #

    def validate(self) -> list[str]:
        errs = self.risk.validate() + self.strategy.validate()

        # Catch a ladder that can never satisfy the risk thresholds at load
        # time, rather than silently producing zero trades in a backtest.
        if self.strategy.tp_ladder and self.strategy.tp_sizes:
            weighted = sum(
                r * s for r, s in zip(self.strategy.tp_ladder, self.strategy.tp_sizes)
            )
            furthest = max(self.strategy.tp_ladder)
            if weighted < 1.0:
                errs.append(
                    f"the TP ladder averages {weighted:.2f}R, below 1R — the "
                    "system cannot be profitable at any realistic hit rate"
                )
            if weighted < self.risk.min_expected_r:
                errs.append(
                    f"the TP ladder averages {weighted:.2f}R but min_expected_r "
                    f"is {self.risk.min_expected_r:.2f} — every signal would be "
                    f"rejected. Lower min_expected_r or weight the ladder toward "
                    f"the further targets"
                )
            if furthest < self.risk.min_reward_risk:
                errs.append(
                    f"the furthest target is {furthest:.2f}R but min_reward_risk "
                    f"is {self.risk.min_reward_risk:.2f} — every signal would be "
                    f"rejected"
                )
        if self.live.mode == "live" and self.live.broker == "paper":
            errs.append("live mode with the paper broker is a misconfiguration")
        return errs

    def raise_on_invalid(self) -> None:
        errs = self.validate()
        if errs:
            raise ValueError("invalid configuration:\n  - " + "\n  - ".join(errs))


DEFAULT_CONFIG = Config()
