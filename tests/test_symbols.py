"""Symbol resolution tests.

The regression these lock down: `XAU/USD` — the most natural way to ask for gold
— was routed to Binance, because the old heuristic sent anything containing a
slash to the crypto exchange. Binance does not list gold, so the request simply
failed. Bare `XAUUSD` reached Yahoo but Yahoo needs `XAUUSD=X` or `GC=F`.
Neither spelling worked.
"""

from __future__ import annotations

import pytest

from trading_engine.core.types import AssetClass
from trading_engine.data.feeds import infer_provider
from trading_engine.data.symbols import (
    CRYPTO, FX, METALS, REGISTRY, canonical, catalogue, describe, resolve,
)


# --------------------------------------------------------------------------- #
# Gold, the reported case
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "spelling",
    ["XAUUSD", "XAU/USD", "xau/usd", "XAU-USD", "GOLD", "gold", "XAU", "xau",
     " XAUUSD "],
)
def test_every_spelling_of_gold_resolves(spelling):
    market = resolve(spelling)
    assert market.provider == "yfinance"
    assert market.provider_symbol == "GC=F"
    assert market.asset_class is AssetClass.COMMODITY


def test_gold_no_longer_routes_to_binance():
    """The exact bug: a slash sent gold to a crypto exchange."""
    assert infer_provider("XAU/USD") == "yfinance"
    assert infer_provider("XAG/USD") == "yfinance"
    assert infer_provider("EUR/USD") == "yfinance"


def test_metals_prefer_futures_for_volume():
    """Futures carry volume; spot from Yahoo does not. Three confluence
    components are volume-based, so the default must be the future."""
    assert resolve("XAUUSD").provider_symbol == "GC=F"
    assert resolve("XAUUSD").has_volume is True
    # Spot is still reachable on request, and is honest about having no volume.
    assert resolve("XAUUSD.SPOT").provider_symbol == "XAUUSD=X"
    assert resolve("XAUUSD.SPOT").has_volume is False


# --------------------------------------------------------------------------- #
# Forex
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "spelling,expected",
    [("EURUSD", "EURUSD=X"), ("EUR/USD", "EURUSD=X"), ("usdjpy", "USDJPY=X"),
     ("GBP/JPY", "GBPJPY=X"), ("DXY", "DX-Y.NYB")],
)
def test_forex_resolves(spelling, expected):
    assert resolve(spelling).provider_symbol == expected


def test_forex_reports_no_volume():
    """Yahoo gives zero volume for spot FX. The engine must know, so the
    volume-based components abstain instead of penalising the market."""
    for market in FX.values():
        if market.asset_class is AssetClass.FX:
            assert market.has_volume is False


# --------------------------------------------------------------------------- #
# Crypto
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "spelling", ["BTC/USDT", "BTCUSDT", "btc", "BITCOIN", "BTC/USD", "BTCUSD"]
)
def test_crypto_resolves_to_a_binance_pair(spelling):
    market = resolve(spelling)
    assert market.provider == "binance"
    assert market.provider_symbol == "BTCUSDT"
    assert market.is_realtime, "Binance public data is real time"


def test_binance_normalise_uses_the_registry():
    from trading_engine.data.feeds import BinanceFeed

    assert BinanceFeed.normalise("BTC/USDT") == "BTCUSDT"
    assert BinanceFeed.normalise("btc") == "BTCUSDT"
    assert BinanceFeed.normalise("SOL/USDT") == "SOLUSDT"


# --------------------------------------------------------------------------- #
# Passthrough and metadata
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "ticker,provider", [("AAPL", "yfinance"), ("^VIX", "yfinance"),
                        ("ES=F", "yfinance"), ("EURSEK=X", "yfinance")]
)
def test_unknown_tickers_pass_through_to_yahoo(ticker, provider):
    """An unlisted symbol is not an error — Yahoo knows thousands of tickers."""
    assert resolve(ticker).provider == provider


