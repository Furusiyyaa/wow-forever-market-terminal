const API = 'http://127.0.0.1:8765';

let currentItemId = 12363; // Arcane Crystal
let currentRange = '7d';
let currentMode = 'area';
let showMinOverlay = true;
let watchlistIds = new Set();
let lastStatus = null;

let chart = null;
let mainSeries = null;
let minSeries = null;
let volSeries = null;
let lastPointsByTime = new Map();

function fmtGold(g) {
  if (g == null || isNaN(g)) return '—';
  if (g >= 10) return `${g.toFixed(2)}g`;
  if (g >= 1) return `${g.toFixed(2)}g`;
  return `${(g * 100).toFixed(1)}s`;
}

function fmtAgo(iso) {
  if (!iso) return 'Never';
  const d = new Date(iso);
  const s = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m ago`;
}

async function apiGet(path) {
  const r = await fetch(`${API}${path}`);
  return r.json();
}

async function apiPost(path, body = {}) {
  const r = await fetch(`${API}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json();
}

function initChart() {
  const el = document.getElementById('chart-container');
  chart = LightweightCharts.createChart(el, {
    layout: {
      background: { type: 'solid', color: '#131722' },
      textColor: '#9598a1',
      fontFamily: 'Cascadia Code, Consolas, monospace',
      fontSize: 11,
    },
    grid: {
      vertLines: { color: 'rgba(42, 46, 57, 0.55)' },
      horzLines: { color: 'rgba(42, 46, 57, 0.55)' },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
    },
    rightPriceScale: {
      borderColor: '#2a2e39',
      scaleMargins: { top: 0.1, bottom: 0.24 },
    },
    timeScale: {
      borderColor: '#2a2e39',
      timeVisible: true,
      secondsVisible: false,
    },
    localization: {
      priceFormatter: (p) => `${p.toFixed(2)}g`,
    },
  });

  volSeries = chart.addHistogramSeries({
    priceFormat: { type: 'volume' },
    priceScaleId: '',
  });
  volSeries.priceScale().applyOptions({
    scaleMargins: { top: 0.80, bottom: 0 },
  });

  chart.subscribeCrosshairMove((param) => {
    const leg = document.getElementById('chart-legend');
    if (!param || !param.time || !lastPointsByTime.has(param.time)) {
      return;
    }
    const pt = lastPointsByTime.get(param.time);
    leg.innerHTML =
      `p10: <strong style="color:#fff">${fmtGold(pt.p10_g)}</strong> &nbsp;` +
      `min: <strong style="color:#26a69a">${fmtGold(pt.min_g)}</strong> &nbsp;` +
      `med: <strong>${fmtGold(pt.median_g)}</strong> &nbsp;` +
      `qty: <strong style="color:#f59e0b">${pt.qty.toLocaleString()}</strong>`;
  });

  const ro = new ResizeObserver(() => {
    chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
  });
  ro.observe(el);
}

function rebuildMainSeries() {
  if (mainSeries) { chart.removeSeries(mainSeries); mainSeries = null; }
  if (minSeries) { chart.removeSeries(minSeries); minSeries = null; }

  if (currentMode === 'candles') {
    mainSeries = chart.addCandlestickSeries({
      upColor: '#26a69a',
      downColor: '#ef5350',
      borderVisible: false,
      wickUpColor: '#26a69a',
      wickDownColor: '#ef5350',
    });
  } else if (currentMode === 'line') {
    mainSeries = chart.addLineSeries({
      color: '#2962ff',
      lineWidth: 2,
    });
  } else {
    mainSeries = chart.addAreaSeries({
      topColor: 'rgba(41, 98, 255, 0.34)',
      bottomColor: 'rgba(41, 98, 255, 0.02)',
      lineColor: '#2962ff',
      lineWidth: 2,
    });
  }

  if (showMinOverlay && currentMode !== 'candles') {
    minSeries = chart.addLineSeries({
      color: 'rgba(38, 166, 154, 0.75)',
      lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed,
    });
  }
}

