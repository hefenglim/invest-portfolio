/* portfolio-dash — 個股詳情抽屜.
   openSymbolDrawer(symbol): price history + cost lines + dividend/trade markers,
   報酬貢獻拆分 (capital gain vs dividends), 配息史, 已實現記錄, 試算 (compute-only).

   DATA SOURCE (spec 19, Task 2.3): the drawer fetches its OWN data on open —
     · GET /api/symbol/{symbol}/detail  → price_history / cost_basis / dividend_events /
       trade_events / realized_rows (all Decimal money/price as STRINGS).
     · the shared window.pdDashboard promise (GET /api/dashboard, reused from app.js /
       charts.js / alerts.js) → the rich holding summary `h` (name, market, market_price,
       weight, market_value, unrealized_pnl, capital_gain, …) which the detail endpoint
       does NOT carry. Fetched ONCE per page; created here when not already present.
   Money/price values are Decimal STRINGS — displayed via window.fmt (f.*), which coerces
   internally; the drawer NEVER sums or compares them as money. The 試算 (what-if) block is
   NO LONGER a local-compute exception (R7 A4): it POSTs /api/whatif and renders the backend's
   OLD-vs-NEW figures verbatim — zero front-end money arithmetic (the prior spec-03 local
   fee/P&L estimate is retired).

   Requires: api.js (window.pdApi), format.js, echarts. */
