# Metrics, sizing and R-multiple math

## Position sizing

```
risk_amount   = equity * risk_per_trade
stop_distance = abs(entry - stop_loss)
raw_size      = risk_amount / stop_distance
size          = min(raw_size,
                    max_notional / entry,          # exposure cap
                    adv_participation * avg_volume) # liquidity cap
```

For leveraged/contract markets, multiply by contract size and check margin:
`notional = size * entry`, `margin = notional / leverage`.

**Volatility targeting** — normalise size so each position contributes equal
volatility rather than equal nominal risk:

```
size = (equity * target_vol) / (annualised_vol_of_asset * entry)
```

**Kelly** — `f* = W - (1-W)/R` where W is win rate and R the win/loss ratio.
Full Kelly is far too aggressive for real trading because W and R are estimated
with error. Use quarter-Kelly at most, and always cap by the per-trade risk
limit above.

## R-multiples

```
R          = abs(entry - stop_loss)                      # 1R in price terms
target_R   = (target - entry) / R                        # long
target_R   = (entry - target) / R                        # short
realised_R = (exit - entry) / R * direction
```

For a scaled exit with weights `w_i` summing to 1 at targets `R_i`, with the
stop moved to break-even after the first fill:

```
E[R | first target hit] = sum(w_i * R_i * P(reach i | reach 1))
```

Practical ladder used by this engine: TP1 at 1R (take 40%, move stop to
break-even), TP2 at 2R (take 35%), TP3 at 3R+ (runner, 25%). If all three fill:
`0.40*1 + 0.35*2 + 0.25*3 = 1.85R`. If only TP1 fills and the rest stop out at
break-even: `+0.40R`. A full stop-out before TP1: `-1.00R`.

## Expectancy

```
E[R] = win_rate * avg_win_R - (1 - win_rate) * avg_loss_R
```

Break-even win rate for a given reward:risk `R`: `W_be = 1 / (1 + R)`.

| Reward:risk | Break-even win rate |
|---|---|
| 1:1 | 50.0% |
| 1.5:1 | 40.0% |
| 2:1 | 33.3% |
| 3:1 | 25.0% |

This is the entire argument for a 2-3R minimum: it buys tolerance for being
wrong most of the time.

## Performance metrics

Let `r_t` be periodic returns, `N` periods per year (252 daily, 252*24 hourly
crypto is wrong — use 365*24 for 24/7 markets).

```
CAGR        = (final/initial)^(N/periods) - 1
Sharpe      = (mean(r) - rf/N) / std(r) * sqrt(N)
Sortino     = (mean(r) - rf/N) / std(min(r,0)) * sqrt(N)
MaxDD       = min(equity/cummax(equity) - 1)
Calmar      = CAGR / abs(MaxDD)
ProfitFactor= sum(wins) / abs(sum(losses))
WinRate     = count(pnl>0) / count(trades)
Exposure    = bars_in_market / total_bars
```

**MAE/MFE** — maximum adverse/favourable excursion per trade. MAE tells you
whether stops are too tight (winners repeatedly dip near the stop) and MFE
whether targets are too far (price consistently reverses just short of TP3).
These two diagnose a strategy faster than any headline metric.

## Sanity thresholds

- Sharpe > 3 on daily bars → look for lookahead bias before celebrating.
- Profit factor > 3 with < 100 trades → almost certainly overfit.
- Zero losing months → the cost model is missing something.
- Max drawdown < 5% with > 30% CAGR → check that stops are actually filling.

## Drawdown and losing-streak math

Probability of a streak of `k` consecutive losses at win rate `W` over `n`
trades is approximately `n * (1-W)^k`. At W=35%, over 200 trades, a run of 10
losses has probability `200 * 0.65^10 ≈ 27%` — i.e. it should be *expected*, not
treated as system failure. At 1% risk per trade that streak is a ~10% drawdown;
at 5% risk it is a ~40% drawdown and the account is unlikely to recover.

Recovery required after a drawdown of `d`: `1/(1-d) - 1`. A 20% drawdown needs
+25%; a 50% drawdown needs +100%.