async function loadItem(itemId) {
  currentItemId = itemId;
  const data = await apiGet(`/api/item/${itemId}/history?range=${currentRange}`);
  if (data.error) return;

  const it = data.item;
  const nameEl = document.getElementById('ticker-name');
  nameEl.textContent = it.name;
  nameEl.className = `q${Math.min(4, it.quality)}`;

  document.getElementById('ticker-id').textContent = `#${it.item_id}`;
  document.getElementById('ticker-price').textContent = fmtGold(it.p10_g);
  const chgEl = document.getElementById('ticker-change');
  chgEl.textContent = `${it.change_pct >= 0 ? '+' : ''}${it.change_pct.toFixed(2)}%`;
  chgEl.className = `chg mono ${it.change_pct >= 0 ? 'up' : 'down'}`;

  document.getElementById('meta-min').textContent = fmtGold(it.min_g);
  document.getElementById('meta-qty').textContent = it.qty.toLocaleString();
  document.getElementById('meta-listings').textContent = it.listings.toLocaleString();
  document.getElementById('meta-vendor').textContent = fmtGold(it.sell_g);

  const pinBtn = document.getElementById('btn-pin');
  pinBtn.classList.toggle('pinned', watchlistIds.has(itemId));

  rebuildMainSeries();

  const pts = data.points || [];
  const emptyEl = document.getElementById('chart-empty');
  lastPointsByTime = new Map();

  if (pts.length === 0) {
    emptyEl.classList.remove('hidden');
    volSeries.setData([]);
    return;
  }
  emptyEl.classList.add('hidden');

  pts.forEach((p) => lastPointsByTime.set(p.time, p));

  if (currentMode === 'candles') {
    mainSeries.setData(
      (data.candles || []).map((c) => ({
        time: c.time,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      }))
    );
    volSeries.setData(
      (data.candles || []).map((c) => ({
        time: c.time,
        value: c.volume,
        color: c.close >= c.open ? 'rgba(38,166,154,0.35)' : 'rgba(239,83,80,0.35)',
      }))
    );
  } else {
    mainSeries.setData(pts.map((p) => ({ time: p.time, value: p.p10_g })));
    if (minSeries) {
      minSeries.setData(pts.map((p) => ({ time: p.time, value: p.min_g })));
    }
    volSeries.setData(
      pts.map((p, idx) => {
        const up = idx === 0 || p.p10_g >= pts[idx - 1].p10_g;
        return {
          time: p.time,
          value: p.qty,
          color: up ? 'rgba(38,166,154,0.30)' : 'rgba(239,83,80,0.30)',
        };
      })
    );
  }

  const last = pts[pts.length - 1];
  document.getElementById('chart-legend').innerHTML =
    `p10: <strong style="color:#fff">${fmtGold(last.p10_g)}</strong> &nbsp;` +
    `min: <strong style="color:#26a69a">${fmtGold(last.min_g)}</strong> &nbsp;` +
    `med: <strong>${fmtGold(last.median_g)}</strong> &nbsp;` +
    `qty: <strong style="color:#f59e0b">${last.qty.toLocaleString()}</strong>`;

  chart.timeScale().fitContent();
  highlightActiveWatchlistRow();
}

function highlightActiveWatchlistRow() {
  document.querySelectorAll('.wl-row').forEach((row) => {
    row.classList.toggle('active', Number(row.dataset.id) === currentItemId);
  });
}

async function loadWatchlist() {
  const data = await apiGet('/api/watchlist');
  const items = data.items || [];
  watchlistIds = new Set(items.map((x) => x.item_id));
  document.getElementById('btn-pin').classList.toggle('pinned', watchlistIds.has(currentItemId));

  const body = document.getElementById('watchlist-body');
  body.innerHTML = items
    .map(
      (it) => `
      <div class="wl-row ${it.item_id === currentItemId ? 'active' : ''}" data-id="${it.item_id}">
        <span class="wl-name q${Math.min(4, it.quality)}">${it.name}</span>
        <span class="right mono">${fmtGold(it.p10_g)}</span>
        <span class="right mono ${it.change_pct >= 0 ? 'up' : 'down'}">${it.change_pct >= 0 ? '+' : ''}${it.change_pct.toFixed(1)}%</span>
        <span class="right mono muted">${it.qty}</span>
      </div>`
    )
    .join('');

  body.querySelectorAll('.wl-row').forEach((row) => {
    row.addEventListener('click', () => loadItem(Number(row.dataset.id)));
  });
}

async function loadFarms() {
  const data = await apiGet('/api/farms?min_level=45&limit=25');
  const body = document.getElementById('farms-body');
  body.innerHTML = (data.farms || [])
    .map(
      (f) => `
      <div class="farm-row">
        <div>
          <div>${f.name} <span class="muted">L${f.level} · ${f.cont}</span></div>
          <div class="farm-sub">${f.type}${f.drops ? ' · ' + f.drops : ''}</div>
        </div>
        <span class="right mono muted">${f.spawns}</span>
        <span class="right mono up">${f.ev_g.toFixed(2)}g</span>
      </div>`
    )
    .join('');
}

