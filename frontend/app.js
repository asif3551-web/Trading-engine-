/* Trading dashboard.
 *
 * Pinned to Lightweight Charts 4.2.0, which uses the addCandlestickSeries()
 * API (v5 moved to addSeries(CandlestickSeries, ...)). The two are not
 * interchangeable, so the version in index.html and the calls here must move
 * together.
 */

'use strict';

const REFRESH_MS = 5000;

const state = {
  chart: null,
  candles: null,
  volume: null,
  equityChart: null,
  equitySeries: null,
  priceLines: [],
  zoneSeries: [],
  symbol: null,
  timeframe: '15m',
  decimals: 2,
  lastBarTime: 0,
  backtestRunning: false,
  failures: 0,
};

/* ---------- formatting ----------
 * Precision is resolved once per instrument and never recomputed per tick, so
 * a price cannot appear to change just because its formatting did.
 */

function decimalsFor(price) {
  if (price >= 1000) return 2;
  if (price >= 10) return 3;
  if (price >= 1) return 4;
  if (price >= 0.01) return 6;
  return 8;
}

const fmtPrice = (v, d = state.decimals) =>
  (v === null || v === undefined || Number.isNaN(v))
    ? '—'
    : v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });

const fmtPct = (v, d = 2) =>
  (v === null || v === undefined || Number.isNaN(v)) ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(d)}%`;

const fmtMoney = (v) =>
  (v === null || v === undefined || Number.isNaN(v))
    ? '—'
    : `${v < 0 ? '-' : ''}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const fmtSignedMoney = (v) =>
  (v === null || v === undefined || Number.isNaN(v))
    ? '—'
    : `${v >= 0 ? '+' : '-'}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const fmtR = (v) =>
  (v === null || v === undefined || Number.isNaN(v)) ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}R`;

const fmtQty = (v) =>
  (v === null || v === undefined || Number.isNaN(v)) ? '—' : v.toFixed(6);

const signClass = (v) => (v > 0 ? 'pos' : v < 0 ? 'neg' : 'muted');

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* ---------- theme ---------- */

function currentTheme() {
  const explicit = document.documentElement.getAttribute('data-theme');
  if (explicit) return explicit;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function themeColors() {
  const dark = currentTheme() === 'dark';
  return dark
    ? { bg: '#161b22', text: '#8b949e', grid: 'rgba(139,148,158,0.08)',
        border: 'rgba(139,148,158,0.2)', up: '#3fb950', down: '#f85149',
        entry: '#58a6ff', stop: '#f85149', target: '#3fb950', accent: '#58a6ff' }
    : { bg: '#f6f8fa', text: '#59636e', grid: 'rgba(89,99,110,0.10)',
        border: 'rgba(89,99,110,0.22)', up: '#1a7f64', down: '#c93c37',
        entry: '#0969da', stop: '#c93c37', target: '#1a7f64', accent: '#0969da' };
}

function applyChartTheme() {
  const c = themeColors();
  [state.chart, state.equityChart].forEach((chart) => {
    if (!chart) return;
    chart.applyOptions({
      layout: { background: { color: c.bg }, textColor: c.text },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      rightPriceScale: { borderColor: c.border },
      timeScale: { borderColor: c.border },
    });
  });
  if (state.candles) {
    state.candles.applyOptions({
      upColor: c.up, downColor: c.down,
      borderUpColor: c.up, borderDownColor: c.down,
      wickUpColor: c.up, wickDownColor: c.down,
    });
  }
  if (state.equitySeries) state.equitySeries.applyOptions({ color: c.accent });
}

/* ---------- chart library loading ----------
 * Tried in order: local vendored copy (works fully offline), then public CDNs.
 * Corporate proxies, ad blockers and offline machines all block CDNs, and the
 * dashboard must stay usable when that happens — so a total failure only costs
 * the chart, never the signals, prices or risk panels.
 */

const CHART_SOURCES = [
  '/vendor/lightweight-charts.standalone.production.js',
  'https://cdnjs.cloudflare.com/ajax/libs/lightweight-charts/4.2.0/lightweight-charts.standalone.production.js',
  'https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js',
  'https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js',
];

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const el = document.createElement('script');
    el.src = src;
    el.async = false;
    el.onload = () => resolve(src);
    el.onerror = () => { el.remove(); reject(new Error(`failed: ${src}`)); };
    document.head.appendChild(el);
  });
}

