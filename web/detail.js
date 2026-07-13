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
   internally; the drawer NEVER sums or compares them as money. The 試算 (what-if) block
   is the documented spec-03 exception: a local fee/P&L estimate over USER INPUT only
   (window.pdFeeTax mirror), never a display of backend money-of-record.

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

  const ACCOUNT_ZH = {
    tw_broker: '台灣券商', schwab: '嘉信 Schwab',
    moomoo_my_us: 'Moomoo 美股', moomoo_my_my: 'Moomoo 馬股'
  };
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
  /* 試算-only approximate spot rates to the reporting ccy (TWD), used SOLELY inside the
     local what-if weight estimate (documented spec-03 exception). NOT a display path for
     backend money — every backend Decimal is rendered via f.* untouched. The dashboard
     payload carries no per-currency spot table, so the what-if weight is an estimate; the
     real权重 shown in 部位摘要 comes from the backend (h.weight). */
  const FX_TO_TWD = { TWD: 1, USD: 32.90, MYR: 7.05 };

  /* Shared /api/dashboard promise — the SAME one app.js / charts.js / alerts.js use, so
     opening the drawer on the dashboard reuses the in-flight/resolved payload (one fetch).
     Off-dashboard, this creates it once. holdings[] here is the rich holding summary. */
  function dashboardPromise() {
    return (window.pdDashboard || (window.pdDashboard = window.pdApi.get('/api/dashboard')));
  }

  /* The resolved holdings list (set once both fetches land in openSymbolDrawer); used by
     prev/next cycling. Null until a successful open; cycling is disabled while null. */
  let currentHoldings = null;
  /* The resolved dashboard payload (set alongside currentHoldings); the 試算 block reads
     kpis.total_market_value from it for the local 新權重 estimate. Null until first open. */
  let currentDash = null;

  function holdingOf(symbol) {
    if (!currentHoldings) return null;
    return currentHoldings.find((h) => h.symbol === symbol) || null;
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /* ---------- fee/tax estimate per account (mirror of the backend fee engine, 試算 only) ---------- */
  /* exposed for rebalance.js — single front-end mirror of the fee engine (試算 only) */
  function feeTax(h, side, shares, price) {
    const amount = shares * price;
    if (h.account_id === 'tw_broker') {
      const fee = Math.max(20, Math.round(amount * 0.001425));
      const taxRate = side === 'sell' ? (h.sector === 'ETF' ? 0.001 : 0.003) : 0;
      return { fee, tax: Math.round(amount * taxRate), dp: 0,
        rule: '0.1425%・最低 NT$20' + (side === 'sell' ? (h.sector === 'ETF' ? '・證交稅 ETF 0.1%' : '・證交稅 0.3%') : '') };
    }
    if (h.account_id === 'schwab') {
      const tax = side === 'sell' ? Number((amount * 0.0000278).toFixed(2)) : 0;
      return { fee: 0, tax, dp: 2, rule: '$0 佣金' + (side === 'sell' ? '・SEC fee 0.00278%' : '') };
    }
    if (h.account_id === 'moomoo_my_us') {
      return { fee: 0.99, tax: 0, dp: 2, rule: '平台費 USD 0.99/筆' };
    }
    /* moomoo_my_my */
    const clearing = Math.min(1000, Number((amount * 0.0003).toFixed(2)));
    const stamp = side === 'buy' || side === 'sell' ? Number((amount * 0.001).toFixed(2)) : 0;
    return { fee: clearing, tax: stamp, dp: 2, rule: 'clearing 0.03% (cap RM1,000)・stamp 0.1%' };
  }

  window.pdFeeTax = feeTax;

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

  window.openSymbolDrawer = function (symbol) {
    close();
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
      currentHoldings = (dash && dash.holdings) || [];
      const h = holdingOf(symbol);
      renderDrawer(drawer, head, body, symbol, detail, h);
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

  /* Render head + sections once both /detail and /dashboard have resolved. `h` is the
     rich dashboard holding summary (null for an unheld / watchlist symbol); `detail` is
     the /api/symbol/{symbol}/detail payload. */
  function renderDrawer(drawer, head, body, symbol, detail, h) {
    /* head */
    head.replaceChildren();
    head.appendChild(el('span', 'sym-code', symbol));
    if (h) {
      head.appendChild(el('span', 'sym-name', h.name));
      if (h.board) head.appendChild(el('span', 'board-badge', h.board));
      head.appendChild(el('span', 'badge', MARKET_ZH[h.market] + '・' + (ACCOUNT_ZH[h.account_id] || h.account_name)));
      const price = el('span', 'sd-price');
      if (h.market_price === null || h.market_price === undefined) {
        const b = el('span', 'badge badge-missing', '缺價');
        b.title = '無法取得價格資料';
        price.appendChild(b);
      } else {
        price.appendChild(el('span', 'v', f.price(h.market_price, h.quote_ccy)));
        price.appendChild(el('span', 'c', h.quote_ccy));
        if (h.price_stale) {
          const b = el('span', 'badge badge-stale-mini', '過期');
          b.title = '價格日期 ' + f.date(h.price_as_of);
          price.appendChild(b);
        }
      }
      head.appendChild(price);
    } else {
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
    body.appendChild(chartSection(detail, h));
    if (h) {
      body.appendChild(statsSection(h));
      body.appendChild(signalsSection(symbol));
      body.appendChild(splitSection(h));
      body.appendChild(simSection(h));
      body.appendChild(dividendSection(symbol, detail));
      body.appendChild(realizedSection(symbol, detail));
    } else {
      /* Watchlist (unheld) symbol: no position/P&L, but 技術訊號 still matter — a watched
         name is an entry candidate (P2 batch 3). Render the signals section (honest-empty
         when data is thin) alongside the price chart; skip the holding-only sections. */
      body.appendChild(signalsSection(symbol));
      body.appendChild(el('div', 'sd-empty', '此標的不在持倉中（觀察清單標的）— 顯示價格走勢與技術訊號，無部位／損益資料。'));
    }
    renderChart(detail, h);
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

  function chartSection(detail, h) {
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
    return sec;
  }

  function renderChart(detail, h) {
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
    if (h) {
      markLines.push({ yAxis: Number(h.original_avg), name: '原始均價',
        lineStyle: { color: cssVar('--series-gray'), type: 'dashed' },
        label: { formatter: '原始均價 ' + f.price(h.original_avg, h.quote_ccy), position: 'insideEndTop', color: cssVar('--text-3'), fontSize: 10 } });
      markLines.push({ yAxis: Number(h.adjusted_avg), name: '調整均價',
        lineStyle: { color: cssVar('--series-myr'), type: 'dashed' },
        label: { formatter: '調整均價 ' + f.price(h.adjusted_avg, h.quote_ccy), position: 'insideEndBottom', color: cssVar('--series-myr'), fontSize: 10 } });
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
        label: { formatter: '息', fontSize: 9, color: '#0c1015' },
        value: label + ' ' + (d.net !== null && d.net !== undefined ? f.money(d.net, d.ccy) + ' ' + (d.ccy || '') : '') });
    });
    (detail.trade_events || []).forEach((t) => {
      if (t.date < dates[0]) return;
      const isBuy = t.side !== 'sell';
      markPoints.push({ coord: [t.date, closeOn(t.date)], name: isBuy ? '買進' : '賣出',
        symbol: isBuy ? 'triangle' : 'arrow', symbolSize: 9, symbolRotate: isBuy ? 0 : 180,
        itemStyle: { color: isBuy ? cssVar('--accent') : cssVar('--text-2') },
        value: (t.side === 'open' ? '期初 ' : isBuy ? '買 ' : '賣 ') + f.num(t.shares) + ' 股 @ ' + f.price(t.price, quoteCcy) });
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
        lineStyle: { color: cssVar('--accent'), width: 1.6 },
        areaStyle: { color: cssVar('--accent-soft') },
        markLine: { symbol: 'none', silent: true, data: markLines },
        markPoint: { data: markPoints,
          tooltip: { formatter: (p) => p.name + '：' + (p.data.value || '') } }
      }]
    });
  }

  function statsSection(h) {
    const sec = el('div', 'sd-section');
    sec.appendChild(secHead('部位摘要', '原幣金額'));
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
    return sec;
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
      exportBtn = el('button', 'btn-export');
      exportBtn.type = 'button';
      exportBtn.title = '匯出對帳級 CSV（配息史，由後端股利帳本產生）';
      exportBtn.appendChild(el('span', null, '⬇'));
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
      tr.appendChild(el('td', 'col-text', ACCOUNT_ZH[r.account_id] || r.account_id));
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

  /* ---------- 試算 (compute-only, never writes) ---------- */
  function simSection(h) {
    const sec = el('div', 'sd-section');
    const badge = el('span', 'sd-sim-badge', '試算不寫入帳本');
    sec.appendChild(secHead('試算', '後端 試算 模式 — 僅計算', badge));
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
    /* 試算-LOCAL numeric copies of the holding's Decimal-STRING fields. The what-if is the
       documented spec-03 exception (a local estimate over user input, not money-of-record
       display): coerce ONCE here so the local arithmetic (+/-) is numeric, never string
       concatenation. Every DISPLAY of these still routes through f.* on the computed value. */
    const hShares = Number(h.shares);
    const hAdjAvg = Number(h.adjusted_avg);
    const hMktPrice = (h.market_price === null || h.market_price === undefined) ? null : Number(h.market_price);
    const hOrigTotal = Number(h.original_cost_total);
    const hAdjTotal = Number(h.adjusted_cost_total);
    const shares = mkField('股數', 'sim-shares', hShares, '1');
    const price = mkField('價格（' + h.quote_ccy + '）', 'sim-price', hMktPrice !== null ? hMktPrice : '', dp);
    controls.appendChild(shares.fd);
    controls.appendChild(price.fd);
    box.appendChild(controls);

    const result = el('div', 'sd-sim-result');
    box.appendChild(result);
    const note = el('div', 'sd-sim-note');
    box.appendChild(note);

    let mode = 'sell';
    bSell.addEventListener('click', () => { mode = 'sell'; bSell.classList.add('active'); bBuy.classList.remove('active'); shares.inp.value = hShares; compute(); });
    bBuy.addEventListener('click', () => { mode = 'buy'; bBuy.classList.add('active'); bSell.classList.remove('active'); shares.inp.value = ''; compute(); });
    shares.inp.addEventListener('input', compute);
    price.inp.addEventListener('input', compute);

    function kv(k, v, signCls) {
      const d = el('div', 'kv');
      d.appendChild(el('span', 'k', k));
      d.appendChild(el('span', 'v' + (signCls ? ' ' + signCls : ''), v));
      return d;
    }

    function compute() {
      result.replaceChildren();
      note.textContent = '';
      const qty = Number(shares.inp.value);
      const px = Number(price.inp.value);
      if (!qty || !px || qty <= 0 || px <= 0) {
        note.textContent = '輸入股數與價格後即時試算；費稅依「' + (ACCOUNT_ZH[h.account_id] || h.account_id) + '」費率規則估算。';
        return;
      }
      const ft = feeTax(h, mode, qty, px);
      const amount = qty * px;
      /* total reporting-ccy market value for the local 新權重 estimate — Decimal STRING
         from the shared dashboard payload; Number() ONCE for the local what-if math. */
      const totalRaw = currentDash && currentDash.kpis && currentDash.kpis.total_market_value;
      const totalTwd = (totalRaw === null || totalRaw === undefined) ? null : Number(totalRaw);
      const fxRate = FX_TO_TWD[h.quote_ccy] || 1;

      if (mode === 'sell') {
        if (qty > hShares) {
          note.textContent = '⚠ 賣出股數 ' + f.num(qty) + ' 超過持有 ' + f.num(hShares) + ' — 實際寫入時將要求確認（輸入錯誤或放空）。';
        }
        const proceeds = amount - ft.fee - ft.tax;
        const costRemoved = hAdjAvg * qty;
        const realized = proceeds - costRemoved;
        const remainShares = Math.max(0, hShares - qty);
        const remainValue = hMktPrice !== null ? remainShares * hMktPrice : null;
        result.appendChild(kv('成交金額', f.money(amount, h.quote_ccy) + ' ' + h.quote_ccy));
        result.appendChild(kv('手續費', f.money(ft.fee, h.quote_ccy)));
        result.appendChild(kv('稅', f.money(ft.tax, h.quote_ccy)));
        result.appendChild(kv('淨收款', f.money(proceeds, h.quote_ccy) + ' ' + h.quote_ccy));
        result.appendChild(kv('調整成本移除', f.money(costRemoved, h.quote_ccy)));
        result.appendChild(kv('已實現損益', f.signed(realized, h.quote_ccy) + ' ' + h.quote_ccy, f.signClass(realized)));
        result.appendChild(kv('剩餘股數', f.num(remainShares)));
        if (remainValue !== null && totalTwd) {
          const newTotal = totalTwd - (qty * hMktPrice * fxRate) + (proceeds - amount) * fxRate;
          result.appendChild(kv('剩餘市值', f.money(remainValue, h.quote_ccy) + ' ' + h.quote_ccy));
          result.appendChild(kv('新權重（約）', f.pct((remainValue * fxRate) / newTotal)));
        }
        if (!note.textContent) note.textContent = '費稅規則：' + ft.rule + '。已實現損益以調整均價計算。';
      } else {
        const cost = amount + ft.fee + ft.tax;
        const newShares = hShares + qty;
        const newOrigTotal = hOrigTotal + cost;
        const newAdjTotal = hAdjTotal + cost;
        result.appendChild(kv('成交金額', f.money(amount, h.quote_ccy) + ' ' + h.quote_ccy));
        result.appendChild(kv('手續費', f.money(ft.fee, h.quote_ccy)));
        result.appendChild(kv('稅', f.money(ft.tax, h.quote_ccy)));
        result.appendChild(kv('總成本（含費稅）', f.money(cost, h.quote_ccy) + ' ' + h.quote_ccy));
        result.appendChild(kv('新持股', f.num(newShares)));
        result.appendChild(kv('新原始均價', f.price(newOrigTotal / newShares, h.quote_ccy)));
        result.appendChild(kv('新調整均價', f.price(newAdjTotal / newShares, h.quote_ccy)));
        if (hMktPrice !== null && totalTwd) {
          const newValue = newShares * hMktPrice;
          const newTotal = totalTwd + (qty * hMktPrice * fxRate);
          result.appendChild(kv('新權重（約）', f.pct((newValue * fxRate) / newTotal)));
        }
        note.textContent = '費稅規則：' + ft.rule + '。';
      }
    }
    compute();
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
