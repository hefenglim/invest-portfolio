/* portfolio-dash — ECharts setup (trend, sector allocation).
   Reads the SAME /api/dashboard payload as app.js via the shared window.pdDashboard
   promise (one network request, identical data). Money in tooltips/labels routes
   through window.fmt (Decimal strings coerced for display only). */
(function () {
  'use strict';
  let D;                       // set in boot() from the shared /api/dashboard promise
  const f = window.fmt;

  let C, baseTooltip;
  function buildPalette() {
    const s = getComputedStyle(document.documentElement);
    const V = (n) => s.getPropertyValue(n).trim();
    C = {
      text: V('--text-2'), textStrong: V('--text'), faint: V('--text-3'),
      grid: V('--border'), panel: V('--panel'),
      accent: V('--accent'), accentSoft: V('--accent-soft'),
      gray: V('--series-gray'), amber: V('--amber'),
      bench: V('--series-usd'),   // benchmark overlay line (FU-D27)
      netWorth: V('--series-usd'), // 總淨值（含現金）trend line (FU-D29); distinct from accent/gray
      up: V('--up'), down: V('--down'),
      ccy: { TWD: V('--accent'), USD: V('--series-usd'), MYR: V('--series-myr') },
      tooltipBg: V('--panel-2'), tooltipBorder: V('--border'),
      fontNum: "'IBM Plex Mono', monospace",
      fontUi: "'Noto Sans TC', sans-serif"
    };
    baseTooltip = {
      backgroundColor: C.tooltipBg, borderColor: C.tooltipBorder,
      textStyle: { color: C.textStrong, fontSize: 12, fontFamily: C.fontNum },
      extraCssText: 'box-shadow: 0 6px 24px rgba(0,0,0,0.25);'
    };
  }

  const charts = [];

  /* ---- 績效比較 (TWR overlay, FU-D27) module state ----
     The trend card has two modes: 市值 (the value chart above) and 績效比較 (a
     server-computed time-weighted-return overlay vs a benchmark). Mode 2 is LAZY:
     its ECharts instance is created only when the user first activates it, and the
     value chart is never disposed on a mode switch (switching back restores it). */
  let trendChart = null;        // #trend-chart instance (so a mode switch can resize it)
  let twrChart = null;          // #twr-chart instance (lazy; NOT in `charts` — managed here)
  let twrData = null;           // last /api/performance/twr payload (theme re-render)
  let trendHasIncomplete = false;
  let currentMode = 'value';
  let twrBenchmark = '0050';
  let twrWindow = '3y';
  let twrSeq = 0;               // stale-fetch guard (only the newest request renders)
  let modeWired = false;        // one-time listener wiring (idempotent across initAll)

  /* ============ C. Trend ============ */
  function initTrend() {
    buildPalette();
    const host = document.getElementById('trend-chart');
    const t = D.trend;
    const nwEl = document.getElementById('trend-networth');
    if (!t || !t.available) {
      trendChart = null;
      trendHasIncomplete = false;
      if (nwEl) nwEl.hidden = true;
      host.replaceChildren(window.emptyState(
        (D.freshness && D.freshness.trend_unavailable_reason) || '尚無趨勢資料'));
      host.style.height = 'auto';
      return;
    }
    const dates = t.points.map((p) => p.date);
    const incompletePts = t.points.filter((p) => p.incomplete);
    trendHasIncomplete = incompletePts.length > 0;
    document.getElementById('trend-note').hidden = incompletePts.length === 0;

    /* 總淨值（含現金）header sub-stat (FU-D29): the newest point whose net worth is known
       (cash complete). net_worth is null on a cash-incomplete day (a pool lacked FX); it
       still carries a value on a holdings-incomplete day, mirroring total_market_value
       being shown despite a missing price. */
    if (nwEl) {
      let curNw = null;
      for (let i = t.points.length - 1; i >= 0; i--) {
        if (t.points[i].net_worth != null) { curNw = t.points[i].net_worth; break; }
      }
      if (curNw != null) {
        nwEl.textContent = '含現金 ' + f.money(curNw, 'TWD');
        nwEl.hidden = false;
      } else {
        nwEl.hidden = true;
      }
    }

    const chart = echarts.init(host);
    trendChart = chart;
    charts.push(chart);
    chart.setOption({
      animationDuration: 400,
      grid: { left: 70, right: 24, top: 36, bottom: 64 },
      legend: {
        top: 0, left: 0, icon: 'rect', itemWidth: 12, itemHeight: 3,
        textStyle: { color: C.text, fontSize: 11, fontFamily: C.fontUi },
        data: ['總市值', '累計淨投入', '總淨值（含現金）']
      },
      tooltip: {
        ...baseTooltip,
        trigger: 'axis',
        axisPointer: { type: 'line', lineStyle: { color: C.faint } },
        formatter: (params) => {
          const date = params[0].axisValue;
          const pt = t.points.find((p) => p.date === date);
          if (!pt) return date;
          const spread = pt.total_value - pt.net_invested;
          const spreadColor = spread > 0 ? C.up : spread < 0 ? C.down : C.text;
          let html = '<div style="font-size:11px;color:' + C.faint + '">' + date + '</div>';
          html += '<div>總市值&nbsp;&nbsp;<b>' + f.money(pt.total_value, 'TWD') + '</b></div>';
          html += '<div>累計淨投入&nbsp;&nbsp;<b>' + f.money(pt.net_invested, 'TWD') + '</b></div>';
          if (pt.net_worth != null) {
            html += '<div>總淨值（含現金）&nbsp;&nbsp;<b style="color:' + C.netWorth + '">' +
                    f.money(pt.net_worth, 'TWD') + '</b></div>';
          }
          html += '<div>浮動損益&nbsp;&nbsp;<b style="color:' + spreadColor + '">' +
                  f.signed(spread, 'TWD') + '</b></div>';
          if (pt.incomplete) {
            html += '<div style="color:' + C.amber + ';font-size:11px">部分標的當日無價格</div>';
          }
          return html;
        }
      },
      xAxis: {
        type: 'category', boundaryGap: false, data: dates,
        axisLine: { lineStyle: { color: C.grid } },
        axisLabel: { color: C.faint, fontSize: 10, fontFamily: C.fontNum },
        axisTick: { show: false }
      },
      yAxis: {
        type: 'value', scale: true,
        splitLine: { lineStyle: { color: C.grid, type: 'dashed' } },
        axisLabel: {
          color: C.faint, fontSize: 10, fontFamily: C.fontNum,
          formatter: (v) => (v / 1000).toLocaleString('en-US') + 'k'
        }
      },
      dataZoom: [
        { type: 'inside' },
        {
          type: 'slider', height: 22, bottom: 10,
          borderColor: C.grid, backgroundColor: 'transparent',
          fillerColor: 'rgba(88,166,221,0.12)',
          dataBackground: { lineStyle: { color: C.faint }, areaStyle: { color: 'rgba(138,150,166,0.15)' } },
          selectedDataBackground: { lineStyle: { color: C.accent }, areaStyle: { color: 'rgba(88,166,221,0.2)' } },
          handleStyle: { color: C.accent, borderColor: C.accent },
          moveHandleStyle: { color: C.faint },
          textStyle: { color: C.faint, fontSize: 9, fontFamily: C.fontNum }
        }
      ],
      series: [
        {
          name: '總市值', type: 'line', data: t.points.map((p) => Number(p.total_value)),
          symbol: 'circle', symbolSize: 5, showSymbol: false,
          lineStyle: { color: C.accent, width: 2 },
          itemStyle: { color: C.accent },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(88,166,221,0.28)' },
              { offset: 1, color: 'rgba(88,166,221,0.02)' }
            ])
          }
        },
        {
          name: '累計淨投入', type: 'line', data: t.points.map((p) => Number(p.net_invested)),
          symbol: 'none', lineStyle: { color: C.gray, width: 1.2, type: 'dashed' },
          itemStyle: { color: C.gray }
        },
        {
          /* 總淨值（含現金）= 市值 + 當日現金（報告幣）. null on a cash-incomplete day ->
             connectNulls:false draws an honest gap, mirroring the value lines' handling. */
          name: '總淨值（含現金）', type: 'line',
          data: t.points.map((p) => (p.net_worth == null ? null : Number(p.net_worth))),
          symbol: 'none', showSymbol: false, connectNulls: false,
          lineStyle: { color: C.netWorth, width: 1.6 },
          itemStyle: { color: C.netWorth }
        },
        {
          name: '部分標的當日無價格', type: 'scatter',
          data: incompletePts.map((p) => [p.date, Number(p.total_value)]),
          symbolSize: 8,
          itemStyle: { color: 'transparent', borderColor: C.amber, borderWidth: 1.5 },
          tooltip: { show: false }, legendHoverLink: false
        }
      ]
    });

    /* range buttons: 1M / 3M / 6M / 全部 (scoped to #value-ranges so the 市值/績效比較
       mode toggle and the TWR window buttons — also .range-btn — never get this handler) */
    const last = new Date(dates[dates.length - 1]);
    document.querySelectorAll('#value-ranges .range-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#value-ranges .range-btn')
          .forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        const months = btn.dataset.range;
        if (months === 'all') {
          chart.dispatchAction({ type: 'dataZoom', start: 0, end: 100 });
          return;
        }
        const from = new Date(last);
        from.setMonth(from.getMonth() - Number(months));
        const fromStr = from.toISOString().slice(0, 10);
        let idx = dates.findIndex((d) => d >= fromStr);
        if (idx < 0) idx = 0;
        chart.dispatchAction({
          type: 'dataZoom',
          startValue: dates[idx],
          endValue: dates[dates.length - 1]
        });
      });
    });
  }

  /* ============ E1. 產業配置 donut ============ */
  function initSector() {
    buildPalette();
    const host = document.getElementById('sector-chart');
    const a = D.allocation;
    if (!a) {
      host.replaceChildren(window.emptyState('匯率資料不足，無法合併計價'));
      host.style.height = 'auto';
      return;
    }
    const palette = ['#58a6dd', '#9b86d8', '#d9a13f', '#5bb8a5', '#c97ba6', '#8a96a6'];
    const data = Object.keys(a.by_sector).map((sector, i) => ({
      name: sector, value: Number(a.by_sector[sector]),  // Decimal string → number for plotting
      itemStyle: { color: palette[i % palette.length] }
    }));
    const chart = echarts.init(host);
    charts.push(chart);
    chart.setOption({
      tooltip: {
        ...baseTooltip, trigger: 'item',
        formatter: (p) =>
          p.name + '<br><b>' + f.money(p.value, a.reporting_currency) + ' ' +
          a.reporting_currency + '</b>&nbsp;&nbsp;' + f.pct(a.weights[p.name])
      },
      legend: {
        /* item 3 (2026-07-03): anchor the legend at 46% width instead of
           right-edge so long sector lines never run under the donut. */
        orient: 'vertical', left: '46%', top: 'middle',
        icon: 'rect', itemWidth: 10, itemHeight: 10,
        textStyle: { color: C.text, fontSize: 11, fontFamily: C.fontNum },
        formatter: (name) =>
          name + '  ' + f.money(a.by_sector[name], a.reporting_currency) +
          '  ' + f.pct(a.weights[name])
      },
      series: [{
        type: 'pie', radius: ['48%', '72%'], center: ['23%', '50%'],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: C.panel, borderWidth: 2 },
        label: { show: false },
        emphasis: {
          label: {
            show: true, color: C.textStrong, fontSize: 13,
            fontFamily: C.fontUi, formatter: '{b}\n{d}%'
          }
        },
        data
      }]
    });
  }

  /* ============ H1. 年度股利 stacked bar ============
     REMOVED (FU-D47, 2026-07-19): the yearly dividend chart now lives in the
     consolidated #dividend-income-card surface (dividends-card.js owns it). */

  /* ============ C2. 績效比較 (TWR overlay, FU-D27) ============ */

  function setActive(selector, activeBtn) {
    document.querySelectorAll(selector).forEach((b) => b.classList.remove('active'));
    activeBtn.classList.add('active');
  }

  /* Wire the mode toggle + benchmark picker + window buttons EXACTLY once (idempotent
     across theme-driven initAll re-runs, which would otherwise stack listeners). */
  function wireModeOnce() {
    if (modeWired) return;
    modeWired = true;
    document.querySelectorAll('#trend-mode .range-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        if (btn.dataset.mode === currentMode) return;
        setActive('#trend-mode .range-btn', btn);
        applyMode(btn.dataset.mode, true);
      });
    });
    document.querySelectorAll('#twr-windows .range-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        if (btn.dataset.win === twrWindow) return;
        twrWindow = btn.dataset.win;
        setActive('#twr-windows .range-btn', btn);
        loadTwr();
      });
    });
    const sel = document.getElementById('twr-benchmark');
    if (sel) sel.addEventListener('change', () => { twrBenchmark = sel.value; loadTwr(); });
  }

  /* Show/hide the two modes' surfaces; on entering 績效比較 fetch (or re-render) lazily. */
  function applyMode(mode, refetch) {
    currentMode = mode;
    const isTwr = mode === 'twr';
    document.getElementById('trend-chart').hidden = isTwr;
    document.getElementById('value-ranges').hidden = isTwr;
    document.getElementById('twr-chart').hidden = !isTwr;
    document.getElementById('twr-controls').hidden = !isTwr;
    document.getElementById('twr-caption').hidden = !isTwr;
    const note = document.getElementById('trend-note');
    if (note) note.hidden = isTwr || !trendHasIncomplete;
    document.getElementById('trend-title').textContent =
      isTwr ? '績效比較（時間加權報酬）' : '總市值 vs 累計淨投入';
    document.getElementById('trend-sub').textContent = isTwr ? '重定基準＝100' : 'TWD・日線';
    if (isTwr) {
      if (refetch) { twrData = null; loadTwr(); }
      else if (twrData) renderTwr(twrData);
      else loadTwr();
    } else if (trendChart) {
      trendChart.resize();  // container was display:none while in TWR mode
    }
  }

  /* Fetch the server-computed, rebased-to-100 TWR series and render it. A newer request
     (window/benchmark change) supersedes an in-flight one via the twrSeq guard. Errors
     never throw out (the e2e asserts ZERO console/page errors): render an empty state. */
  async function loadTwr() {
    const host = document.getElementById('twr-chart');
    const seq = ++twrSeq;
    try {
      const payload = await window.pdApi.get(
        '/api/performance/twr', { benchmark: twrBenchmark, window: twrWindow });
      if (seq !== twrSeq) return;   // superseded
      twrData = payload;
      renderTwr(payload);
    } catch (e) {
      if (seq !== twrSeq) return;
      twrData = null;
      if (twrChart) { twrChart.dispose(); twrChart = null; }
      host.replaceChildren(window.emptyState('績效比較載入失敗'));
      host.style.height = 'auto';
      const cap = document.getElementById('twr-caption');
      if (cap) cap.textContent = '';
    }
  }

  function renderTwr(payload) {
    buildPalette();
    const host = document.getElementById('twr-chart');
    const cap = document.getElementById('twr-caption');
    if (twrChart) { twrChart.dispose(); twrChart = null; }
    if (!payload || !payload.available || !payload.points || payload.points.length === 0) {
      host.replaceChildren(window.emptyState((payload && payload.reason) || '尚無績效比較資料'));
      host.style.height = 'auto';
      if (cap) cap.textContent = '';
      return;
    }
    host.style.height = '360px';
    host.replaceChildren();  // clear any prior empty-state node before echarts.init
    const pts = payload.points;
    const dates = pts.map((p) => p.date);
    const label = (payload.benchmark && payload.benchmark.label) || '基準';
    twrChart = echarts.init(host);
    twrChart.setOption({
      animationDuration: 400,
      grid: { left: 52, right: 24, top: 36, bottom: 40 },
      legend: {
        top: 0, left: 0, icon: 'rect', itemWidth: 12, itemHeight: 3,
        textStyle: { color: C.text, fontSize: 11, fontFamily: C.fontUi },
        data: ['投資組合', label]
      },
      tooltip: {
        ...baseTooltip, trigger: 'axis',
        axisPointer: { type: 'line', lineStyle: { color: C.faint } },
        formatter: (params) => {
          let html = '<div style="font-size:11px;color:' + C.faint + '">' +
                     params[0].axisValue + '</div>';
          params.forEach((s) => {
            const v = Number(s.value);            // rebased index string → display only
            const delta = v - 100;
            const col = delta > 0 ? C.up : delta < 0 ? C.down : C.text;
            html += '<div>' + s.marker + s.seriesName + '&nbsp;&nbsp;<b>' + v.toFixed(2) +
                    '</b>&nbsp;<span style="color:' + col + '">(' +
                    (delta >= 0 ? '+' : '−') + Math.abs(delta).toFixed(2) + '%)</span></div>';
          });
          return html;
        }
      },
      xAxis: {
        type: 'category', boundaryGap: false, data: dates,
        axisLine: { lineStyle: { color: C.grid } },
        axisLabel: { color: C.faint, fontSize: 10, fontFamily: C.fontNum },
        axisTick: { show: false }
      },
      yAxis: {
        type: 'value', scale: true,
        splitLine: { lineStyle: { color: C.grid, type: 'dashed' } },
        axisLabel: { color: C.faint, fontSize: 10, fontFamily: C.fontNum }
      },
      series: [
        {
          name: '投資組合', type: 'line', showSymbol: false,
          data: pts.map((p) => Number(p.portfolio)),
          lineStyle: { color: C.accent, width: 2 }, itemStyle: { color: C.accent }
        },
        {
          name: label, type: 'line', showSymbol: false,
          data: pts.map((p) => Number(p.benchmark)),
          lineStyle: { color: C.bench, width: 1.6 }, itemStyle: { color: C.bench }
        }
      ]
    });
    if (cap && payload.basis_notes) {
      cap.textContent = '投組＝' + payload.basis_notes.portfolio + '　｜　' +
                        label + '＝' + payload.basis_notes.benchmark;
      cap.hidden = currentMode !== 'twr';
    }
  }

  function initTrendMode() {
    wireModeOnce();
    applyMode(currentMode, false);  // re-apply visibility (and re-render TWR from cache)
  }

  function initAll() {
    buildPalette();
    initTrend();
    initSector();
    initTrendMode();
  }

  /* ============ boot ============ */
  /* Await the SAME shared /api/dashboard promise as app.js (created by whichever script
     runs first) before building charts, so the trend/sector series read real
     data. On failure (non-401; api.js handles 401) leave the chart hosts empty rather
     than throwing — the e2e smoke asserts ZERO console errors / pageerrors. */
  async function boot() {
    try {
      D = await (window.pdDashboard || (window.pdDashboard = window.pdApi.get('/api/dashboard')));
    } catch (e) {
      return;  // app.js's boot surfaces the load-failure UI; charts stay empty.
    }
    initAll();
  }

  /* Resize + theme handlers are registered synchronously; they operate on whatever
     charts exist at event time (none until boot resolves — both are harmless no-ops). */
  window.addEventListener('resize', () => {
    charts.forEach((c) => c.resize());
    if (twrChart) twrChart.resize();
  });
  window.addEventListener('pd-theme-change', () => {
    if (!D) return;  // nothing built yet
    charts.forEach((c) => c.dispose());
    charts.length = 0;
    if (twrChart) { twrChart.dispose(); twrChart = null; }
    // initAll() rebuilds the value-mode charts; initTrendMode re-applies the current mode
    // and re-renders the TWR overlay from the cached payload (no refetch).
    initAll();
  });

  boot();
})();
