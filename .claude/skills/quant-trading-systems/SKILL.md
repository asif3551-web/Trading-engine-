---
name: quant-trading-systems
description: Build, review, or debug systematic trading systems — signal generation, liquidity/market-structure analysis, risk sizing, backtesting, and live/auto execution. Use when working on trading strategies, backtest engines, broker adapters, position sizing, order management, market data feeds, or when the user mentions entry/stoploss/take-profit, R-multiples, drawdown, Sharpe, order blocks, fair value gaps, liquidity sweeps, funding rates, or slippage modelling.
---

# Quant Trading Systems

Distilled from the highest-rated open-source trading projects: freqtrade (53.8k),
ccxt (43.8k), hummingbot (19.7k), jesse (8.4k), rqalpha (6.7k), OctoBot (6.5k),
Superalgos (5.6k), hftbacktest (4.6k), pybroker (3.5k). See
`references/ecosystem.md` for what each one is worth borrowing from.

## The non-negotiables

Every one of these has sunk a real trading system. Check them before anything else.

### 1. Never look ahead

The single most common way a backtest lies. A bar's close is not knowable while
the bar is forming.

- Decide on bar `t`, fill on bar `t+1`'s open. Never fill at the close of the
  bar whose close you used to decide.
- Indicators must be causal. `rolling(...).mean()` is fine; `center=True` is not.
- Normalisation, scaling and feature selection fit on training data only —
  fitting a scaler on the full series leaks the future into the past.
- Resampling to a higher timeframe must use *closed* higher-timeframe bars. A
  4h bar is only usable after 4h have elapsed, not at the start.
- If a stop and a target are both touched inside one bar, assume the **stop**
  filled first unless you have intrabar data. Optimism here is how a losing
  system backtests profitably.

### 2. Model costs or don't bother

An untaxed backtest is fiction. Always include:

- **Commission/fees** — maker vs taker matters; a strategy that assumes maker
  fills but sends market orders is mispriced.
- **Slippage** — scale with volatility and size relative to available depth, not
  a flat number of ticks.
- **Spread** — buy the ask, sell the bid.
- **Funding/borrow** for perpetuals and shorts, accrued per period held.

A strategy whose edge disappears under realistic costs never had an edge.

### 3. Survivorship and reconstruction bias

Backtesting today's index constituents over ten years tests the survivors.
Delisted tickers, and the symbol universe as it stood at each point in time,
must be part of the data.

### 4. Risk is defined before entry, not after

Position size follows from the stop distance; it is never a fixed quantity.

```
risk_amount = equity * risk_per_trade          # e.g. 0.005 -> 0.5%
stop_distance = abs(entry - stop_loss)
position_size = risk_amount / stop_distance    # then cap by exposure & liquidity
```

If the stop is wide, the position is small. The loss is constant; the size
varies. Any code path that produces a size without consulting the stop is a bug.

## Signal contract

A signal that does not carry its own exit is not a signal. Every emitted signal
must state, before entry: direction, entry price, stop loss, take-profit ladder,
the reward/risk ratio, position size, and *why* — the confluences that fired.
An R-multiple is `(target - entry) / (entry - stop)` for longs. Reject any setup
whose realistic first target is under ~1.5R and whose ladder cannot reach 2-3R
before the next opposing liquidity level; a low-R setup with a high hit rate is
usually a stop-loss that is too wide.

Expectancy is what matters, and it must be stated in R:

```
E[R] = win_rate * avg_win_R - (1 - win_rate) * avg_loss_R
```

At 2.5R average wins and 1R losses, a 35% win rate is profitable
(0.35*2.5 - 0.65*1 = +0.225R per trade). Marketing a system on win rate rather
than expectancy is the classic tell of a bad one.

## Liquidity-based analysis

Price moves toward resting orders. The tradeable structures:

- **Swing points** — a fractal high/low with `n` bars on each side. Everything
  else is built on these.
- **Liquidity pools** — clusters of equal highs/lows where stops rest. Price
  is drawn to them.
- **Sweep / stop hunt** — price penetrates a pool then closes back inside the
  prior range. This is the highest-quality entry trigger: the sweep supplies the
  liquidity for the reversal, and it gives you a *tight, structural* stop just
  beyond the wick, which is what makes 3R reachable.
- **BOS (break of structure)** — a close beyond the prior swing in the trend
  direction: continuation. **CHoCH (change of character)** — the first break
  against the prevailing structure: possible reversal.
