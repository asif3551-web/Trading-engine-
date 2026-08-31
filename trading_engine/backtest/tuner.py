"""Parameter tuning with walk-forward validation.

This exists because of a specific, honest limitation: nobody — not the author of
a strategy, and not a model writing one — can tell from first principles which
exit-management or confluence settings suit a given market. It has to be
measured, on that market's real data, and validated out-of-sample.

The tuner is deliberately hostile to overfitting:

  * Every candidate is scored on **out-of-sample** expectancy only. In-sample
    numbers are reported for comparison but never used to rank.
  * Candidates with too few out-of-sample trades are disqualified rather than
    ranked, because a great number on 6 trades is noise.
  * Consistency across windows is reported alongside the mean, since a setting
    that is superb in one window and terrible in three is worse than a mediocre
    one that holds up everywhere.
  * The winning setting is reported with its degradation, so you can see how
    much of the in-sample result survived contact with unseen data.

A sweep that produces no positive-expectancy candidate is a real and useful
result: it says this entry has no edge on this market at these settings, and
the answer is to change the entry or the market, not to keep searching exits.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd

from ..config import Config
from .engine import Backtester
from .metrics import compute_metrics, split_walk_forward


# The knobs that actually change behaviour, with sensible search ranges. Keys
# are "section.field" paths into Config.
DEFAULT_GRID: dict[str, list[Any]] = {
    "strategy.move_to_breakeven_after_tp": [1, 2, 9],   # 9 = effectively never
    "strategy.min_confidence": [50.0, 60.0, 70.0],
    "strategy.trail_after_r": [1.5, 2.0, 3.0],
}

# Ladders are swept as a unit: sizes and R multiples must stay consistent.
LADDER_CHOICES: list[tuple[tuple[float, ...], tuple[float, ...]]] = [
    ((1.0, 2.0, 3.0), (0.40, 0.35, 0.25)),
    ((1.0, 2.0, 3.0), (0.30, 0.30, 0.40)),
    ((1.0, 2.0, 4.0), (0.25, 0.25, 0.50)),
    ((1.5, 3.0), (0.50, 0.50)),
]


@dataclass
class Candidate:
    params: dict[str, Any]
    in_sample_expectancy: float = 0.0
    out_sample_expectancy: float = 0.0
    out_sample_trades: int = 0
    in_sample_trades: int = 0
    window_expectancies: list[float] = field(default_factory=list)
    out_sample_win_rate: float = 0.0
    out_sample_avg_win_r: float = 0.0
    out_sample_avg_loss_r: float = 0.0
    disqualified: str = ""

    @property
    def consistency(self) -> float:
        """Fraction of out-of-sample windows with positive expectancy.

        A mean can be carried by one lucky window; this cannot.
        """
        if not self.window_expectancies:
            return 0.0
        positive = sum(1 for e in self.window_expectancies if e > 0)
        return positive / len(self.window_expectancies)

    @property
    def degradation(self) -> float:
        if self.in_sample_expectancy == 0:
            return 0.0
        return 1.0 - (self.out_sample_expectancy / self.in_sample_expectancy)

    @property
    def score(self) -> float:
        """Ranking score: out-of-sample expectancy weighted by consistency.

        Deliberately not in-sample anything. A candidate that is positive in
        every window beats one with a higher mean carried by a single window.
        """
        if self.disqualified:
            return float("-inf")
        return self.out_sample_expectancy * (0.5 + 0.5 * self.consistency)

    def label(self) -> str:
        bits = []
        for key, value in self.params.items():
            short = key.split(".")[-1]
            if isinstance(value, tuple):
                value = "/".join(f"{v:g}" for v in value)
            bits.append(f"{short}={value}")
        return " ".join(bits)

    def to_dict(self) -> dict:
        return {
            "params": {k: list(v) if isinstance(v, tuple) else v
                       for k, v in self.params.items()},
            "out_sample_expectancy": round(self.out_sample_expectancy, 4),
            "in_sample_expectancy": round(self.in_sample_expectancy, 4),
            "out_sample_trades": self.out_sample_trades,
            "in_sample_trades": self.in_sample_trades,
            "out_sample_win_rate": round(self.out_sample_win_rate, 4),
            "out_sample_avg_win_r": round(self.out_sample_avg_win_r, 3),
            "out_sample_avg_loss_r": round(self.out_sample_avg_loss_r, 3),
            "consistency": round(self.consistency, 3),
            "degradation": round(self.degradation, 4),
            "score": round(self.score, 4) if self.disqualified == "" else None,
            "disqualified": self.disqualified,
        }


def _apply(config: Config, params: dict[str, Any]) -> None:
    for path, value in params.items():
        section, _, field_name = path.partition(".")
        target = getattr(config, section)
        setattr(target, field_name, value)


def build_candidates(
    grid: dict[str, list[Any]] | None = None,
    ladders: list[tuple[tuple[float, ...], tuple[float, ...]]] | None = None,
    max_candidates: int = 60,
) -> list[dict[str, Any]]:
    """Cartesian product of the grid, with ladders swept as coupled pairs."""
    grid = DEFAULT_GRID if grid is None else grid
    ladders = LADDER_CHOICES if ladders is None else ladders

    keys = sorted(grid)
    combos = []
    for values in itertools.product(*(grid[k] for k in keys)):
        base = dict(zip(keys, values))
        for ladder, sizes in ladders:
            combo = dict(base)
            combo["strategy.tp_ladder"] = ladder
            combo["strategy.tp_sizes"] = sizes
            combos.append(combo)

    if len(combos) > max_candidates:
        # Even stride, so the sample spans the grid rather than its first corner.
        stride = len(combos) / max_candidates
        combos = [combos[int(i * stride)] for i in range(max_candidates)]
    return combos


def tune(
    df: pd.DataFrame,
    base_config: Config | None = None,
    symbol: str = "",
    grid: dict[str, list[Any]] | None = None,
    n_splits: int = 4,
    in_sample_ratio: float = 0.7,
    min_out_sample_trades: int = 20,
    max_candidates: int = 60,
    progress: Callable[[int, int, str], None] | None = None,
) -> list[Candidate]:
    """Walk-forward sweep. Returns candidates ranked best-first by OOS score."""
    base_config = base_config or Config()
    combos = build_candidates(grid, max_candidates=max_candidates)

    try:
        splits = split_walk_forward(df, n_splits, in_sample_ratio)
    except ValueError as exc:
        raise ValueError(f"not enough data to tune: {exc}") from exc

    results: list[Candidate] = []
    for n, params in enumerate(combos, 1):
        if progress is not None:
            progress(n, len(combos), _short(params))

        config = Config.from_dict(base_config.to_dict())
        _apply(config, params)
        # A ladder change can trip the reward:risk floors; relax the weighted
        # floor so the sweep compares exits rather than silently taking no
        # trades. The furthest-target floor still applies.
        config.risk.min_expected_r = min(
            config.risk.min_expected_r,
            sum(r * s for r, s in zip(config.strategy.tp_ladder,
                                      config.strategy.tp_sizes)),
        )
        errors = config.validate()
        candidate = Candidate(params=params)
        if errors:
            candidate.disqualified = f"invalid config: {errors[0]}"
            results.append(candidate)
            continue

        backtester = Backtester(config)
        is_exp, oos_exp, oos_trades, is_trades = [], [], 0, 0
        oos_wins: list[float] = []
        oos_losses: list[float] = []

        for is_df, oos_df in splits:
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
            is_exp.append(is_m.expectancy_r)
            oos_exp.append(oos_m.expectancy_r)
            is_trades += is_m.total_trades
            oos_trades += oos_m.total_trades
            oos_wins += [t.r_multiple for t in oos_res.trades if t.pnl > 0]
            oos_losses += [t.r_multiple for t in oos_res.trades if t.pnl <= 0]

        candidate.in_sample_expectancy = float(np.mean(is_exp)) if is_exp else 0.0
        candidate.out_sample_expectancy = float(np.mean(oos_exp)) if oos_exp else 0.0
        candidate.window_expectancies = oos_exp
        candidate.in_sample_trades = is_trades
        candidate.out_sample_trades = oos_trades
        total = len(oos_wins) + len(oos_losses)
        candidate.out_sample_win_rate = len(oos_wins) / total if total else 0.0
        candidate.out_sample_avg_win_r = float(np.mean(oos_wins)) if oos_wins else 0.0
        candidate.out_sample_avg_loss_r = (
            float(np.mean(oos_losses)) if oos_losses else 0.0
        )

        if oos_trades < min_out_sample_trades:
            candidate.disqualified = (
                f"only {oos_trades} out-of-sample trades "
                f"(need {min_out_sample_trades})"
            )

        results.append(candidate)

    results.sort(key=lambda c: c.score, reverse=True)
    return results


def _short(params: dict[str, Any]) -> str:
    be = params.get("strategy.move_to_breakeven_after_tp")
    conf = params.get("strategy.min_confidence")
    ladder = params.get("strategy.tp_ladder")
    lad = "/".join(f"{v:g}" for v in ladder) if ladder else "?"
    return f"be={be} conf={conf} ladder={lad}"


def format_report(candidates: list[Candidate], top: int = 10) -> str:
    """Human-readable ranking, with the caveats that matter stated inline."""
    ranked = [c for c in candidates if not c.disqualified]
    skipped = [c for c in candidates if c.disqualified]

    lines = [
        "=" * 96,
        "WALK-FORWARD PARAMETER SWEEP",
        "=" * 96,
        "Ranked on OUT-OF-SAMPLE expectancy only, weighted by how many windows",
        "stayed positive. In-sample numbers are shown for comparison, never used",
        "to rank.",
        "",
    ]

    if not ranked:
        lines += [
            f"  No candidate qualified. {len(skipped)} were skipped, most for too",
            "  few out-of-sample trades.",
            "",
            "  Lower --min-trades, widen the date range, or use a lower timeframe",
            "  to get a usable sample.",
            "=" * 96,
        ]
        return "\n".join(lines)

    header = (
        f"{'#':>3} {'setting':<44}{'OOS exp':>9}{'IS exp':>9}"
        f"{'OOS tr':>8}{'win%':>7}{'avgW':>7}{'avgL':>7}{'consist':>9}"
    )
    lines += [header, "-" * len(header)]
    for i, c in enumerate(ranked[:top], 1):
        lines.append(
            f"{i:>3} {c.label():<44}{c.out_sample_expectancy:>9.3f}"
            f"{c.in_sample_expectancy:>9.3f}{c.out_sample_trades:>8}"
            f"{c.out_sample_win_rate * 100:>7.1f}{c.out_sample_avg_win_r:>7.2f}"
            f"{c.out_sample_avg_loss_r:>7.2f}{c.consistency * 100:>8.0f}%"
        )

    best = ranked[0]
    lines += ["", "-" * 96]
    if best.out_sample_expectancy <= 0:
        lines += [
            "  EVERY setting is negative out-of-sample.",
            "",
            "  This is a real result, not a tuning failure: no exit scheme can",
            "  rescue an entry with no edge — changing exits only trades win rate",
            "  against win size, which is visible in the table above.",
            "",
            "  The productive responses are to change the ENTRY (stricter",
            "  confluence, a different trigger, better entry location), try a",
            "  different market or timeframe, or accept that this edge is not",
            "  present here. Continuing to search exits will only find noise.",
        ]
    else:
        lines += [
            f"  Best out-of-sample: {best.out_sample_expectancy:+.3f}R over "
            f"{best.out_sample_trades} trades,",
            f"  positive in {best.consistency:.0%} of windows, "
            f"{best.degradation:.0%} degradation from in-sample.",
            "",
            f"  Apply with:  {_as_yaml_hint(best)}",
        ]
        if best.out_sample_trades < 100:
            lines += [
                "",
                f"  CAUTION: {best.out_sample_trades} out-of-sample trades is a "
                f"small sample. Treat this as",
                "  provisional and re-run on more data before committing money.",
            ]
        if best.consistency < 0.75:
            lines += [
                "",
                f"  CAUTION: positive in only {best.consistency:.0%} of windows — "
                f"the mean is being carried",
                "  by a subset. That is the signature of a fragile setting.",
            ]

    if skipped:
        lines += ["", f"  {len(skipped)} candidates skipped (too few trades or "
                      f"invalid config)."]
    lines.append("=" * 96)
    return "\n".join(lines)


def _as_yaml_hint(candidate: Candidate) -> str:
    parts = []
    for key, value in candidate.params.items():
        field_name = key.split(".")[-1]
        if isinstance(value, tuple):
            value = "[" + ", ".join(f"{v:g}" for v in value) + "]"
        parts.append(f"{field_name}: {value}")
    return "; ".join(parts)
