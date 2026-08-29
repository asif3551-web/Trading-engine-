---
name: trading-frontend-design
description: Design and build financial/trading front-ends — candlestick charts, live signal panels, order tickets, P&L and risk dashboards, market depth and watchlists. Use when building or restyling a trading UI, a market dashboard, an analytics panel, or any interface displaying prices, positions, equity curves or real-time streaming numbers.
---

# Trading Front-End Design

For charting use **TradingView Lightweight Charts** (Apache-2.0, ~50KB) — it is
the de-facto standard for financial candlestick UIs on the web and the only
free library that handles time-series panning, crosshairs, price lines and
markers well at scale. For layout/components, the shadcn/ui + Tailwind pattern
(the dominant modern dashboard stack) works well; keep it optional so the UI
degrades to plain HTML.

## Rules specific to financial UIs

Trading UIs are not ordinary dashboards. People make irreversible money
decisions from them in seconds, often under stress.

### 1. Never let a number be ambiguous

- **Always show the sign and the unit.** `+1.24%` and `-1.24%`, never a bare
  `1.24`. Show absolute and percentage together for P&L: `+$412.50 (+2.1%)`.
- **Fixed decimal places per instrument**, and never re-format on tick. A price
  jumping between `42150.5` and `42150.50` reads as a change when it isn't.
- **Tabular numerals** (`font-variant-numeric: tabular-nums`) on every price,
  size and P&L. Without it digits shift width on every tick and the column
  jitters — this is the single highest-impact CSS line in a trading UI.
- **Label the timezone** on every timestamp, and prefer UTC for markets.
- Never abbreviate a price. `1.2M` is fine for volume, never for a price.

### 2. Colour carries meaning, so it must be reliable

- Up/down is the only semantic colour pair that matters. Reserve green and red
  exclusively for direction and P&L sign — never for buttons, badges or status
  chips, or the eye stops trusting them.
- ~8% of men have red/green colour deficiency. Never encode direction in colour
  alone: pair it with the sign, an arrow, or position. Teal/amber is a safer
  pair than green/red and is common in professional terminals.
- Both themes must hold contrast. Dark is the default for trading UIs (long
  sessions, low light) but light must be equally legible — define both as token
  sets and never hard-code a colour outside them.
- Flash-on-update (brief background tint on tick) aids scanning, but must decay
  within ~300ms and must never move layout.

### 3. Density is a feature

Traders want more on screen, not less. Airy consumer-app spacing is wrong here.

- Compact rows (28-32px), 12-13px type for tables, generous only around the
  primary chart.
- The chart is the hero: give it the largest region and let it fill available
  height.
- Group by decision, not by data type: everything needed to act on one signal
  (direction, entry, stop, targets, R:R, size, reasons) belongs in one visually
  bounded card. Making someone assemble a decision from three panels is a design
  bug.

### 4. Real-time state must be visible

- **Connection status is mandatory** — live / reconnecting / stale, always
  on screen. A frozen price with no indicator is dangerous: users assume a flat
  market rather than a dead socket.
- Show the **last update time** for every data region.
- Distinguish *no data* from *zero* from *loading*. `0.00` and "no position" are
  different states and must not render identically.
- Streaming updates must not steal focus, reset scroll, or reorder rows the user
  is reading.

### 5. Destructive actions need friction

Order entry and anything that moves real money confirms first, states the full
consequence in the confirmation ("Buy 0.25 BTC at market, ~$26,400, risking
$132 to the stop at 104,200"), and clearly separates paper from live — a
persistent, unmissable banner when live trading is armed. Colour alone is not
enough for that distinction.

## Signal card anatomy

The core component. Everything needed to act, in one bounded block:

```
┌───────────────────────────────────────────────┐
│ ▲ LONG   BTC/USDT · 15m        confidence 78  │  direction + symbol + score
│ Liquidity sweep + FVG + bullish OB            │  why, in plain words
├───────────────────────────────────────────────┤
│ Entry    104,850.0                            │
│ Stop     104,190.0     -0.63%    risk 1R      │  stop with distance
│ TP1      105,510.0     +0.63%    1.0R  40%    │  each target: price, %, R, size
│ TP2      106,170.0     +1.26%    2.0R  35%    │
│ TP3      106,830.0     +1.89%    3.0R  25%    │
├───────────────────────────────────────────────┤
│ R:R 2.4  ·  size 0.048 BTC  ·  risk $250      │  the summary line
└───────────────────────────────────────────────┘
```

Stop and targets must be visually distinguishable at a glance — the stop is the
one number that must never be misread. Give it its own colour token and its own
row weight.

## Chart overlays for a liquidity-based system

Draw what the strategy actually reasoned about, or the user cannot audit it:

- Entry / stop / target **price lines** (Lightweight Charts `createPriceLine`),
  the stop dashed and distinctly coloured.
- **Markers** at signal bars (`setMarkers`) with direction arrows.
- **Liquidity zones** — order blocks and fair-value gaps as translucent boxes.
  Keep opacity low (~0.10-0.15); overlays must never obscure candles.
- **Swept levels** — mark equal highs/lows and strike through them once taken.
- Volume in a separate pane at ~20% height, never overlaid on price.

Cap the number of simultaneous overlays (~10-15). Beyond that the chart becomes
unreadable and users stop trusting any of it. Let the user toggle overlay groups.

## Layout that works

```
┌──────────────────────────────────────────────────────┐
│ header: symbol · timeframe · connection · mode badge │
├────────────────────────────────┬─────────────────────┤
│                                │  active signals     │
│   chart (hero, fills height)   │  (signal cards)     │
│                                ├─────────────────────┤
│                                │  positions / P&L    │
├────────────────────────────────┼─────────────────────┤
│ equity curve · metrics strip   │  risk budget        │
└────────────────────────────────┴─────────────────────┘
```

Below ~900px, stack: chart first, then signals, then positions. Never hide the
connection status or the live/paper badge in a responsive collapse.

## Accessibility and performance

- Every colour-coded state also carries text or an icon.
- Charts need a text alternative — a table view of the same data.
- Keyboard: `Esc` cancels an order ticket; focus must never be trapped in a
  streaming region.
- Throttle DOM updates to animation frames; batch ticks. Updating a row per
  message will melt the main thread on a busy feed.
- Never re-render the whole chart on a tick — use the library's incremental
  `update()` path.
- Respect `prefers-reduced-motion`: drop flash animations and transitions.

## References

- `references/lightweight-charts.md` — working setup, overlays, price lines,
  markers, live updates, theming.
- `references/design-tokens.md` — a complete dark/light token set for trading
  UIs, with the numeric formatting rules.