async function loadChartLibrary() {
  if (typeof LightweightCharts !== 'undefined') return true;
  for (const src of CHART_SOURCES) {
    try {
      await loadScript(src);
      if (typeof LightweightCharts !== 'undefined') {
        console.info(`chart library loaded from ${src}`);
        return true;
      }
    } catch {
      /* try the next source */
    }
  }
  console.warn('chart library unavailable from all sources; charts disabled');
  return false;
}

/* ---------- charts ---------- */

function buildCharts() {
  // The charting library is loaded from a CDN. If it does not arrive, the
  // dashboard must still show signals, positions and risk — those are the
  // panels someone actually needs to act. Losing the chart is a degradation,
  // not a failure.
  if (typeof LightweightCharts === 'undefined') {
    const message = '<div class="empty">Chart unavailable — the charting '
      + 'library could not be loaded from any source.<br>'
      + 'For a fully offline copy run: <code>python -m trading_engine '
      + 'vendor-chart</code><br>'
      + 'All prices, signals, positions and risk below are live and '
      + 'unaffected.</div>';
    document.getElementById('chart').innerHTML = message;
    document.getElementById('equity-chart').innerHTML = message;
    return;
  }

  const c = themeColors();
  const common = {
    layout: {
      background: { color: c.bg }, textColor: c.text,
      fontFamily: 'ui-sans-serif, system-ui, sans-serif',
      attributionLogo: false,
    },
    grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
    rightPriceScale: { borderColor: c.border },
    timeScale: { borderColor: c.border, timeVisible: true, secondsVisible: false },
    autoSize: true,
  };

  state.chart = LightweightCharts.createChart(document.getElementById('chart'), {
    ...common,
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });
  state.candles = state.chart.addCandlestickSeries({
    upColor: c.up, downColor: c.down,
    borderUpColor: c.up, borderDownColor: c.down,
    wickUpColor: c.up, wickDownColor: c.down,
  });

  // Volume in its own pane at the bottom, never overlaid on price.
  state.volume = state.chart.addHistogramSeries({
    priceFormat: { type: 'volume' },
    priceScaleId: '',
    lastValueVisible: false,
    priceLineVisible: false,
  });
  state.volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

  state.equityChart = LightweightCharts.createChart(
    document.getElementById('equity-chart'),
    { ...common, handleScroll: false, handleScale: false },
  );
  state.equitySeries = state.equityChart.addLineSeries({
    color: c.accent, lineWidth: 2, priceLineVisible: false,
  });
}

/* Old lines and zone series must be cleared before drawing new ones —
 * createPriceLine/addSeries accumulate, and a chart carrying 60 stale levels is
 * the classic bug in these dashboards. */
function clearOverlays() {
  if (!state.candles) return;
  state.priceLines.forEach((line) => {
    try { state.candles.removePriceLine(line); } catch { /* already gone */ }
  });
  state.priceLines = [];
  state.zoneSeries.forEach((series) => {
    try { state.chart.removeSeries(series); } catch { /* already gone */ }
  });
  state.zoneSeries = [];
}

function drawSignalLevels(signal) {
  if (!signal || !state.candles) return;
  const c = themeColors();
  const add = (price, color, title, dashed) => {
    state.priceLines.push(state.candles.createPriceLine({
      price, color, title,
      lineWidth: dashed ? 2 : 1,
      lineStyle: dashed ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid,
      axisLabelVisible: true,
    }));
  };
  add(signal.entry, c.entry, 'ENTRY', false);
  add(signal.stop_loss, c.stop, 'STOP', true);
  (signal.take_profits || []).forEach((tp, i) => {
    add(tp.price, c.target, `TP${i + 1} ${tp.r_multiple}R`, false);
  });
}

function drawZones(zones) {
  if (!state.chart) return;
  const c = themeColors();
  zones.slice(0, 10).forEach((z) => {
    const color = z.zone_kind === 'demand'
      ? 'rgba(63,185,80,0.35)'
      : 'rgba(248,81,73,0.35)';
    [z.top, z.bottom].forEach((price) => {
      const series = state.chart.addLineSeries({
        color, lineWidth: 1, lastValueVisible: false,
        priceLineVisible: false, crosshairMarkerVisible: false,
      });
      series.setData([
        { time: z.from_time, value: price },
        { time: z.to_time, value: price },
      ]);
      state.zoneSeries.push(series);
    });
  });
  void c;
}

/* ---------- data ---------- */

