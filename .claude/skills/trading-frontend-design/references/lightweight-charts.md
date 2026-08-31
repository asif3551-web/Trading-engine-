# TradingView Lightweight Charts — working reference

Apache-2.0, ~50KB gzipped. v5 is the current major line and changed the series
API from `chart.addCandlestickSeries()` to `chart.addSeries(CandlestickSeries, …)`.
Pin an exact version; the two APIs are not compatible.

CDN — use **jsdelivr or unpkg**, which mirror the npm package:

```html
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@5.0.9/dist/lightweight-charts.standalone.production.js"></script>
```

**cdnjs does not host lightweight-charts.** Requesting
`cdnjs.cloudflare.com/ajax/libs/lightweight-charts/...` returns 404 at every
version. This is worth knowing because the failure is easy to misread: a 404 on
the first source in a fallback chain looks exactly like a blocked corporate
proxy or an ad blocker, and sends you debugging the network instead of the URL.
Verify a CDN actually serves a library before putting it first in a chain.

## Create a chart

```js
const chart = LightweightCharts.createChart(el, {
  layout: {
    background: { color: '#0d1117' },
    textColor: '#8b949e',
    fontFamily: 'ui-sans-serif, system-ui, sans-serif',
    attributionLogo: false,
  },
  grid: {
    vertLines: { color: 'rgba(139,148,158,0.08)' },
    horzLines: { color: 'rgba(139,148,158,0.08)' },
  },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  rightPriceScale: { borderColor: 'rgba(139,148,158,0.2)' },
  timeScale: { borderColor: 'rgba(139,148,158,0.2)', timeVisible: true, secondsVisible: false },
  autoSize: true,   // v5: replaces manual resize handling
});

// v5 series API
const candles = chart.addSeries(LightweightCharts.CandlestickSeries, {
  upColor: '#26a69a', downColor: '#ef5350',
  borderUpColor: '#26a69a', borderDownColor: '#ef5350',
  wickUpColor: '#26a69a', wickDownColor: '#ef5350',
});

// v4 equivalent, if pinned to 4.x:
// const candles = chart.addCandlestickSeries({ ... });
```

## Data shape

`time` is a **UTC seconds** integer (or `'YYYY-MM-DD'` for daily). Data must be
sorted ascending with no duplicate timestamps, or the chart throws.

```js
candles.setData([
  { time: 1735689600, open: 93000, high: 93500, low: 92800, close: 93400 },
]);
```

## Volume in a separate pane

```js
const volume = chart.addSeries(LightweightCharts.HistogramSeries, {
  priceFormat: { type: 'volume' },
  priceScaleId: '',           // own scale
});
volume.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
volume.setData(bars.map(b => ({
  time: b.time, value: b.volume,
  color: b.close >= b.open ? 'rgba(38,166,154,0.5)' : 'rgba(239,83,80,0.5)',
})));
```

## Entry / stop / target price lines

```js
const lines = [];
function drawSignal(sig) {
  lines.forEach(l => candles.removePriceLine(l));
  lines.length = 0;
  const add = (price, color, title, dashed) => lines.push(candles.createPriceLine({
    price, color, title,
    lineWidth: 1,
    lineStyle: dashed ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid,
    axisLabelVisible: true,
  }));
  add(sig.entry, '#58a6ff', 'ENTRY', false);
  add(sig.stop_loss, '#f85149', 'STOP', true);     // dashed: the number that must not be misread
  sig.take_profits.forEach((tp, i) => add(tp.price, '#3fb950', `TP${i + 1} ${tp.r_multiple}R`, false));
}
```

Always remove old lines before drawing new ones — `createPriceLine` accumulates,
and a chart with 60 stale lines is the most common bug in these UIs.

## Markers

v5 moved markers to a plugin function; v4 used a series method.

```js
// v5
LightweightCharts.createSeriesMarkers(candles, [
  { time: sig.time, position: sig.side === 'long' ? 'belowBar' : 'aboveBar',
    color: sig.side === 'long' ? '#3fb950' : '#f85149',
    shape: sig.side === 'long' ? 'arrowUp' : 'arrowDown',
    text: `${sig.side.toUpperCase()} ${sig.rr}R` },
]);

// v4: candles.setMarkers([...])
```

## Liquidity zones (order blocks, fair value gaps)

There is no native box primitive. Two practical options:

1. **Two line series filled between** — draw the zone's top and bottom as
   `LineSeries` with `lineWidth: 1` and a low-opacity colour. Simple, robust.
2. **Custom series primitive** (v5 `attachPrimitive`) — full control, more code.

For most dashboards option 1 is enough:

```js
function drawZone(top, bottom, from, to, rgba) {
  [top, bottom].forEach(price => {
    const s = chart.addSeries(LightweightCharts.LineSeries,
      { color: rgba, lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
    s.setData([{ time: from, value: price }, { time: to, value: price }]);
    zoneSeries.push(s);
  });
}
```

Keep zone opacity at 0.10-0.15 so candles stay readable, and cap the count.

## Live updates

Never call `setData` on a tick — it resets the viewport and is O(n).

```js
// same timestamp -> updates the forming bar; newer timestamp -> appends
candles.update({ time, open, high, low, close });
volume.update({ time, value, color });
```

`update()` requires the time to be >= the last bar's time. Out-of-order ticks
throw, so drop or buffer late messages.

## Theming

Swap options on both the chart and the series; don't rebuild the chart.

```js
chart.applyOptions({
  layout: { background: { color: t.bg }, textColor: t.textMuted },
  grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
});
```

Hook this to a `matchMedia('(prefers-color-scheme: dark)')` listener plus an
explicit user toggle, mirroring the CSS token set.

## Gotchas

- The container needs an explicit height. With `autoSize` and a zero-height
  parent the chart renders as a 0px strip and looks broken.
- `time` in seconds, not milliseconds. `Date.now()/1000 | 0`.
- Duplicate or unsorted timestamps throw on `setData`.
- Call `chart.remove()` on unmount, or you leak canvases and resize observers.
- `chart.timeScale().fitContent()` after `setData`, not after every `update()`.
