"""Symbol resolution across markets.

One friendly name in, the right provider symbol out. Without this layer the
natural way to ask for gold — `XAU/USD` — routed to Binance (because it
contains a slash) and failed, while bare `XAUUSD` went to Yahoo which needs
`XAUUSD=X`. Neither worked, for the most obvious spelling of the request.

The table also carries what the engine needs to treat each market *correctly*
rather than merely fetch it:

  * `asset_class`  — drives which fundamentals apply and how sizing is capped.
  * `has_volume`   — Yahoo reports no volume for spot FX and spot metals. Three
                     of the confluence components are volume-based, so the
                     scorer must renormalise instead of silently docking those
                     markets ~30 points against a fixed threshold.
  * `delay_sec`    — Yahoo is delayed ~15 minutes. The live trader's staleness
                     guard has to know that, or it declares a perfectly healthy
                     feed dead and refuses to trade.

Preferring futures for metals is deliberate: `GC=F` (COMEX gold) carries real
volume and dense intraday bars, whereas spot `XAUUSD=X` has neither. For a
liquidity-driven strategy that difference matters more than the small basis
between them. Ask for `XAUUSD=X` explicitly if you want spot.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.types import AssetClass


@dataclass(frozen=True, slots=True)
class Market:
    """How to fetch and interpret one instrument."""

    provider_symbol: str
    provider: str
    asset_class: AssetClass
    description: str
    has_volume: bool = True
    delay_sec: int = 0          # 0 = real time
    quote_decimals: int = 2

    @property
    def is_realtime(self) -> bool:
        return self.delay_sec == 0


# Yahoo publishes delayed data. 15 minutes is the documented worst case for
# most instruments; being generous here is safe because it only widens the
# staleness tolerance, it never makes the engine trade on older data.
_YF_DELAY = 900


def _yf(
    symbol: str, asset_class: AssetClass, description: str,
    has_volume: bool = True, decimals: int = 2,
) -> Market:
    return Market(symbol, "yfinance", asset_class, description,
                  has_volume, _YF_DELAY, decimals)


def _binance(symbol: str, description: str, decimals: int = 2) -> Market:
    return Market(symbol, "binance", AssetClass.CRYPTO, description,
                  True, 0, decimals)


# --------------------------------------------------------------------------- #
# Metals — futures preferred: they have volume, spot does not.
# --------------------------------------------------------------------------- #

METALS: dict[str, Market] = {
    "XAUUSD": _yf("GC=F", AssetClass.COMMODITY, "Gold (COMEX front future)", True, 2),
    "XAGUSD": _yf("SI=F", AssetClass.COMMODITY, "Silver (COMEX front future)", True, 3),
    "XPTUSD": _yf("PL=F", AssetClass.COMMODITY, "Platinum future", True, 2),
    "XPDUSD": _yf("PA=F", AssetClass.COMMODITY, "Palladium future", True, 2),
    "XCUUSD": _yf("HG=F", AssetClass.COMMODITY, "Copper future", True, 4),
    # Spot equivalents, on request. No volume from Yahoo.
    "XAUUSD.SPOT": _yf("XAUUSD=X", AssetClass.COMMODITY, "Gold spot", False, 2),
    "XAGUSD.SPOT": _yf("XAGUSD=X", AssetClass.COMMODITY, "Silver spot", False, 3),
}

# --------------------------------------------------------------------------- #
# FX — Yahoo gives no volume on any of these.
# --------------------------------------------------------------------------- #

FX: dict[str, Market] = {
    "EURUSD": _yf("EURUSD=X", AssetClass.FX, "Euro / US Dollar", False, 5),
    "GBPUSD": _yf("GBPUSD=X", AssetClass.FX, "Pound / US Dollar", False, 5),
    "USDJPY": _yf("USDJPY=X", AssetClass.FX, "US Dollar / Yen", False, 3),
    "AUDUSD": _yf("AUDUSD=X", AssetClass.FX, "Aussie / US Dollar", False, 5),
    "NZDUSD": _yf("NZDUSD=X", AssetClass.FX, "Kiwi / US Dollar", False, 5),
    "USDCAD": _yf("USDCAD=X", AssetClass.FX, "US Dollar / Canadian", False, 5),
    "USDCHF": _yf("USDCHF=X", AssetClass.FX, "US Dollar / Swiss Franc", False, 5),
    "EURJPY": _yf("EURJPY=X", AssetClass.FX, "Euro / Yen", False, 3),
    "GBPJPY": _yf("GBPJPY=X", AssetClass.FX, "Pound / Yen", False, 3),
    "EURGBP": _yf("EURGBP=X", AssetClass.FX, "Euro / Pound", False, 5),
    "USDMXN": _yf("USDMXN=X", AssetClass.FX, "US Dollar / Peso", False, 4),
    "USDINR": _yf("USDINR=X", AssetClass.FX, "US Dollar / Rupee", False, 4),
    "DXY": _yf("DX-Y.NYB", AssetClass.INDEX, "US Dollar Index", False, 3),
}

# --------------------------------------------------------------------------- #
# Crypto — Binance public API, genuinely real time.
# --------------------------------------------------------------------------- #

_CRYPTO_BASES = [
    ("BTC", "Bitcoin", 2), ("ETH", "Ethereum", 2), ("SOL", "Solana", 3),
    ("BNB", "BNB", 2), ("XRP", "XRP", 5), ("ADA", "Cardano", 5),
    ("DOGE", "Dogecoin", 6), ("AVAX", "Avalanche", 3), ("LINK", "Chainlink", 3),
    ("DOT", "Polkadot", 4), ("LTC", "Litecoin", 2), ("TRX", "Tron", 6),
    ("MATIC", "Polygon", 5), ("ATOM", "Cosmos", 3), ("NEAR", "NEAR", 4),
    ("ARB", "Arbitrum", 4), ("OP", "Optimism", 4), ("SUI", "Sui", 4),
]
CRYPTO: dict[str, Market] = {
    f"{base}USDT": _binance(f"{base}USDT", f"{name} / USDT", dec)
    for base, name, dec in _CRYPTO_BASES
}

# --------------------------------------------------------------------------- #
# Indices and energy
# --------------------------------------------------------------------------- #

INDICES: dict[str, Market] = {
    "US500": _yf("^GSPC", AssetClass.INDEX, "S&P 500", False, 2),
    "US100": _yf("^NDX", AssetClass.INDEX, "Nasdaq 100", False, 2),
    "US30": _yf("^DJI", AssetClass.INDEX, "Dow Jones 30", False, 2),
    "DE40": _yf("^GDAXI", AssetClass.INDEX, "DAX 40", False, 2),
    "UK100": _yf("^FTSE", AssetClass.INDEX, "FTSE 100", False, 2),
    "JP225": _yf("^N225", AssetClass.INDEX, "Nikkei 225", False, 2),
    "VIX": _yf("^VIX", AssetClass.INDEX, "Volatility Index", False, 2),
}

ENERGY: dict[str, Market] = {
    "USOIL": _yf("CL=F", AssetClass.COMMODITY, "WTI Crude future", True, 2),
    "UKOIL": _yf("BZ=F", AssetClass.COMMODITY, "Brent Crude future", True, 2),
    "NATGAS": _yf("NG=F", AssetClass.COMMODITY, "Natural Gas future", True, 3),
}

REGISTRY: dict[str, Market] = {**METALS, **FX, **CRYPTO, **INDICES, **ENERGY}

# Common spellings people actually type.
ALIASES: dict[str, str] = {
    "GOLD": "XAUUSD", "XAU": "XAUUSD", "GC": "XAUUSD",
    "SILVER": "XAGUSD", "XAG": "XAGUSD",
    "PLATINUM": "XPTUSD", "XPT": "XPTUSD",
    "PALLADIUM": "XPDUSD", "XPD": "XPDUSD",
    "COPPER": "XCUUSD", "XCU": "XCUUSD",
    "WTI": "USOIL", "CRUDE": "USOIL", "OIL": "USOIL", "USCRUDE": "USOIL",
    "BRENT": "UKOIL",
    "SPX": "US500", "SP500": "US500", "SPX500": "US500",
    "NAS100": "US100", "NASDAQ": "US100", "NDX": "US100",
    "DOW": "US30", "DJI": "US30",
    "DAX": "DE40", "FTSE": "UK100", "NIKKEI": "JP225",
    "BITCOIN": "BTCUSDT", "BTC": "BTCUSDT",
    "ETHEREUM": "ETHUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "GOLDSPOT": "XAUUSD.SPOT", "XAUUSDSPOT": "XAUUSD.SPOT",
}


def canonical(symbol: str) -> str:
    """Normalise user input to a registry key.

    Strips separators and quote-currency variations so `XAU/USD`, `xau-usd`,
    `XAUUSD` and `gold` all land on the same entry.
    """
    s = symbol.strip().upper().replace(" ", "")
    if s in REGISTRY or s in ALIASES:
        return ALIASES.get(s, s)

    stripped = s.replace("/", "").replace("-", "").replace("_", "")
    if stripped in REGISTRY or stripped in ALIASES:
        return ALIASES.get(stripped, stripped)

    # BTC/USD and BTCUSD mean BTCUSDT on Binance, which has no USD pairs.
    if stripped.endswith("USD") and f"{stripped}T" in REGISTRY:
        return f"{stripped}T"
    # Bare crypto base, e.g. "AVAX".
    if f"{stripped}USDT" in REGISTRY:
        return f"{stripped}USDT"
    return stripped


def resolve(symbol: str) -> Market:
    """Return the Market for a symbol, or a best-effort passthrough.

    An unknown symbol is not an error: it is passed to Yahoo verbatim so that
    any ticker Yahoo knows still works without needing a registry entry. Volume
    is assumed absent in that case, which is the conservative choice — it makes
    the scorer renormalise rather than reward a market for volume it may not
    have.
    """
    key = canonical(symbol)
    market = REGISTRY.get(key)
    if market is not None:
        return market

    raw = symbol.strip().upper()
    # Recognisable Yahoo shapes, passed through as-is.
    if raw.endswith("=X"):
        return _yf(raw, AssetClass.FX, f"{raw} (Yahoo FX)", False, 5)
    if raw.endswith("=F"):
        return _yf(raw, AssetClass.FUTURES, f"{raw} (Yahoo future)", True, 3)
    if raw.startswith("^"):
        return _yf(raw, AssetClass.INDEX, f"{raw} (Yahoo index)", False, 2)
    if raw.endswith(("USDT", "USDC", "BUSD")):
        return _binance(raw.replace("/", ""), f"{raw} (Binance)", 4)
    return _yf(raw, AssetClass.EQUITY, f"{raw} (Yahoo equity)", True, 2)


def describe(symbol: str) -> str:
    m = resolve(symbol)
    delay = "real time" if m.is_realtime else f"~{m.delay_sec // 60}min delayed"
    volume = "with volume" if m.has_volume else "no volume"
    return (
        f"{symbol} -> {m.provider}:{m.provider_symbol} "
        f"({m.description}, {delay}, {volume})"
    )


def catalogue() -> dict[str, list[tuple[str, Market]]]:
    """Grouped listing for the `symbols` command and the dashboard."""
    return {
        "Crypto (Binance, real time)": sorted(CRYPTO.items()),
        "Metals (Yahoo futures, delayed)": sorted(METALS.items()),
        "Forex (Yahoo, delayed, no volume)": sorted(FX.items()),
        "Indices (Yahoo, delayed)": sorted(INDICES.items()),
        "Energy (Yahoo futures, delayed)": sorted(ENERGY.items()),
    }
