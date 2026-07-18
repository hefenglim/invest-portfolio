/* portfolio-dash — 股利收入 card (FU-D38).

   A self-contained, additive dashboard surface with four blocks:
     (a) 近 12 個月實收股利 (TTM) headline, per currency (from dividends.ttm_net);
     (b) 歷年實收股利 grouped bar chart (dividends.by_year), current year marked partial;
     (c) 本年度預估年度股利 (dividend_projection) — FORECAST-ONLY, 「預估・僅供參考」;
     (d) 近期除息預覽 (ex_dividend_calendar), compact next-N list.

   DISPLAY-ONLY attribution. Dividends are ALREADY folded into adjusted cost (see
   rules/domain-ledger.md); nothing here feeds returns, and per-currency figures are
   NEVER summed across currencies. The card does no money arithmetic — every number is a
   payload Decimal STRING routed through window.fmt (display-only coercion; the chart's
   Number() calls are plotting geometry, not money of record — same precedent as charts.js).

   Reads the SAME /api/dashboard payload as app.js / charts.js via the shared
   window.pdDashboard promise (exactly one network request regardless of script order).
   NEVER edits app.js / charts.js / styles.css (parallel-wave ownership). Its own scoped
   styles live in the injected <style id="dvc-styles"> block; theme colours come from CSS
   custom properties, so both light/dark recolour automatically (the ECharts instance is
   rebuilt on pd-theme-change, mirroring charts.js). */