async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).error || detail; } catch { /* non-JSON */ }
    throw new Error(detail);
  }
  return res.json();
}

function setConnection(kind, text) {
  const el = document.getElementById('connection');
  el.className = `status status-${kind}`;
  el.textContent = text;
}

async function loadChart() {
  const symbol = state.symbol;
  if (!symbol) return;
  const data = await fetchJSON(
    `/api/chart?symbol=${encodeURIComponent(symbol)}&tf=${state.timeframe}&limit=500`,
  );

  if (!data.bars || !data.bars.length) return;
  state.decimals = decimalsFor(data.bars[data.bars.length - 1].close);

  const last = data.bars[data.bars.length - 1];
  if (!state.candles) {
    // No chart, but the header readouts and side panels still update.
    document.getElementById('chart-title').textContent =
      `${data.symbol} · ${data.timeframe}`;
    document.getElementById('chart-meta').textContent =
      `${fmtPrice(last.close)} · feed: ${data.feed}`;
    document.getElementById('chart-updated').textContent =
      `updated ${new Date().toUTCString().slice(17, 25)} UTC`;
    renderLiquidity(data);
    renderBanners(data);
    return;
  }
  // setData resets the viewport, so only use it when the series is new or the
  // instrument changed; otherwise append incrementally.
  if (state.lastBarTime === 0 || last.time < state.lastBarTime) {
    state.candles.setData(data.bars);
    state.volume.setData(data.bars.map((b) => ({
      time: b.time,
      value: b.volume,
      color: b.close >= b.open ? 'rgba(63,185,80,0.45)' : 'rgba(248,81,73,0.45)',
    })));
    state.chart.timeScale().fitContent();
  } else {
    state.candles.update(last);
    state.volume.update({
      time: last.time,
      value: last.volume,
      color: last.close >= last.open ? 'rgba(63,185,80,0.45)' : 'rgba(248,81,73,0.45)',
    });
  }
  state.lastBarTime = last.time;

  clearOverlays();
  drawZones(data.zones || []);

  document.getElementById('chart-title').textContent = `${data.symbol} · ${data.timeframe}`;
  document.getElementById('chart-meta').textContent =
    `${fmtPrice(last.close)} · feed: ${data.feed}`;
  document.getElementById('chart-updated').textContent =
    `updated ${new Date().toUTCString().slice(17, 25)} UTC`;

  renderLiquidity(data);
  renderBanners(data);
}

function renderBanners(data) {
  const banners = [];
  if (data && data.is_synthetic) {
    banners.push(
      '<div class="banner banner-info">Synthetic data feed — generated prices, '
      + 'for development only. Nothing shown here reflects a real market.</div>',
    );
  }
  document.getElementById('banners').innerHTML = banners.join('');
}

function renderLiquidity(data) {
  const liq = data.liquidity;
  const el = document.getElementById('liquidity');
  if (!liq) { el.innerHTML = '<div class="empty">No data</div>'; return; }

  const structure = liq.structure || {};
  const pools = data.pools || [];
  const rows = [];

  rows.push(`<div><strong>Structure:</strong> ${esc(structure.trend || 'unknown')}</div>`);
  rows.push(`<div><strong>Bias:</strong> ${esc(liq.bias)} (score ${liq.score})</div>`);

  if (liq.recent_sweep) {
    const s = liq.recent_sweep;
    rows.push(
      `<div><strong>Recent sweep:</strong> ${esc(s.direction)} at `
      + `${fmtPrice(s.pool.price)}, quality ${s.quality}`
      + `${s.reclaimed ? ' (reclaimed)' : ' (not reclaimed)'}</div>`,
    );
  }

  const above = pools.filter((p) => p.side === 'above').slice(0, 3);
  const below = pools.filter((p) => p.side === 'below').slice(0, 3);
  if (above.length) {
    rows.push(`<div><strong>Liquidity above:</strong> ${above.map((p) => fmtPrice(p.price)).join(', ')}</div>`);
  }
  if (below.length) {
    rows.push(`<div><strong>Liquidity below:</strong> ${below.map((p) => fmtPrice(p.price)).join(', ')}</div>`);
  }
  if (liq.profile) {
    rows.push(`<div><strong>POC:</strong> ${fmtPrice(liq.profile.poc)} · VA `
      + `${fmtPrice(liq.profile.value_area_low)}–${fmtPrice(liq.profile.value_area_high)}</div>`);
  }
  if (liq.reasons && liq.reasons.length) {
    rows.push(`<ul style="margin:6px 0 0;padding-left:16px;color:var(--text-muted)">${
      liq.reasons.slice(0, 5).map((r) => `<li>${esc(r)}</li>`).join('')}</ul>`);
  }

  el.innerHTML = `<div style="font-size:12px;line-height:1.7">${rows.join('')}</div>`;
}

