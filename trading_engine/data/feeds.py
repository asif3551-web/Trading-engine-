"""Market data feeds.

Three providers, all behind one interface:

  BinanceFeed    — crypto OHLCV, order book and perpetual-futures funding/OI.
                   Public endpoints, no API key needed.
  YFinanceFeed   — equities, FX, indices, futures and commodities, if the
                   optional `yfinance` package is installed.
  SyntheticFeed  — deterministic generated bars. Not a toy: it is what makes the
                   test suite reproducible and lets the whole engine run with no
                   network at all.

`get_feed("auto", symbol)` picks a provider from the symbol's shape, falling
back to synthetic when nothing else is reachable.

Only the standard library is used for HTTP so the engine has no hard third-party
dependency beyond numpy/pandas.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ..core.types import AssetClass, OrderBook, OrderBookLevel

# Timeframe -> seconds. The single source of truth for bar durations.
TIMEFRAME_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400, "1w": 604800,
}

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


class DataError(RuntimeError):
    """Raised when a feed cannot return usable data."""


def timeframe_seconds(timeframe: str) -> int:
    try:
        return TIMEFRAME_SECONDS[timeframe]
    except KeyError:
        raise ValueError(
            f"unsupported timeframe {timeframe!r}; "
            f"expected one of {sorted(TIMEFRAME_SECONDS)}"
        ) from None


def _http_get_json(url: str, params: dict, timeout: int = 15) -> object:
    query = urllib.parse.urlencode(params)
    full = f"{url}?{query}" if query else url
    request = urllib.request.Request(
        full, headers={"User-Agent": "trading-engine/1.0"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def validate_ohlcv(df: pd.DataFrame, symbol: str = "") -> pd.DataFrame:
    """Reject or repair malformed OHLCV.

    Bad data produces confident, wrong signals, so this is strict: duplicate or
    unsorted timestamps and impossible bars (high < low, close outside the
    range) are removed rather than passed downstream.
    """
    if df.empty:
        raise DataError(f"no data returned for {symbol or 'symbol'}")

    missing = set(OHLCV_COLUMNS) - set(df.columns)
    if missing:
        raise DataError(f"missing columns {sorted(missing)} for {symbol}")

    df = df[~df.index.duplicated(keep="last")].sort_index()

    valid = (
        (df["high"] >= df["low"])
        & (df["high"] >= df["open"]) & (df["high"] >= df["close"])
        & (df["low"] <= df["open"]) & (df["low"] <= df["close"])
        & (df[OHLCV_COLUMNS] > 0).all(axis=1).reindex(df.index, fill_value=False)
        | (df["volume"] == 0) & (df["high"] >= df["low"])
    )
    dropped = int((~valid).sum())
    if dropped:
        df = df[valid]
    if df.empty:
        raise DataError(f"all bars for {symbol} failed validation")
    return df


class DataFeed(ABC):
    """Common interface for every provider."""

    name = "base"

    @abstractmethod
    def get_bars(
        self, symbol: str, timeframe: str = "15m", limit: int = 500,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        """Return a UTC-indexed OHLCV frame, oldest first."""

    def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook | None:
        return None

    def asset_class(self, symbol: str) -> AssetClass:
        return AssetClass.CRYPTO

    def tick_size(self, symbol: str) -> float:
        return 0.0


# --------------------------------------------------------------------------- #
# Binance
# --------------------------------------------------------------------------- #

class BinanceFeed(DataFeed):
    """Crypto data from Binance's public REST endpoints (no key required)."""

    name = "binance"
    SPOT = "https://api.binance.com"
    FUTURES = "https://fapi.binance.com"

    def __init__(self, timeout: int = 15) -> None:
        self.timeout = timeout
        self._filters: dict[str, float] = {}

    @staticmethod
    def normalise(symbol: str) -> str:
        """BTC/USDT -> BTCUSDT."""
        return symbol.replace("/", "").replace("-", "").upper()

    def get_bars(
        self, symbol: str, timeframe: str = "15m", limit: int = 500,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        timeframe_seconds(timeframe)   # validate early
        params = {
            "symbol": self.normalise(symbol),
            "interval": timeframe,
            "limit": min(limit, 1000),
        }
        if end is not None:
            params["endTime"] = int(end.timestamp() * 1000)

        try:
            raw = _http_get_json(f"{self.SPOT}/api/v3/klines", params, self.timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DataError(f"binance klines request failed: {exc}") from exc

        if not isinstance(raw, list) or not raw:
            raise DataError(f"binance returned no klines for {symbol}")

        df = pd.DataFrame(
            raw,
            columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "trades", "taker_base",
                "taker_quote", "ignore",
            ],
        )
        df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df = df.set_index("timestamp")[OHLCV_COLUMNS].astype("float64")
        df.attrs["symbol"] = symbol
        df.attrs["timeframe"] = timeframe
        return validate_ohlcv(df, symbol)

    def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook | None:
        valid_depths = (5, 10, 20, 50, 100, 500, 1000)
        depth = min(valid_depths, key=lambda d: abs(d - depth))
        try:
            raw = _http_get_json(
                f"{self.SPOT}/api/v3/depth",
                {"symbol": self.normalise(symbol), "limit": depth},
                self.timeout,
            )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        return OrderBook(
            timestamp=datetime.now(timezone.utc),
            symbol=symbol,
            bids=[OrderBookLevel(float(p), float(q)) for p, q in raw.get("bids", [])],
            asks=[OrderBookLevel(float(p), float(q)) for p, q in raw.get("asks", [])],
        )

    def get_funding(self, symbol: str) -> dict | None:
        """Perpetual funding rate and open interest — the crypto fundamentals."""
        pair = self.normalise(symbol)
        out: dict = {}
        try:
            premium = _http_get_json(
                f"{self.FUTURES}/fapi/v1/premiumIndex", {"symbol": pair}, self.timeout
            )
            if isinstance(premium, dict) and "lastFundingRate" in premium:
                # Binance reports the rate as a fraction per 8h period.
                out["funding_rate_bps"] = float(premium["lastFundingRate"]) * 10_000.0
                mark = float(premium.get("markPrice", 0) or 0)
                index = float(premium.get("indexPrice", 0) or 0)
                if mark and index:
                    out["basis_bps"] = (mark - index) / index * 10_000.0
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
            pass

        try:
            oi = _http_get_json(
                f"{self.FUTURES}/fapi/v1/openInterest", {"symbol": pair}, self.timeout
            )
            if isinstance(oi, dict) and "openInterest" in oi:
                out["open_interest"] = float(oi["openInterest"])
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
            pass

        return out or None

    def tick_size(self, symbol: str) -> float:
        pair = self.normalise(symbol)
        if pair in self._filters:
            return self._filters[pair]
        try:
            info = _http_get_json(
                f"{self.SPOT}/api/v3/exchangeInfo", {"symbol": pair}, self.timeout
            )
            for s in info.get("symbols", []):  # type: ignore[union-attr]
                for f in s.get("filters", []):
                    if f.get("filterType") == "PRICE_FILTER":
                        tick = float(f["tickSize"])
                        self._filters[pair] = tick
                        return tick
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError,
                AttributeError):
            pass
        self._filters[pair] = 0.0
        return 0.0

    def asset_class(self, symbol: str) -> AssetClass:
        return AssetClass.CRYPTO


