/* portfolio-dash — 股利總覽: the ONE consolidated dividend surface (FU-D47).

   Owner ruling (r5 mini-spec): the legacy 年度股利 chart + 除息日曆 panels (app.js /
   charts.js) and the r4 股利收入 card (FU-D38) are replaced by this single surface.
   Composition (dense, data-first):
     (a) headline strip — per-ccy 實收 tiles (TTM big number + 本年/歷年累計 sub-lines)
         beside the forecast-only 年度預估 tile (amber 「預估・僅供參考」 badge);
     (b) ONE yearly received-bars chart (dividends.by_year, grouped per ccy; the current
         partial year drawn lighter + dashed). The projection is NOT overlaid on the
         chart — an amber forecast mark inside a received-cash chart risks reading as
         received money, so it stays text-only in the headline tile;
     (c) the full 除息日曆 list (ex-date block, symbol → detail drawer, 發放日,
         per-share amount) in a bounded scroll region;
     (d) 回本進度 strip — per-holding cumulative cash dividends (dividend_portion) and
         payback_ratio (= cumulative dividends / original cost, DISPLAY-ONLY), the
         attribution the yearly chart cannot show.

   DISPLAY-ONLY attribution. Dividends are ALREADY folded into adjusted cost (see
   rules/domain-ledger.md); nothing here feeds returns, and per-currency figures are
   NEVER summed across currencies. The card does NO money arithmetic — every number is
   a payload Decimal STRING routed through window.fmt. The former client-side 入帳預覽 /
   年內股利預估 float estimates are gone: dividend_projection (server-computed) is the
   only projection. Number() appears solely for plotting geometry, bar widths, and
   sort/filter ordering — never to produce a displayed amount.

   Reads the SAME /api/dashboard payload as app.js / charts.js via the shared
   window.pdDashboard promise (exactly one network request regardless of script order).
   NEVER edits app.js / charts.js / styles.css (parallel-wave ownership); it reuses the
   generic .exdiv-item / .sym-link building blocks from styles.css and injects its own
   scoped <style id="dvc-styles">. Theme colours come from CSS custom properties, so
   both light/dark recolour automatically (the ECharts instance is rebuilt on
   pd-theme-change, mirroring charts.js). */
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
  function openDrawer(symbol) {
    if (window.pdOpenSymbol) window.pdOpenSymbol(symbol);
  }

  /* ---- scoped styles (injected once; all colours reference theme tokens) ---- */
  function injectStyles() {
    if (document.getElementById('dvc-styles')) return;
    const css = `
#dvc-body{min-height:340px}
#dividend-income-card .dvc-caption{padding:2px 16px 10px;font-size:11.5px;color:var(--text-3);
  font-family:var(--font-ui);line-height:1.55}
#dividend-income-card .dvc-section{padding:0 16px 14px;min-width:0}
#dividend-income-card .dvc-sec-label{display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  font-size:11px;letter-spacing:.03em;color:var(--text-2);font-family:var(--font-ui);margin:2px 0 9px}
#dividend-income-card .dvc-sec-note{color:var(--text-3);font-family:var(--font-num);margin-left:auto}
#dividend-income-card .dvc-head{display:flex;flex-wrap:wrap;gap:10px;align-items:stretch}
#dividend-income-card .dvc-stat{min-width:150px;padding:9px 14px;border:1px solid var(--border);
  border-radius:var(--radius);background:var(--panel-2)}
#dividend-income-card .dvc-stat-ccy{font-size:11px;color:var(--text-3);font-family:var(--font-num);
  letter-spacing:.06em}
#dividend-income-card .dvc-stat-val{font-size:20px;color:var(--text);font-family:var(--font-num);
  font-weight:600;margin-top:2px;white-space:nowrap}
#dividend-income-card .dvc-stat-unit{font-size:12px;color:var(--text-3);margin-left:5px;font-weight:400}
#dividend-income-card .dvc-stat-sub{font-size:11px;color:var(--text-3);font-family:var(--font-num);
  margin-top:4px;white-space:nowrap}
#dividend-income-card .dvc-badge{display:inline-block;font-size:10.5px;padding:1px 8px;
  border-radius:999px;background:var(--amber-soft);color:var(--amber);font-family:var(--font-ui);
  letter-spacing:.02em}
#dividend-income-card .dvc-proj{flex:1 1 240px;min-width:220px;padding:9px 14px;
  border:1px dashed var(--border);border-radius:var(--radius);background:transparent}
#dividend-income-card .dvc-proj-head{display:flex;align-items:center;gap:8px;font-size:11px;
  color:var(--text-3);font-family:var(--font-ui)}
#dividend-income-card .dvc-proj-row{display:flex;justify-content:space-between;gap:12px;
  align-items:baseline;font-family:var(--font-num);font-size:12.5px;color:var(--text-2);padding:3px 0}
#dividend-income-card .dvc-proj-row .k{color:var(--text-3);font-family:var(--font-ui);font-size:12px}
#dividend-income-card .dvc-proj-row b{color:var(--text);font-weight:600}
#dividend-income-card .dvc-proj-row .sub{color:var(--text-3);font-size:11.5px;margin-left:6px}
#dividend-income-card .dvc-grid{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr)}
@media (max-width: 1100px){#dividend-income-card .dvc-grid{grid-template-columns:1fr}}
#dividend-income-card .dvc-scroll{overflow-x:auto}
#dividend-income-card .dvc-chart{width:100%;max-width:100%;height:252px;min-width:280px}
#dividend-income-card .dvc-chart-note{font-size:11px;color:var(--text-3);font-family:var(--font-num);
  padding:4px 0 0}
#dividend-income-card .dvc-cal{display:flex;flex-direction:column;gap:8px;max-height:296px;
  overflow-y:auto;padding-right:2px}
#dividend-income-card .dvc-pb{display:flex;flex-wrap:wrap;gap:8px 18px}
#dividend-income-card .dvc-pb-item{display:flex;align-items:center;gap:8px;padding:6px 10px;
  border:1px solid var(--border-soft);border-radius:var(--radius);background:var(--panel-2)}
#dividend-income-card .dvc-pb-track{display:inline-block;width:64px;height:5px;border-radius:3px;
  background:var(--border);overflow:hidden}
#dividend-income-card .dvc-pb-fill{display:block;height:100%;border-radius:3px;background:var(--accent)}
#dividend-income-card .dvc-pb-pct{font-family:var(--font-num);font-size:12px;color:var(--text);
  font-weight:600;white-space:nowrap}
#dividend-income-card .dvc-pb-amt{font-family:var(--font-num);font-size:11px;color:var(--text-3);
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

  /* ---- (a) headline strip: per-ccy 實收 tiles + the forecast-only 預估 tile ---- */
  function renderHeadline(D, host, curYear) {
    const dv = D.dividends || {};
    const ttm = dv.ttm_net || {};
    const total = dv.total_by_currency || {};
    const byYear = dv.by_year || [];
    const curRow = byYear.find((y) => y.year === curYear);
    const curByCcy = (curRow && curRow.by_currency) || {};
    /* Currency order: all-time keys first (superset of TTM), then any TTM stragglers. */
    const ccys = Object.keys(total);
    Object.keys(ttm).forEach((c) => { if (!ccys.includes(c)) ccys.push(c); });

    const sec = section('實收股利（實際淨入帳，各幣別分列）');
    const strip = elc('div', 'dvc-head');

    if (!ccys.length) {
      strip.appendChild(emptyState('尚無現金股利入帳'));
    } else {
      ccys.forEach((ccy) => {
        const stat = elc('div', 'dvc-stat');
        stat.appendChild(elc('div', 'dvc-stat-ccy', ccy + '・近 12 個月'));
        const val = elc('div', 'dvc-stat-val');
        /* A ccy with history but nothing in the trailing window shows the null glyph —
           the 累計 sub-line below carries its story; never fabricate a "0" amount. */
        val.appendChild(document.createTextNode(
          ttm[ccy] !== undefined ? f.money(ttm[ccy], ccy) : f.NULL_GLYPH));
        val.appendChild(elc('span', 'dvc-stat-unit', ccy));
        stat.appendChild(val);
        const subParts = [];
        if (curByCcy[ccy] !== undefined) {
          subParts.push('本年 ' + f.money(curByCcy[ccy], ccy));
        }
        if (total[ccy] !== undefined) {
          subParts.push('歷年累計 ' + f.money(total[ccy], ccy));
        }
        if (subParts.length) {
          stat.appendChild(elc('div', 'dvc-stat-sub', subParts.join('・')));
        }
        strip.appendChild(stat);
      });
    }

    /* Forecast tile — server-computed declared-only projection; FORECAST-ONLY labeling
       (rebate-forecast precedent): never enters cost, P&L, or returns. */
    const proj = D.dividend_projection;
    const tile = elc('div', 'dvc-proj');
    const head = elc('div', 'dvc-proj-head');
    head.appendChild(elc('span', null,
      ((proj && proj.year) || curYear) + ' 年度預估（已宣告事件）'));
    head.appendChild(elc('span', 'dvc-badge', '預估・僅供參考'));
    tile.appendChild(head);
    const byCcy = (proj && proj.by_currency) || {};
    const projKeys = Object.keys(byCcy);
    if (!projKeys.length) {
      tile.appendChild(emptyState('本年度尚無已宣告股利可估算'));
    } else {
      projKeys.forEach((ccy) => {
        const c = byCcy[ccy];
        const row = elc('div', 'dvc-proj-row');
        row.appendChild(elc('span', 'k', ccy + ' 預估淨額'));
        const right = elc('span', null);
        const b = elc('b');
        b.textContent = f.money(c.declared_net, ccy) + ' ' + ccy;
        right.appendChild(b);
        right.appendChild(elc('span', 'sub',
          '毛額 ' + f.money(c.declared_gross, ccy) + '・' + c.events + ' 筆已宣告'));
        row.appendChild(right);
        tile.appendChild(row);
      });
    }
    strip.appendChild(tile);

    sec.appendChild(strip);
    host.appendChild(sec);
  }

  /* ---- (c) 除息日曆 — full upcoming list in a bounded scroll region ---- */
  function renderCalendar(D, host) {
    const cal = D.ex_dividend_calendar || [];
    const sec = section('除息日曆');
    if (!cal.length) {
      sec.appendChild(emptyState('近期無除息事件'));
      host.appendChild(sec);
      return;
    }
    const lbl = sec.querySelector('.dvc-sec-label');
    lbl.appendChild(elc('span', 'dvc-sec-note',
      '共 ' + cal.length + ' 筆・下次 ' + f.date(cal[0].ex_date) + ' ' + cal[0].symbol));
    const list = elc('div', 'dvc-cal');
    cal.forEach((e) => {
      const item = elc('div', 'exdiv-item');
      const dt = elc('div', 'exdiv-date');
      dt.appendChild(elc('span', 'mm', e.ex_date.slice(0, 7)));
      dt.appendChild(elc('span', 'dd', e.ex_date.slice(8, 10)));
      dt.title = '除息日 ' + f.date(e.ex_date);
      item.appendChild(dt);
      const main = elc('div', 'exdiv-main');
      const sym = elc('div', 'exdiv-sym sym-link');
      sym.title = '點擊查看個股詳情';
      sym.addEventListener('click', () => openDrawer(e.symbol));
      sym.appendChild(elc('span', 'sym-code', e.symbol));
      sym.appendChild(elc('span', 'sym-name', e.name));
      main.appendChild(sym);
      if (e.pay_date) {
        main.appendChild(elc('span', 'exdiv-pay', '發放日 ' + f.date(e.pay_date)));
      }
      item.appendChild(main);
      const amt = elc('div', 'exdiv-amt');
      if (e.cash_amount !== null && e.cash_amount !== undefined) {
        amt.appendChild(elc('span', 'a', f.price(e.cash_amount, e.currency)));
        amt.appendChild(elc('span', 'c', (e.currency || '') + ' / 股'));
      } else {
        amt.appendChild(elc('span', 'a', f.NULL_GLYPH));
      }
      item.appendChild(amt);
      list.appendChild(item);
    });
    sec.appendChild(list);
    host.appendChild(sec);
  }

  /* ---- (d) 回本進度 strip — served per-holding attribution (display-only) ---- */
  function renderPayback(D, host) {
    const rows = (D.holdings || []).filter((h) =>
      h.payback_ratio !== null && h.payback_ratio !== undefined &&
      Number(h.payback_ratio) > 0);
    if (!rows.length) return;  // nothing to attribute — no empty-state duplication
    /* Number() for ORDERING only (same class as chart geometry); display via fmt. */
    rows.sort((a, b) => Number(b.payback_ratio) - Number(a.payback_ratio));
    const sec = section('回本進度（累計現金股利 ÷ 原始投入成本・僅供展示）');
    const strip = elc('div', 'dvc-pb');
    rows.slice(0, 6).forEach((h) => {
      const item = elc('div', 'dvc-pb-item sym-link');
      item.title = h.name + '・點擊查看個股詳情';
      item.addEventListener('click', () => openDrawer(h.symbol));
      item.appendChild(elc('span', 'sym-code', h.symbol));
      const track = elc('span', 'dvc-pb-track');
      const fill = elc('span', 'dvc-pb-fill');
      fill.style.width = Math.min(Number(h.payback_ratio) * 100, 100) + '%';
      track.appendChild(fill);
      item.appendChild(track);
      item.appendChild(elc('span', 'dvc-pb-pct', f.pct(h.payback_ratio)));
      if (h.dividend_portion !== null && h.dividend_portion !== undefined) {
        item.appendChild(elc('span', 'dvc-pb-amt',
          '累計 ' + f.money(h.dividend_portion, h.quote_ccy) + ' ' + h.quote_ccy));
      }
      strip.appendChild(item);
    });
    sec.appendChild(strip);
    host.appendChild(sec);
  }

  /* ---- full card render ---- */
  function render(D, host) {
    host.replaceChildren();
    const curYear = Number(String(D.as_of || '').slice(0, 4));

    /* Double-counting guard copy: this card is a distribution view, not income on top. */
    host.appendChild(elc('div', 'dvc-caption',
      '股利已計入調整成本，此為分佈展示，不另計入報酬。各幣別分列，不可跨幣加總。'));

    /* (a) headline strip: 實收 tiles + forecast-only 預估 tile. */
    renderHeadline(D, host, curYear);

    /* (b)+(c) main grid: ONE yearly chart beside the 除息日曆 list. */
    const grid = elc('div', 'dvc-grid');
    const byYear = (D.dividends && D.dividends.by_year) || [];
    const chartSec = section('歷年實收股利（原幣金額）');
    grid.appendChild(chartSec);
    host.appendChild(grid);   // attach BEFORE echarts.init so the host has layout
    if (!byYear.length) {
      chartSec.appendChild(emptyState('尚無股利資料'));
    } else {
      const scroll = elc('div', 'dvc-scroll');
      const chartHost = elc('div', 'dvc-chart');
      chartHost.id = 'dvc-chart';
      scroll.appendChild(chartHost);
      chartSec.appendChild(scroll);
      if (byYear.some((y) => y.year === curYear)) {
        chartSec.appendChild(elc('div', 'dvc-chart-note',
          '＊' + curYear + ' 為當年度累計（尚未結束，較淺、虛線標示）'));
      }
      initChart(D, chartHost, curYear);
    }
    renderCalendar(D, grid);

    /* (d) 回本進度 attribution strip (rendered only when a holding has payback). */
    renderPayback(D, host);
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