- **Fair value gap / imbalance** — a three-bar pattern where bar 1's and bar 3's
  ranges do not overlap. Unfilled gaps act as magnets and as entry zones.
- **Order block** — the last opposing candle before an impulsive displacement
  move. Its range is a demand/supply zone.
- **Volume profile** — POC, value area high/low. High-volume nodes attract and
  stall price; low-volume nodes are traversed fast.
- **Order book depth imbalance** — `(bid_vol - ask_vol) / (bid_vol + ask_vol)`
  over the top N levels. Real-time pressure, but spoofable; treat as a
  confirming filter, never a standalone trigger.

Confluence beats any single structure. Score setups, require a threshold, and
weight the score by whether the higher timeframe agrees.

## Fundamentals as a filter

Fundamentals rarely time entries well but they veto trades reliably:

- **Event blackout** — do not open new risk into a scheduled high-impact release
  (CPI, FOMC, NFP, earnings). Spreads widen, stops get skipped, slippage is
  unbounded. Flatten or stand aside.
- **Regime** — trend-following works in trending, risk-on regimes; mean
  reversion works in range-bound ones. Detect the regime and switch, or trade
  only the one you have an edge in.
- **Crypto**: funding rate (extreme positive = crowded longs = squeeze risk),
  open interest (rising OI + rising price = real trend; rising OI + flat price
  = building squeeze), basis, long/short ratio.
- **Equities**: earnings date proximity, sector relative strength.
- **FX**: rate differentials, central-bank stance, DXY direction.

## Risk management layers

Sizing is only the first layer. A complete system also has:

1. **Per-trade risk** — 0.25%-1% of equity. Above 2% a normal losing streak is
   fatal.
2. **Portfolio heat** — total risk across open positions, capped (e.g. 3-6%).
3. **Correlation** — correlated positions are one position. Count them together.
4. **Daily loss limit** — stop trading for the day at e.g. -3%. This exists to
   stop revenge trading, which is a human failure mode the code must enforce.
5. **Drawdown throttle** — halve size at -10% peak-to-trough, stop at -20%.
6. **Kill switch** — a hard flatten-everything on data staleness, broker error
   storms, or a breach of any of the above.

Move to break-even after TP1 fills; then the trade is free and the remaining
runner carries the 3R expectancy.

## Backtesting that you can trust

- **Event-driven, bar by bar.** Vectorised backtests are fast for research but
  systematically over-report because they cannot model intrabar stop/target
  ordering or position lifecycle. Use vectorised for screening, event-driven
  for the number you actually believe.
- **Walk-forward** over in-sample/out-of-sample splits. A single train/test
  split gets overfitted the moment you look at the test result twice.
- Report **Sharpe, Sortino, Calmar, max drawdown, profit factor, expectancy in
  R, MAE/MFE, exposure, and trade count**. A Sharpe on 20 trades is noise;
  below ~100 trades treat every metric as a rumour.
- Be suspicious of your own results. A backtest Sharpe above ~3 on daily bars
  almost always means a bug — look for lookahead first.

## Live execution

- **Idempotency.** Every order carries a client order ID. Reconnects re-fetch
  state and reconcile rather than re-sending.
- **The broker is the source of truth**, not local state. Reconcile positions on
  every start-up and after every disconnect.
- **Bracket/OCO orders.** Submit the stop with the entry, not after. A fill
  without a resting stop is naked risk for as long as the gap lasts.
- **Fail closed.** On stale data, unknown state, or repeated errors: stop
  opening, and flatten if the stop is unconfirmed. Never fail open.
- **Paper trade first**, on the same code path as live. If paper and live are
  different code, you have tested neither.
- Rate-limit, back off exponentially, and never busy-poll a WebSocket you could
  subscribe to.

## Honest claims

Never state or imply a guaranteed return. What a system controls is the
**reward-to-risk ratio per trade** and the risk per trade; what it cannot
control is the hit rate, which is a property of the market. "Targets 3R with a
minimum 2R filter" is a true statement about design. "Produces 3x returns" is
not, and writing it into a README is how a project becomes indefensible. Include
the drawdown and the losing-streak math alongside any performance figure.

## References

- `references/ecosystem.md` — what to borrow from which top-rated project, and
  the data/broker API landscape.
- `references/metrics.md` — exact formulas for every performance metric, plus
  the position-sizing and R-multiple math.