/* ---------- signals ---------- */

function signalCard(sig) {
  const long = sig.side === 'long';
  const arrow = long ? '▲' : '▼';
  const dirClass = long ? 'dir-long' : 'dir-short';

  const tpRows = (sig.take_profits || []).map((tp, i) => {
    const movePct = ((tp.price - sig.entry) / sig.entry) * 100 * (long ? 1 : -1);
    return `<tr class="row-tp">
      <td class="lbl">TP${i + 1}</td>
      <td class="num">${fmtPrice(tp.price)}</td>
      <td class="num muted">${fmtPct(movePct)}</td>
      <td class="num">${tp.r_multiple}R</td>
      <td class="num muted">${Math.round(tp.size_pct * 100)}%</td>
    </tr>`;
  }).join('');

  const stopPct = ((sig.stop_loss - sig.entry) / sig.entry) * 100 * (long ? 1 : -1);

  return `<article class="signal">
    <div class="signal-head">
      <span class="dir ${dirClass}"><span aria-hidden="true">${arrow}</span> ${long ? 'LONG' : 'SHORT'}</span>
      <span class="sym">${esc(sig.symbol)}</span>
      <span class="tf">${esc(sig.timeframe)}</span>
      <span class="conf">confidence ${Math.round(sig.confidence)}</span>
    </div>
    <div class="why">
      <ul>${(sig.reasons || []).slice(0, 4).map((r) => `<li>${esc(r)}</li>`).join('')}</ul>
    </div>
    <table class="levels">
      <tr class="row-entry">
        <td class="lbl">Entry</td><td class="num">${fmtPrice(sig.entry)}</td>
        <td class="num muted">—</td><td class="num">—</td><td class="num muted">—</td>
      </tr>
      <tr class="row-stop">
        <td class="lbl">Stop</td><td class="num">${fmtPrice(sig.stop_loss)}</td>
        <td class="num">${fmtPct(stopPct)}</td><td class="num">-1.0R</td>
        <td class="num muted">100%</td>
      </tr>
      ${tpRows}
    </table>
    <div class="signal-foot">
      <span>R:R ${sig.reward_risk}</span>
      <span>max ${sig.max_r}R</span>
      <span>size ${fmtQty(sig.position_size)}</span>
      <span>risk ${fmtMoney(sig.risk_amount)}</span>
      <span>b/e win rate ${(sig.breakeven_win_rate * 100).toFixed(0)}%</span>
    </div>
  </article>`;
}

async function loadSignals() {
  const data = await fetchJSON('/api/signals');
  const el = document.getElementById('signals');
  const signals = data.signals || [];
  document.getElementById('signal-count').textContent =
    signals.length ? `${signals.length} recent` : '';

  if (!signals.length) {
    // "No qualifying setup" is a real state and must not look like an error or
    // like data still loading.
    el.innerHTML = '<div class="empty">No qualifying setup right now.<br>'
      + 'The engine rejects anything below its confluence and reward:risk floors.</div>';
    return;
  }
  el.innerHTML = signals.slice(0, 6).map(signalCard).join('');
  drawSignalLevels(signals[0]);
}

/* ---------- status ---------- */

function metric(label, value, cls = '') {
  return `<div class="metric"><div class="label">${esc(label)}</div>`
    + `<div class="value ${cls}">${value}</div></div>`;
}