# --------------------------------------------------------------------------- #
# yfinance
# --------------------------------------------------------------------------- #

_YF_INTERVALS = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "60m", "1d": "1d", "1w": "1wk",
}


class YFinanceFeed(DataFeed):
    """Equities, FX, indices, futures and commodities via the optional
    `yfinance` package.

    Free and convenient, but note the caveats: intraday history is limited to
    roughly 60 days, and delisted tickers are absent — so any long backtest run
    on this source carries survivorship bias.
    """

    name = "yfinance"

    def __init__(self) -> None:
        try:
            import yfinance  # noqa: F401
        except ImportError as exc:
            raise DataError(
                "yfinance is not installed; run `pip install yfinance` to trade "
                "equities/FX, or use the binance/synthetic feeds"
            ) from exc

    def get_bars(
        self, symbol: str, timeframe: str = "15m", limit: int = 500,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        import yfinance as yf

        interval = _YF_INTERVALS.get(timeframe)
        if interval is None:
            raise DataError(
                f"yfinance does not support the {timeframe} interval; "
                f"supported: {sorted(_YF_INTERVALS)}"
            )

        seconds = timeframe_seconds(timeframe)
        span = timedelta(seconds=seconds * limit * 2)
        # Yahoo caps intraday history; asking for more silently returns nothing.
        if seconds < 86400:
            span = min(span, timedelta(days=59))

        end = end or datetime.now(timezone.utc)
        try:
            raw = yf.download(
                symbol, start=end - span, end=end, interval=interval,
                progress=False, auto_adjust=True, threads=False,
            )
        except Exception as exc:  # yfinance raises a wide variety
            raise DataError(f"yfinance download failed for {symbol}: {exc}") from exc

        if raw is None or raw.empty:
            raise DataError(f"yfinance returned no data for {symbol}")

        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)

        df = raw.rename(columns=str.lower)[OHLCV_COLUMNS].astype("float64")
        df.index = pd.to_datetime(df.index, utc=True)
        df.attrs["symbol"] = symbol
        df.attrs["timeframe"] = timeframe
        return validate_ohlcv(df.tail(limit), symbol)

    def asset_class(self, symbol: str) -> AssetClass:
        s = symbol.upper()
        if s.endswith("=X"):
            return AssetClass.FX
        if s.endswith("=F"):
            return AssetClass.FUTURES
        if s.startswith("^"):
            return AssetClass.INDEX
        if s.endswith("-USD"):
            return AssetClass.CRYPTO
        return AssetClass.EQUITY


