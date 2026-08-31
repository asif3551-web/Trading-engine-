# Trading Engine

A liquidity-driven systematic trading system for crypto, equities, FX, indices
and futures. It generates trade signals from market liquidity structure, sizes
every position from its stop, backtests without lookahead, and trades
automatically on paper or live through one shared code path.

```
python -m trading_engine scan      --symbol BTC/USDT
python -m trading_engine backtest  --symbol BTC/USDT --bars 3000
python -m trading_engine symbols                    # what markets are supported
python -m trading_engine scan      --symbol XAUUSD  # gold; also XAU/USD or GOLD
python -m trading_engine tune      --symbol BTC/USDT --bars 20000
python -m trading_engine serve                      # dashboard on :8000
```

Crypto, metals, forex, indices, energy and equities. Crypto is real time via
Binance; the rest come from Yahoo ~15 minutes delayed — see
[What "live data" actually means](#what-live-data-actually-means-per-market).

Paper trading is the default everywhere. Live trading needs three explicit
opt-ins (see [Auto-trading](#auto-trading)); nothing here can place a real order
by accident.

---

## What this system does and does not promise

Read this first. It is the most important section in the file.

**What the engine controls:** the *geometry* of every trade. It will not open a
position unless the furthest take-profit sits at least **2R** away — two times
the distance to the stop — with the ladder aiming for **3R**, and it rejects the
setup outright if the nearest opposing liquidity is too close for that to be
realistic. Risk per trade is a fixed fraction of equity, so a losing trade costs
a known amount and a winner pays a multiple of it.

**What no system controls:** the hit rate. Whether price reaches the target is a
property of the market, not of this code. A "2-3x return on the risked amount"
is therefore a statement about **reward-to-risk per trade**, which the engine
enforces, and not a statement about account returns, which nobody can guarantee.

The two are connected by expectancy:

```
E[R] = win_rate × avg_win_R − (1 − win_rate) × avg_loss_R
```

At an average 2.5R win against 1R losses, a **35% win rate is profitable**
(0.35 × 2.5 − 0.65 × 1 = **+0.225R per trade**). That is the entire argument for
insisting on 2-3R: it buys tolerance for being wrong most of the time.

| Reward:risk | Win rate needed just to break even |
|---|---|
| 1:1 | 50.0% |
| 2:1 | 33.3% |
| 3:1 | 25.0% |

And the cost of being wrong repeatedly, which any honest system must state: at a
35% win rate over 200 trades, a run of **10 consecutive losses is expected**, not
exceptional. At the default 0.5% risk per trade that is a ~5% drawdown. At 5%
risk per trade it is a ~40% drawdown, and a 50% drawdown requires a +100% gain
to recover. This is why the per-trade risk default is small and why the drawdown
throttle exists.

Anyone promising a guaranteed multiple of your capital is selling something.
This engine enforces discipline; it does not manufacture edge.

---

## How it decides

The strategy trades **failed breakouts**, not breakouts.

Clusters of equal highs and lows are where stop-loss orders accumulate. Price is
repeatedly drawn into those clusters, takes the stops out, and reverses —
because that resting liquidity is exactly what large participants needed to fill
against. The tradeable event is the **sweep and reclaim**: price penetrates the
pool, then closes back inside the prior range.

That specific event is the trigger because it is the only common setup that
hands you a *precise structural invalidation* at the same moment it gives you a
direction. The stop goes just beyond the sweep wick. A tight stop is not a
cosmetic detail — it is the whole reason a 3R target can sit inside a normal
day's range instead of requiring an exceptional move.

A trade is taken only when independent things agree:

| Layer | What it contributes |
|---|---|
| **Liquidity sweep** | The trigger, the direction, and the stop location |
| **Market structure** | BOS/CHoCH and swing sequence must support the side |
| **Zones** | A fresh order block or fair-value gap to enter against |
| **Volume profile** | Position vs POC and the value area |
| **Order book depth** | Real-time confirmation only — never a trigger, since depth is spoofable |
| **Higher timeframe** | Must not be actively fighting the trade |
| **Fundamentals** | Can veto, but never chooses direction |
| **Geometry** | Must clear the 2R floor after costs, or the setup is dropped |

Each alone is weak. Requiring confluence cuts trade count hard — that is the
point. The edge is in selectivity.

### Fundamentals are a veto, not a signal

Fundamentals rarely time entries well, but they veto reliably. No new risk is
opened into a scheduled high-impact release (CPI, FOMC, NFP, earnings): spreads
widen, stops get skipped, and slippage is unbounded. No technical setup is good
enough to pay for that. Beyond the blackout the engine reads macro regime
(DXY, yields, VIX), and for crypto the positioning data that is the closest
thing it has to fundamentals — funding rate, open interest, basis, long/short
ratio. Extreme funding is a *crowding warning*: the side paying to hold is the
side that gets squeezed.

---

## Risk management

Position size is always *derived* from the stop. Any code path returning a size
without consulting the stop would be a bug:

```
risk_amount   = equity × risk_per_trade
stop_distance = |entry − stop_loss|
position_size = risk_amount / stop_distance
```

A wide stop gives a small position. The dollar loss stays constant; the size
varies. Sizing is then capped by exposure, leverage, and available liquidity
(never more than ~1% of average volume, or the slippage on the way out destroys
the edge).

Sizing is only the first layer. Per-trade risk alone does not bound the damage —
five *correlated* 1% positions behave as one 5% position that can gap through
every stop at once. So the engine also enforces:

| Guardrail | Default | Why |
|---|---|---|
| Risk per trade | 0.5% | Survives a long losing streak |
| Portfolio heat | 6% | Total open risk if everything stops out together |
| Correlated positions | 2 | Correlated positions are one position |
| Daily loss limit | 3% | Stops revenge trading, a human failure the code must enforce |
| Drawdown throttle | from 10% | Size scales down linearly as drawdown deepens |
| Hard stop | 20% | System halts entirely |
| Consecutive losses | 5 | Forced cool-off |
| Kill switch | file on disk | Flattens everything; deliberately the simplest possible mechanism, so it works when nothing else does |

**Exit ladder.** TP1 at 1R closes 40% and moves the stop to break-even — from
there the trade cannot lose. TP2 at 2R closes 35%. TP3 at 3R+ runs the remaining
25%, trailed by ATR. If everything fills, the trade returns 1.85R weighted; if
only TP1 fills and the rest exit at break-even, +0.40R; a clean stop-out, −1.00R.

This is why the engine enforces **two** reward:risk thresholds. Gating only on
the weighted average would reject every laddered exit (the default ladder
averages 1.85R, below 2.0). Gating only on the furthest target would let a trade
that banks 90% at 0.5R advertise itself as "3R". Both are checked.

---

## Backtesting you can believe

An untaxed, forward-peeking backtest is fiction. This one is deliberately
pessimistic:

- **No lookahead.** A signal from bar `t`'s close fills at bar `t+1`'s open.
- **Stops win ties.** If a bar's range touches both the stop and a target,
  the stop is assumed to have filled first. Without intrabar data you cannot
  know the order, and optimism here is exactly what makes losing systems
  backtest profitably.
- **Costs on every fill** — maker/taker fees, spread, and slippage that scales
  with volatility (stop-outs happen in fast markets, where slippage is worst).
- **Limit entries only fill** if price actually traded through the limit.
- **Metrics carry their own caveats.** A Sharpe above 3, a profit factor above 3
  on a small sample, or zero recorded fees produce explicit warnings printed
  alongside the results, not buried in a footnote.

The test suite enforces the causality claim rather than asserting it. The
load-bearing test runs the backtester over a prefix of the data and over the
full series, and requires the trades in the shared window to be **identical**.
It has already caught a real bug: liquidity pool clustering was absorbing future
swings, letting target placement consult levels that had not formed yet.

Walk-forward analysis is built in, because a single train/test split is
overfitted the moment you look at the test result twice:

```
python -m trading_engine walkforward --symbol BTC/USDT --bars 8000
```

A strategy that is positive in-sample and negative out-of-sample is fitted, not
predictive. Consistency across windows matters more than the average.

---

## Tuning, and an honest word on edge

The engine's *machinery* is tested: no lookahead, honest accounting, enforced
risk geometry. Whether its **entry** has an edge on your market is a separate
question, and one that cannot be answered by reasoning — only by measurement on
that market's real data.

That distinction matters because of a result worth reporting. Sweeping the exit
management (when the stop moves to break-even, how the ladder is weighted) moves
win rate and win size around exactly as theory predicts — pushing break-even
later took average wins from 0.94R to 1.58R and hit rate from 53% to 34% — but
**expectancy barely moved**. No exit scheme rescues an entry without edge; it
only trades hit rate against win size.

So the defaults here are deliberately *not* tuned to any particular market. Tune
them yourself, out-of-sample:

```bash
python -m trading_engine tune --symbol BTC/USDT --bars 20000
```

The sweep ranks candidates on **out-of-sample expectancy only**, disqualifies
settings with too few out-of-sample trades, and weights by how many windows
stayed positive — a setting that is superb in one window and awful in three is
worse than a mediocre one that holds everywhere. If nothing comes out positive,
it says so plainly rather than crowning a "best" loser.

### The diagnostic that matters most

Compare **Avg peak** (MFE) with **Avg win** in any backtest or on the dashboard:

- Peak ≫ win → winners are being cut. Targets or trailing are too tight.
- Peak ≈ win → exits are efficient; if expectancy is still negative, the problem
  is the entry.
- Avg worst (MAE) near −1R → trades sit on the stop from the start, meaning
  entries are early or stops are too tight.

This pair diagnoses a strategy faster than any headline metric, which is why
both are printed with the results.

---

## Step-by-step: running it across crypto, metals and forex

### 1. Install

```bash
git clone -b claude/trading-system-live-signals-gwcirh https://github.com/asif3551-web/Trading-engine-.git
cd Trading-engine-
pip install -r requirements.txt
pip install yfinance PyYAML pyarrow          # yfinance is REQUIRED for metals/FX
python -m trading_engine vendor-chart        # chart works offline afterwards
```

`yfinance` is not optional if you want gold, silver or forex — those come from
Yahoo. Crypto works without it.

### 2. Check a symbol resolves before relying on it

```bash
python -m trading_engine symbols                      # everything supported
python -m trading_engine symbols XAUUSD,EURUSD,BTC/USDT
```

```
  XAUUSD
    provider   yfinance:GC=F
    market     Gold (COMEX front future)
    timing     ~15 min delayed
    volume     yes
```

All of these spell gold: `XAUUSD`, `XAU/USD`, `GOLD`, `xau`. Silver is `XAGUSD`
or `SILVER`; forex is `EURUSD` or `EUR/USD`; crypto is `BTC/USDT` or just `BTC`.
Anything not listed is passed to Yahoo verbatim, so `AAPL`, `^VIX` and `ES=F`
work too.

### 3. Look at one signal

```bash
python -m trading_engine scan --symbol XAUUSD --timeframe 15m
```

Prints entry, stop, the TP ladder, reward:risk, and the confluence reasons — or
names the gate that rejected it. `NO SIGNAL` is the normal answer most of the
time; the engine takes roughly one trade per 200 bars by design.

### 4. Backtest it

```bash
python -m trading_engine backtest --symbol XAUUSD --bars 3000
python -m trading_engine backtest --symbol EURUSD --timeframe 1h --bars 3000
```

Yahoo caps intraday history at ~60 days, so for a longer sample use `1h` or
`1d`. Crypto has no such limit — `--bars 20000` on 15m works fine.

### 5. Run the dashboard with paper trading

```bash
python -m trading_engine serve --symbol BTC/USDT,ETH/USDT,XAUUSD,XAGUSD,EURUSD
```

Open <http://127.0.0.1:8000>. Paper trading is on by default. The symbol box is
free text, so you can chart anything without restarting; the watchlist above is
what the autotrader actually trades.

### 6. Validate before trusting it

```bash
python -m trading_engine tune --symbol XAUUSD --timeframe 1h --bars 8000
python -m trading_engine walkforward --symbol BTC/USDT --bars 20000
```

---

## What "live data" actually means per market

Be clear-eyed about this before wiring money to it:

| Market | Source | Timing | Volume | Suitable for |
|---|---|---|---|---|
| **Crypto** | Binance public API | **Real time** | Yes | Live signals, paper, live trading |
| **Metals** (XAU, XAG, XPT, XCU) | Yahoo, COMEX futures | **~15 min delayed** | Yes | Signals, paper, research |
| **Forex** | Yahoo spot | **~15 min delayed** | **None** | Signals, paper, research |
| **Indices / Energy** | Yahoo | ~15 min delayed | Mixed | Signals, paper, research |
| **Equities** | Yahoo | ~15 min delayed | Yes | Signals, paper, research |

Only crypto is genuinely real time. **A 15-minute delay is fine for research and
paper trading on 15m or higher bars, and is not adequate for live execution** —
you would be acting on prices that have already moved. If you want to trade
metals or FX live, you need a broker feed (OANDA, Interactive Brokers, Twelve
Data, Polygon). The `DataFeed` interface in `trading_engine/data/feeds.py` is
three methods; adding one is a small job, and the engine treats it identically.

Three consequences the engine handles for you, because each one caused a real
failure during development:

1. **Delay-aware staleness.** The live trader adds the provider's publication
   delay to its staleness allowance. Without it Yahoo's 15 minutes tripped the
   "data is stale — no new risk" guard on every poll, and metals and FX never
   traded at all.
2. **Volume-free markets are not handicapped.** Spot FX has no volume, which
   disables three confluence components. Those components only ever *add*
   points, so leaving them out silently docked FX ~30 points against a fixed
   threshold. The score is renormalised over the components actually available.
3. **Futures over spot for metals.** `XAUUSD` resolves to `GC=F`, not
   `XAUUSD=X`, because COMEX gold carries real volume and dense intraday bars
   while Yahoo's spot gold has neither. Ask for `XAUUSD.SPOT` if you want spot.

### Market hours

Crypto trades 24/7. Metals, FX, indices and equities close at weekends and
observe session breaks, so expect `no data` or a stale feed outside hours — that
is the market being shut, not a fault. Forex runs Sunday 22:00 to Friday 22:00
UTC; COMEX metals have a daily maintenance break around 22:00-23:00 UTC.

---

## Installation

```bash
git clone https://github.com/asif3551-web/trading-engine-.git
cd trading-engine-
pip install -r requirements.txt        # numpy + pandas is all that is required
```

Optional extras: `yfinance` for equities/FX/indices, `PyYAML` for config files,
`pyarrow` for the on-disk data cache, `pytest` to run the suite.

---

## Usage

### Signal for the current bar

```bash
python -m trading_engine scan --symbol BTC/USDT --timeframe 15m
```

```
BTC/USDT · 15m · last 104,912

  LONG  confidence 74/100

  Entry       104,850.0
  Stop        104,190.0   (0.63% away, 1R)
  TP1         105,510.0   (+0.63%, 1.00R, exit 40%)
  TP2         106,170.0   (+1.26%, 2.00R, exit 35%)
  TP3         106,830.0   (+1.89%, 3.00R, exit 25%)

  Reward:risk 1.85 weighted · 3.00R at the furthest target
  Break-even win rate needed: 35.1%

  Why:
    - swept sell-side liquidity at 104,180 (3 touches, quality 0.78)
    - price reclaimed the swept level (rejection confirmed)
    - bullish structure: higher highs and higher lows
    - price at order block demand zone 104,760-104,890
    - unswept buy-side liquidity 3.2 ATR above at 106,900
```

Every signal states its own reasoning. If you cannot audit why a trade was
taken, you cannot tell a broken system from an unlucky one.

### Backtest and dashboard

```bash
python -m trading_engine backtest --symbol BTC/USDT --bars 5000
python -m trading_engine serve                  # http://127.0.0.1:8000

# Watch several markets — these become the autotrader's watchlist
python -m trading_engine serve --symbol BTC/USDT,ETH/USDT,SOL/USDT

# Dashboard only, no paper trading loop
python -m trading_engine serve --no-trade
```

The chart uses TradingView Lightweight Charts, loaded from a CDN. Corporate
proxies, ad blockers and offline machines all block CDNs, so the dashboard tries
a local copy first, then three CDNs, and if all fail it drops **only** the chart
— prices, signals, positions and risk stay live. For a permanently offline copy:

```bash
python -m trading_engine vendor-chart          # ~50KB into frontend/vendor/
```

The dashboard runs the **paper** autotrader by default and shows: the chart with
entry/stop/target lines and liquidity zone overlays, active signal cards, open
positions, live risk budget, backtest results with their warnings, and a **"Why
no signal"** panel naming the exact gate that rejected the latest bar — so a
quiet engine is visibly working rather than indistinguishable from a dead one.
The symbol box is free text: any symbol the feed resolves can be charted, and the
page says when you are charting something outside the autotrader's watchlist. It binds to localhost by default — the API is
unauthenticated and can close positions, so it must not be exposed to a network.

### Auto-trading

Paper is the default, and paper runs the *same* code path as live:

```bash
python -m trading_engine trade --symbol BTC/USDT           # paper
python -m trading_engine trade --symbol BTC/USDT --live    # real money
```

Live requires API keys in the environment (never in config files), an explicit
`--live` flag, an armed broker, and a typed confirmation. Three independent
gates, because the failure mode of a mis-wired config is unbounded.

```bash
export BINANCE_API_KEY=...
export BINANCE_API_SECRET=...
```

The live loop **fails closed**: stale data stops new positions; the protective
stop is submitted with the entry, and a position whose stop cannot be placed is
closed immediately rather than left naked; repeated errors halt the system,
because trading on unknown state is worse than not trading; and `touch .state/KILL`
flattens everything.

---

## Data sources

| Provider | Coverage | Notes |
|---|---|---|
| `binance` | Crypto spot + perp funding/OI | Public endpoints, no key needed, real time |
| `yfinance` | Metals, FX, indices, energy, equities | Needed for anything non-crypto; ~15 min delayed, ~60 days of intraday history, and **delisted tickers are absent, so long backtests carry survivorship bias** |
| `csv:<dir>` | Your own data | `SYMBOL_TIMEFRAME.csv` |
| `synthetic` | Generated bars | Deterministic; development and tests only |

The **synthetic feed is not a demo of profitability.** It is a seeded generator
used so the engine and its tests run with no network. Results on it show that
the code works and say nothing about whether the strategy has an edge. Every
surface that can serve synthetic data says so explicitly — the CLI prints a
warning, the API returns `is_synthetic`, and the dashboard shows a banner.

Data quality is enforced on ingest: duplicate or unsorted timestamps and
impossible bars (high below low, close outside the range) are rejected rather
than passed downstream, because bad data produces confident, wrong signals.

Fetched bars are cached on disk, and the cache judges freshness from the **data**
rather than the file's age: an entry is reused only while its newest bar is still
the currently forming one. Expiring on file age instead is what made an earlier
version serve ~1800s-stale bars to the live trader, which then correctly declared
the feed dead and refused to trade — `no new risk` forever. Historical bars never
change, so repeated backtests over the same window still avoid the network.

---

## Configuration

Defaults are deliberately conservative. Override with YAML:

```yaml
risk:
  risk_per_trade: 0.005
  max_portfolio_heat: 0.06
  min_reward_risk: 2.0      # furthest target, in R
  min_expected_r: 1.5       # size-weighted ladder average, in R
  daily_loss_limit: 0.03

strategy:
  timeframe: "15m"
  htf_timeframe: "4h"
  min_confidence: 55.0
  tp_ladder: [1.0, 2.0, 3.0]
  tp_sizes: [0.40, 0.35, 0.25]
```

```bash
python -m trading_engine backtest -c config.yaml
```

Configuration is validated on load, and impossible combinations are rejected
with an explanation rather than silently producing zero trades. For example, a
ladder averaging 1.85R against a 2.5R weighted floor fails immediately, naming
the conflict — that exact misconfiguration cost real debugging time during
development, which is why the check exists.

---

## Project layout

```
trading_engine/
  core/types.py           Signal, Position, Trade, Order — the domain model
  indicators/core.py      Causal indicators on numpy/pandas, no TA-Lib
  liquidity/
    structure.py          Swings, BOS/CHoCH, trend state
    pools.py              Liquidity pools and sweep detection
    zones.py              Order blocks, fair value gaps, volume profile
    analyzer.py           Fuses the above into a per-bar liquidity read
  fundamentals/context.py Event blackout, macro regime, crypto positioning
  risk/
    manager.py            Sizing and the portfolio guardrails
    levels.py             Stop and target placement — the 2-3R geometry
  strategy/               Confluence scoring and signal generation
  backtest/               Event-driven engine, metrics, walk-forward, tuner
  live/                   Broker adapters and the autotrader
  api/server.py           REST API and dashboard host
frontend/                 Dashboard (Lightweight Charts)
tests/                    173 tests
.claude/skills/           Reusable skills distilled from top-rated projects
```

### Skills

`.claude/skills/` contains two skills distilled from the highest-rated
open-source work in each area, so the practices survive beyond this repo:

- **`quant-trading-systems`** — drawn from freqtrade (53.8k), ccxt (43.8k),
  hummingbot (19.7k), jesse (8.4k), rqalpha (6.7k), OctoBot (6.5k),
  hftbacktest (4.6k) and pybroker (3.5k). Covers lookahead traps, cost
  modelling, liquidity concepts, risk layers, and honest performance claims.
- **`trading-frontend-design`** — built on TradingView Lightweight Charts and
  the shadcn/Tailwind dashboard conventions. Covers the rules specific to
  financial UIs: unambiguous numbers, tabular numerals, reserved direction
  colours, mandatory connection status, and friction on money-moving actions.

---

## Testing

```bash
python -m pytest tests/ -q      # 173 tests
```

The suite covers indicator correctness and causality, structure and sweep
detection, every risk guardrail, backtest accounting (equity change must equal
the sum of trade P&L exactly), and the paper broker's idempotency.

---

## Limitations

Stated plainly, because a system's limits matter more than its features:

- **Backtest fills are modelled, not real.** Without tick and order-book replay,
  intrabar sequencing is an assumption. The pessimistic choice is made
  throughout, but it remains an assumption.
- **Single-position-per-symbol** in the backtester. Portfolio-level backtesting
  across simultaneous positions is not implemented; the live risk manager does
  handle multiple positions.
- **No slippage model for size.** Market impact is approximated by a volume cap,
  not modelled from the book.
- **Fundamentals need wiring.** The blackout, macro and crypto scoring are
  implemented, but the economic calendar has no bundled data source. Without one
  the blackout check does not run, and the engine says so in its warnings rather
  than pretending the calendar was clear.
- **Not financial advice.** This is software. Test on paper, understand every
  guardrail, and never risk money you cannot afford to lose.

## Licence

MIT