async function refreshStatus() {
  try {
    const st = await apiGet('/api/status');
    const prevScan = lastStatus?.last_scan_utc;
    lastStatus = st;

    const badge = document.getElementById('status-badge');
    const btnSvc = document.getElementById('btn-service');
    const btnScan = document.getElementById('btn-scan');

    if (st.scanning) {
      badge.textContent = '● SCANNING';
      badge.className = 'badge badge-scan';
      btnScan.disabled = true;
      btnScan.textContent = 'Scanning...';
    } else if (st.online) {
      badge.textContent = '● ONLINE';
      badge.className = 'badge badge-on';
      btnScan.disabled = false;
      btnScan.textContent = 'Scan Now';
    } else {
      badge.textContent = '● OFFLINE';
      badge.className = 'badge badge-off';
      btnScan.disabled = false;
      btnScan.textContent = 'Scan Now';
    }

    btnSvc.textContent = st.online ? 'Stop Feed' : 'Start Feed';
    btnSvc.className = `btn ${st.online ? 'btn-stop' : 'btn-start'}`;

    document.getElementById('auto-rescan').checked = st.auto_rescan;
    const intInput = document.getElementById('interval-min');
    if (document.activeElement !== intInput) {
      intInput.value = st.interval_minutes;
    }

    document.getElementById('sb-target').textContent =
      `${(st.region || 'eu').toUpperCase()} · Ruleset ${st.ruleset_id || '?'} · House ${st.house_id || '?'}`;
    document.getElementById('sb-last').textContent = fmtAgo(st.last_scan_utc);

    const latest = st.database?.latest_snapshots?.[0];
    document.getElementById('sb-blizz').textContent = latest ? latest.last_modified : '—';

    if (st.online && st.auto_rescan && st.next_scan_in_sec != null) {
      const m = Math.floor(st.next_scan_in_sec / 60);
      const s = st.next_scan_in_sec % 60;
      document.getElementById('sb-next').textContent = `${m}m ${String(s).padStart(2, '0')}s`;
    } else if (st.online) {
      document.getElementById('sb-next').textContent = 'Manual';
    } else {
      document.getElementById('sb-next').textContent = 'Offline';
    }

    const lastLog = st.logs?.[st.logs.length - 1];
    if (lastLog) {
      document.getElementById('sb-log').textContent = lastLog.msg;
    }

    // If a new scan just landed, refresh chart & watchlist automatically!
    if (prevScan && st.last_scan_utc && prevScan !== st.last_scan_utc) {
      loadWatchlist();
      loadItem(currentItemId);
    }
  } catch (_) {}
}

function wireEvents() {
  // Search box
  const inp = document.getElementById('search-input');
  const dd = document.getElementById('search-results');
  let searchTimer = null;

  inp.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      const q = inp.value.trim();
      const res = await apiGet(`/api/search?q=${encodeURIComponent(q)}&limit=15`);
      const items = res.items || [];
      if (!items.length) {
        dd.classList.add('hidden');
        return;
      }
      dd.innerHTML = items
        .map(
          (it) => `
          <div class="search-item" data-id="${it.item_id}">
            <span class="q${Math.min(4, it.quality)}">${it.name} <span class="muted">#${it.item_id}</span></span>
            <span class="mono">${fmtGold(it.p10_g)} <span class="muted">(${it.qty})</span></span>
          </div>`
        )
        .join('');
      dd.classList.remove('hidden');
      dd.querySelectorAll('.search-item').forEach((el) => {
        el.addEventListener('click', () => {
          dd.classList.add('hidden');
          inp.value = '';
          loadItem(Number(el.dataset.id));
        });
      });
    }, 140);
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.search-wrap')) dd.classList.add('hidden');
  });

  // Timeframe pills
  document.querySelectorAll('#range-group .pill').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#range-group .pill').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      currentRange = btn.dataset.range;
      loadItem(currentItemId);
    });
  });

  // Mode pills
  document.querySelectorAll('#mode-group .pill').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#mode-group .pill').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      currentMode = btn.dataset.mode;
      loadItem(currentItemId);
    });
  });

  document.getElementById('toggle-min').addEventListener('change', (e) => {
    showMinOverlay = e.target.checked;
    loadItem(currentItemId);
  });

  // Watchlist pin toggle
  document.getElementById('btn-pin').addEventListener('click', async () => {
    const action = watchlistIds.has(currentItemId) ? 'remove' : 'add';
    await apiPost('/api/watchlist', { action, item_id: currentItemId });
    await loadWatchlist();
  });

  // Sidebar tabs
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab-btn').forEach((b) => b.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach((p) => p.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');
      if (btn.dataset.tab === 'farms') loadFarms();
    });
  });

  // Service controls
  document.getElementById('btn-service').addEventListener('click', async () => {
    const nextOnline = !(lastStatus && lastStatus.online);
    await apiPost('/api/service', {
      online: nextOnline,
      auto_rescan: document.getElementById('auto-rescan').checked,
      interval_minutes: Number(document.getElementById('interval-min').value) || 15,
    });
    refreshStatus();
  });

  const pushAutoSettings = async () => {
    await apiPost('/api/service', {
      auto_rescan: document.getElementById('auto-rescan').checked,
      interval_minutes: Math.max(1, Math.min(240, Number(document.getElementById('interval-min').value) || 15)),
    });
    refreshStatus();
  };
  document.getElementById('auto-rescan').addEventListener('change', pushAutoSettings);
  document.getElementById('interval-min').addEventListener('change', pushAutoSettings);

  document.getElementById('btn-scan').addEventListener('click', async () => {
    await apiPost('/api/poll', { force: false });
    refreshStatus();
  });

  document.getElementById('btn-db2').addEventListener('click', async () => {
    await apiPost('/api/sync-db2', {});
    refreshStatus();
  });
}

async function boot() {
  initChart();
  wireEvents();
  await Promise.all([refreshStatus(), loadWatchlist()]);
  await loadItem(currentItemId);
  setInterval(refreshStatus, 2000);
}

boot();
