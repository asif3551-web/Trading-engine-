# Trading ecosystem reference

Star counts sampled 2026-08 via the GitHub API, sorted by rating/adoption.

## Frameworks worth borrowing from

| Project | Stars | Take away |
|---|---|---|
| [freqtrade](https://github.com/freqtrade/freqtrade) | 53.8k | Strategy interface (`populate_indicators` / `populate_entry_trend` / `populate_exit_trend`), hyperopt loss functions, dry-run mode sharing the live code path, `protections` (cooldown, max drawdown, stoploss guard) |
| [ccxt](https://github.com/ccxt/ccxt) | 43.8k | Unified exchange API surface: normalised symbols, `fetch_ohlcv`, unified order params, precision/limits per market. Model any broker abstraction on this |
| [hummingbot](https://github.com/hummingbot/hummingbot) | 19.7k | Market-making and inventory-skew logic, order-book event handling, connector architecture |
| [jesse](https://github.com/jesse-ai/jesse) | 8.4k | Clean strategy DSL, `should_long`/`go_long`/`update_position` lifecycle, built-in partial-exit handling |
| [rqalpha](https://github.com/ricequant/rqalpha) | 6.7k | Event-driven engine core, extensible mod system, multi-asset accounting |
| [OctoBot](https://github.com/Drakkar-Software/OctoBot) | 6.5k | Tentacle/plugin architecture, TradingView signal ingestion, paper-trading design |
| [Superalgos](https://github.com/Superalgos/Superalgos) | 5.6k | Visual strategy design, integrated charting + data mining concepts |
| [hftbacktest](https://github.com/nkaz001/hftbacktest) | 4.6k | The gold standard for realistic fills: queue position, latency modelling, L2/L3 book replay. Read this before claiming your backtest is realistic |
| [pybroker](https://github.com/edtechre/pybroker) | 3.5k | Walk-forward analysis with bootstrapped confidence intervals; ML-strategy plumbing |
| [NostalgiaForInfinity](https://github.com/iterativv/NostalgiaForInfinity) | 3.4k | A real, heavily-iterated production strategy — useful as a study of how many guard conditions live strategies actually need |

## Data sources

**Crypto (keyless public REST/WS)**
- Binance `api.binance.com/api/v3/klines`, depth, ticker; `fapi.binance.com` for
  perpetual funding rate and open interest.
- Bybit, OKX, Coinbase — same shape; `ccxt` normalises all of them.

**Equities / FX / futures / indices**
- `yfinance` — free OHLCV, intraday limited to ~60 days of 1m history; fine for
  research, not for production. Delisted tickers are missing (survivorship).
- Alpaca — free real-time IEX data + paper trading API.
- Polygon, Tiingo, Databento — paid, production-grade.

**Fundamentals / macro**
- FRED (`fred.stlouisfed.org`) — rates, yields, macro series. Free API key.
- Trading Economics / Finnhub / Investing.com — economic calendars.
- Binance futures endpoints — funding, OI, long/short ratio.

## Broker / execution APIs

| Broker | Asset classes | Paper trading |
|---|---|---|
| Alpaca | US equities, options, crypto | Yes, first class |
| Binance / Bybit / OKX | Crypto spot + perps | Testnet |
| Interactive Brokers | Everything | Yes (TWS paper account) |
| OANDA | FX, CFDs | Yes |

Always implement a local **paper broker** as well: it lets you run the full
strategy loop deterministically in tests with no network.

## Indicator libraries

- **TA-Lib** — C library, fastest, but a compiled dependency that breaks
  installs. Avoid as a hard requirement.
- **pandas-ta / bta-lib** — pure pandas, easy install, slower.
- **Hand-rolled** — for a focused system, ~200 lines of numpy covers EMA, ATR,
  RSI, ADX, Bollinger, VWAP and volume metrics with no dependency risk. Prefer
  this, and unit-test each one against known values.

## Charting front-ends

- **TradingView Lightweight Charts** — the standard for candlestick web UIs.
  ~50KB, canvas-based, free (Apache-2.0). Supports markers, price lines and
  overlays, which is exactly what a signal display needs.
- **Plotly / ECharts** — good for analytics and equity curves; heavier.
- **Recharts / visx** — React-native charting for dashboards, not candles.