(function () {
  'use strict';
  const f = window.fmt;

  let dvcChart = null;    // lazy ECharts instance (only created when by_year has data)
  let lastData = null;    // cached payload for theme re-render
  let curYearCache = null;

  /* ---- tiny DOM helpers (local; do not depend on app.js internals) ---- */
  function elc(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function section(label) {
    const sec = elc('div', 'dvc-section');
    const lbl = elc('div', 'dvc-sec-label');
    lbl.appendChild(elc('span', null, label));
    sec.appendChild(lbl);
    return sec;
  }
  function emptyState(msg) {
    const wrap = elc('div', 'dvc-empty');
    wrap.appendChild(elc('span', 'dvc-empty-glyph', '∅'));
    wrap.appendChild(elc('span', 'dvc-empty-msg', msg));
    return wrap;
  }

  /* ---- scoped styles (injected once; all colours reference theme tokens) ---- */
  function injectStyles() {
    if (document.getElementById('dvc-styles')) return;
    const css = `
#dividend-income-card .dvc-caption{padding:2px 16px 12px;font-size:11.5px;color:var(--text-3);
  font-family:var(--font-ui);line-height:1.55}
#dividend-income-card .dvc-section{padding:0 16px 14px}
#dividend-income-card .dvc-sec-label{display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  font-size:11px;letter-spacing:.03em;color:var(--text-2);font-family:var(--font-ui);margin:2px 0 9px}
#dividend-income-card .dvc-ttm-grid{display:flex;flex-wrap:wrap;gap:10px}
#dividend-income-card .dvc-stat{min-width:118px;padding:9px 14px;border:1px solid var(--border);
  border-radius:var(--radius);background:var(--panel-2)}
#dividend-income-card .dvc-stat-ccy{font-size:11px;color:var(--text-3);font-family:var(--font-num);
  letter-spacing:.06em}
#dividend-income-card .dvc-stat-val{font-size:20px;color:var(--text);font-family:var(--font-num);
  font-weight:600;margin-top:2px;white-space:nowrap}
#dividend-income-card .dvc-stat-unit{font-size:12px;color:var(--text-3);margin-left:5px;font-weight:400}
#dividend-income-card .dvc-scroll{overflow-x:auto}
#dividend-income-card .dvc-chart{width:100%;max-width:100%;height:236px;min-width:280px}
#dividend-income-card .dvc-chart-note{font-size:11px;color:var(--text-3);font-family:var(--font-num);
  padding:4px 0 0}
#dividend-income-card .dvc-badge{display:inline-block;font-size:10.5px;padding:1px 8px;
  border-radius:999px;background:var(--amber-soft);color:var(--amber);font-family:var(--font-ui);
  letter-spacing:.02em}
#dividend-income-card .dvc-proj{border:1px dashed var(--border);border-radius:var(--radius);
  background:transparent;padding:9px 14px}
#dividend-income-card .dvc-proj-row{display:flex;justify-content:space-between;gap:12px;
  align-items:baseline;font-family:var(--font-num);font-size:12.5px;color:var(--text-2);padding:3px 0}
#dividend-income-card .dvc-proj-row .k{color:var(--text-3);font-family:var(--font-ui);font-size:12px}
#dividend-income-card .dvc-proj-row b{color:var(--text);font-weight:600}
#dividend-income-card .dvc-proj-row .sub{color:var(--text-3);font-size:11.5px;margin-left:6px}
#dividend-income-card .dvc-cal{display:flex;flex-direction:column;gap:0}
#dividend-income-card .dvc-cal-item{display:flex;align-items:center;gap:10px;padding:7px 6px}
#dividend-income-card .dvc-cal-item+.dvc-cal-item{border-top:1px solid var(--border-soft)}
#dividend-income-card .dvc-cal-date{font-family:var(--font-num);font-size:12px;color:var(--accent);
  white-space:nowrap;min-width:80px}
#dividend-income-card .dvc-cal-main{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
#dividend-income-card .dvc-cal-sym{font-family:var(--font-num);font-size:12.5px;color:var(--text)}
#dividend-income-card .dvc-cal-name{font-size:11px;color:var(--text-3);margin-left:6px}
#dividend-income-card .dvc-cal-amt{font-family:var(--font-num);font-size:12px;color:var(--text-2);
  white-space:nowrap}
#dividend-income-card .dvc-empty{display:flex;align-items:center;gap:8px;padding:8px 2px;
  color:var(--text-3);font-size:12px;font-family:var(--font-ui)}
#dividend-income-card .dvc-empty-glyph{opacity:.6}
`;
    const style = elc('style');
    style.id = 'dvc-styles';
    style.textContent = css;
    document.head.appendChild(style);
  }

  /* ---- ECharts palette from the live theme tokens (rebuilt each render) ---- */
  function palette() {
    const s = getComputedStyle(document.documentElement);
    const V = (n) => s.getPropertyValue(n).trim();
    return {
      text: V('--text-2'), faint: V('--text-3'), grid: V('--border'),
      panelBg: V('--panel-2'), tipBorder: V('--border'), textStrong: V('--text'),
      ccy: { TWD: V('--accent'), USD: V('--series-usd'), MYR: V('--series-myr') },
      fontNum: "'IBM Plex Mono', monospace", fontUi: "'Noto Sans TC', sans-serif"
    };
  }

  /* ---- (b) 歷年實收股利 grouped bar; lazy — only invoked when by_year has data ---- */
  function initChart(D, hostEl, curYear) {
    if (!window.echarts) return;
    curYearCache = curYear;
    const byYear = D.dividends.by_year;
    const years = byYear.map((y) => String(y.year));
    const ccys = [];
    byYear.forEach((y) => Object.keys(y.by_currency).forEach((c) => {
      if (!ccys.includes(c)) ccys.push(c);
    }));
    const C = palette();
    if (dvcChart) { dvcChart.dispose(); dvcChart = null; }
    dvcChart = window.echarts.init(hostEl);
    dvcChart.setOption({
      grid: { left: 58, right: 14, top: 26, bottom: 26 },
      legend: {
        top: 0, left: 0, icon: 'rect', itemWidth: 10, itemHeight: 10,
        textStyle: { color: C.text, fontSize: 11, fontFamily: C.fontNum }
      },
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'shadow' },
        backgroundColor: C.panelBg, borderColor: C.tipBorder,
        textStyle: { color: C.textStrong, fontSize: 12, fontFamily: C.fontNum },
        extraCssText: 'box-shadow: 0 6px 24px rgba(0,0,0,0.25);',
        formatter: (params) => {
          const yr = params[0].axisValue;
          let html = '<div style="font-size:11px;color:' + C.faint + '">' + yr +
            ' 年（原幣金額）' + (Number(yr) === curYear ? '・當年度累計' : '') + '</div>';
          params.forEach((p) => {
            if (p.value === null || p.value === undefined) return;
            html += '<div>' + p.marker + p.seriesName + '&nbsp;&nbsp;<b>' +
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
        axisLabel: {
          color: C.faint, fontSize: 10, fontFamily: C.fontNum,
          formatter: (v) => (Math.abs(v) >= 1000
            ? (v / 1000).toLocaleString('en-US') + 'k' : String(v))
        }
      },
      series: ccys.map((ccy) => ({
        name: ccy, type: 'bar', barMaxWidth: 30,
        itemStyle: { color: C.ccy[ccy] || '#8a96a6' },
        /* by_currency values are Decimal STRINGS → Number() for plotting geometry only.
           The current (partial) year gets a lighter, dashed-border bar so it never reads
           as a completed full-year figure. */
        data: byYear.map((y) => {
          const raw = y.by_currency[ccy];
          if (raw === undefined) return null;
          const v = Number(raw);
          if (y.year === curYear) {
            return {
              value: v,
              itemStyle: {
                color: C.ccy[ccy] || '#8a96a6', opacity: 0.5,
                borderColor: C.ccy[ccy] || '#8a96a6', borderType: 'dashed', borderWidth: 1
              }
            };
          }
          return v;
        })
      }))
    });
  }

  /* ---- full card render ---- */
  function render(D, host) {
    host.replaceChildren();

    /* Double-counting guard copy: this card is a distribution view, not income on top. */
    host.appendChild(elc('div', 'dvc-caption',
      '股利已計入調整成本，此卡為分佈展示，不另計入報酬。各幣別分列，不可跨幣加總。'));

    /* (a) TTM headline — trailing 12 months of actual net cash received, per currency. */
    const ttm = (D.dividends && D.dividends.ttm_net) || {};
    const ttmSec = section('近 12 個月實收股利（實際淨入帳）');
    const ttmKeys = Object.keys(ttm);
    if (!ttmKeys.length) {
      ttmSec.appendChild(emptyState('近 12 個月尚無現金股利入帳'));
    } else {
      const grid = elc('div', 'dvc-ttm-grid');
      ttmKeys.forEach((ccy) => {
        const stat = elc('div', 'dvc-stat');
        stat.appendChild(elc('div', 'dvc-stat-ccy', ccy));
        const val = elc('div', 'dvc-stat-val');
        val.appendChild(document.createTextNode(f.money(ttm[ccy], ccy)));
        val.appendChild(elc('span', 'dvc-stat-unit', ccy));
        stat.appendChild(val);
        grid.appendChild(stat);
      });
      ttmSec.appendChild(grid);
    }
    host.appendChild(ttmSec);

    /* (b) yearly bar chart — lazy-init only when by_year has data. The section is
       attached to the live #dvc-body BEFORE echarts.init so the chart host already has
       layout (a detached host would init at 0x0 and need a resize to appear). */
    const byYear = (D.dividends && D.dividends.by_year) || [];
    const chartSec = section('歷年實收股利（原幣金額）');
    host.appendChild(chartSec);
    if (!byYear.length) {
      chartSec.appendChild(emptyState('尚無股利資料'));
    } else {
      const scroll = elc('div', 'dvc-scroll');
      const chartHost = elc('div', 'dvc-chart');
      chartHost.id = 'dvc-chart';
      scroll.appendChild(chartHost);
      chartSec.appendChild(scroll);
      const curYear = Number(String(D.as_of || '').slice(0, 4));
      if (byYear.some((y) => y.year === curYear)) {
        chartSec.appendChild(elc('div', 'dvc-chart-note',
          '＊' + curYear + ' 為當年度累計（尚未結束，較淺、虛線標示）'));
      }
      initChart(D, chartHost, curYear);
    }

    /* (c) declared projection — FORECAST-ONLY (rebate-forecast precedent). */
    const proj = D.dividend_projection;
    const projSec = section('本年度預估年度股利');
    projSec.querySelector('.dvc-sec-label').appendChild(
      elc('span', 'dvc-badge', '預估・僅供參考'));
    const byCcy = (proj && proj.by_currency) || {};
    const projKeys = Object.keys(byCcy);
    if (!projKeys.length) {
      projSec.appendChild(emptyState('本年度尚無已宣告股利可估算'));
    } else {
      const box = elc('div', 'dvc-proj');
      projKeys.forEach((ccy) => {
        const c = byCcy[ccy];
        const row = elc('div', 'dvc-proj-row');
        row.appendChild(elc('span', 'k', proj.year + ' 年 ' + ccy + ' 預估淨額'));
        const right = elc('span', null);
        const b = elc('b');
        b.textContent = f.money(c.declared_net, ccy) + ' ' + ccy;
        right.appendChild(b);
        right.appendChild(elc('span', 'sub',
          '毛額 ' + f.money(c.declared_gross, ccy) + '・' + c.events + ' 筆已宣告'));
        row.appendChild(right);
        box.appendChild(row);
      });
      projSec.appendChild(box);
    }
    host.appendChild(projSec);

    /* (d) compact ex-dividend calendar — next N upcoming ex-dates. */
    const cal = D.ex_dividend_calendar || [];
    const calSec = section('近期除息預覽');
    if (!cal.length) {
      calSec.appendChild(emptyState('近期無除息事件'));
    } else {
      const N = 5;
      const list = elc('div', 'dvc-cal');
      cal.slice(0, N).forEach((e) => {
        const item = elc('div', 'dvc-cal-item');
        item.appendChild(elc('span', 'dvc-cal-date', f.date(e.ex_date)));
        const main = elc('div', 'dvc-cal-main');
        main.appendChild(elc('span', 'dvc-cal-sym', e.symbol));
        main.appendChild(elc('span', 'dvc-cal-name', e.name));
        item.appendChild(main);
        const amt = elc('span', 'dvc-cal-amt',
          e.cash_amount !== null && e.cash_amount !== undefined
            ? f.price(e.cash_amount, e.currency) + ' ' + (e.currency || '') + ' / 股'
            : f.NULL_GLYPH);
        item.appendChild(amt);
        list.appendChild(item);
      });
      calSec.appendChild(list);
      if (cal.length > N) {
        calSec.appendChild(elc('div', 'dvc-chart-note',
          '另有 ' + (cal.length - N) + ' 筆除息事件（詳見下方除息日曆）'));
      }
    }
    host.appendChild(calSec);
  }

  /* ---- theme + resize: rebuild only the chart; the rest is CSS-var driven ---- */
  window.addEventListener('pd-theme-change', () => {
    if (!dvcChart || !lastData) return;
    const hostEl = document.getElementById('dvc-chart');
    if (hostEl) initChart(lastData, hostEl, curYearCache);
  });
  window.addEventListener('resize', () => { if (dvcChart) dvcChart.resize(); });

  /* ---- boot: await the shared payload, then render. On failure, stay silent —
     app.js already surfaces the load-failure UI (e2e asserts ZERO console errors). ---- */
  async function boot() {
    injectStyles();
    const host = document.getElementById('dvc-body');
    if (!host) return;
    let D;
    try {
      D = await (window.pdDashboard ||
        (window.pdDashboard = window.pdApi.get('/api/dashboard')));
    } catch (e) {
      return;
    }
    lastData = D;
    render(D, host);
  }

  boot();
})();
