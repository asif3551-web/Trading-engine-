"""Technical indicators, hand-rolled on numpy/pandas.

TA-Lib is faster but is a compiled dependency that routinely breaks installs, so
this module implements what the engine needs directly. Every function here is
**causal**: the value at index `i` uses only data up to and including `i`. That
property is what keeps the backtester honest, so any change to this file must
preserve it — no `center=True`, no backward fills, no whole-series statistics
leaking into early bars.

All functions take and return pandas Series/DataFrames aligned to the input index.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "sma", "ema", "wma", "rma", "true_range", "atr", "rsi", "adx",
    "bollinger", "keltner", "macd", "stochastic", "vwap", "anchored_vwap",
    "obv", "cmf", "volume_zscore", "realised_volatility", "zscore",
    "linreg_slope", "donchian", "supertrend", "choppiness",
]


def _as_series(x: pd.Series | np.ndarray, index=None) -> pd.Series:
    if isinstance(x, pd.Series):
        return x.astype("float64")
    return pd.Series(np.asarray(x, dtype="float64"), index=index)


# --------------------------------------------------------------------------- #
# Moving averages
# --------------------------------------------------------------------------- #

def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Standard EMA, alpha = 2/(n+1), seeded so early values aren't spurious."""
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def wma(series: pd.Series, period: int) -> pd.Series:
    weights = np.arange(1, period + 1, dtype="float64")
    return series.rolling(period, min_periods=period).apply(
        lambda w: float(np.dot(w, weights) / weights.sum()), raw=True
    )


def rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (alpha = 1/n). Used by ATR, RSI and ADX.

    Distinct from EMA — using EMA where Wilder is expected shifts RSI/ADX values
    enough to change signals, which is a subtle and common bug.
    """
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


# --------------------------------------------------------------------------- #
# Volatility
# --------------------------------------------------------------------------- #

def true_range(df: pd.DataFrame) -> pd.Series:
    """max(H-L, |H-Cprev|, |L-Cprev|) — captures gaps, unlike a bare H-L."""
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range — the engine's unit of volatility.

    Stops, target spacing and slippage all scale off this, so that a signal on a
    quiet instrument and one on a violent instrument carry the same real risk.
    """
    return rma(true_range(df), period)


def realised_volatility(
    series: pd.Series, period: int = 20, periods_per_year: int = 365 * 24 * 4
) -> pd.Series:
    """Annualised stdev of log returns."""
    returns = np.log(series / series.shift(1))
    return returns.rolling(period, min_periods=period).std() * np.sqrt(periods_per_year)


def zscore(series: pd.Series, period: int = 20) -> pd.Series:
    mean = series.rolling(period, min_periods=period).mean()
    std = series.rolling(period, min_periods=period).std()
    return (series - mean) / std.replace(0.0, np.nan)


