/* portfolio-dash — ECharts setup (trend, sector allocation, dividends by year).
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

  /* ============ C. Trend ============ */
  function initTrend() {
    buildPalette();
    const host = document.getElementById('trend-chart');
    const t = D.trend;
    if (!t || !t.available) {
      host.replaceChildren(window.emptyState(
        (D.freshness && D.freshness.trend_unavailable_reason) || '尚無趨勢資料'));
      host.style.height = 'auto';
      return;
    }
    const dates = t.points.map((p) => p.date);
    const incompletePts = t.points.filter((p) => p.incomplete);
    document.getElementById('trend-note').hidden = incompletePts.length === 0;

    const chart = echarts.init(host);
    charts.push(chart);
    chart.setOption({
      animationDuration: 400,
      grid: { left: 70, right: 24, top: 36, bottom: 64 },
      legend: {
        top: 0, left: 0, icon: 'rect', itemWidth: 12, itemHeight: 3,
        textStyle: { color: C.text, fontSize: 11, fontFamily: C.fontUi },
        data: ['總市值', '累計淨投入']
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
          name: '部分標的當日無價格', type: 'scatter',
          data: incompletePts.map((p) => [p.date, Number(p.total_value)]),
          symbolSize: 8,
          itemStyle: { color: 'transparent', borderColor: C.amber, borderWidth: 1.5 },
          tooltip: { show: false }, legendHoverLink: false
        }
      ]
    });

    /* range buttons: 1M / 3M / 6M / 全部 */
    const last = new Date(dates[dates.length - 1]);
    document.querySelectorAll('.range-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.range-btn').forEach((b) => b.classList.remove('active'));
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
        orient: 'vertical', right: 8, top: 'middle',
        icon: 'rect', itemWidth: 10, itemHeight: 10,
        textStyle: { color: C.text, fontSize: 11, fontFamily: C.fontNum },
        formatter: (name) =>
          name + '  ' + f.money(a.by_sector[name], a.reporting_currency) +
          '  ' + f.pct(a.weights[name])
      },
      series: [{
        type: 'pie', radius: ['52%', '76%'], center: ['32%', '50%'],
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

  /* ============ H1. 年度股利 stacked bar (原幣金額) ============ */
  function initDividends() {
    buildPalette();
    const host = document.getElementById('dividend-chart');
    const dv = D.dividends;
    if (!dv || !dv.by_year || dv.by_year.length === 0) {
      host.replaceChildren(window.emptyState('尚無股利資料'));
      host.style.height = 'auto';
      return;
    }
    const years = dv.by_year.map((y) => String(y.year));
    const ccys = [];
    dv.by_year.forEach((y) => Object.keys(y.by_currency).forEach((c) => {
      if (!ccys.includes(c)) ccys.push(c);
    }));
    const chart = echarts.init(host);
    charts.push(chart);
    chart.setOption({
      grid: { left: 60, right: 16, top: 30, bottom: 28 },
      legend: {
        top: 0, left: 0, icon: 'rect', itemWidth: 10, itemHeight: 10,
        textStyle: { color: C.text, fontSize: 11, fontFamily: C.fontNum }
      },
      tooltip: {
        ...baseTooltip, trigger: 'axis', axisPointer: { type: 'shadow' },
        formatter: (params) => {
          let html = '<div style="font-size:11px;color:' + C.faint + '">' +
                     params[0].axisValue + ' 年（原幣金額）</div>';
          params.forEach((p) => {
            if (p.value === null || p.value === undefined) return;
            html += '<div>' + p.seriesName + '&nbsp;&nbsp;<b>' +
                    f.money(p.value, p.seriesName) + '</b></div>';
          });
          return html;
        }
      },
      xAxis: {
        type: 'category', data: years,
        axisLine: { lineStyle: { color: C.grid } },
        axisLabel: { color: C.text, fontSize: 11, fontFamily: C.fontNum },
        axisTick: { show: false }
      },
      yAxis: {
        type: 'value',
        splitLine: { lineStyle: { color: C.grid, type: 'dashed' } },
        axisLabel: { color: C.faint, fontSize: 10, fontFamily: C.fontNum }
      },
      series: ccys.map((ccy) => ({
        name: ccy, type: 'bar', stack: 'div', barWidth: 36,
        itemStyle: { color: C.ccy[ccy] || '#777' },
        data: dv.by_year.map((y) =>
          y.by_currency[ccy] !== undefined ? Number(y.by_currency[ccy]) : null)
      }))
    });
  }

  function initAll() {
    buildPalette();
    initTrend();
    initSector();
    initDividends();
  }

  /* ============ boot ============ */
  /* Await the SAME shared /api/dashboard promise as app.js (created by whichever script
     runs first) before building charts, so the trend/sector/dividend series read real
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
  window.addEventListener('resize', () => charts.forEach((c) => c.resize()));
  window.addEventListener('pd-theme-change', () => {
    if (!D) return;  // nothing built yet
    charts.forEach((c) => c.dispose());
    charts.length = 0;
    initAll();
  });

  boot();
})();
