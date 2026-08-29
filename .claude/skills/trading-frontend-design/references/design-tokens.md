# Trading UI design tokens

Dark-first (the default for trading terminals) with a full light set. Every
colour used in the UI must come from a token — no literals in component code.

```css
:root {
  /* light */
  --bg:            #ffffff;
  --bg-elevated:   #f6f8fa;
  --bg-inset:      #eef1f4;
  --border:        #d0d7de;
  --border-strong: #9aa5b1;
  --text:          #1f2328;
  --text-muted:    #59636e;
  --text-faint:    #8c959f;

  /* direction / P&L — reserved, never used for chrome */
  --up:            #1a7f64;
  --up-bg:         rgba(26,127,100,0.10);
  --down:          #c93c37;
  --down-bg:       rgba(201,60,55,0.10);

  /* trade levels */
  --entry:         #0969da;
  --stop:          #c93c37;   /* must be unmistakable */
  --target:        #1a7f64;
  --zone-demand:   rgba(26,127,100,0.12);
  --zone-supply:   rgba(201,60,55,0.12);

  /* status */
  --live:          #1a7f64;
  --warn:          #9a6700;
  --stale:         #c93c37;
  --accent:        #0969da;

  --radius:        6px;
  --row-h:         30px;
  --font-num:      ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace;
}

/* system dark, unless the user forced light */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { /* dark values, see below */ }
}
/* explicit dark choice wins in both directions */
:root[data-theme="dark"] { /* same dark values */ }
```

Dark values:

```css
  --bg:            #0d1117;
  --bg-elevated:   #161b22;
  --bg-inset:      #010409;
  --border:        #30363d;
  --border-strong: #6e7681;
  --text:          #e6edf3;
  --text-muted:    #8b949e;
  --text-faint:    #6e7681;

  --up:            #3fb950;
  --up-bg:         rgba(63,185,80,0.14);
  --down:          #f85149;
  --down-bg:       rgba(248,81,73,0.14);

  --entry:         #58a6ff;
  --stop:          #f85149;
  --target:        #3fb950;
  --zone-demand:   rgba(63,185,80,0.13);
  --zone-supply:   rgba(248,81,73,0.13);

  --live:          #3fb950;
  --warn:          #d29922;
  --stale:         #f85149;
  --accent:        #58a6ff;
```

## Numeric formatting — mandatory

```css
.num, td.num, .price, .pnl {
  font-family: var(--font-num);
  font-variant-numeric: tabular-nums;
  text-align: right;
}
```

Without `tabular-nums` every tick reflows the column. This is the highest-impact
rule in the whole stylesheet.

```js
// Fixed precision per instrument — resolve once, never per tick.
const fmtPrice = (v, d) => v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtPct   = v => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;      // sign always
const fmtPnl   = (v, c = '$') => `${v >= 0 ? '+' : '-'}${c}${Math.abs(v).toFixed(2)}`;
const fmtQty   = (v, d) => v.toFixed(d);
const fmtVol   = v => v >= 1e9 ? `${(v/1e9).toFixed(2)}B`
                    : v >= 1e6 ? `${(v/1e6).toFixed(2)}M`
                    : v >= 1e3 ? `${(v/1e3).toFixed(1)}K` : v.toFixed(0);
// Volume abbreviates. Prices never do.
```

Decimal places by instrument class: FX majors 5, JPY pairs 3, US equities 2,
BTC/ETH 1-2, small-cap crypto up to 8. Derive from the instrument's tick size
and cache it.

## Flash on update

```css
@keyframes flash-up   { from { background: var(--up-bg); }   to { background: transparent; } }
@keyframes flash-down { from { background: var(--down-bg); } to { background: transparent; } }
.tick-up   { animation: flash-up   280ms ease-out; }
.tick-down { animation: flash-down 280ms ease-out; }

@media (prefers-reduced-motion: reduce) {
  .tick-up, .tick-down { animation: none; }
}
```

Background only — never animate size, position or font weight, or the row jumps.

## Status and mode indicators

```css
.status { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; }
.status::before { content: ''; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.status-live  { color: var(--live); }
.status-warn  { color: var(--warn); }
.status-stale { color: var(--stale); }

/* Live trading must be unmissable and must not rely on colour alone */
.mode-live {
  background: var(--down); color: #fff; font-weight: 700;
  letter-spacing: 0.04em; padding: 3px 10px; border-radius: var(--radius);
}
.mode-paper {
  background: var(--bg-inset); color: var(--text-muted);
  border: 1px solid var(--border); padding: 3px 10px; border-radius: var(--radius);
}
```

The text ("LIVE" / "PAPER") carries the meaning; the colour only reinforces it.

## Direction without relying on colour

```html
<span class="dir dir-long"><span aria-hidden="true">▲</span> LONG</span>
<span class="dir dir-short"><span aria-hidden="true">▼</span> SHORT</span>
```

Arrow + word + colour. Any two of the three surviving is enough to read it.

## Density scale

| Element | Size |
|---|---|
| Table row height | 28-32px |
| Table font | 12-13px |
| Card padding | 12-14px |
| Section gap | 12px |
| Chart min-height | 380px (hero: fill available) |
| Border radius | 6px |

## Contrast

All text/background pairs above must clear WCAG AA (4.5:1 for body, 3:1 for
large text and UI boundaries) in both themes. `--text-faint` on `--bg` is the
pair that usually fails — verify it before shipping, and never use it for
anything a user must read to trade.