async function loadStatus() {
  const data = await fetchJSON('/api/status');
  const s = data.status;
  const r = data.risk;

  const modeEl = document.getElementById('mode');
  const live = s.mode === 'live';
  modeEl.className = `mode ${live ? 'mode-live' : 'mode-paper'}`;
  modeEl.textContent = live ? 'LIVE' : 'PAPER';

  if (s.halted) {
    setConnection('stale', 'halted');
  } else if (s.data_stale) {
    setConnection('warn', 'data stale');
  } else {
    setConnection('live', 'live');
  }

  document.getElementById('account-metrics').innerHTML = [
    metric('Equity', fmtMoney(s.equity)),
    metric('P&L', fmtPct(s.pnl_pct), signClass(s.pnl_pct)),
    metric('Drawdown', fmtPct(-r.drawdown * 100), r.drawdown > 0 ? 'neg' : 'muted'),
    metric('Positions', s.open_positions),
    metric('Signals today', s.signals_today),
  ].join('');

  const heat = data.portfolio_heat || 0;
  const limits = data.limits;
  document.getElementById('risk-metrics').innerHTML = [
    metric('Risk / trade', `${(limits.risk_per_trade * 100).toFixed(2)}%`),
    metric('Heat', `${(heat * 100).toFixed(2)}%`,
      heat > limits.max_portfolio_heat * 0.8 ? 'neg' : 'muted'),
    metric('Heat cap', `${(limits.max_portfolio_heat * 100).toFixed(1)}%`),
    metric('Daily P&L', fmtPct(r.daily_pnl_pct * 100), signClass(r.daily_pnl_pct)),
    metric('Min R:R', `${limits.min_reward_risk}`),
    metric('Loss streak', r.consecutive_losses,
      r.consecutive_losses >= 3 ? 'neg' : 'muted'),
  ].join('');

  const banners = [];
  if (s.halted) {
    banners.push(`<div class="banner banner-warn">HALTED — ${esc(s.halt_reason)}</div>`);
  }
  if (s.data_stale) {
    banners.push('<div class="banner banner-warn">Data is stale — no new positions '
      + 'will be opened until the feed recovers.</div>');
  }
  if (s.last_error) {
    banners.push(`<div class="banner banner-info">Last error: ${esc(s.last_error)}</div>`);
  }
  if (banners.length) {
    document.getElementById('banners').innerHTML = banners.join('');
  }
}

async function loadPositions() {
  const data = await fetchJSON('/api/positions');
  const rows = (data.positions || []).map((p) => `<tr>
    <td>${esc(p.symbol)}</td>
    <td class="${p.side === 'long' ? 'pos' : 'neg'}">
      ${p.side === 'long' ? '▲ LONG' : '▼ SHORT'}</td>
    <td class="num">${fmtQty(p.size)}</td>
    <td class="num">${fmtPrice(p.entry_price)}</td>
    <td class="num ${signClass(p.unrealised_r)}">${fmtR(p.unrealised_r)}</td>
  </tr>`).join('');

  document.querySelector('#positions tbody').innerHTML =
    rows || '<tr><td colspan="5" class="empty">Flat</td></tr>';
}

/* ---------- backtest ---------- */

async function runBacktest() {
  if (state.backtestRunning) return;
  state.backtestRunning = true;
  const button = document.getElementById('run-backtest');
  button.disabled = true;
  button.textContent = 'Running…';
  document.getElementById('backtest-meta').textContent = 'running…';

  try {
    const data = await fetchJSON(
      `/api/backtest?symbol=${encodeURIComponent(state.symbol)}&tf=${state.timeframe}&limit=3000`,
    );
    const m = data.metrics;

    document.getElementById('backtest-metrics').innerHTML = [
      metric('Return', fmtPct(m.total_return * 100), signClass(m.total_return)),
      metric('Trades', m.total_trades),
      metric('Win rate', `${(m.win_rate * 100).toFixed(1)}%`),
      metric('Expectancy', `${m.expectancy_r.toFixed(2)}R`, signClass(m.expectancy_r)),
      metric('Profit factor', m.profit_factor.toFixed(2), signClass(m.profit_factor - 1)),
      metric('Sharpe', m.sharpe.toFixed(2), signClass(m.sharpe)),
      metric('Max DD', fmtPct(-m.max_drawdown * 100), 'neg'),
      metric('Avg win', `${m.avg_win_r.toFixed(2)}R`, 'pos'),
      metric('Avg loss', `${m.avg_loss_r.toFixed(2)}R`, 'neg'),
      metric('Fees', fmtMoney(m.total_fees), 'muted'),
    ].join('');

    document.getElementById('backtest-meta').textContent =
      `${data.symbol} · ${data.timeframe} · ${data.bars_tested} bars · feed: ${data.feed}`;

    // The engine's own caveats are shown with the numbers, not hidden away.
    const warn = [];
    if (data.is_synthetic) {
      warn.push('Synthetic data — this measures whether the code runs, not '
        + 'whether the strategy has an edge.');
    }
    (m.warnings || []).forEach((w) => warn.push(w));
    document.getElementById('backtest-warnings').innerHTML = warn.length
      ? `<div class="banner banner-warn">${warn.map(esc).join('<br>')}</div>`
      : '';

    if (state.equitySeries && data.equity_curve && data.equity_curve.length) {
      state.equitySeries.setData(data.equity_curve);
      state.equityChart.timeScale().fitContent();
    }

    const rejections = Object.entries(data.rejections || {}).slice(0, 8);
    document.getElementById('rejections').innerHTML = rejections.length
      ? '<div style="margin-bottom:4px;color:var(--text-faint)">Why setups were '
        + 'rejected</div>'
        + rejections.map(([k, v]) => `<div><span>${esc(k)}</span><span>${v}</span></div>`).join('')
      : '';

    const trades = (data.trades || []).slice().reverse().slice(0, 40);
    document.querySelector('#trades tbody').innerHTML = trades.length
      ? trades.map((t) => `<tr>
          <td class="${t.side === 'long' ? 'pos' : 'neg'}">
            ${t.side === 'long' ? '▲ LONG' : '▼ SHORT'}</td>
          <td class="num">${fmtPrice(t.entry_price)}</td>
          <td class="num">${fmtPrice(t.exit_price)}</td>
          <td class="num ${signClass(t.pnl)}">${fmtSignedMoney(t.pnl)}</td>
          <td class="num ${signClass(t.r_multiple)}">${fmtR(t.r_multiple)}</td>
          <td class="muted">${esc(t.exit_reason.replace(/_/g, ' '))}</td>
        </tr>`).join('')
      : '<tr><td colspan="6" class="empty">No trades taken in this window</td></tr>';
  } catch (err) {
    document.getElementById('backtest-meta').textContent = '';
    document.getElementById('backtest-warnings').innerHTML =
      `<div class="banner banner-warn">Backtest failed: ${esc(err.message)}</div>`;
  } finally {
    state.backtestRunning = false;
    button.disabled = false;
    button.textContent = 'Run backtest';
  }
}

