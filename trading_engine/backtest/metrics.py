"""Performance metrics and walk-forward analysis.

The metrics here are deliberately unflattering. Sharpe on its own hides tail
risk, win rate on its own hides the payoff, and both look wonderful on 12
trades. `PerformanceReport.warnings` therefore carries an explicit list of
reasons not to trust the numbers, and `summary()` prints them alongside the
results rather than in a footnote.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..core.types import ExitReason, Trade, safe_div


@dataclass
class PerformanceReport:
    # Returns
    total_return: float = 0.0
    cagr: float = 0.0
    final_equity: float = 0.0
    starting_equity: float = 0.0

    # Risk-adjusted
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_duration: int = 0
    volatility: float = 0.0

    # Trade statistics
    total_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy_r: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    avg_r: float = 0.0
    best_trade_r: float = 0.0
    worst_trade_r: float = 0.0
    max_consecutive_losses: int = 0
    max_consecutive_wins: int = 0
    avg_bars_held: float = 0.0
    exposure: float = 0.0

    # Excursions
    avg_mae_r: float = 0.0
    avg_mfe_r: float = 0.0

    # Breakdowns
    exit_breakdown: dict[str, int] = field(default_factory=dict)
    total_fees: float = 0.0

    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_return": round(self.total_return, 4),
            "cagr": round(self.cagr, 4),
            "final_equity": round(self.final_equity, 2),
            "starting_equity": round(self.starting_equity, 2),
            "sharpe": round(self.sharpe, 3),
            "sortino": round(self.sortino, 3),
            "calmar": round(self.calmar, 3),
            "max_drawdown": round(self.max_drawdown, 4),
            "max_drawdown_duration": self.max_drawdown_duration,
            "volatility": round(self.volatility, 4),
            "total_trades": self.total_trades,
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 3),
            "expectancy_r": round(self.expectancy_r, 4),
            "avg_win_r": round(self.avg_win_r, 3),
            "avg_loss_r": round(self.avg_loss_r, 3),
            "avg_r": round(self.avg_r, 3),
            "best_trade_r": round(self.best_trade_r, 3),
            "worst_trade_r": round(self.worst_trade_r, 3),
            "max_consecutive_losses": self.max_consecutive_losses,
            "max_consecutive_wins": self.max_consecutive_wins,
            "avg_bars_held": round(self.avg_bars_held, 1),
            "exposure": round(self.exposure, 4),
            "avg_mae_r": round(self.avg_mae_r, 3),
            "avg_mfe_r": round(self.avg_mfe_r, 3),
            "exit_breakdown": dict(self.exit_breakdown),
            "total_fees": round(self.total_fees, 2),
            "warnings": list(self.warnings),
        }

    def summary(self) -> str:
        lines = [
            "=" * 62,
            "BACKTEST PERFORMANCE",
            "=" * 62,
            f"  Starting equity     {self.starting_equity:>14,.2f}",
            f"  Final equity        {self.final_equity:>14,.2f}",
            f"  Total return        {self.total_return:>14.2%}",
            f"  CAGR                {self.cagr:>14.2%}",
            "",
            f"  Sharpe              {self.sharpe:>14.2f}",
            f"  Sortino             {self.sortino:>14.2f}",
            f"  Calmar              {self.calmar:>14.2f}",
            f"  Max drawdown        {self.max_drawdown:>14.2%}",
            f"  Volatility (ann.)   {self.volatility:>14.2%}",
            "",
            f"  Trades              {self.total_trades:>14d}",
            f"  Win rate            {self.win_rate:>14.2%}",
            f"  Profit factor       {self.profit_factor:>14.2f}",
            f"  Expectancy          {self.expectancy_r:>13.3f}R",
            f"  Avg win             {self.avg_win_r:>13.2f}R",
            f"  Avg loss            {self.avg_loss_r:>13.2f}R",
            f"  Best / worst        {self.best_trade_r:>7.2f}R /{self.worst_trade_r:>6.2f}R",
            f"  Max losing streak   {self.max_consecutive_losses:>14d}",
            f"  Avg MAE / MFE       {self.avg_mae_r:>7.2f}R /{self.avg_mfe_r:>6.2f}R",
            f"  Exposure            {self.exposure:>14.2%}",
            f"  Fees paid           {self.total_fees:>14,.2f}",
        ]
        if self.exit_breakdown:
            lines.append("")
            lines.append("  Exits:")
            for reason, count in sorted(
                self.exit_breakdown.items(), key=lambda kv: -kv[1]
            ):
                pct = safe_div(count, self.total_trades)
                lines.append(f"    {reason:<22} {count:>5d}  ({pct:.1%})")
        if self.warnings:
            lines.append("")
            lines.append("  READ BEFORE TRUSTING THESE NUMBERS:")
            for w in self.warnings:
                lines.append(f"    ! {w}")
        lines.append("=" * 62)
        return "\n".join(lines)


def max_drawdown(equity: pd.Series) -> tuple[float, int]:
    """Peak-to-trough drawdown and its longest duration in bars."""
    if equity.empty:
        return 0.0, 0
    running_max = equity.cummax()
    dd = equity / running_max - 1.0
    max_dd = float(abs(dd.min())) if len(dd) else 0.0

    duration = longest = 0
    for value in dd.to_numpy():
        if value < -1e-12:
            duration += 1
            longest = max(longest, duration)
        else:
            duration = 0
    return max_dd, longest


def _streaks(trades: list[Trade]) -> tuple[int, int]:
    max_loss = max_win = loss = win = 0
    for t in trades:
        if t.pnl > 0:
            win += 1
            loss = 0
        else:
            loss += 1
            win = 0
        max_win = max(max_win, win)
        max_loss = max(max_loss, loss)
    return max_loss, max_win


def compute_metrics(
    trades: list[Trade],
    equity_curve: pd.Series | None,
    starting_equity: float,
    periods_per_year: int = 365 * 24 * 4,
    exposure: float = 0.0,
    risk_free_rate: float = 0.0,
) -> PerformanceReport:
    report = PerformanceReport(
        starting_equity=starting_equity, exposure=exposure
    )

    if equity_curve is not None and len(equity_curve) > 1:
        report.final_equity = float(equity_curve.iloc[-1])
        report.total_return = safe_div(
            report.final_equity - starting_equity, starting_equity
        )

        returns = equity_curve.pct_change().dropna()
        returns = returns.replace([np.inf, -np.inf], np.nan).dropna()

        if len(returns) > 1:
            mean_r = float(returns.mean())
            std_r = float(returns.std())
            rf_per_period = risk_free_rate / periods_per_year
            ann = math.sqrt(periods_per_year)

            report.volatility = std_r * ann
            if std_r > 0:
                report.sharpe = (mean_r - rf_per_period) / std_r * ann

            downside = returns[returns < 0]
            if len(downside) > 1:
                dstd = float(downside.std())
                if dstd > 0:
                    report.sortino = (mean_r - rf_per_period) / dstd * ann

            n_periods = len(equity_curve)
            if n_periods > 0 and starting_equity > 0 and report.final_equity > 0:
                years = n_periods / periods_per_year
                if years > 0:
                    report.cagr = (
                        (report.final_equity / starting_equity) ** (1.0 / years) - 1.0
                    )

        report.max_drawdown, report.max_drawdown_duration = max_drawdown(equity_curve)
        if report.max_drawdown > 0:
            report.calmar = report.cagr / report.max_drawdown
    else:
        report.final_equity = starting_equity

    if trades:
        report.total_trades = len(trades)
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]

        report.win_rate = len(wins) / len(trades)
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        report.profit_factor = (
            safe_div(gross_win, gross_loss, float("inf") if gross_win > 0 else 0.0)
        )

        r_values = [t.r_multiple for t in trades]
        report.avg_r = float(np.mean(r_values))
        report.expectancy_r = report.avg_r     # the realised per-trade expectancy
        report.avg_win_r = float(np.mean([t.r_multiple for t in wins])) if wins else 0.0
        report.avg_loss_r = (
            float(np.mean([t.r_multiple for t in losses])) if losses else 0.0
        )
        report.best_trade_r = float(max(r_values))
        report.worst_trade_r = float(min(r_values))
        report.max_consecutive_losses, report.max_consecutive_wins = _streaks(trades)
        report.avg_bars_held = float(np.mean([t.bars_held for t in trades]))
        report.avg_mae_r = float(np.mean([t.mae_r for t in trades]))
        report.avg_mfe_r = float(np.mean([t.mfe_r for t in trades]))
        report.total_fees = float(sum(t.fees for t in trades))

        breakdown: dict[str, int] = {}
        for t in trades:
            key = t.exit_reason.value if isinstance(t.exit_reason, ExitReason) else str(t.exit_reason)
            breakdown[key] = breakdown.get(key, 0) + 1
        report.exit_breakdown = breakdown

    report.warnings = _sanity_warnings(report, trades)
    return report


def _sanity_warnings(
    report: PerformanceReport, trades: list[Trade]
) -> list[str]:
    """Flag results that are more likely to be bugs than edge."""
    warnings: list[str] = []

    if report.total_trades == 0:
        warnings.append("no trades were taken — nothing here is measurable")
        return warnings

    if report.total_trades < 30:
        warnings.append(
            f"only {report.total_trades} trades — every metric below is noise, "
            f"not evidence"
        )
    elif report.total_trades < 100:
        warnings.append(
            f"{report.total_trades} trades is a small sample; treat these as "
            f"provisional"
        )

    if report.sharpe > 3.0:
        warnings.append(
            f"Sharpe of {report.sharpe:.2f} is implausibly high — check for "
            f"lookahead bias before believing it"
        )
    if report.profit_factor > 3.0 and report.total_trades < 100:
        warnings.append(
            f"profit factor {report.profit_factor:.2f} on a small sample is a "
            f"classic overfitting signature"
        )
    if report.max_drawdown < 0.02 and report.total_return > 0.30:
        warnings.append(
            "large return with almost no drawdown — verify stops are actually "
            "filling"
        )
    if report.total_fees == 0 and report.total_trades > 0:
        warnings.append("zero fees recorded — the cost model is not engaged")
    if report.win_rate > 0.75:
        warnings.append(
            f"win rate of {report.win_rate:.0%} is unusual; confirm losers are "
            f"being recorded"
        )

    if report.avg_mfe_r > 0 and report.avg_win_r > 0:
        give_back = report.avg_mfe_r - report.avg_win_r
        if give_back > 1.0:
            warnings.append(
                f"winners give back {give_back:.1f}R on average from peak — "
                f"targets or trailing may be too loose"
            )
    if report.avg_mae_r < -0.85:
        warnings.append(
            f"average MAE is {report.avg_mae_r:.2f}R — trades routinely sit near "
            f"the stop, which suggests entries are early or stops are too tight"
        )

    return warnings


# --------------------------------------------------------------------------- #
# Walk-forward
# --------------------------------------------------------------------------- #

@dataclass
class WalkForwardWindow:
    index: int
    in_sample_start: pd.Timestamp
    in_sample_end: pd.Timestamp
    out_sample_start: pd.Timestamp
    out_sample_end: pd.Timestamp
    in_sample: PerformanceReport
    out_sample: PerformanceReport

    @property
    def degradation(self) -> float:
        """How much the out-of-sample expectancy fell short of in-sample.

        Some degradation is normal. A large gap means the in-sample result was
        fitted rather than discovered.
        """
        if self.in_sample.expectancy_r == 0:
            return 0.0
        return 1.0 - (self.out_sample.expectancy_r / self.in_sample.expectancy_r)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "in_sample_start": self.in_sample_start.isoformat(),
            "in_sample_end": self.in_sample_end.isoformat(),
            "out_sample_start": self.out_sample_start.isoformat(),
            "out_sample_end": self.out_sample_end.isoformat(),
            "in_sample": self.in_sample.to_dict(),
            "out_sample": self.out_sample.to_dict(),
            "degradation": round(self.degradation, 4),
        }


def split_walk_forward(
    df: pd.DataFrame, n_splits: int = 4, in_sample_ratio: float = 0.7
) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
    """Anchored rolling in-sample/out-of-sample splits.

    Windows advance forward in time and never overlap between IS and OOS, so
    each out-of-sample segment is genuinely unseen relative to its own window.
    """
    if n_splits < 1:
        raise ValueError("n_splits must be >= 1")
    if not 0 < in_sample_ratio < 1:
        raise ValueError("in_sample_ratio must be between 0 and 1")

    n = len(df)
    window = n // n_splits
    if window < 50:
        raise ValueError(
            f"{n} bars cannot be split into {n_splits} usable windows"
        )

    splits = []
    for i in range(n_splits):
        start = i * window
        end = start + window if i < n_splits - 1 else n
        cut = start + int((end - start) * in_sample_ratio)
        is_df, oos_df = df.iloc[start:cut], df.iloc[cut:end]
        if len(is_df) > 20 and len(oos_df) > 10:
            splits.append((is_df, oos_df))
    return splits