# --------------------------------------------------------------------------- #
# Momentum
# --------------------------------------------------------------------------- #

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = rma(gain, period)
    avg_loss = rma(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # avg_loss == 0 means an unbroken run of gains -> RSI 100 by definition.
    return out.where(avg_loss != 0, 100.0).where(avg_gain.notna(), np.nan)


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Wilder's ADX with +DI/-DI. ADX > 25 is the usual trending threshold."""
    up = df["high"].diff()
    down = -df["low"].diff()

    plus_dm = pd.Series(
        np.where((up > down) & (up > 0), up, 0.0), index=df.index, dtype="float64"
    )
    minus_dm = pd.Series(
        np.where((down > up) & (down > 0), down, 0.0), index=df.index, dtype="float64"
    )

    tr = rma(true_range(df), period)
    plus_di = 100.0 * rma(plus_dm, period) / tr.replace(0.0, np.nan)
    minus_di = 100.0 * rma(minus_dm, period) / tr.replace(0.0, np.nan)

    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    return pd.DataFrame(
        {"adx": rma(dx, period), "plus_di": plus_di, "minus_di": minus_di},
        index=df.index,
    )


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame(
        {
            "macd": macd_line,
            "signal": signal_line,
            "histogram": macd_line - signal_line,
        },
        index=series.index,
    )


def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> pd.DataFrame:
    low = df["low"].rolling(k_period, min_periods=k_period).min()
    high = df["high"].rolling(k_period, min_periods=k_period).max()
    k = 100.0 * (df["close"] - low) / (high - low).replace(0.0, np.nan)
    return pd.DataFrame({"k": k, "d": k.rolling(d_period, min_periods=d_period).mean()})


def linreg_slope(series: pd.Series, period: int = 20) -> pd.Series:
    """Slope of a rolling least-squares fit, normalised by price.

    A cleaner trend read than comparing two moving averages because it responds
    to the shape of the move rather than just its endpoints.
    """
    x = np.arange(period, dtype="float64")
    x_mean = x.mean()
    denom = float(((x - x_mean) ** 2).sum())

    def _slope(window: np.ndarray) -> float:
        y_mean = window.mean()
        return float(((x - x_mean) * (window - y_mean)).sum() / denom)

    raw = series.rolling(period, min_periods=period).apply(_slope, raw=True)
    return raw / series.replace(0.0, np.nan)


# --------------------------------------------------------------------------- #
# Bands and channels
# --------------------------------------------------------------------------- #

def bollinger(
    series: pd.Series, period: int = 20, std_mult: float = 2.0
) -> pd.DataFrame:
    mid = sma(series, period)
    std = series.rolling(period, min_periods=period).std()
    upper, lower = mid + std_mult * std, mid - std_mult * std
    return pd.DataFrame(
        {
            "middle": mid,
            "upper": upper,
            "lower": lower,
            # Bandwidth compression precedes expansion — a squeeze is a setup.
            "bandwidth": (upper - lower) / mid.replace(0.0, np.nan),
            "percent_b": (series - lower) / (upper - lower).replace(0.0, np.nan),
        },
        index=series.index,
    )


def keltner(
    df: pd.DataFrame, period: int = 20, atr_period: int = 10, mult: float = 2.0
) -> pd.DataFrame:
    mid = ema(df["close"], period)
    band = atr(df, atr_period) * mult
    return pd.DataFrame(
        {"middle": mid, "upper": mid + band, "lower": mid - band}, index=df.index
    )


def donchian(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """Rolling extremes, shifted by one bar.

    The shift matters: without it the 'high' includes the current bar, so a bar
    making a new high appears to have already broken out. That is lookahead.
    """
    upper = df["high"].rolling(period, min_periods=period).max().shift(1)
    lower = df["low"].rolling(period, min_periods=period).min().shift(1)
    return pd.DataFrame(
        {"upper": upper, "lower": lower, "middle": (upper + lower) / 2.0},
        index=df.index,
    )


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.DataFrame:
    """SuperTrend — trailing stop / regime line. Returns line and direction."""
    atr_v = atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2.0
    upper_basic = hl2 + mult * atr_v
    lower_basic = hl2 - mult * atr_v

    close = df["close"].to_numpy(dtype="float64")
    ub = upper_basic.to_numpy(dtype="float64")
    lb = lower_basic.to_numpy(dtype="float64")
    n = len(df)
    final_ub = np.full(n, np.nan)
    final_lb = np.full(n, np.nan)
    direction = np.ones(n)
    line = np.full(n, np.nan)

    for i in range(n):
        if i == 0 or np.isnan(ub[i]) or np.isnan(final_ub[i - 1]):
            final_ub[i], final_lb[i] = ub[i], lb[i]
            continue
        # Bands ratchet: they only tighten while price stays on the same side.
        final_ub[i] = (
            ub[i] if ub[i] < final_ub[i - 1] or close[i - 1] > final_ub[i - 1]
            else final_ub[i - 1]
        )
        final_lb[i] = (
            lb[i] if lb[i] > final_lb[i - 1] or close[i - 1] < final_lb[i - 1]
            else final_lb[i - 1]
        )
        if close[i] > final_ub[i]:
            direction[i] = 1.0
        elif close[i] < final_lb[i]:
            direction[i] = -1.0
        else:
            direction[i] = direction[i - 1]
        line[i] = final_lb[i] if direction[i] > 0 else final_ub[i]

    return pd.DataFrame({"supertrend": line, "direction": direction}, index=df.index)


def choppiness(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Choppiness Index, 0-100. Above ~61 = ranging, below ~38 = trending.

    Used as a regime gate: a breakout strategy taken in a chop regime is the
    fastest way to bleed an account.
    """
    tr_sum = true_range(df).rolling(period, min_periods=period).sum()
    rng = (
        df["high"].rolling(period, min_periods=period).max()
        - df["low"].rolling(period, min_periods=period).min()
    )
    ratio = tr_sum / rng.replace(0.0, np.nan)
    return 100.0 * np.log10(ratio.where(ratio > 0)) / np.log10(period)


# --------------------------------------------------------------------------- #
# Volume
# --------------------------------------------------------------------------- #

def vwap(df: pd.DataFrame, period: int | None = None) -> pd.Series:
    """VWAP. `period` gives a rolling window; None gives a cumulative session VWAP."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = tp * df["volume"]
    if period is None:
        return pv.cumsum() / df["volume"].cumsum().replace(0.0, np.nan)
    return (
        pv.rolling(period, min_periods=period).sum()
        / df["volume"].rolling(period, min_periods=period).sum().replace(0.0, np.nan)
    )


def anchored_vwap(df: pd.DataFrame, anchor_idx: int) -> pd.Series:
    """VWAP anchored to a specific bar — typically a swing high/low or an event.

    Institutional participants who entered at the anchor are, on average, flat
    at this line, which is why it so often acts as support/resistance.
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (tp * df["volume"]).copy()
    vol = df["volume"].copy()
    pv.iloc[:anchor_idx] = np.nan
    vol.iloc[:anchor_idx] = np.nan
    return pv.cumsum() / vol.cumsum().replace(0.0, np.nan)


def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume — cumulative signed volume."""
    direction = np.sign(df["close"].diff().fillna(0.0))
    return (direction * df["volume"]).cumsum()


def cmf(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Chaikin Money Flow — where in its range each bar closed, volume-weighted."""
    rng = (df["high"] - df["low"]).replace(0.0, np.nan)
    mfm = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / rng
    mfv = mfm * df["volume"]
    return (
        mfv.rolling(period, min_periods=period).sum()
        / df["volume"].rolling(period, min_periods=period).sum().replace(0.0, np.nan)
    )


def volume_zscore(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """How unusual this bar's volume is. A sweep on high-z volume is real
    participation; on low-z volume it is more likely noise."""
    return zscore(df["volume"], period)


def add_all(df: pd.DataFrame, atr_period: int = 14) -> pd.DataFrame:
    """Attach the standard indicator set used by the strategies.

    Returns a copy — never mutate the caller's frame, or a backtest loop that
    slices the same frame repeatedly will accumulate columns.
    """
    out = df.copy()
    out["atr"] = atr(out, atr_period)
    out["atr_pct"] = out["atr"] / out["close"] * 100.0
    out["ema_20"] = ema(out["close"], 20)
    out["ema_50"] = ema(out["close"], 50)
    out["ema_200"] = ema(out["close"], 200)
    out["rsi"] = rsi(out["close"], 14)

    adx_df = adx(out, 14)
    out["adx"] = adx_df["adx"]
    out["plus_di"] = adx_df["plus_di"]
    out["minus_di"] = adx_df["minus_di"]

    bb = bollinger(out["close"], 20, 2.0)
    out["bb_upper"] = bb["upper"]
    out["bb_lower"] = bb["lower"]
    out["bb_bandwidth"] = bb["bandwidth"]
    out["bb_percent_b"] = bb["percent_b"]

    out["vwap"] = vwap(out, 50)
    out["volume_z"] = volume_zscore(out, 20)
    out["cmf"] = cmf(out, 20)
    out["choppiness"] = choppiness(out, 14)
    out["slope"] = linreg_slope(out["close"], 20)
    return out