(function () {
  'use strict';
  const f = window.fmt;
  const $ = (s, root) => (root || document).querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  /* Account zh-TW display name — single source of truth is web/names.js (FU-D37,
     window.pdNames). Local delegator with a graceful no-op (id fallback) when names.js
     has not loaded on this page yet (index.html's <script> tag is added by the
     orchestrator sweep). Server-side account.display_name is the planned successor. */
  const acctZh = (id) => (window.pdNames ? window.pdNames.account(id) : id);
  const MARKET_ZH = { TW: '台股', US: '美股', MY: '馬股' };
  /* dividend wire type (lowercase, from /detail) -> display chip label */
  const DIV_TYPE_ZH = { cash: '現金', drip: 'DRIP', stock: '配股', net: '淨額' };

  /* ---- 技術訊號 (rule engine, GET /api/signals/{symbol}) ----
     zh-TW labels for the four v1 rules + their per-rule state vocabulary. Numbers arrive
     as Decimal STRINGS from the API; every DISPLAY routes through f.* (which coerce for
     presentation). TechScore is NOT P&L → neutral styling, never the red/green sign class. */
  const RULE_KEYS = ['trend_filter', 'ma_cross', 'momentum_12_1', 'rsi_regime'];
  const RULE_LABEL = {
    trend_filter: '趨勢濾網', ma_cross: '均線交叉',
    momentum_12_1: '12-1 動能', rsi_regime: 'RSI 情境'
  };
  const RULE_STATE_ZH = {
    trend_filter: {
      above_confirmed: '站上 MA200', below_confirmed: '跌破 MA200',
      above_unconfirmed: '站上（待確認）', below_unconfirmed: '跌破（待確認）',
      in_band: '均線帶內'
    },
    ma_cross: {
      golden: '黃金交叉', death: '死亡交叉',
      fast_above: '短均在上', fast_below: '短均在下', aligned: '均線糾結'
    },
    momentum_12_1: { positive: '正動能', negative: '負動能', flat: '動能持平' },
    rsi_regime: { overbought: '超買', oversold: '超賣', neutral: '中性' }
  };
  /* Shared /api/dashboard promise — the SAME one app.js / charts.js / alerts.js use, so
     opening the drawer on the dashboard reuses the in-flight/resolved payload (one fetch).
     Off-dashboard, this creates it once. holdings[] here is the rich holding summary. */
  function dashboardPromise() {
    return (window.pdDashboard || (window.pdDashboard = window.pdApi.get('/api/dashboard')));
  }

  /* The resolved holdings list (set once both fetches land in openSymbolDrawer); used by
     prev/next cycling. Null until a successful open; cycling is disabled while null. */
  let currentHoldings = null;
  /* The resolved dashboard payload (set alongside currentHoldings). Cached for the shared
     holdings list; the drawer's holding summary now comes from detail.position (the server
     cross-account aggregate), NOT a lookup into this list (round-8.1 Wave A owner #2c). */
  let currentDash = null;

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /* ---------- drawer scaffold ---------- */
  let chart = null;
  function close() {
    const b = $('.sd-backdrop');
    if (b) b.remove();
    if (chart) { chart.dispose(); chart = null; }
    document.removeEventListener('keydown', onKey);
    if (location.hash.indexOf('#sym=') === 0) {
      history.replaceState(null, '', location.pathname + location.search);
    }
  }
  function onKey(e) {
    if (e.key === 'Escape') { close(); return; }
    /* E6: ←/→ 切換上一檔/下一檔持倉（持倉清單來自已載入的 /api/dashboard；未就緒則停用） */
    if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && !e.target.closest('input, textarea, select')) {
      if (!currentHoldings || !currentHoldings.length || !currentSymbol) return;
      const syms = currentHoldings.map((h) => h.symbol);
      const i = syms.indexOf(currentSymbol);
      if (i < 0) return;
      e.preventDefault();
      const next = e.key === 'ArrowRight' ? (i + 1) % syms.length : (i - 1 + syms.length) % syms.length;
      window.openSymbolDrawer(syms[next]);
    }
  }
  let currentSymbol = null;
  /* Monotonic open token: bumped on EVERY drawer open (even a re-open of the SAME symbol).
     Async section work (e.g. the 交易明細 self-fetch + its pager) captures the token in effect
     when it started and drops any reply whose token is stale — so a fetch in flight when the
     drawer is closed / re-opened can never write into a torn-down (or superseded) DOM. The
     `currentSymbol === symbol` guard alone cannot see a same-symbol re-open; this can. */
  let drawerSeq = 0;

  window.openSymbolDrawer = function (symbol) {
    close();
    drawerSeq += 1;
    currentSymbol = symbol;

    /* Synchronous scaffold: backdrop + drawer + keydown are wired immediately so Esc /
       backdrop-click / open-close work even while data is loading; the data-dependent
       head + sections render AFTER both fetches resolve. */
    const backdrop = el('div', 'sd-backdrop');
    const drawer = el('div', 'sd-drawer');
    backdrop.appendChild(drawer);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
    document.addEventListener('keydown', onKey);

    const head = el('div', 'sd-head');
    head.appendChild(el('span', 'sym-code', symbol));
    head.appendChild(el('span', 'sd-loading', '載入中…'));
    const x = el('button', 'sd-close', '✕');
    x.type = 'button';
    x.title = '關閉（Esc）・←/→ 切換持倉';
    x.addEventListener('click', close);
    head.appendChild(x);
    drawer.appendChild(head);

    const body = el('div', 'sd-body');
    drawer.appendChild(body);
    document.body.appendChild(backdrop);

    /* Fetch BOTH in parallel: the per-symbol detail + the shared dashboard payload (the
       latter supplies the rich holding summary `h`). Render only after both land; on any
       failure, show a graceful in-drawer error — never an unhandled rejection (the e2e
       smoke asserts ZERO console errors). */
    Promise.all([
      window.pdApi.get('/api/symbol/' + encodeURIComponent(symbol) + '/detail'),
      dashboardPromise()
    ]).then(([detail, dash]) => {
      if (currentSymbol !== symbol) return;  // a newer open superseded this one
      currentDash = dash || null;
      currentHoldings = (dash && dash.holdings) || [];  // for ←/→ cycling (dashboard order)
      /* The holding summary the drawer renders is the SERVER aggregate (detail.position) — the
         cross-account TOTAL — NOT a single dashboard holding row (round-8.1 Wave A owner #2c). */
      renderDrawer(drawer, head, body, symbol, detail);
    }).catch((err) => {
      if (currentSymbol !== symbol) return;
      head.replaceChildren(el('span', 'sym-code', symbol));
      const x2 = el('button', 'sd-close', '✕'); x2.type = 'button';
      x2.addEventListener('click', close);
      head.appendChild(x2);
      body.replaceChildren(window.emptyState
        ? window.emptyState('標的資料載入失敗，請稍後再試。')
        : el('div', 'sd-empty', '標的資料載入失敗，請稍後再試。'));
      if (window.toast) {
        window.toast('標的資料載入失敗', 'fail', err && err.message ? err.message : undefined);
      }
    });
  };

  /* Render head + sections once both /detail and /dashboard have resolved. The holding
     summary is the SERVER aggregate ``pos`` = detail.position (the cross-account TOTAL: total
     shares / market value / unrealized, blended avg cost) — NOT a single dashboard holding row
     (that was the FIRST account only, understating a symbol held in >1 account: owner #2c).
     ``accts`` = detail.position_accounts is the per-account breakdown behind the aggregate.
     Both are null/empty for an unheld / watchlist symbol. */
  function renderDrawer(drawer, head, body, symbol, detail) {
    const pos = (detail && detail.position) || null;
    const accts = (detail && detail.position_accounts) || [];
    /* head */
    head.replaceChildren();
    head.appendChild(el('span', 'sym-code', symbol));
    if (pos) {
      if (pos.name) head.appendChild(el('span', 'sym-name', pos.name));
      if (pos.board) head.appendChild(el('span', 'board-badge', pos.board));
      /* Aggregate-aware account label: 「N 個帳戶」 when the symbol spans >1 account, else the
         single account's name (owner #2c — the head reflects the aggregate, not one account). */
      const acctLabel = accts.length > 1
        ? (accts.length + ' 個帳戶')
        : (accts.length === 1 ? acctZh(accts[0].account_id) : '');
      head.appendChild(el('span', 'badge',
        MARKET_ZH[pos.market] + (acctLabel ? '・' + acctLabel : '')));
      const price = el('span', 'sd-price');
      if (pos.market_price === null || pos.market_price === undefined) {
        const b = el('span', 'badge badge-missing', '缺價');
        b.title = '無法取得價格資料';
        price.appendChild(b);
      } else {
        price.appendChild(el('span', 'v', f.price(pos.market_price, pos.quote_ccy)));
        price.appendChild(el('span', 'c', pos.quote_ccy));
        if (pos.price_stale) {
          const b = el('span', 'badge badge-stale-mini', '過期');
          b.title = '價格日期 ' + f.date(pos.price_as_of);
          price.appendChild(b);
        }
      }
      head.appendChild(price);
    } else {
      /* Non-held / watchlist: no position summary, but the /detail payload carries the
         registry name (FU-D24). */
      const nm = detail && detail.name;
      if (nm) head.appendChild(el('span', 'sym-name', nm));
      head.appendChild(el('span', 'badge', '非持倉標的'));
      head.appendChild(el('span', 'header-spacer'));
    }
    const x = el('button', 'sd-close', '✕');
    x.type = 'button';
    x.title = '關閉（Esc）・←/→ 切換持倉';
    x.addEventListener('click', close);
    head.appendChild(x);

    /* body */
    body.replaceChildren();
    body.appendChild(chartSection(detail, pos));
    if (pos) {
      body.appendChild(statsSection(pos, accts));
      body.appendChild(signalsSection(symbol));
      body.appendChild(splitSection(pos));
      /* 試算 binds to ONE account (fees/tax are per-account); default to the PRIMARY
         (most-shares) account, which the server returns first in position_accounts. */
      const primary = accts[0] || null;
      if (primary) body.appendChild(simSection(primary));
      body.appendChild(dividendSection(symbol, detail));
      body.appendChild(realizedSection(symbol, detail));
    } else {
      /* Watchlist (unheld) symbol: no position/P&L, but 技術訊號 still matter — a watched
         name is an entry candidate (P2 batch 3). Render the signals section (honest-empty
         when data is thin) alongside the price chart; skip the holding-only sections. */
      body.appendChild(signalsSection(symbol));
      body.appendChild(el('div', 'sd-empty', '此標的不在持倉中（觀察清單標的）— 顯示價格走勢與技術訊號，無部位／損益資料。'));
    }
    /* 交易明細 — the UNIFIED activity list (期初 + 買 + 賣 + 配股/DRIP), rendered from
       detail.activity with a reconciliation footer + (when multi-account) an account filter.
       A CLOSED position is unheld yet still has history, so it renders whenever activity is
       present; null (omitted) for a pure watchlist name with zero activity. */
    const tx = txSection(symbol, detail);
    if (tx) body.appendChild(tx);
    renderChart(detail, pos);
  }

  /* ---------- sections ---------- */
  function secHead(title, sub, extra) {
    const head = el('div', 'sd-sec-head');
    head.appendChild(el('h3', 'sd-sec-title', title));
    if (sub) head.appendChild(el('span', 'sd-sec-sub', sub));
    head.appendChild(el('span', 'spacer'));
    if (extra) head.appendChild(extra);
    return head;
  }

  function chartSection(detail, pos) {
    const sec = el('div', 'sd-section');
    sec.appendChild(secHead('價格與成本', '日線・配息與買賣事件標記'));
    const ph = detail.price_history || {};
    if (!ph.available) {
      const note = ph.note || '無歷史價格資料';
      sec.appendChild(window.emptyState ? window.emptyState(note) : el('div', 'sd-empty', note));
      return sec;
    }
    if (ph.note) sec.appendChild(el('div', 'sd-chart-note', ph.note));
    if (ph.stale && ph.last_date) {
      sec.appendChild(el('div', 'sd-chart-note', '價格過期：最後報價 ' + f.date(ph.last_date)));
    }
    const box = el('div'); box.id = 'sd-chart';
    sec.appendChild(box);
    /* Compact legend for the redesigned buy/sell markers (owner #6: 彩色標籤三角 + 圖例). Shown
       whenever the chart will carry ≥1 buy/sell marker. Buy=green ▲ / sell=red ▼ — the
       owner-signed chart convention (a trading-platform idiom), deliberately distinct from the
       TW P&L sign colours (where red=gain). Inline-styled so it needs no styles.css addition. */
    const hasTrades = (detail.trade_events || []).some(
      (t) => t.side === 'buy' || t.side === 'sell');
    if (hasTrades) {
      const legend = el('div', 'sd-chart-legend');
      legend.style.cssText =
        'display:flex;gap:16px;margin-top:6px;font-size:11px;color:var(--text-3)';
      const item = (glyph, label, color) => {
        const s = el('span');
        const g = el('span', null, glyph);
        g.style.cssText = 'color:' + color + ';font-weight:700;margin-right:4px';
        s.appendChild(g);
        s.appendChild(document.createTextNode(label));
        return s;
      };
      /* --down is green, --up is red in the token set (values, not P&L semantics). */
      legend.appendChild(item('▲', '買', cssVar('--down')));
      legend.appendChild(item('▼', '賣', cssVar('--up')));
      sec.appendChild(legend);
    }
    return sec;
  }

  /* Custom marker paths (owner #6): a filled triangle + a thin stem bar that reaches the price
     point, so buy/sell read at a glance by colour+shape+position. Path box is 0..100; ECharts
     scales it to symbolSize. BUY points UP with the stem at the TOP (marker sits BELOW the
     point); SELL points DOWN with the stem at the BOTTOM (marker sits ABOVE the point). The
     stem is a real (non-zero-area) bar so it renders under a fill-only symbol. */
  const BUY_MARK = 'path://M46,0 L54,0 L54,40 L46,40 Z M8,100 L92,100 L50,40 Z';
  const SELL_MARK = 'path://M46,60 L54,60 L54,100 L46,100 Z M8,0 L92,0 L50,60 Z';
  const TRADE_LABEL_MAX = 8;  // ≤ this many buy/sell markers → always-on labels; else hover-only

  function renderChart(detail, pos) {
    const box = document.getElementById('sd-chart');
    if (!box || !window.echarts) return;
    const ph = detail.price_history || {};
    if (!ph.available || !ph.points || !ph.points.length) return;
    chart = echarts.init(box, null, { renderer: 'canvas' });
    const dates = ph.points.map((p) => p.date);
    /* close is a Decimal STRING from the API; Number() it ONLY to feed the ECharts numeric
       series + markPoint coords (chart plotting). All DISPLAY labels go through f.*. */
    const closes = ph.points.map((p) => Number(p.close));
    const markLines = [];
    const h = pos;  // cost lines anchor to the AGGREGATE blended average cost
    if (h) {
      /* Cost-line labels anchor to the LEFT/START edge (insideStart*), NOT the crowded right
         end where the latest price + the newest trade markers cluster — the owner screenshot
         showed 原始均價 / 調整均價 / 買… / 賣… stacked into an illegible clump on the right.
         EQUAL-average edge case: when the two averages render IDENTICALLY at display precision
         (no dividend adjustment, or a payback that lands within one displayed tick — exactly
         the 原始=調整=1,721.33 screenshot), collapse to ONE combined line + a single「均價」
         label instead of two identical stacked labels. The comparison is on the SERVER-
         formatted display strings (f.price) — a presentation decision, never money arithmetic. */
      const origPx = f.price(h.original_avg, h.quote_ccy);
      const adjPx = f.price(h.adjusted_avg, h.quote_ccy);
      if (origPx === adjPx) {
        markLines.push({ yAxis: Number(h.original_avg), name: '均價',
          lineStyle: { color: cssVar('--series-myr'), type: 'dashed' },
          label: { formatter: '均價 ' + origPx, position: 'insideStartTop', color: cssVar('--series-myr'), fontSize: 10 } });
      } else {
        markLines.push({ yAxis: Number(h.original_avg), name: '原始均價',
          lineStyle: { color: cssVar('--series-gray'), type: 'dashed' },
          label: { formatter: '原始均價 ' + origPx, position: 'insideStartTop', color: cssVar('--text-3'), fontSize: 10 } });
        markLines.push({ yAxis: Number(h.adjusted_avg), name: '調整均價',
          lineStyle: { color: cssVar('--series-myr'), type: 'dashed' },
          label: { formatter: '調整均價 ' + adjPx, position: 'insideStartBottom', color: cssVar('--series-myr'), fontSize: 10 } });
      }
    }
    const closeOn = (date) => {
      let last = closes[0];
      for (let i = 0; i < dates.length; i++) { if (dates[i] > date) break; last = closes[i]; }
      return last;
    };
    const quoteCcy = h ? h.quote_ccy : null;
    const markPoints = [];
    (detail.dividend_events || []).forEach((d) => {
      if (d.date < dates[0]) return;
      const label = DIV_TYPE_ZH[d.type] || d.type;
      markPoints.push({ coord: [d.date, closeOn(d.date)], name: '配息',
        symbol: 'pin', symbolSize: 26, itemStyle: { color: cssVar('--series-myr') },
        label: { show: true, formatter: '息', fontSize: 9, color: '#0c1015' },
        value: label + ' ' + (d.net !== null && d.net !== undefined ? f.money(d.net, d.ccy) + ' ' + (d.ccy || '') : '') });
    });
    /* Buy/sell markers, redesigned (owner #6). Green ▲ (buy) BELOW the point / red ▼ (sell)
       ABOVE the point, each larger + with a stem reaching the price point. An always-on label
       「買N」/「賣N」 shows when trades are SPARSE (≤ TRADE_LABEL_MAX in view) so they never
       clump; otherwise labels are hover-only. The full 「買/賣 N 股 @ price」 is always on the
       hover tooltip via `value`. Opening rows render a small neutral marker (the cost line +
       交易明細 carry their detail). --down=green / --up=red are token VALUES (not P&L sign). */
    const GREEN = cssVar('--down');
    const RED = cssVar('--up');
    const tradesInView = (detail.trade_events || []).filter(
      (t) => (t.side === 'buy' || t.side === 'sell') && t.date >= dates[0]);
    const showTradeLabels = tradesInView.length <= TRADE_LABEL_MAX;
    (detail.trade_events || []).forEach((t) => {
      if (t.date < dates[0]) return;
      if (t.side === 'open') {
        markPoints.push({ coord: [t.date, closeOn(t.date)], name: '期初',
          symbol: 'diamond', symbolSize: 9, itemStyle: { color: cssVar('--series-gray') },
          label: { show: false },
          value: '期初 ' + f.num(t.shares) + ' 股 @ ' + f.price(t.price, quoteCcy) });
        return;
      }
      const isBuy = t.side === 'buy';
      markPoints.push({ coord: [t.date, closeOn(t.date)], name: isBuy ? '買進' : '賣出',
        symbol: isBuy ? BUY_MARK : SELL_MARK, symbolSize: [18, 22],
        /* offset the marker off the point so its stem tip touches (buy below / sell above). */
        symbolOffset: [0, isBuy ? '50%' : '-50%'],
        itemStyle: { color: isBuy ? GREEN : RED },
        label: { show: showTradeLabels, position: isBuy ? 'bottom' : 'top',
          formatter: (isBuy ? '買' : '賣') + f.num(t.shares),
          color: isBuy ? GREEN : RED, fontSize: 10, fontWeight: 'bold' },
        value: (isBuy ? '買 ' : '賣 ') + f.num(t.shares) + ' 股 @ ' + f.price(t.price, quoteCcy) });
    });
    chart.setOption({
      animation: false,
      grid: { left: 8, right: 76, top: 18, bottom: 38, containLabel: true },
      tooltip: {
        trigger: 'axis',
        backgroundColor: cssVar('--panel-2'), borderColor: cssVar('--border'),
        textStyle: { color: cssVar('--text'), fontSize: 11 },
        valueFormatter: (v) => f.price(v, quoteCcy)
      },
      xAxis: { type: 'category', data: dates, boundaryGap: false,
        axisLine: { lineStyle: { color: cssVar('--border') } },
        axisLabel: { color: cssVar('--text-3'), fontSize: 10 } },
      yAxis: { type: 'value', scale: true,
        splitLine: { lineStyle: { color: cssVar('--border-soft') } },
        axisLabel: { color: cssVar('--text-3'), fontSize: 10 } },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 14, bottom: 6,
        borderColor: cssVar('--border'), backgroundColor: 'transparent' }],
      series: [{
        type: 'line', data: closes, showSymbol: false,
        /* Overlap avoidance for any residual label collisions (e.g. two cost lines whose
           averages are close-but-not-identical): ECharts drops the loser rather than letting
           labels overprint. Symbol-only trade markers already carry no label to collide. */
        labelLayout: { hideOverlap: true },
        lineStyle: { color: cssVar('--accent'), width: 1.6 },
        areaStyle: { color: cssVar('--accent-soft') },
        markLine: { symbol: 'none', silent: true, data: markLines },
        markPoint: { data: markPoints,
          tooltip: { formatter: (p) => p.name + '：' + (p.data.value || '') } }
      }]
    });
  }

  /* 部位摘要 — the AGGREGATE across accounts is PRIMARY (owner #2c). `h` is detail.position
     (server-computed cross-account Decimal totals); `accts` is the per-account breakdown,
     rendered as a SECONDARY table only when the symbol spans >1 account. The drawer NEVER
     sums money across accounts — every figure here is a server Decimal STRING via f.*. */
  function statsSection(h, accts) {
    const multi = !!(accts && accts.length > 1);
    const sec = el('div', 'sd-section');
    sec.appendChild(secHead('部位摘要',
      multi ? ('原幣金額・' + accts.length + ' 個帳戶合計') : '原幣金額'));
    const grid = el('div', 'sd-stats');
    const stat = (k, v, sub, signCls) => {
      const d = el('div', 'sd-stat');
      d.appendChild(el('span', 'k', k));
      const vv = el('span', 'v' + (signCls ? ' ' + signCls : ''), v);
      d.appendChild(vv);
      if (sub) d.appendChild(el('span', 's', sub));
      return d;
    };
    /* unrealized % vs adjusted cost: both are Decimal STRINGS; coerce locally for the
       ratio (a derived display percentage), guarding a zero/empty denominator. */
    const adjTotalN = Number(h.adjusted_cost_total);
    const pnlPct = (h.unrealized_pnl != null && adjTotalN)
      ? f.signedPct(Number(h.unrealized_pnl) / adjTotalN) : null;
    grid.appendChild(stat('股數', f.num(h.shares)));
    grid.appendChild(stat('市值', h.market_value === null ? f.NULL_GLYPH : f.money(h.market_value, h.quote_ccy), h.market_value === null ? '缺價' : h.quote_ccy));
    grid.appendChild(stat('未實現損益', h.unrealized_pnl === null ? f.NULL_GLYPH : f.signed(h.unrealized_pnl, h.quote_ccy), pnlPct, f.signClass(h.unrealized_pnl)));
    grid.appendChild(stat('權重', h.weight === null ? f.NULL_GLYPH : f.pct(h.weight), '報告幣別市值'));
    grid.appendChild(stat('原始均價', f.price(h.original_avg, h.quote_ccy), '總成本 ' + f.money(h.original_cost_total, h.quote_ccy)));
    grid.appendChild(stat('調整均價', f.price(h.adjusted_avg, h.quote_ccy), '配息沖減後'));
    grid.appendChild(stat('累計配息', f.money(h.dividend_portion, h.quote_ccy), h.quote_ccy));
    grid.appendChild(stat('回本進度', f.pct(h.payback_ratio), '配息 / 原始成本'));
    sec.appendChild(grid);
    if (multi) sec.appendChild(accountBreakdown(accts));
    return sec;
  }

  /* Per-account breakdown table (SECONDARY to the aggregate). Each figure is a server Decimal
     STRING from detail.position_accounts — never a client-side split of the aggregate. */
  function accountBreakdown(accts) {
    const wrap = el('div', 'table-wrap sd-acct-breakdown');
    wrap.style.marginTop = '10px';
    const table = el('table', 'data');
    table.innerHTML = '<thead><tr><th class="col-text">帳戶</th><th>股數</th><th>市值</th>'
      + '<th>未實現</th><th>原始均價</th><th>調整均價</th></tr></thead>';
    const tbody = el('tbody');
    accts.forEach((a) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'col-text', acctZh(a.account_id)));
      tr.appendChild(el('td', 'num', f.num(a.shares)));
      tr.appendChild(el('td', 'num', a.market_value == null ? f.NULL_GLYPH : f.money(a.market_value, a.quote_ccy)));
      tr.appendChild(el('td', 'num ' + f.signClass(a.unrealized_pnl), a.unrealized_pnl == null ? f.NULL_GLYPH : f.signed(a.unrealized_pnl, a.quote_ccy)));
      tr.appendChild(el('td', 'num', f.price(a.original_avg, a.quote_ccy)));
      tr.appendChild(el('td', 'num', f.price(a.adjusted_avg, a.quote_ccy)));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  /* ---------- 技術訊號 (rule engine signals) ---------- */
  function signalsSection(symbol) {
    const sec = el('div', 'sd-section');
    sec.appendChild(secHead('技術訊號', '法則引擎・掃描產生（非即時）'));
    const box = el('div', 'sd-signals');
    box.appendChild(el('div', 'sd-sig-loading', '載入技術訊號…'));
    sec.appendChild(box);
    /* Self-fetch through the single fetch layer; a failure degrades to an honest note (the
       e2e smoke asserts ZERO console errors — never an unhandled rejection). Guard on the
       still-current symbol so a superseding open does not populate a stale box. */
    window.pdApi.get('/api/signals/' + encodeURIComponent(symbol))
      .then((data) => { if (currentSymbol === symbol) renderSignals(box, data); })
      .catch(() => {
        if (currentSymbol !== symbol) return;
        box.replaceChildren(el('div', 'sd-empty sd-sig-empty', '技術訊號暫時無法取得'));
      });
    return sec;
  }

  function renderSignals(box, data) {
    if (!box) return;
    box.replaceChildren();
    const rules = (data && data.rules) || {};
    const comp = data && data.composite;
    const anyRule = RULE_KEYS.some((k) => rules[k]);
    if (!comp && !anyRule) {
      box.appendChild(el('div', 'sd-empty sd-sig-empty', '資料不足 — 歷史長度不夠，尚無法形成技術判斷。'));
      return;
    }
    if (comp) {
      const head = el('div', 'sd-sig-score');
      const num = el('div', 'sd-sig-scorenum');
      num.appendChild(el('span', 'v', comp.tech_score));
      num.appendChild(el('span', 'k', 'TechScore・涵蓋 ' + comp.coverage));
      head.appendChild(num);
      const meter = el('div', 'sd-sig-meter');
      const fill = el('span');
      /* tech_score is a Decimal STRING (0-100); Number() it ONLY for the meter-fill width
         geometry (presentation) — never a P&L computation, never a sign class. */
      fill.style.width = Math.max(0, Math.min(100, Number(comp.tech_score))) + '%';
      meter.appendChild(fill);
      head.appendChild(meter);
      box.appendChild(head);
      if (comp.context_note) box.appendChild(el('div', 'sd-sig-note', comp.context_note));
    }
    const chips = el('div', 'sd-sig-chips');
    RULE_KEYS.forEach((k) => chips.appendChild(ruleChip(k, rules[k])));
    box.appendChild(chips);
  }

  function ruleChip(key, rule) {
    const chip = el('div', 'sd-chip');
    chip.appendChild(el('span', 'sd-chip-label', RULE_LABEL[key]));
    if (!rule) {
      chip.classList.add('is-empty');
      chip.appendChild(el('span', 'sd-chip-state', '資料不足'));
      return chip;
    }
    const stateMap = RULE_STATE_ZH[key] || {};
    const stateZh = stateMap[rule.state] || rule.state;
    chip.appendChild(el('span', 'sd-chip-state', stateZh));
    const sub = ruleEvidence(key, rule);
    if (sub) {
      chip.appendChild(el('span', 'sd-chip-sub', sub));
      chip.title = RULE_LABEL[key] + '：' + stateZh + '（' + sub + '）';
    }
    return chip;
  }

  /* Compact key-evidence subline per rule. Evidence values are Decimal STRINGS; f.* coerce
     for display (the sanctioned presentation path — the drawer never computes money). */
  function ruleEvidence(key, rule) {
    const ev = rule.evidence || {};
    if (key === 'trend_filter') {
      return ev.price_vs_ma != null ? '偏離 MA200 ' + f.signedPct(ev.price_vs_ma) : null;
    }
    if (key === 'ma_cross') {
      /* fresh cross -> its age; otherwise the state chip already shows the relationship,
         so the subline stays empty (no redundant echo). */
      if (ev.cross && ev.days_ago != null) return ev.days_ago + ' 天前' + (ev.cross === 'golden' ? '黃金交叉' : '死亡交叉');
      return null;
    }
    if (key === 'momentum_12_1') {
      return ev.return_12_1 != null ? '12-1 報酬 ' + f.signedPct(ev.return_12_1) : null;
    }
    if (key === 'rsi_regime') {
      return ev.rsi14 != null ? 'RSI ' + f.num(ev.rsi14, 0) : null;
    }
    return null;
  }

  function splitSection(h) {
    const sec = el('div', 'sd-section');
    sec.appendChild(secHead('報酬貢獻拆分', '資本利得 vs 股利（未實現，vs 原始成本）'));
    const wrap = el('div', 'sd-split');
    if (h.capital_gain === null || h.capital_gain === undefined) {
      wrap.appendChild(el('div', 'sd-empty', '缺價 — 無法計算貢獻拆分'));
      sec.appendChild(wrap);
      return sec;
    }
    /* cap / div are backend Decimal STRINGS; coerce to local numbers ONLY for the bar-width
       geometry and sign decisions. The displayed cap / div values render via f.* on the
       original strings; the 合計 total displayed is the backend money-of-record
       h.unrealized_pnl (Decimal STRING) — by the proven identity capital_gain +
       dividend_portion ≡ unrealized_pnl (price×shares − adjusted_total) — never a client
       money-sum. totalN below stays a float ONLY for the bar-width geometry (presentation). */
    const cap = h.capital_gain;
    const div = h.dividend_portion != null ? h.dividend_portion : 0;
    const capN = Number(cap);
    const divN = Number(div);
    const totalN = capN + divN;
    const bar = el('div', 'sd-split-bar');
    if (totalN > 0) {
      const capSeg = el('span', capN >= 0 ? 'seg-cap' : 'seg-neg');
      capSeg.style.width = Math.max(0, (capN / totalN) * 100) + '%';
      capSeg.title = '資本利得 ' + f.signed(cap, h.quote_ccy);
      const divSeg = el('span', 'seg-div');
      divSeg.style.width = Math.max(0, (divN / totalN) * 100) + '%';
      divSeg.title = '股利貢獻 ' + f.money(div, h.quote_ccy);
      bar.appendChild(capSeg);
      bar.appendChild(divSeg);
    }
    wrap.appendChild(bar);
    const legend = el('div', 'sd-split-legend');
    const item = (cls, label, val) => {
      const s = el('span');
      const sw = el('span', 'sw');
      sw.style.background = cls === 'cap' ? cssVar('--accent') : cssVar('--series-myr');
      s.appendChild(sw);
      s.appendChild(document.createTextNode(label + ' '));
      s.appendChild(el('b', null, val));
      return s;
    };
    legend.appendChild(item('cap', '資本利得', f.signed(cap, h.quote_ccy) + ' ' + h.quote_ccy));
    legend.appendChild(item('div', '股利貢獻', f.money(div, h.quote_ccy) + ' ' + h.quote_ccy));
    legend.appendChild(item('cap', '合計', f.signed(h.unrealized_pnl, h.quote_ccy) + ' ' + h.quote_ccy));
    wrap.appendChild(legend);
    sec.appendChild(wrap);
    return sec;
  }

  function dividendSection(symbol, detail) {
    const sec = el('div', 'sd-section');
    const rows = detail.dividend_events || [];
    const typeZh = (d) => DIV_TYPE_ZH[d.type] || d.type;
    /* 配息史 → POST /api/export/symbol-detail (reconciliation channel over the dividend
       ledger). Owner directive 2026-07-14: no more DOM/display-value dumps. */
    let exportBtn = null;
    if (rows.length) {
      exportBtn = el('button', 'btn btn-sm btn-export');
      exportBtn.type = 'button';
      exportBtn.title = '匯出對帳級 CSV（配息史，由後端股利帳本產生）';
      exportBtn.appendChild(el('span', 'ico', '⬇'));
      exportBtn.appendChild(el('span', null, '匯出 CSV'));
      exportBtn.addEventListener('click', async () => {
        const restore = window.pdBusy ? window.pdBusy(exportBtn, '匯出中…') : function () {};
        try {
          await window.pdApi.download('/api/export/symbol-detail', { symbol: symbol });
        } catch (err) {
          if (window.toast) window.toast(err && err.message ? err.message : '匯出失敗', 'fail', err && err.code);
        } finally {
          restore();
        }
      });
    }
    sec.appendChild(secHead('配息史', '帳本 dividends・' + rows.length + ' 筆', exportBtn));
    if (!rows.length) {
      sec.appendChild(el('div', 'sd-empty', '尚無配息紀錄'));
      return sec;
    }
    const wrap = el('div', 'table-wrap');
    const table = el('table', 'data');
    const thead = el('thead');
    const trh = el('tr');
    ['日期', '類型', 'Gross', 'Net', '再投資'].forEach((t, i) => trh.appendChild(el('th', i < 2 ? 'col-text' : null, t)));
    thead.appendChild(trh);
    table.appendChild(thead);
    const tbody = el('tbody');
    rows.forEach((d) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'col-text num', d.date));
      const tdType = el('td', 'col-text');
      const chipCls = d.type === 'drip' ? 'chip-drip' : d.type === 'stock' ? 'chip-stock' : d.type === 'net' ? 'chip-net' : 'chip-cash';
      tdType.appendChild(el('span', 'type-chip ' + chipCls, typeZh(d)));
      tr.appendChild(tdType);
      tr.appendChild(el('td', 'num', d.gross == null ? f.NULL_GLYPH : f.money(d.gross, d.ccy)));
      tr.appendChild(el('td', 'num', f.money(d.net, d.ccy)));
      tr.appendChild(el('td', 'num', d.reinvest_shares ? f.num(d.reinvest_shares, 4) + ' 股 @ ' + f.price(d.reinvest_price, d.ccy) : f.NULL_GLYPH));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    sec.appendChild(wrap);
    return sec;
  }

  function realizedSection(symbol, detail) {
    const sec = el('div', 'sd-section');
    const rows = detail.realized_rows || [];
    sec.appendChild(secHead('已實現記錄', rows.length ? rows.length + ' 筆' : null));
    if (!rows.length) {
      sec.appendChild(el('div', 'sd-empty', '此標的尚無已實現損益'));
      return sec;
    }
    const wrap = el('div', 'table-wrap');
    const table = el('table', 'data');
    table.innerHTML = '<thead><tr><th class="col-text">帳戶</th><th>賣出股數</th><th>淨收款</th><th>調整成本移除</th><th>已實現損益</th></tr></thead>';
    const tbody = el('tbody');
    rows.forEach((r) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'col-text', acctZh(r.account_id)));
      tr.appendChild(el('td', 'num', f.num(r.shares_sold)));
      tr.appendChild(el('td', 'num', f.money(r.proceeds_net, r.quote_ccy)));
      tr.appendChild(el('td', 'num', f.money(r.adjusted_cost_removed, r.quote_ccy)));
      tr.appendChild(el('td', 'num ' + f.signClass(r.realized), f.signed(r.realized, r.quote_ccy)));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    sec.appendChild(wrap);
    return sec;
  }

  /* ---------- 交易明細 (all transactions for this symbol, paginated 10/page) ---------- */
  /* pager.js is not on every page that hosts the drawer — index.html (the drawer's home)
     omits it from its <script> list. Lazy-load it ON DEMAND so 交易明細 pagination works
     without touching the page markup. Idempotent: at most one injected tag; concurrent
     callers share one Promise; an inject failure degrades to page-1-only (never rejects). */
  let pagerLoad = null;
  function ensurePager() {
    if (window.pdPager) return Promise.resolve();
    if (pagerLoad) return pagerLoad;
    pagerLoad = new Promise((resolve) => {
      const s = document.createElement('script');
      s.src = 'pager.js';
      s.async = true;
      s.onload = () => resolve();
      s.onerror = () => resolve();
      document.head.appendChild(s);
    });
    return pagerLoad;
  }

  /* 事件別 chip — reuses the ledger's neutral direction chip classes (.dir-chip /.dir-buy/
     .dir-sell in styles.css: 買賣是方向不是損益 → NOT red/green). The unified activity list adds
     期初 (open) / DRIP再投 (drip) / 配股 (stock) reinvest rows beyond buy/sell; each gets a
     neutral chip. Wire `side` is lowercase (open/buy/sell/drip/stock) from /api/symbol/detail. */
  function sideChip(side) {
    const s = String(side || '').toLowerCase();
    if (s === 'sell') return el('span', 'dir-chip dir-sell', '賣');
    if (s === 'open') return el('span', 'dir-chip', '期初');
    if (s === 'drip') return el('span', 'dir-chip', 'DRIP再投');
    if (s === 'stock') return el('span', 'dir-chip', '配股');
    return el('span', 'dir-chip dir-buy', '買');
  }

  /* 交易明細 — the UNIFIED, account-tagged activity list (期初 + 買 + 賣 + 配股/DRIP), rendered
     from detail.activity (owner #2a). This is the ONE authoritative share-affecting list, so
     its share sum reconciles with 部位摘要 by construction — a reconciliation FOOTER makes that
     identity visible (期初＋買−賣(＋配股/DRIP)＝部位摘要). When the symbol spans >1 account an
     account filter (全部 / each account) narrows the table AND the footer. Paginated 10/page
     over the in-memory activity via pdPager (no per-page network — the whole list arrives in
     the /detail payload). Returns null (section omitted) when there is no activity at all.
     Money is NEVER computed here — every cell + footer figure is a server Decimal STRING. */
  function txSection(symbol, detail) {
    const allRows = (detail && detail.activity) || [];
    if (!allRows.length) return null;   // pure watchlist name → omit the section
    const reconcile = (detail && detail.activity_reconcile) || { total: null, by_account: {} };
    const seq = drawerSeq;   // lifecycle token (guards the async pager-load hop only)
    const LIMIT = 10;

    /* distinct accounts, first-seen order */
    const accounts = [];
    allRows.forEach((r) => {
      if (!accounts.some((a) => a.id === r.account_id)) {
        accounts.push({ id: r.account_id, name: r.account });
      }
    });
    const multi = accounts.length > 1;

    let filterAcct = null;   // null = 全部
    let pager = null;

    const sec = el('div', 'sd-section sd-tx-section');
    const filterHost = multi ? el('div', 'sd-tx-filter') : null;
    if (filterHost) filterHost.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap';
    sec.appendChild(secHead('交易明細', '帳本活動・共 ' + allRows.length + ' 筆', filterHost));

    const wrap = el('div', 'table-wrap');
    const table = el('table', 'data');
    const thead = el('thead');
    const trh = el('tr');
    /* 日期 / 帳戶 / 事件 / 股數 / 價格 / 費用 / 稅 / 合計 (帳戶 + 事件 are text-aligned) */
    ['日期', '帳戶', '事件', '股數', '價格', '費用', '稅', '合計'].forEach((t, i) => {
      trh.appendChild(el('th', (i === 1 || i === 2) ? 'col-text' : null, t));
    });
    thead.appendChild(trh);
    table.appendChild(thead);
    const tbody = el('tbody');
    table.appendChild(tbody);
    wrap.appendChild(table);
    sec.appendChild(wrap);

    const pagerHost = el('div');
    sec.appendChild(pagerHost);
    const footHost = el('div', 'sd-tx-reconcile');
    footHost.style.cssText =
      'margin-top:8px;font-size:11px;color:var(--text-2);font-family:var(--font-num)';
    sec.appendChild(footHost);

    function currentRows() {
      return filterAcct ? allRows.filter((r) => r.account_id === filterAcct) : allRows;
    }

    function renderRows(rows) {
      tbody.replaceChildren();
      rows.forEach((t) => {
        const tr = el('tr');
        tr.appendChild(el('td', 'num', f.date(t.date)));
        tr.appendChild(el('td', 'col-text', t.account));
        const tdSide = el('td', 'col-text');
        tdSide.appendChild(sideChip(t.side));
        tr.appendChild(tdSide);
        tr.appendChild(el('td', 'num', f.num(t.shares)));
        /* opening/配股 may carry no price/fee/tax → em-dash (never fabricate a 0). */
        tr.appendChild(el('td', 'num', t.price == null ? f.NULL_GLYPH : f.price(t.price, t.ccy)));
        tr.appendChild(el('td', 'num', t.fee == null ? f.NULL_GLYPH : f.money(t.fee, t.ccy)));
        tr.appendChild(el('td', 'num', t.tax == null ? f.NULL_GLYPH : f.money(t.tax, t.ccy)));
        /* 合計 is a signed cash-flow (買 −, 賣 +, 期初 −成本, 再投 0) — neutral like the ledger
           (direction, not P&L), so no sign colour; the server Decimal STRING is shown verbatim. */
        const tdTotal = el('td', 'num');
        tdTotal.textContent = f.signed(t.total, t.ccy) + (t.ccy ? ' ' + t.ccy : '');
        tr.appendChild(tdTotal);
        tbody.appendChild(tr);
      });
    }

    /* Reconciliation footer — 期初 X ＋買 Y −賣 Z （＋配股/DRIP W）＝ 部位摘要 N，with a
       server-provided ✓/⚠ balances flag. Uses the total reconcile, or the per-account one when
       filtered — both are server-computed (no client share arithmetic). */
    function renderFooter() {
      footHost.replaceChildren();
      const rec = filterAcct
        ? (reconcile.by_account && reconcile.by_account[filterAcct])
        : reconcile.total;
      if (!rec) return;
      const parts = ['期初 ' + f.num(rec.opening_shares),
        '＋買 ' + f.num(rec.buy_shares), '−賣 ' + f.num(rec.sell_shares)];
      if (Number(rec.reinvest_shares) !== 0) {
        parts.push('＋配股/DRIP ' + f.num(rec.reinvest_shares));
      }
      footHost.appendChild(el('span', null,
        parts.join(' ') + ' ＝ 部位摘要 ' + f.num(rec.book_shares) + ' 股'));
      const badge = el('span', null, rec.balances ? ' ✓ 對帳一致' : ' ⚠ 對帳不一致');
      badge.style.cssText = 'margin-left:8px;font-weight:700;color:'
        + (rec.balances ? cssVar('--down') : cssVar('--up'));
      footHost.appendChild(badge);
    }

    function showPage(offset) {
      const rows = currentRows();
      renderRows(rows.slice(offset, offset + LIMIT));
      if (pager) pager.update({ limit: LIMIT, offset: offset, totalCount: rows.length });
    }

    function buildFilter() {
      if (!filterHost) return;
      filterHost.replaceChildren();
      const mk = (id, label) => {
        const active = id === filterAcct;
        const b = el('button', 'sd-tx-filter-btn' + (active ? ' active' : ''), label);
        b.type = 'button';
        b.style.cssText = 'font-size:11px;padding:2px 9px;border-radius:6px;cursor:pointer;'
          + 'border:1px solid var(--border);background:'
          + (active ? 'var(--accent-soft)' : 'transparent')
          + ';color:' + (active ? 'var(--accent)' : 'var(--text-2)');
        b.addEventListener('click', () => {
          if (id === filterAcct) return;
          filterAcct = id;
          buildFilter();
          showPage(0);      // reset to page 1 of the filtered set
          renderFooter();
        });
        return b;
      };
      filterHost.appendChild(mk(null, '全部'));
      accounts.forEach((a) => filterHost.appendChild(mk(a.id, acctZh(a.id))));
    }

    /* Lazy-load pager.js (index.html omits it) then wire client-side pagination; on inject
       failure the page-1 rows already rendered below stand (graceful degrade). */
    ensurePager().then(() => {
      if (seq !== drawerSeq || !sec.isConnected) return;
      pager = window.pdPager
        ? window.pdPager.create({ host: pagerHost, limit: LIMIT, offset: 0,
            totalCount: currentRows().length, onPage: (offset) => showPage(offset) })
        : null;
    });

    buildFilter();
    renderRows(currentRows().slice(0, LIMIT));
    renderFooter();
    return sec;
  }

  /* ---------- 試算 (backend /api/whatif — compute-only, never writes) ---------- */
  function simSection(h) {
    const sec = el('div', 'sd-section');
    const badge = el('span', 'sd-sim-badge', '試算不寫入帳本');
    sec.appendChild(secHead('試算', '後端 試算 模式 — POST /api/whatif', badge));
    const box = el('div', 'sd-sim');

    const controls = el('div', 'sd-sim-controls');
    const seg = el('div', 'segmented');
    const bSell = el('button', 'active', '賣出試算'); bSell.type = 'button';
    const bBuy = el('button', null, '加碼試算'); bBuy.type = 'button';
    seg.appendChild(bSell); seg.appendChild(bBuy);
    controls.appendChild(seg);

    const mkField = (label, id, val, step) => {
      const fd = el('div', 'field');
      const lb = el('label', null, label); lb.setAttribute('for', id);
      fd.appendChild(lb);
      const inp = el('input'); inp.id = id; inp.type = 'number'; inp.min = '0';
      inp.step = step || '1'; inp.value = val;
      fd.appendChild(inp);
      return { fd, inp };
    };
    const dp = h.quote_ccy === 'MYR' ? '0.001' : '0.01';
    const ccy = h.quote_ccy;
    /* Prefills are the holding's Decimal-STRING fields, passed to /api/whatif AS-IS. The
       drawer performs ZERO money arithmetic: `shares`/`price` are the RAW input strings the
       backend computes over, and every result is a SERVER Decimal string rendered via f.*. */
    const hShares = (h.shares === null || h.shares === undefined) ? '' : String(h.shares);
    const hMktPrice = (h.market_price === null || h.market_price === undefined) ? '' : String(h.market_price);
    const shares = mkField('股數', 'sim-shares', hShares, '1');
    const price = mkField('價格（' + ccy + '）', 'sim-price', hMktPrice, dp);
    controls.appendChild(shares.fd);
    controls.appendChild(price.fd);
    box.appendChild(controls);

    const result = el('div', 'sd-sim-result');
    box.appendChild(result);
    const note = el('div', 'sd-sim-note');
    box.appendChild(note);

    let mode = 'sell';
    let seq = 0;       // stale-response guard (mirrors inst-quickadd.js runLookup)
    let timer = null;  // debounce handle
    bSell.addEventListener('click', () => { mode = 'sell'; bSell.classList.add('active'); bBuy.classList.remove('active'); shares.inp.value = hShares; schedule(); });
    bBuy.addEventListener('click', () => { mode = 'buy'; bBuy.classList.add('active'); bSell.classList.remove('active'); shares.inp.value = ''; schedule(); });
    shares.inp.addEventListener('input', schedule);
    price.inp.addEventListener('input', schedule);

    function kv(k, v, signCls) {
      const d = el('div', 'kv');
      d.appendChild(el('span', 'k', k));
      d.appendChild(el('span', 'v' + (signCls ? ' ' + signCls : ''), v));
      return d;
    }
    /* OLD → NEW comparison row — two SERVER-formatted strings joined by an arrow; no math. */
    function pair(k, oldV, newV) {
      const d = el('div', 'kv sd-sim-pair');
      d.appendChild(el('span', 'k', k));
      const v = el('span', 'v');
      v.appendChild(el('span', 'sd-old', oldV));
      v.appendChild(el('span', 'sd-arrow', ' → '));
      v.appendChild(el('span', 'sd-new', newV));
      d.appendChild(v);
      return d;
    }

    /* debounce (~300 ms) — same cadence as inst-quickadd.js's lookup debounce. */
    function schedule() {
      if (timer) clearTimeout(timer);
      timer = setTimeout(run, 300);
    }

    /* POST the RAW input strings; the backend owns all fee/tax/position math (C6). */
    function run() {
      const qtyRaw = shares.inp.value.trim();
      const pxRaw = price.inp.value.trim();
      /* completeness gate is a NUMERIC guard on the raw fields — never money arithmetic. */
      const qtyNum = Number(qtyRaw);
      const pxNum = Number(pxRaw);
      if (!qtyRaw || !pxRaw || !(qtyNum > 0) || !(pxNum > 0)) {
        result.replaceChildren();
        note.textContent = '輸入股數與價格後即時試算（後端計算費稅與部位）。';
        return;
      }
      const mySeq = ++seq;
      note.textContent = '試算中…';  // loading state while the request is in flight
      window.pdApi.post('/api/whatif', {
        symbol: h.symbol, side: mode, shares: qtyRaw, price: pxRaw, account_id: h.account_id
      }).then((r) => {
        if (mySeq !== seq) return;  // superseded by a newer edit — drop this reply
        render(r);
      }).catch(() => {
        if (mySeq !== seq) return;  // superseded — ignore a stale failure
        result.replaceChildren();
        note.textContent = '試算暫不可用';  // never fabricate, never fall back to local math
      });
    }

    function render(r) {
      result.replaceChildren();
      note.textContent = '';
      /* OLD → NEW pairs (持股 / 原始均價 / 調整均價 / 權重). On a SELL the averages are
         unchanged, so the backend returns no new_*_avg — new == old is a correct render, not
         a gap (Senior Review #10); 持股-new is the remaining shares. */
      const newShares = mode === 'sell' ? r.remaining_shares : r.new_shares;
      const newOrigAvg = mode === 'sell' ? r.old_original_avg : r.new_original_avg;
      const newAdjAvg = mode === 'sell' ? r.old_adjusted_avg : r.new_adjusted_avg;
      result.appendChild(pair('持股', f.num(r.old_shares), f.num(newShares)));
      result.appendChild(pair('原始均價', f.price(r.old_original_avg, ccy), f.price(newOrigAvg, ccy)));
      result.appendChild(pair('調整均價', f.price(r.old_adjusted_avg, ccy), f.price(newAdjAvg, ccy)));
      result.appendChild(pair('權重', f.pct(r.old_weight), f.pct(r.new_weight)));

      /* transaction figures — all SERVER Decimal strings, rendered via f.* (zero arithmetic). */
      result.appendChild(kv('成交金額', f.money(r.amount, ccy) + ' ' + ccy));
      result.appendChild(kv('手續費', f.money(r.fee, ccy)));
      result.appendChild(kv('稅', f.money(r.tax, ccy)));
      if (mode === 'sell') {
        result.appendChild(kv('淨收款', f.money(r.proceeds_net, ccy) + ' ' + ccy));
        result.appendChild(kv('調整成本移除', f.money(r.adjusted_cost_removed, ccy)));
        result.appendChild(kv('已實現損益', f.signed(r.realized, ccy) + ' ' + ccy, f.signClass(r.realized)));
        result.appendChild(kv('剩餘股數', f.num(r.remaining_shares)));
        result.appendChild(kv('剩餘市值', f.money(r.remaining_market_value, ccy) + ' ' + ccy));
      } else {
        result.appendChild(kv('總成本（含費稅）', f.money(r.total_cost, ccy) + ' ' + ccy));
      }
      /* fee/tax rule summary + oversell honesty, from the backend reply. */
      const parts = [];
      if (r.fee_rule_desc) parts.push('費稅規則：' + r.fee_rule_desc);
      if (mode === 'sell' && r.oversell) {
        parts.push('⚠ 賣出股數超過持有 — 實際寫入時將要求確認（輸入錯誤或放空）');
      }
      note.textContent = parts.join('。');
    }

    schedule();  // auto-run the initial (sell, full holding) 試算 on open
    sec.appendChild(box);
    return sec;
  }

  /* deep-link: index.html#sym=2330 (from全域搜尋) */
  function checkHash() {
    const m = location.hash.match(/^#sym=(.+)$/);
    if (m) {
      const sym = decodeURIComponent(m[1]);
      setTimeout(() => window.openSymbolDrawer(sym), 60);
    }
  }
  window.addEventListener('hashchange', checkHash);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', checkHash);
  else checkHash();
})();