/* ---------- polling ---------- */

async function refresh() {
  try {
    await Promise.all([loadStatus(), loadSignals(), loadPositions(), loadChart()]);
    state.failures = 0;
  } catch (err) {
    state.failures += 1;
    // A frozen price with no indicator reads as a flat market rather than a
    // dead connection, so surface the failure immediately.
    setConnection('stale', `disconnected (${state.failures})`);
    console.error('refresh failed:', err);
  }
}

/* ---------- init ---------- */

async function init() {
  await loadChartLibrary();
  buildCharts();

  let config = null;
  try {
    config = await fetchJSON('/api/config');
  } catch {
    setConnection('stale', 'cannot reach the engine');
    return;
  }

  const symbols = (config.data && config.data.symbols) || ['BTC/USDT'];
  const select = document.getElementById('symbol');
  select.innerHTML = symbols.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
  state.symbol = symbols[0];

  const tf = document.getElementById('timeframe');
  if (config.strategy && config.strategy.timeframe) {
    state.timeframe = config.strategy.timeframe;
    tf.value = state.timeframe;
  }

  select.addEventListener('change', (e) => {
    state.symbol = e.target.value;
    state.lastBarTime = 0;          // force a full redraw on instrument change
    refresh();
  });
  tf.addEventListener('change', (e) => {
    state.timeframe = e.target.value;
    state.lastBarTime = 0;
    refresh();
  });

  document.getElementById('run-backtest').addEventListener('click', runBacktest);

  document.getElementById('flatten').addEventListener('click', async () => {
    // Money-moving action: confirm with the full consequence stated.
    const positions = await fetchJSON('/api/positions').catch(() => ({ positions: [] }));
    const count = (positions.positions || []).length;
    if (!count) { window.alert('No open positions to flatten.'); return; }
    if (!window.confirm(
      `Close ${count} open position${count > 1 ? 's' : ''} at market, immediately?\n\n`
      + 'This realises all open P&L and cannot be undone.',
    )) return;
    try {
      await fetchJSON('/api/flatten', { method: 'POST' });
      refresh();
    } catch (err) {
      window.alert(`Flatten failed: ${err.message}`);
    }
  });

  document.getElementById('theme').addEventListener('click', () => {
    const next = currentTheme() === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    applyChartTheme();
  });

  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (!document.documentElement.getAttribute('data-theme')) applyChartTheme();
  });

  await refresh();
  setInterval(refresh, REFRESH_MS);
}

document.addEventListener('DOMContentLoaded', init);