# --------------------------------------------------------------------------- #
# Synthetic
# --------------------------------------------------------------------------- #

class SyntheticFeed(DataFeed):
    """Deterministic generated OHLCV.

    Produces price action with the features the strategy looks for — trends,
    ranges, equal highs/lows and sweeps of them — so the whole pipeline can be
    exercised without a network. Seeded, so a given seed always yields the same
    series and tests are reproducible.

    This is for development and testing. Results from synthetic data say
    something about whether the code works, and nothing whatsoever about whether
    the strategy is profitable.
    """

    name = "synthetic"

    def __init__(self, seed: int = 42, base_price: float = 50_000.0,
                 volatility: float = 0.012) -> None:
        self.seed = seed
        self.base_price = base_price
        self.volatility = volatility

    def get_bars(
        self, symbol: str, timeframe: str = "15m", limit: int = 500,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        seconds = timeframe_seconds(timeframe)
        # Seed from the symbol too, so different symbols differ but stay stable.
        # crc32, not hash(): Python salts string hashes per process, which would
        # make this feed — and every test built on it — silently irreproducible
        # across runs.
        rng = np.random.default_rng(
            self.seed + (zlib.crc32(symbol.encode("utf-8")) % 10_000)
        )

        end = end or datetime.now(timezone.utc)
        end = end.replace(second=0, microsecond=0)
        index = pd.date_range(
            end=end, periods=limit, freq=pd.Timedelta(seconds=seconds), tz="UTC"
        )

        # Regime-switching drift: alternating trends and ranges, which is what
        # gives the structure detector something real to find.
        n = limit
        regimes = rng.choice([0.0006, -0.0006, 0.0], size=max(1, n // 80),
                             p=[0.35, 0.35, 0.30])
        drift = np.repeat(regimes, 80)[:n]
        if len(drift) < n:
            drift = np.concatenate([drift, np.zeros(n - len(drift))])

        shocks = rng.normal(0.0, self.volatility, n)
        # Volatility clustering: calm begets calm, violence begets violence.
        vol_scale = np.ones(n)
        for i in range(1, n):
            vol_scale[i] = 0.92 * vol_scale[i - 1] + 0.08 * abs(rng.normal(1.0, 0.4))
        returns = drift + shocks * vol_scale

        close = self.base_price * np.exp(np.cumsum(returns))

        # Build OHLC around the closes with realistic wick behaviour.
        open_ = np.empty(n)
        open_[0] = self.base_price
        open_[1:] = close[:-1]
        spread = np.abs(rng.normal(0.0, self.volatility * 0.6, n)) * close
        high = np.maximum(open_, close) + spread * rng.uniform(0.3, 1.0, n)
        low = np.minimum(open_, close) - spread * rng.uniform(0.3, 1.0, n)

        # Inject equal highs/lows, then sweeps of them — the exact structures
        # the strategy trades, so the pipeline is genuinely exercised.
        for start in range(60, n - 20, 97):
            level = high[start] * 1.0005
            for k in (start + 6, start + 13):
                if k < n:
                    high[k] = level * rng.uniform(0.9995, 1.0002)
            sweep = start + 20
            if sweep < n:
                high[sweep] = level * 1.004                 # take the stops
                close[sweep] = level * 0.996                # then reject
                low[sweep] = min(low[sweep], close[sweep] * 0.998)

        high = np.maximum.reduce([high, open_, close])
        low = np.minimum.reduce([low, open_, close])

        volume = np.abs(rng.lognormal(6.0, 0.6, n)) * (1.0 + np.abs(returns) * 60.0)

        df = pd.DataFrame(
            {
                "open": open_, "high": high, "low": low,
                "close": close, "volume": volume,
            },
            index=index,
        ).astype("float64")
        df.attrs["symbol"] = symbol
        df.attrs["timeframe"] = timeframe
        return validate_ohlcv(df, symbol)

    def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook | None:
        rng = np.random.default_rng(self.seed)
        mid = self.base_price
        tick = mid * 0.0001
        return OrderBook(
            timestamp=datetime.now(timezone.utc),
            symbol=symbol,
            bids=[
                OrderBookLevel(mid - tick * (i + 1), float(rng.uniform(0.5, 5.0)))
                for i in range(depth)
            ],
            asks=[
                OrderBookLevel(mid + tick * (i + 1), float(rng.uniform(0.5, 5.0)))
                for i in range(depth)
            ],
        )


# --------------------------------------------------------------------------- #
# CSV + caching + factory
# --------------------------------------------------------------------------- #

class CSVFeed(DataFeed):
    """Load bars from local CSV files named `<SYMBOL>_<TIMEFRAME>.csv`."""

    name = "csv"

    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)

    def get_bars(
        self, symbol: str, timeframe: str = "15m", limit: int = 500,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        safe = symbol.replace("/", "").replace(" ", "")
        path = self.directory / f"{safe}_{timeframe}.csv"
        if not path.exists():
            raise DataError(f"no CSV at {path}")
        df = pd.read_csv(path)
        time_col = next(
            (c for c in ("timestamp", "time", "date", "datetime") if c in df.columns),
            None,
        )
        if time_col is None:
            raise DataError(f"{path} has no recognisable timestamp column")
        df["timestamp"] = pd.to_datetime(df[time_col], utc=True)
        df = df.set_index("timestamp")
        df.columns = [c.lower() for c in df.columns]
        df = df[OHLCV_COLUMNS].astype("float64")
        df.attrs["symbol"] = symbol
        df.attrs["timeframe"] = timeframe
        return validate_ohlcv(df.tail(limit), symbol)


class CachedFeed(DataFeed):
    """Wraps a feed with an on-disk parquet cache.

    The cache validates the **data**, not the file's age. An earlier version
    expired entries after one bar duration, which was wrong for live polling:
    a file 899s old whose newest bar was already 899s old when written served
    data ~1800s stale, well past the live trader's staleness limit, so the
    trader declared the feed dead and refused to trade — permanently.

    So an entry is reused only when both hold:
      1. its newest bar is still the *currently forming* bar, and
      2. the file is younger than `max_age_sec`, so that forming bar's
         high/low/close are not badly out of date.

    Historical bars never change, so this still spares the network on repeated
    backtests over the same window while keeping live polling honest.
    """

    def __init__(
        self, feed: DataFeed, cache_dir: str, enabled: bool = True,
        max_age_sec: int = 45,
    ) -> None:
        self.feed = feed
        self.name = f"cached:{feed.name}"
        self.cache_dir = Path(cache_dir)
        self.enabled = enabled
        self.max_age_sec = max_age_sec
        if enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str, timeframe: str, limit: int) -> Path:
        safe = symbol.replace("/", "_").replace(":", "_")
        return self.cache_dir / f"{self.feed.name}_{safe}_{timeframe}_{limit}.parquet"

    def get_bars(
        self, symbol: str, timeframe: str = "15m", limit: int = 500,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        if not self.enabled or end is not None:
            return self.feed.get_bars(symbol, timeframe, limit, end)

        path = self._path(symbol, timeframe, limit)
        bar_seconds = timeframe_seconds(timeframe)
        file_age = (
            time.time() - path.stat().st_mtime if path.exists() else float("inf")
        )

        if file_age < self.max_age_sec:
            try:
                df = pd.read_parquet(path)
                if self._last_bar_is_current(df, bar_seconds):
                    df.attrs["symbol"] = symbol
                    df.attrs["timeframe"] = timeframe
                    return df
            except Exception:
                pass  # a corrupt cache entry should never be fatal

        df = self.feed.get_bars(symbol, timeframe, limit, end)
        try:
            df.to_parquet(path)
        except Exception:
            pass      # pyarrow missing or disk full — caching is best-effort
        return df

    @staticmethod
    def _last_bar_is_current(df: pd.DataFrame, bar_seconds: int) -> bool:
        """True if the frame's newest bar is the period we are still inside."""
        if df.empty:
            return False
        last = df.index[-1]
        if last.tzinfo is None:
            last = last.tz_localize("UTC")
        age = (datetime.now(timezone.utc) - last.to_pydatetime()).total_seconds()
        # Allow a little clock skew, but reject once the next bar has opened.
        return -60.0 <= age < bar_seconds

    def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook | None:
        return self.feed.get_orderbook(symbol, depth)   # never cache live depth

    def asset_class(self, symbol: str) -> AssetClass:
        return self.feed.asset_class(symbol)

    def tick_size(self, symbol: str) -> float:
        return self.feed.tick_size(symbol)


def infer_provider(symbol: str) -> str:
    """Guess the right provider from the symbol's shape."""
    s = symbol.upper()
    if "/" in s or s.endswith(("USDT", "USDC", "BUSD")):
        return "binance"
    return "yfinance"


def get_feed(
    provider: str = "auto",
    symbol: str = "",
    cache_dir: str | None = None,
    cache_enabled: bool = True,
    fallback_to_synthetic: bool = True,
) -> DataFeed:
    """Build a feed, falling back to synthetic when the real one is unavailable.

    The fallback keeps development and CI working offline, and it is loud about
    it — the returned feed's `name` is `synthetic`, and callers surface that so
    nobody mistakes generated data for the market.
    """
    if provider == "auto":
        provider = infer_provider(symbol) if symbol else "synthetic"

    def build() -> DataFeed:
        if provider == "binance":
            return BinanceFeed()
        if provider == "yfinance":
            return YFinanceFeed()
        if provider == "synthetic":
            return SyntheticFeed()
        if provider.startswith("csv:"):
            return CSVFeed(provider.split(":", 1)[1])
        raise DataError(f"unknown provider {provider!r}")

    try:
        feed = build()
    except DataError:
        if not fallback_to_synthetic:
            raise
        feed = SyntheticFeed()

    if cache_dir and not isinstance(feed, SyntheticFeed):
        return CachedFeed(feed, cache_dir, cache_enabled)
    return feed
