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
      body.appendChild(adviceSection(symbol));
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
      body.appendChild(adviceSection(symbol));
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
          value: '期初 ' + f.shares(t.shares) + ' 股 @ ' + f.price(t.price, quoteCcy) });
        return;
      }
      const isBuy = t.side === 'buy';
      markPoints.push({ coord: [t.date, closeOn(t.date)], name: isBuy ? '買進' : '賣出',
        symbol: isBuy ? BUY_MARK : SELL_MARK, symbolSize: [18, 22],
        /* offset the marker off the point so its stem tip touches (buy below / sell above). */
        symbolOffset: [0, isBuy ? '50%' : '-50%'],
        itemStyle: { color: isBuy ? GREEN : RED },
        label: { show: showTradeLabels, position: isBuy ? 'bottom' : 'top',
          formatter: (isBuy ? '買' : '賣') + f.shares(t.shares),
          color: isBuy ? GREEN : RED, fontSize: 10, fontWeight: 'bold' },
        value: (isBuy ? '買 ' : '賣 ') + f.shares(t.shares) + ' 股 @ ' + f.price(t.price, quoteCcy) });
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

  /* 待釐清 badges for ONE position row (audit F-52: the drawer drew NONE of them — it
     consumed only `fully_recovered` and `price_stale`, so a symbol whose share count is
     wrong by a whole corporate-action ratio looked exactly like a healthy one, and §6.3's
     red reconciliation footer would render with nothing beside it to name the cause).

     Labels, titles and the severity ORDER are deliberately identical to the holdings table
     in app.js — one vocabulary for one state, so 「股數待釐清」 in the table and in the drawer
     are provably the same flag. Severity descending: a discarded basis (賣超), then a wrong
     SHARE COUNT (every valued figure off by the ratio), then a missing payout (the share
     count is right and the row is short by exactly one dividend), then a healthy declared
     short — which is a real priced position, NOT a data problem, and must never look like
     one. Returns [] for a clean position. */
  function flagBadges(h) {
    const out = [];
    const add = (cls, label, title) => {
      const b = el('span', 'badge ' + cls, label);
      b.title = title;
      out.push(b);
    };
    if (h.oversold) {
      add('badge-missing', '賣超',
        '賣出數量超過持股，部位為負、損益待釐清（請補記期初庫存或遺漏的買進）');
    } else if (h.unbookable_action) {
      add('badge-missing', '股數待釐清',
        '公司行動未套用：股數仍是行動前的數字，價格卻已是行動後的，'
        + '市值與未實現損益因此失真（請修正該筆公司行動或補齊持倉紀錄）');
    } else if (h.unbookable_dividend) {
      add('badge-missing', '股利待釐清',
        '放空期間有股利紀錄：放空方需支付股利，此筆未列入計算，'
        + '本列數字少計該筆金額（請改以現金收支登錄）');
    } else if (h.short_open) {
      add('badge-short', '放空中',
        '已宣告的放空部位；成本基礎為賣出價款，買回時結算損益');
    }
    return out;
  }

  /* 部位摘要 — the AGGREGATE across accounts is PRIMARY (owner #2c). `h` is detail.position
     (server-computed cross-account Decimal totals); `accts` is the per-account breakdown,
     rendered as a SECONDARY table only when the symbol spans >1 account. The drawer NEVER
     sums money across accounts — every figure here is a server Decimal STRING via f.*. */
  function statsSection(h, accts) {
    const multi = !!(accts && accts.length > 1);
    const sec = el('div', 'sd-section');
    const flags = flagBadges(h);
    const flagHost = flags.length ? el('div', 'sd-pos-flags') : null;
    if (flagHost) {
      flagHost.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap';
      flags.forEach((b) => flagHost.appendChild(b));
    }
    sec.appendChild(secHead('部位摘要',
      multi ? ('原幣金額・' + accts.length + ' 個帳戶合計') : '原幣金額', flagHost));
    const grid = el('div', 'sd-stats');
    const stat = (k, v, sub, signCls) => {
      const d = el('div', 'sd-stat');
      d.appendChild(el('span', 'k', k));
      const vv = el('span', 'v' + (signCls ? ' ' + signCls : ''), v);
      d.appendChild(vv);
      if (sub) d.appendChild(el('span', 's', sub));
      return d;
    };
    /* 未實現% is SERVER-computed (`unrealized_pct` = 未實現 ÷ ORIGINAL cost — the same basis
       as 回本進度 and the KPI 累計報酬率, so every percentage in this drawer reads on ONE
       basis, now stated in the sub-label). The frontend must NOT derive it: the previous
       client-side divide used ADJUSTED cost, which is legally <= 0 once cumulative cash
       dividends exceed cost (domain-ledger.md) and therefore silently FLIPPED the sign —
       a +223,473 gain was rendered as −1399.07%. Audit H1, 2026-07-26. */
    const pnlPct = h.unrealized_pct == null
      ? null : f.signedPct(h.unrealized_pct) + '・vs 原始成本';
    grid.appendChild(stat('股數', f.shares(h.shares)));
    grid.appendChild(stat('市值', h.market_value === null ? f.NULL_GLYPH : f.money(h.market_value, h.quote_ccy), h.market_value === null ? '缺價' : h.quote_ccy));
    grid.appendChild(stat('未實現損益', h.unrealized_pnl === null ? f.NULL_GLYPH : f.signed(h.unrealized_pnl, h.quote_ccy), pnlPct, f.signClass(h.unrealized_pnl)));
    grid.appendChild(stat('權重', h.weight === null ? f.NULL_GLYPH : f.pct(h.weight), '報告幣別市值'));
    grid.appendChild(stat('原始均價', f.price(h.original_avg, h.quote_ccy), '總成本 ' + f.money(h.original_cost_total, h.quote_ccy)));
    /* 已回本 (server flag `fully_recovered`, exact Decimal comparison): the adjusted basis
       has gone <= 0, so a NEGATIVE 調整均價 is correct — say so instead of leaving the user
       to wonder why an average price is negative (audit H1). */
    grid.appendChild(stat('調整均價', f.price(h.adjusted_avg, h.quote_ccy),
      h.fully_recovered ? '已回本・配息已完全沖減成本' : '配息沖減後'));
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
      const tdAcct = el('td', 'col-text');
      tdAcct.appendChild(el('span', null, acctZh(a.account_id)));
      /* Per-account 待釐清 badges: the aggregate above flags with `any(...)`, so ONE tainted
         account reddens the total while the others are clean. Without the badge here the
         drawer would say the position is 待釐清 and give no way to see which account. */
      flagBadges(a).forEach((b) => { b.style.marginLeft = '6px'; tdAcct.appendChild(b); });
      tr.appendChild(tdAcct);
      tr.appendChild(el('td', 'num', f.shares(a.shares)));
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

  /* ---------- AI 建議 (W2, AI-D1/D12, 2026-08-16) ----------
     The assistant's surface for ONE symbol: the latest per-symbol advice card, plus a
     立即產生 button on the 「持倉建議與提點」 preset. Everything is server-computed — the
     card body arrives as text and the run is a POST, so the drawer never computes money and
     never parses markdown. Degrades to honest notes on any failure.

     renderAdvice is defined FIRST because a card already on screen wires the run button's
     click to runAdvice — a function declaration made AFTER it would not be hoisted into the
     enclosing function's scope (the fix for the e2e-caught ReferenceError), so the order
     here is load-bearing, not stylistic. */
  function renderAdvice(box, card, tasks, taskId, onRun) {
    box.replaceChildren();
    const anyAdviceTask = tasks.some((t) => t.preset_key === 'advice');
    if (!anyAdviceTask) {
      /* The preset is not installed (fresh ledger / never clicked 一鍵安裝). Point at the
         pipeline hub rather than leaving a dead button. */
      const note = el('div', 'sd-empty sd-sig-empty',
        '尚未建立「持倉建議與提點」任務');
      const go = el('a', 'sd-advice-link', '前往洞察管線一鍵安裝');
      go.href = 'pipeline-hub.html';
      note.appendChild(go);
      box.appendChild(note);
      return;
    }
    if (card) {
      const head = el('div', 'sd-advice-card');
      head.appendChild(el('h4', 'sd-advice-title', card.title || ''));
      if (card.summary) head.appendChild(el('p', 'sd-advice-body', card.summary));
      const foot = el('div', 'sd-advice-foot');
      foot.appendChild(el('span', 'sd-advice-time', f.datetime(card.created_at)));
      const attrib = f.aiAttrib(card.model, card.tokens_in, card.tokens_out, card.cost_usd);
      if (attrib) foot.appendChild(el('span', 'sd-advice-cost ai-attrib num', attrib));
      head.appendChild(foot);
      box.appendChild(head);
    } else {
      box.appendChild(el('div', 'sd-empty sd-sig-empty',
        '尚無此標的的建議卡 — 等待排程產生，或立即產生一張'));
    }
    if (taskId) {
      const run = el('button', 'btn sd-advice-run', card ? '重新產生' : '立即產生');
      run.type = 'button';
      run.title = '依現有倉位與最新資料產生一張建議卡（批次，約需數秒）';
      run.addEventListener('click', onRun);
      box.appendChild(run);
    } else {
      /* The preset exists but is DISABLED (AI-D12 default-on was overridden) — say so
         instead of offering a button that would 409. */
      box.appendChild(el('div', 'sd-empty sd-sig-empty',
        '「持倉建議與提點」任務已停用 — 至洞察管線啟用後即可產生'));
    }
  }

  function adviceSection(symbol) {
    const sec = el('div', 'sd-section');
    sec.appendChild(secHead('AI 建議', '批次產生・僅詮釋已算好的數字'));
    const box = el('div', 'sd-advice');
    box.appendChild(el('div', 'sd-sig-loading', '載入 AI 建議…'));
    sec.appendChild(box);

    let adviceTaskId = null;

    /* The latest advice card for THIS symbol: fetch the symbol's cards and keep the newest
       whose task is the 「持倉建議與提點」 preset. preset_key is the join key (M3), never the
       task NAME — the owner can rename the task and the drawer still finds it. */
    function loadCard() {
      Promise.all([
        window.pdApi.get('/api/insights', { symbol: symbol, limit: 25 }),
        window.pdApi.get('/api/insight-types'),
      ]).then((pair) => {
        if (currentSymbol !== symbol) return;
        const rows = (pair[0] && pair[0].rows) || [];
        const tasks = Array.isArray(pair[1]) ? pair[1] : [];
        const adviceIds = {};
        adviceTaskId = null;
        tasks.forEach((t) => {
          if (t.preset_key !== 'advice') return;
          adviceIds[t.id] = t;
          if (t.enabled) adviceTaskId = t.id;  // the ENABLED one is the one a run may target
        });
        const card = rows.find((c) => adviceIds[c.insight_type_id]);
        renderAdvice(box, card, tasks, adviceTaskId, runAdvice);
      }).catch((err) => {
        if (currentSymbol !== symbol) return;
        box.replaceChildren(el('div', 'sd-empty sd-sig-empty', 'AI 建議暫時無法取得'));
      });
    }

    function runAdvice() {
      if (!adviceTaskId) return;
      const btn = box.querySelector('.sd-advice-run');
      if (btn) btn.disabled = true;
      if (window.toast) window.toast('已觸發', 'ok', '持倉建議與提點：產生中…');
      window.pdApi.post('/api/insight-types/' + adviceTaskId + '/run')
        .then((resp) => pollRun(adviceTaskId, resp.run_id, 0))
        .catch((err) => {
          if (btn) btn.disabled = false;
          if (window.toast) window.toast((err && err.message) || '觸發失敗', 'fail', err && err.code);
        });
    }

    /* Poll GET …/runs until the run finishes, then re-pull the card. Mirrors the insights
       page's own poll (≤60s); a still-running poll leaves a toast rather than a spinner that
       never resolves. */
    function pollRun(taskId, runId, tries) {
      if (tries >= 20) {
        if (window.toast) window.toast('仍在執行', 'ok', '完成後重新開啟本視窗即可看到新卡');
        return;
      }
      window.pdApi.get('/api/insight-types/' + taskId + '/runs', { limit: 5 }).then((resp) => {
        const row = ((resp && resp.rows) || []).find((r) => r.id === runId);
        if (!row || !row.finished_at) {
          setTimeout(() => pollRun(taskId, runId, tries + 1), 3000);
          return;
        }
        if (window.toast) {
          const ok = row.status === 'ok' || row.status === 'partial';
          window.toast(ok ? '產生完成' : (row.status === 'skipped' ? '本次略過' : '執行失敗'),
            ok ? 'ok' : 'fail', row.detail || row.reason || row.status);
        }
        loadCard();
      }).catch(() => { /* polling is best-effort; the card list refreshes on next open */ });
    }

    loadCard();
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
       h.unrealized_pnl (Decimal STRING) — because capital_gain + dividend_portion is the
       DECOMPOSITION of unrealized_pnl, never a client money-sum. The relation is derived,
       not exact: unrealized_pnl is (price − avg) × shares with avg computed on read, so it
       carries a ~1e-25 residue against the exact price×shares − adjusted_total. That is
       inherent to computing the average on read (data-and-pricing.md) and is not a defect —
       but it is not an identity either, and calling it "proven" invited someone to assert
       equality on it. totalN below stays a float ONLY for the bar-width geometry. */
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
      tr.appendChild(el('td', 'num', d.reinvest_shares ? f.shares(d.reinvest_shares) + ' 股 @ ' + f.price(d.reinvest_price, d.ccy) : f.NULL_GLYPH));
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
      /* kind="dividend": a cash dividend that landed AFTER this position closed — realized
         income with no cost to remove (audit H2). Mark it and show the sale-only columns as
         不適用 rather than a misleading 0. */
      const isDiv = r.kind === 'dividend';
      const tdAcct = el('td', 'col-text', acctZh(r.account_id));
      if (isDiv) {
        const chip = el('span', 'rz-kind', '股利');
        chip.title = '清倉後入帳的現金股利 — 已無成本可沖減，列為已實現收益';
        tdAcct.appendChild(chip);
      }
      tr.appendChild(tdAcct);
      tr.appendChild(el('td', 'num', isDiv ? f.NULL_GLYPH : f.shares(r.shares_sold)));
      tr.appendChild(el('td', 'num', f.money(r.proceeds_net, r.quote_ccy)));
      tr.appendChild(el('td', 'num',
        isDiv ? f.NULL_GLYPH : f.money(r.adjusted_cost_removed, r.quote_ccy)));
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
    if (s === 'action') return el('span', 'dir-chip', '公司行動');
    return el('span', 'dir-chip dir-buy', '買');
  }

  const ACTION_KIND_ZH = { SPLIT: '拆併股', EXCHANGE: '換股', SPINOFF: '分割' };

  /* One-line description of a corporate-action activity row: what happened, at what ratio,
     and (when the two ends differ) which symbol it came from or went to. The row carries no
     share count on purpose — the exact figure is the footer's ＋公司行動 term, computed by the
     share walker; re-deriving a per-row delta in the browser would be a third implementation
     of the ratio algebra AND client-side quantity math. */
  function actionLabel(t) {
    const kind = ACTION_KIND_ZH[t.kind] || t.kind || '公司行動';
    const ratio = t.ratio_to + '：' + t.ratio_from;
    if (t.role === 'self') return kind + ' ' + ratio;                 // SPLIT: in place
    if (t.role === 'destination') return kind + ' ' + ratio + '（來自 ' + t.from_symbol + '）';
    return kind + ' ' + ratio + '（移轉至 ' + t.to_symbol + '）';
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
        if (t.side === 'action') {
          const lbl = el('span', 'sd-act-label', actionLabel(t));
          lbl.style.cssText = 'margin-left:6px;color:var(--text-2)';
          if (t.note) lbl.title = t.note;
          tdSide.appendChild(lbl);
        }
        tr.appendChild(tdSide);
        /* An action row carries `shares: null` (see actionLabel) -> em-dash, never a
           fabricated 0: f.shares nil-guards, but the intent is stated here. */
        tr.appendChild(el('td', 'num', f.shares(t.shares)));
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
      /* No footer for this filter (a per-account slice the server did not emit) — still show
         the cause lines: an unexplained absence is the same defect as an unexplained ⚠. */
      if (!rec) { renderActionIssues(); return; }
      const parts = ['期初 ' + f.shares(rec.opening_shares),
        '＋買 ' + f.shares(rec.buy_shares), '−賣 ' + f.shares(rec.sell_shares)];
      /* Show the 配股/DRIP term only when it is non-zero AND still non-zero once rendered —
         otherwise the equation grows a "＋配股/DRIP 0" term that explains nothing (which is
         exactly how the 2026-08-05 report read). */
      if (Number(rec.reinvest_shares) !== 0 && f.shares(rec.reinvest_shares) !== '0') {
        parts.push('＋配股/DRIP ' + f.shares(rec.reinvest_shares));
      }
      /* ＋公司行動 (spec §6.3, W5): a corporate action adds shares OUTSIDE the four ledger
         buckets — no transaction, no opening, no dividend row — so without this term every
         symbol carrying one reported ⚠ 對帳不一致 while being perfectly consistent. Shown
         only when non-zero, the same rule as 配股/DRIP above and for the same reason: a
         「＋公司行動 0」 explains nothing.

         The term is SIGNED by the server — a SPLIT adds shares, an EXCHANGE's source loses
         its whole position — and the sign is moved into the OPERATOR rather than left on the
         number, so the equation reads 「−公司行動 40」 like its 「−賣」 neighbour instead of
         「＋公司行動 -40」. That is a string swap on an already-formatted display value, NOT
         client-side arithmetic: the magnitude shown is whatever f.shares produced, with its
         leading ASCII minus (see format.js `num`) taken off. */
      const corp = rec.corporate_delta_shares;
      if (corp != null && Number(corp) !== 0 && f.shares(corp) !== '0') {
        const shown = f.shares(corp);
        parts.push(shown.charAt(0) === '-'
          ? '−公司行動 ' + shown.slice(1)
          : '＋公司行動 ' + shown);
      }
      footHost.appendChild(el('span', null,
        parts.join(' ') + ' ＝ 部位摘要 ' + f.shares(rec.book_shares) + ' 股'));
      const badge = el('span', null, rec.balances ? ' ✓ 對帳一致' : ' ⚠ 對帳不一致');
      badge.style.cssText = 'margin-left:8px;font-weight:700;color:'
        + (rec.balances ? cssVar('--down') : cssVar('--up'));
      footHost.appendChild(badge);
      /* Name the gap. A red flag without its size tells the owner nothing they can act on;
         `diff_shares` is the server's exact signed figure (net − 部位摘要). */
      if (!rec.balances && rec.diff_shares != null) {
        footHost.appendChild(el('span', null, '（差額 ' + f.shares(rec.diff_shares) + ' 股）'));
      }
      renderRepairDoor(!rec.balances);
      renderActionIssues();
    }

    /* DOOR 2 (§6.7) — the repair offered where the owner is already looking at the evidence.
       Doors 1 and 3 are entry surfaces the owner has to go and find; this one appears beside
       the mismatch itself, which is the whole argument for having three doors rather than one.
       Shown only on a RED footer: on a green one it is a control with nothing to fix.
       `pdCorpActionForm` is the shared component doors 1 and 3 use, loaded by index.html —
       so this is a call, not a fourth implementation of the form. If the script is absent the
       button simply does not render, because a door that opens onto nothing is worse than no
       door: the owner would conclude the repair is unavailable rather than that it is broken. */
    function renderRepairDoor(mismatched) {
      const form = window.pdCorpActionForm;
      if (!mismatched || !form || typeof form.open !== 'function') return;
      const btn = el('button', 'btn-ghost', '補登公司行動');
      btn.type = 'button';
      btn.style.cssText = 'margin-left:10px;font-size:12px;padding:2px 8px';
      btn.addEventListener('click', () => form.open({
        account_id: filterAcct || '',
        from_symbol: symbol,
        reason: '此標的的對帳結果為 ⚠ 對帳不一致',
        /* Re-open through the shell's own entry point rather than re-rendering in place:
           the saved action changes `corporate_delta`, the position, the flags AND the
           prices, so a partial refresh would leave the drawer showing a mix of before and
           after — the aggregate-vs-detail divergence this repo has already had three times. */
        onSaved: () => { if (window.pdOpenSymbol) window.pdOpenSymbol(symbol); },
      }));
      footHost.appendChild(btn);
    }

    /* The CAUSE lines that must accompany a red footer (§6.3 / audit F-17). A flagged
       position SHOULD fail to reconcile — §6.3 rules that "a position whose basis was
       discarded genuinely does not reconcile; reporting ⚠ 對帳不一致 on it is the correct
       answer, not a false alarm" — so the drawer never forces it green; it names the reason
       instead. Three distinct channels, three different sentences (see _action_issues):
       the replay's refused rows, D31's depth-capped walk, D33's negative-side skip.
       Rendered whenever non-empty, not only when the footer is red: a depth-capped walk can
       still balance, and the share count is untrustworthy either way. */
    function renderActionIssues() {
      const issues = (detail && detail.action_issues) || null;
      if (!issues) return;
      const keep = (r) => !filterAcct || r.account_id === filterAcct;
      const lines = [];
      (issues.unapplied || []).filter(keep).forEach((u) => {
        lines.push('⚠ 公司行動未套用（' + acctZh(u.account_id) + '・' + f.date(u.date) + '・'
          + (ACTION_KIND_ZH[u.kind] || u.kind) + ' ' + u.from_symbol + '→' + u.to_symbol
          + '）：' + u.reason);
      });
      (issues.depth_capped || []).filter(keep).forEach((d) => {
        lines.push('⚠ 公司行動鏈過長（' + acctZh(d.account_id)
          + '）：股數改用未套用行動的舊基準，此列股數待釐清');
      });
      (issues.negative_side_skipped || []).filter(keep).forEach((d) => {
        lines.push('⚠ 公司行動跳過（' + acctZh(d.account_id)
          + '）：行動當日來源或目標股數為負（放空或賣超），本筆未套用，股數待釐清');
      });
      lines.forEach((text) => {
        const row = el('div', 'sd-tx-issue', text);
        row.style.cssText = 'margin-top:4px;color:var(--up)';
        footHost.appendChild(row);
      });
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
      }).catch((err) => {
        if (mySeq !== seq) return;  // superseded — ignore a stale failure
        result.replaceChildren();
        /* Never fabricate, never fall back to local math — but DO repeat the server's own
           explanation when it gave one (F-02). An oversell anywhere in the ledger blocks
           every symbol's 試算, and a bare 「試算暫不可用」 left the user with no way to find
           the offending row. Only a deliberate 400 is quoted: a 500 or a dropped connection
           carries `statusText`, not an explanation, and 「Internal Server Error」 on the card
           would be worse than the generic line. */
        const explained = err && err.status === 400
          && err.code === 'validation_error' && err.message;
        note.textContent = explained || '試算暫不可用';
      });
    }

    function render(r) {
      result.replaceChildren();
      note.textContent = '';
      /* OLD → NEW pairs (持股 / 原始均價 / 調整均價 / 權重); 持股-new is the remaining shares.
         ⚠ This comment used to record as settled ("Senior Review #10") that a SELL leaves the
         averages unchanged, so old == old was "a correct render, not a gap". That is true of
         the ORDINARY branch alone (sweep F-01, 2026-08-27) — and the comment is how the
         assumption survived being questioned. The server now sends new_*_avg on both sides:
         null on an oversell, where this surface has no 放空 declaration to read and so states
         the fork rather than picking a branch, and null on a full exit, which leaves no
         position at all. f.price(null) renders the dash. */
      const newShares = mode === 'sell' ? r.remaining_shares : r.new_shares;
      const newOrigAvg = r.new_original_avg;
      const newAdjAvg = r.new_adjusted_avg;
      result.appendChild(pair('持股', f.shares(r.old_shares), f.shares(newShares)));
      result.appendChild(pair('原始均價', f.price(r.old_original_avg, ccy), f.price(newOrigAvg, ccy)));
      result.appendChild(pair('調整均價', f.price(r.old_adjusted_avg, ccy), f.price(newAdjAvg, ccy)));
      result.appendChild(pair('權重', f.pct(r.old_weight), f.pct(r.new_weight)));

      /* transaction figures — all SERVER Decimal strings, rendered via f.* (zero arithmetic). */
      result.appendChild(kv('成交金額', f.money(r.amount, ccy) + ' ' + ccy));
      result.appendChild(kv('手續費', f.money(r.fee, ccy)));
      result.appendChild(kv('稅', f.money(r.tax, ccy)));
      if (mode === 'sell') {
        result.appendChild(kv('淨收款', f.money(r.proceeds_net, ccy) + ' ' + ccy));
        /* realized / adjusted_cost_removed are NULL when the sell exceeds the holding: the
           declared-short and 賣超 readings book different amounts and this drawer is not told
           which (review 2026-08-24). f.* renders the null glyph; realized_note carries the
           server's wording for the fork — web never composes that sentence itself. */
        result.appendChild(kv('調整成本移除', f.money(r.adjusted_cost_removed, ccy)));
        result.appendChild(kv('已實現損益', f.signed(r.realized, ccy) + (r.realized == null ? '' : ' ' + ccy), f.signClass(r.realized)));
        result.appendChild(kv('剩餘股數', f.shares(r.remaining_shares)));
        result.appendChild(kv('剩餘市值', f.money(r.remaining_market_value, ccy) + ' ' + ccy));
      } else {
        result.appendChild(kv('總成本（含費稅）', f.money(r.total_cost, ccy) + ' ' + ccy));
        /* A BUY against an OPEN SHORT covers it and REALIZES (QA-04): the server now sends
           covered_shares + realized on that branch (null on an ordinary buy), so the card
           must show them — a realized figure the API returns and the card omits is the same
           silence as not computing it. Same two labels the trade form uses (web/input.js), so
           the two doors read identically for one trade. No arithmetic here: f.* only. */
        if (r.covered_shares != null) {
          result.appendChild(kv('回補空單股數', f.shares(r.covered_shares)));
          result.appendChild(kv('已實現損益',
            f.signed(r.realized, ccy) + (r.realized == null ? '' : ' ' + ccy),
            f.signClass(r.realized)));
        }
      }
      /* fee/tax rule summary + oversell honesty, from the backend reply. */
      const parts = [];
      if (r.fee_rule_desc) parts.push('費稅規則：' + r.fee_rule_desc);
      if (mode === 'sell' && r.oversell) {
        parts.push('⚠ 賣出股數超過持有 — 實際寫入時將要求確認（輸入錯誤或放空）');
      }
      if (r.realized_note) parts.push(r.realized_note);
      if (r.etf_flag_note) parts.push(r.etf_flag_note);
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