def test_passthrough_assumes_no_volume_for_fx_shapes():
    """Conservative default: assume absent volume so the scorer renormalises
    rather than crediting a market for volume it may not have."""
    assert resolve("EURSEK=X").has_volume is False


def test_indices_and_energy_resolve():
    assert resolve("US500").provider_symbol == "^GSPC"
    assert resolve("NAS100").provider_symbol == "^NDX"
    assert resolve("WTI").provider_symbol == "CL=F"
    assert resolve("BRENT").provider_symbol == "BZ=F"


def test_yahoo_markets_declare_their_delay():
    """The live trader adds this to its staleness allowance. Without it a
    healthy but delayed feed is branded dead and trading stops."""
    assert resolve("XAUUSD").delay_sec >= 600
    assert resolve("EURUSD").delay_sec >= 600
    assert resolve("BTC/USDT").delay_sec == 0


def test_registry_entries_are_self_consistent():
    for key, market in REGISTRY.items():
        assert market.provider in ("binance", "yfinance"), key
        assert market.provider_symbol, key
        assert market.description, key
        assert market.quote_decimals >= 0, key
        if market.provider == "binance":
            assert market.is_realtime, f"{key}: Binance data is real time"


def test_canonical_is_idempotent():
    for name in list(REGISTRY)[:20]:
        assert canonical(canonical(name)) == canonical(name)


def test_catalogue_covers_the_asked_for_markets():
    groups = catalogue()
    joined = " ".join(groups)
    assert "Crypto" in joined and "Metals" in joined and "Forex" in joined
    assert all(entries for entries in groups.values())


def test_describe_is_readable():
    text = describe("XAUUSD")
    assert "GC=F" in text and "delayed" in text
    assert "real time" in describe("BTC/USDT")


# --------------------------------------------------------------------------- #
# Zero-volume markets must not be handicapped
# --------------------------------------------------------------------------- #

def test_zero_volume_markets_are_not_score_handicapped():
    """FX and spot metals report no volume, which disables three confluence
    components. Those components can only ADD points, so without
    renormalisation every such market is silently docked ~30 points against a
    fixed confidence threshold — rejecting setups that would pass on crypto.
    """
    import collections

    from trading_engine.config import Config
    from trading_engine.data.feeds import SyntheticFeed
    from trading_engine.strategy.liquidity_sweep import LiquiditySweepStrategy

    def peak_confidence(zero_volume: bool) -> float:
        df = SyntheticFeed(seed=5).get_bars("X", "15m", 1200)
        if zero_volume:
            df = df.assign(volume=0.0)
        strategy = LiquiditySweepStrategy(Config().strategy).prepare(df)
        best = 0.0
        for i in range(300, len(df)):
            best = max(best, strategy.evaluate(i, symbol="X").confidence)
        return best

    with_volume = peak_confidence(False)
    without_volume = peak_confidence(True)
    assert with_volume > 0 and without_volume > 0
    # Within a reasonable band of each other — no systematic penalty.
    assert without_volume >= with_volume * 0.85, (
        f"zero-volume markets peak at {without_volume:.1f} vs "
        f"{with_volume:.1f} — they are being handicapped"
    )


def test_zero_volume_data_produces_finite_scores():
    """NaNs from volume indicators must never leak into the score."""
    import math

    from trading_engine.data.feeds import SyntheticFeed
    from trading_engine.indicators.core import add_all
    from trading_engine.liquidity.analyzer import LiquidityAnalyzer

    df = SyntheticFeed(seed=5).get_bars("XAUUSD", "15m", 600).assign(volume=0.0)
    analyzer = LiquidityAnalyzer().prepare(add_all(df))
    for i in range(400, len(df), 25):
        ctx = analyzer.context_at(i)
        assert math.isfinite(ctx.score), f"bar {i} scored {ctx.score}"
        assert 0.0 <= ctx.score <= 100.0
    for sweep in analyzer._sweeps:
        assert math.isfinite(sweep.quality)
