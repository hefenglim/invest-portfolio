/* portfolio-dash — DOM rendering. All rows are generated from /api/dashboard.
   The dashboard payload is fetched ONCE via a shared promise (window.pdDashboard)
   that app.js / charts.js / alerts.js race-safely reuse (they load in different
   orders; whichever runs first creates the single in-flight request). Money/price/
   rate values arrive as Decimal STRINGS — the frontend never computes money; all
   numbers route through window.fmt (which coerces internally for display only). */
(function () {
  'use strict';
  let D;                       // set in boot() from the shared /api/dashboard promise
  const f = window.fmt;

  /* zh-TW display names (brief §5) */
  const ACCOUNT_ZH = {
    tw_broker: '台灣券商',
    schwab: '嘉信 Schwab',
    moomoo_my_us: 'Moomoo 美股',
    moomoo_my_my: 'Moomoo 馬股'
  };
  const MARKET_ZH = { TW: '台股', US: '美股', MY: '馬股' };
  const CCY_COLOR = { TWD: '#58a6dd', USD: '#9b86d8', MYR: '#d9a13f' };

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  /* ============ A. Header ============ */
  function renderHeader() {
    $('#asof-value').textContent = f.datetime(D.as_of);
    $('#report-ccy').textContent = '報告幣別 ' + D.reporting_currency;
    const chip = $('#fresh-chip');
    if (D.freshness && D.freshness.any_stale) {
      chip.className = 'badge badge-fresh-stale';
      chip.innerHTML = '<span class="dot"></span>部分過期';
      chip.title = '部分價格或匯率資料已過期，點擊查看資料新鮮度明細';
    } else {
      chip.className = 'badge badge-fresh-ok';
      chip.innerHTML = '<span class="dot"></span>資料新鮮';
      chip.removeAttribute('href');
    }
    renderUnregisteredBanner();
  }

  /* Unregistered-symbol warning (2026-07-02): ledger rows whose symbol has no
     instrument registration are EXCLUDED from every number on this page — surface
     that loudly with a fix link, or the exclusion would look like silent data loss. */
  function renderUnregisteredBanner() {
    const syms = (D.freshness && D.freshness.unregistered_symbols) || [];
    const page = document.querySelector('.page');
    const old = document.getElementById('unreg-banner');
    if (old) old.remove();
    if (!syms.length || !page) return;
    const bar = el('div', 'unreg-banner');
    bar.id = 'unreg-banner';
    bar.appendChild(el('span', 'unreg-ico', '⚠'));
    const txt = el('span', 'unreg-text',
      '帳本中有 ' + syms.length + ' 檔未註冊標的（' + syms.join('、') +
      '）— 相關交易未納入任何統計。');
    bar.appendChild(txt);
    const link = el('a', 'unreg-link', '前往標的管理註冊');
    link.href = 'instruments.html';
    bar.appendChild(link);
    page.insertBefore(bar, page.firstChild);
  }

  /* ============ B. KPI band v2 — 3 hero + 2 combo (5 visual units) ============ */
  function renderKpis() {
    const k = D.kpis;
    const ccy = k ? k.reporting_currency : D.reporting_currency;
    const band = $('#kpi-band');
    band.classList.add('v2');
    band.replaceChildren();

    const nil = (v) => v === null || v === undefined;
    const mkValue = (v, render, signed) => {
      const value = el('div', 'kpi-value num');
      if (nil(v)) { value.textContent = f.NULL_GLYPH; value.classList.add('sign-nil'); }
      else {
        value.textContent = render(v);
        if (signed) value.classList.add(f.signClass(v));
      }
      return value;
    };
    const nilBadge = (label, reason) => {
      const b = el('span', 'badge badge-stale-mini', reason || '資料不足');
      b.title = reason || '資料不足';
      label.appendChild(b);
      return label;
    };

    /* hero 1: 總市值 */
    {
      const card = el('div', 'kpi-card kpi-hero');
      const label = el('div', 'kpi-label', '總市值');
      if (nil(k && k.total_market_value)) nilBadge(label, '匯率資料不足');
      card.appendChild(label);
      const value = mkValue(k && k.total_market_value, (v) => f.money(v, ccy), false);
      if (!nil(k && k.total_market_value)) value.appendChild(el('span', 'kpi-unit', ' ' + ccy));
      card.appendChild(value);
      band.appendChild(card);
    }

    /* hero 2: 總報酬 + 累計報酬率 subline */
    {
      const card = el('div', 'kpi-card kpi-hero');
      const label = el('div', 'kpi-label', '總報酬');
      if (nil(k && k.total_return)) nilBadge(label);
      card.appendChild(label);
      const value = mkValue(k && k.total_return, (v) => f.signed(v, ccy), true);
      card.appendChild(value);
      const sub = el('div', 'kpi-subline');
      sub.appendChild(el('span', null, '累計報酬率'));
      const rate = el('span', nil(k && k.total_return_rate) ? 'sign-nil' : f.signClass(k.total_return_rate),
        nil(k && k.total_return_rate) ? f.NULL_GLYPH : f.signedPct(k.total_return_rate));
      sub.appendChild(rate);
      sub.appendChild(el('span', null, '· vs 原始投入成本'));
      card.appendChild(sub);
      if (!nil(k && k.total_return)) {
        if (k.total_return > 0) card.classList.add('kpi-up');
        if (k.total_return < 0) card.classList.add('kpi-down');
      }
      band.appendChild(card);
    }

    /* hero 3: XIRR */
    {
      const card = el('div', 'kpi-card kpi-hero');
      const label = el('div', 'kpi-label', '年化報酬 (XIRR)');
      const xirrNil = nil(k && k.xirr);
      if (xirrNil) nilBadge(label, (D.freshness && D.freshness.xirr_unavailable_reason) || '資料不足');
      card.appendChild(label);
      const value = mkValue(k && k.xirr, f.signedPct, true);
      card.appendChild(value);
      const sub = el('div', 'kpi-subline');
      sub.appendChild(el('span', null, '資金加權・FX-aware・決策主指標'));
      card.appendChild(sub);
      if (!xirrNil) {
        if (k.xirr > 0) card.classList.add('kpi-up');
        if (k.xirr < 0) card.classList.add('kpi-down');
      }
      band.appendChild(card);
    }

    /* combo helper */
    const combo = (title, rows) => {
      const card = el('div', 'kpi-card kpi-combo');
      const label = el('div', 'kpi-label', title);
      card.appendChild(label);
      rows.forEach(([rk, v, reason]) => {
        const row = el('div', 'combo-row');
        row.appendChild(el('span', 'k', rk));
        const vv = el('span', 'v');
        if (nil(v)) {
          vv.textContent = f.NULL_GLYPH;
          vv.classList.add('sign-nil');
          vv.title = reason || '資料不足';
        } else {
          vv.textContent = f.signed(v, ccy);
          vv.classList.add(f.signClass(v));
        }
        row.appendChild(vv);
        card.appendChild(row);
      });
      return card;
    };
    band.appendChild(combo('損益（' + ccy + '）', [
      ['已實現', k && k.realized_total],
      ['未實現', k && k.unrealized_total]
    ]));
    band.appendChild(combo('換匯損益（歸因拆分）', [
      ['已實現', k && k.fx_realized, '匯率資料不足'],
      ['未實現', k && k.fx_unrealized, '匯率資料不足']
    ]));
  }

  /* ============ B2. 各幣別報酬拆分 ============ */
  function renderCcyReturns() {
    const host = document.getElementById('ccyret-body');
    if (!host) return;
    const r = D.returns;
    const wrap = document.getElementById('ccyret-wrap');
    if (!r || !r.by_currency) {
      if (wrap) {
        wrap.replaceChildren(emptyState('尚無各幣別報酬資料'));
      }
      return;
    }
    host.replaceChildren();
    Object.keys(r.by_currency).forEach((ccy) => {
      const row = r.by_currency[ccy];
      const tr = el('tr');
      tr.appendChild(el('td', null, ccy));
      tr.appendChild(el('td', 'num ' + f.signClass(row.realized), f.signed(row.realized, ccy)));
      tr.appendChild(el('td', 'num ' + f.signClass(row.unrealized), f.signed(row.unrealized, ccy)));
      tr.appendChild(el('td', 'num ' + f.signClass(row.total_return), f.signed(row.total_return, ccy)));
      tr.appendChild(el('td', 'num', f.money(row.gross_invested, ccy)));
      tr.appendChild(el('td', 'num ' + f.signClass(row.rate), f.signedPct(row.rate)));
      host.appendChild(tr);
    });
    const chips = document.getElementById('ccyret-chips');
    if (chips) {
      chips.replaceChildren();
      Object.keys(r.by_currency).forEach((ccy) => {
        const row = r.by_currency[ccy];
        const chip = el('span', 'ccy-chip');
        chip.appendChild(el('span', null, ccy + ' '));
        chip.appendChild(el('b', f.signClass(row.rate), f.signedPct(row.rate)));
        chips.appendChild(chip);
      });
    }
  }

  /* ============ D. Holdings table ============ */
  const holdingsState = { account: 'all', market: 'all', sortKey: null, sortDir: -1 };

  function renderFilterChips() {
    const bar = $('#filter-bar');
    bar.replaceChildren();
    const mkChip = (group, value, label) => {
      const c = el('button', 'chip', label);
      c.type = 'button';
      if (holdingsState[group] === value) c.classList.add('active');
      c.addEventListener('click', () => {
        holdingsState[group] = value;
        renderFilterChips();
        renderHoldings();
      });
      return c;
    };
    bar.appendChild(el('span', 'group-label', '帳戶'));
    bar.appendChild(mkChip('account', 'all', '全部'));
    const seen = [];
    D.holdings.forEach((h) => { if (!seen.includes(h.account_id)) seen.push(h.account_id); });
    seen.forEach((id) => bar.appendChild(mkChip('account', id, ACCOUNT_ZH[id] || id)));
    bar.appendChild(el('span', 'divider'));
    bar.appendChild(el('span', 'group-label', '市場'));
    bar.appendChild(mkChip('market', 'all', '全部'));
    ['TW', 'US', 'MY'].forEach((m) => bar.appendChild(mkChip('market', m, MARKET_ZH[m])));
  }

  const HOLDING_COLS = [
    { key: 'symbol', label: '代號 / 名稱', text: true },
    { key: 'market', label: '市場', text: true },
    { key: 'account_id', label: '帳戶', text: true },
    { key: 'shares', label: '股數' },
    { key: 'original_avg', label: '原始均價' },
    { key: 'adjusted_avg', label: '調整均價' },
    { key: 'market_price', label: '現價' },
    { key: '_spark', label: '30 日走勢', nosort: true },
    { key: 'market_value', label: '市值' },
    { key: 'unrealized_pnl', label: '未實現損益' },
    { key: 'payback_ratio', label: '股利回收率' },
    { key: 'weight', label: '權重' }
  ];

  function renderHoldingsHead() {
    const tr = $('#holdings-head');
    tr.replaceChildren();
    HOLDING_COLS.forEach((c) => {
      if (c.nosort) {
        tr.appendChild(el('th', c.text ? 'col-text' : null, c.label));
        return;
      }
      const th = el('th', 'sortable' + (c.text ? ' col-text' : ''), c.label);
      if (holdingsState.sortKey === c.key) {
        th.appendChild(el('span', 'arrow', holdingsState.sortDir > 0 ? '▲' : '▼'));
      }
      th.addEventListener('click', () => {
        if (holdingsState.sortKey === c.key) holdingsState.sortDir *= -1;
        else { holdingsState.sortKey = c.key; holdingsState.sortDir = c.text ? 1 : -1; }
        renderHoldingsHead();
        renderHoldings();
      });
      tr.appendChild(th);
    });
  }

  function sortedFilteredHoldings() {
    let rows = D.holdings.filter((h) =>
      (holdingsState.account === 'all' || h.account_id === holdingsState.account) &&
      (holdingsState.market === 'all' || h.market === holdingsState.market));
    const k = holdingsState.sortKey;
    if (k) {
      const dir = holdingsState.sortDir;
      /* Numeric columns now arrive as Decimal STRINGS over the wire, so we cannot rely
         on typeof to pick string-vs-number compare. Use the column's declared `text`
         flag instead: text columns sort lexically, numeric columns compare as numbers
         (string−string coerces to a number; that is display-ordering, not money math). */
      const col = HOLDING_COLS.find((c) => c.key === k);
      const isText = !!(col && col.text);
      rows = rows.slice().sort((a, b) => {
        const av = a[k], bv = b[k];
        if (av === null || av === undefined) return 1;   /* nulls last */
        if (bv === null || bv === undefined) return -1;
        if (isText) return String(av).localeCompare(String(bv)) * dir;
        return (Number(av) - Number(bv)) * dir;
      });
    }
    return rows;
  }

  /* E2: 30日迷你走勢圖（inline SVG，紅漲綠跌依 30 日變動）
     Consumes the holding's spark_30d (a Decimal-STRING array from /api/dashboard);
     each point is mapped through Number() for the SVG geometry only (the coordinate
     math is display-derived, not money of record). */
  function sparkline(spark) {
    if (!Array.isArray(spark) || spark.length < 2) {
      const sp = el('span', 'sign-nil', f.NULL_GLYPH);
      sp.title = '無歷史價格';
      return sp;
    }
    const pts = spark.slice(-22).map((p) => Number(p));
    const w = 72, hh = 22, pad = 2;
    const min = Math.min(...pts), max = Math.max(...pts);
    const span = max - min || 1;
    const step = (w - pad * 2) / (pts.length - 1);
    const coords = pts.map((v, i) =>
      (pad + i * step).toFixed(1) + ',' + (hh - pad - ((v - min) / span) * (hh - pad * 2)).toFixed(1));
    const chg = (pts[pts.length - 1] - pts[0]) / pts[0];
    const color = chg > 0 ? 'var(--up)' : chg < 0 ? 'var(--down)' : 'var(--text-3)';
    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('width', w);
    svg.setAttribute('height', hh);
    svg.setAttribute('viewBox', '0 0 ' + w + ' ' + hh);
    svg.classList.add('sparkline');
    const poly = document.createElementNS(svgNS, 'polyline');
    poly.setAttribute('points', coords.join(' '));
    poly.setAttribute('fill', 'none');
    poly.setAttribute('stroke', color);
    poly.setAttribute('stroke-width', '1.3');
    svg.appendChild(poly);
    const dot = document.createElementNS(svgNS, 'circle');
    const lastXY = coords[coords.length - 1].split(',');
    dot.setAttribute('cx', lastXY[0]);
    dot.setAttribute('cy', lastXY[1]);
    dot.setAttribute('r', '1.8');
    dot.setAttribute('fill', color);
    svg.appendChild(dot);
    const wrap = el('span', 'spark-wrap');
    wrap.title = '30 日 ' + f.signedPct(chg);
    wrap.appendChild(svg);
    return wrap;
  }

  function renderHoldings() {
    const tbody = $('#holdings-body');
    tbody.replaceChildren();
    const rows = sortedFilteredHoldings();
    const maxWeight = Math.max(...D.holdings.map((h) => h.weight || 0));
    const maxPayback = Math.max(...D.holdings.map((h) => h.payback_ratio || 0));

    rows.forEach((h) => {
      const tr = el('tr');
      if (h.market_price === null || h.market_price === undefined || h.price_stale) {
        tr.classList.add('row-stale');
        tr.title = h.market_price === null || h.market_price === undefined
          ? '缺價 — 此列數字不可信' : '價格過期 — 損益以舊價計算';
      }
      if (h.oversold) {
        tr.classList.add('row-stale');
        tr.title = '賣超：賣出數量超過持股，部位為負、損益待釐清'
          + '（請補記期初庫存或遺漏的買進）';
      }

      /* 代號 + 名稱 + board badge — 點擊開啟個股詳情 */
      const tdSym = el('td', 'col-text');
      const cell = el('div', 'sym-cell sym-link');
      cell.title = '點擊查看個股詳情（價格與成本、配息史、試算）';
      cell.appendChild(el('span', 'sym-code', h.symbol));
      cell.appendChild(el('span', 'sym-name', h.name));
      if (h.board) cell.appendChild(el('span', 'board-badge', h.board));
      if (h.oversold) {
        const ob = el('span', 'badge badge-missing', '賣超');
        ob.title = '賣出數量超過持股，部位為負、損益待釐清';
        cell.appendChild(ob);
      }
      cell.addEventListener('click', () => {
        if (window.openSymbolDrawer) window.openSymbolDrawer(h.symbol);
      });
      tdSym.appendChild(cell);
      tr.appendChild(tdSym);

      tr.appendChild(el('td', 'col-text', MARKET_ZH[h.market] || h.market));
      tr.appendChild(el('td', 'col-text', ACCOUNT_ZH[h.account_id] || h.account_name));
      tr.appendChild(el('td', 'num', f.num(h.shares)));
      tr.appendChild(el('td', 'num', f.price(h.original_avg, h.quote_ccy)));
      tr.appendChild(el('td', 'num', f.price(h.adjusted_avg, h.quote_ccy)));

      /* 現價 (+ 過期 / 缺價 badge) */
      const tdPrice = el('td', 'num');
      if (h.market_price === null || h.market_price === undefined) {
        tdPrice.appendChild(el('span', 'sign-nil', f.NULL_GLYPH + ' '));
        const b = el('span', 'badge badge-missing', '缺價');
        b.title = '無法取得價格資料';
        tdPrice.appendChild(b);
      } else {
        tdPrice.appendChild(el('span', null, f.price(h.market_price, h.quote_ccy)));
        if (h.price_stale) {
          tdPrice.appendChild(document.createTextNode(' '));
          const b = el('span', 'badge badge-stale-mini', '過期');
          b.title = '價格日期 ' + f.date(h.price_as_of);
          tdPrice.appendChild(b);
        }
      }
      tr.appendChild(tdPrice);

      /* 30日 sparkline (E2) — from the holding's spark_30d (Decimal-string array) */
      const tdSpark = el('td', 'spark-cell');
      tdSpark.appendChild(sparkline(h.spark_30d));
      tr.appendChild(tdSpark);

      /* 市值 (native ccy) */
      const tdMv = el('td', 'num');
      if (h.market_value === null || h.market_value === undefined) {
        tdMv.textContent = f.NULL_GLYPH;
        tdMv.classList.add('sign-nil');
      } else {
        tdMv.textContent = f.money(h.market_value, h.quote_ccy);
        tdMv.appendChild(el('span', 'kpi-unit', ' ' + h.quote_ccy));
      }
      tr.appendChild(tdMv);

      /* 未實現損益 value + % vs adjusted cost */
      const tdPnl = el('td', 'num ' + f.signClass(h.unrealized_pnl));
      if (h.unrealized_pnl === null || h.unrealized_pnl === undefined) {
        tdPnl.textContent = f.NULL_GLYPH;
      } else {
        tdPnl.appendChild(el('span', null, f.signed(h.unrealized_pnl, h.quote_ccy)));
        if (h.adjusted_cost_total) {
          tdPnl.appendChild(el('span', 'subpct',
            f.signedPct(h.unrealized_pnl / h.adjusted_cost_total)));
        }
      }
      tr.appendChild(tdPnl);

      /* 股利回收率 mini progress */
      const tdPb = el('td', 'num');
      if (h.payback_ratio === null || h.payback_ratio === undefined) {
        tdPb.textContent = f.NULL_GLYPH;
        tdPb.classList.add('sign-nil');
      } else {
        const wrap = el('span', 'mini-bar');
        const track = el('span', 'track');
        const fill = el('span', 'fill payback');
        fill.style.width = (maxPayback ? (h.payback_ratio / maxPayback) * 100 : 0) + '%';
        track.appendChild(fill);
        wrap.appendChild(track);
        wrap.appendChild(el('span', null, f.pct(h.payback_ratio)));
        tdPb.appendChild(wrap);
      }
      tr.appendChild(tdPb);

      /* 權重 mini bar + % */
      const tdW = el('td', 'num');
      if (h.weight === null || h.weight === undefined) {
        tdW.textContent = f.NULL_GLYPH;
        tdW.classList.add('sign-nil');
      } else {
        const wrap = el('span', 'mini-bar');
        const track = el('span', 'track');
        const fill = el('span', 'fill');
        fill.style.width = (maxWeight ? (h.weight / maxWeight) * 100 : 0) + '%';
        track.appendChild(fill);
        wrap.appendChild(track);
        wrap.appendChild(el('span', null, f.pct(h.weight)));
        tdW.appendChild(wrap);
      }
      tr.appendChild(tdW);

      tbody.appendChild(tr);
    });

    /* totals row — TWD totals come from kpis (already merged in reporting ccy) */
    const tfoot = $('#holdings-foot');
    tfoot.replaceChildren();
    const tr = el('tr');
    const tdLabel = el('td', 'col-text', '合計（' + D.reporting_currency + '，缺價標的除外）');
    tdLabel.colSpan = 8;
    tr.appendChild(tdLabel);
    const tdMv = el('td', 'num');
    tdMv.textContent = f.money(D.kpis && D.kpis.total_market_value, D.reporting_currency);
    tr.appendChild(tdMv);
    const pnl = D.kpis && D.kpis.unrealized_total;
    const tdPnl = el('td', 'num ' + f.signClass(pnl), f.signed(pnl, D.reporting_currency));
    tr.appendChild(tdPnl);
    const tdRest = el('td');
    tdRest.colSpan = 2;
    tr.appendChild(tdRest);
    tfoot.appendChild(tr);
  }

  /* ============ E2. 幣別組成 ============ */
  function renderCurrencyView() {
    const panel = $('#ccy-content');
    panel.replaceChildren();
    const cv = D.currency_view;
    if (!cv) {
      panel.appendChild(emptyState('匯率資料不足，無法合併計價'));
      return;
    }
    const head = el('div', 'ccy-headline');
    const big = el('span', 'num', f.money(cv.reporting_total_value, cv.reporting_currency));
    head.appendChild(big);
    head.appendChild(el('span', 'cap', cv.reporting_currency + ' 合併計價總值'));
    panel.appendChild(head);

    /* share of each currency derived from holdings[].weight (reporting terms);
       holdings with null weight (缺價) are excluded. weight is a RATIO (not money)
       and arrives as a Decimal STRING — summing with `+` concatenated strings and
       rendered NaN% whenever a currency held 2+ positions (fixed 2026-07-03).
       Coercing a display-only ratio is the documented input-side exception. */
    const shareByCcy = {};
    let excluded = 0;
    D.holdings.forEach((h) => {
      const w = Number(h.weight);
      if (h.weight === null || h.weight === undefined || !isFinite(w)) {
        excluded += 1;
        return;
      }
      shareByCcy[h.quote_ccy] = (shareByCcy[h.quote_ccy] || 0) + w;
    });

    const stack = el('div', 'ccy-stack');
    Object.keys(cv.by_currency_value).forEach((ccy) => {
      if (!shareByCcy[ccy]) return;
      const seg = el('span', 'seg');
      seg.style.width = (shareByCcy[ccy] * 100) + '%';
      seg.style.background = CCY_COLOR[ccy] || '#777';
      seg.title = ccy + ' ' + f.pct(shareByCcy[ccy]);
      stack.appendChild(seg);
    });
    panel.appendChild(stack);

    const rows = el('div', 'ccy-rows');
    Object.keys(cv.by_currency_value).forEach((ccy) => {
      const row = el('div', 'ccy-row');
      const key = el('div', 'ccy-key');
      const sw = el('span', 'ccy-swatch');
      sw.style.background = CCY_COLOR[ccy] || '#777';
      key.appendChild(sw);
      key.appendChild(el('span', 'ccy-code', ccy));
      row.appendChild(key);
      row.appendChild(el('span', 'ccy-share',
        shareByCcy[ccy] !== undefined ? '權重 ' + f.pct(shareByCcy[ccy]) : ''));
      const amt = el('span', 'ccy-amt', f.money(cv.by_currency_value[ccy], ccy));
      amt.appendChild(el('span', 'kpi-unit', ' ' + ccy));
      row.appendChild(amt);
      rows.appendChild(row);
    });
    panel.appendChild(rows);
    panel.appendChild(el('div', 'ccy-note',
      '各列為原幣金額；權重以報告幣別市值計算' +
      (excluded ? '，缺價標的（' + excluded + '）不計入權重。' : '。')));
  }

  /* ============ F2. 各帳戶現金 (R6 item 7) ============
     Separate lightweight GET /api/cash (dashboard payload untouched); degrades
     to a hint on failure. Amounts are Decimal STRINGS via fmt. */
  async function renderCashMini() {
    const host = $('#cash-mini');
    if (!host) return;
    let resp;
    try {
      resp = await window.pdApi.get('/api/cash');
    } catch (err) {
      host.replaceChildren(el('div', 'hint', '現金資料載入失敗'));
      return;
    }
    host.replaceChildren();
    const balances = (resp && resp.balances) || [];
    if (!balances.length) {
      host.appendChild(el('div', 'hint',
        '尚無現金紀錄 — 到「資金管理」補一筆初始入金，現金池就會開始追蹤。'));
      return;
    }
    const grid = el('div', 'cash-mini-grid');
    const byAcct = new Map();
    balances.forEach((b) => {
      if (!byAcct.has(b.account_id)) byAcct.set(b.account_id, { name: b.account, lines: [] });
      byAcct.get(b.account_id).lines.push(b);
    });
    byAcct.forEach((entry) => {
      const card = el('div', 'cash-mini-card');
      card.appendChild(el('div', 'acct', entry.name));
      entry.lines.forEach((b) => {
        const line = el('div', 'line num');
        const neg = String(b.amount).indexOf('-') === 0;
        line.textContent = b.ccy + '  ' + f.money(b.amount, b.ccy);
        if (neg) {
          line.classList.add('neg');
          line.title = '負現金 — 通常代表漏記入金或換匯';
        }
        card.appendChild(line);
      });
      grid.appendChild(card);
    });
    host.appendChild(grid);
    if (resp.reporting_total != null) {
      host.appendChild(el('div', 'hint',
        '合併現金（' + resp.reporting_currency + '）: ' +
        f.money(resp.reporting_total, resp.reporting_currency) + ' ' + resp.reporting_currency));
    }
  }

  /* ============ I2. 月度成績 (R6 item 8) ============ */
  async function renderSnapshots() {
    const tbody = $('#snapshots-body');
    if (!tbody) return;
    let resp;
    try {
      resp = await window.pdApi.get('/api/snapshots', { limit: 12 });
    } catch (err) {
      return;  // non-critical panel: stay empty on failure
    }
    const rows = (resp && resp.rows) || [];
    tbody.replaceChildren();
    if (!rows.length) {
      const tr = el('tr');
      const td = el('td', 'sign-nil', '尚無快照 — 排程每晚 23:50 產生當月快照，月底值即定格。');
      td.colSpan = 5;
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    rows.forEach((s) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'col-text num', s.month));
      const cell = (v, pct) => {
        const td = el('td', 'num');
        if (v == null) { td.textContent = f.NULL_GLYPH; td.classList.add('sign-nil'); }
        else td.textContent = pct ? f.signedPct(v) : f.money(v, s.reporting_ccy);
        return td;
      };
      tr.appendChild(cell(s.total_value, false));
      const ret = el('td', 'num ' + f.signClass(s.total_return));
      ret.textContent = s.total_return == null ? f.NULL_GLYPH : f.signed(s.total_return, s.reporting_ccy);
      tr.appendChild(ret);
      tr.appendChild(cell(s.total_return_rate, true));
      tr.appendChild(cell(s.xirr, true));
      tbody.appendChild(tr);
    });
  }

  /* ============ F. 換匯損益 ============ */
  function renderFx() {
    const grid = $('#fx-grid');
    const footer = $('#fx-footer');
    grid.replaceChildren();
    footer.replaceChildren();
    const fx = D.fx;
    if (!fx) {
      grid.appendChild(emptyState('匯率資料不足，無法合併計價'));
      return;
    }
    Object.keys(fx.by_account).forEach((id) => {
      const a = fx.by_account[id];
      const card = el('div', 'fx-card');
      const head = el('div', 'fx-card-head');
      head.appendChild(el('span', 'fx-account', ACCOUNT_ZH[a.account_id] || a.account_id));
      head.appendChild(el('span', 'fx-pair', a.foreign_ccy + ' → ' + a.home_ccy));
      card.appendChild(head);

      const rates = el('div', 'fx-rates');
      const b1 = el('div', 'fx-rate-block');
      b1.appendChild(el('span', 'fx-rate-label', '平均取得匯率'));
      b1.appendChild(el('span', 'fx-rate-val', f.rate(a.avg_rate)));
      rates.appendChild(b1);
      rates.appendChild(el('span', 'fx-arrow', '→'));
      const b2 = el('div', 'fx-rate-block');
      b2.appendChild(el('span', 'fx-rate-label', '現時匯率'));
      b2.appendChild(el('span', 'fx-rate-val', f.rate(a.current_spot)));
      rates.appendChild(b2);
      if (a.avg_rate !== null && a.current_spot !== null &&
          a.avg_rate !== undefined && a.current_spot !== undefined) {
        const delta = a.current_spot - a.avg_rate;
        rates.appendChild(el('span', 'fx-delta ' + f.signClass(delta),
          f.signedNum(delta, a.current_spot < 10 ? 4 : 2)));
      }
      card.appendChild(rates);

      const stats = el('div', 'fx-stats');
      /* Per-account combined unrealized FX is a DISPLAY attribution of two components
         the backend already broke out (no combined per-account field on the wire). Both
         are Decimal STRINGS — coerce via Number() so we add, not string-concatenate
         ("1"+"2"="12"). The authoritative reporting-currency total is backend-supplied. */
      const unrelSum = (a.unrealized_fx_stocks ?? null) === null || (a.unrealized_fx_cash ?? null) === null
        ? null : Number(a.unrealized_fx_stocks) + Number(a.unrealized_fx_cash);
      const items = [
        ['外幣現金', a.foreign_cash, a.foreign_ccy, false],
        ['外幣股票市值', a.foreign_stock_value, a.foreign_ccy, false],
        ['已實現匯損益', a.realized_fx, a.home_ccy, true],
        ['未實現匯損益（股票）', a.unrealized_fx_stocks, a.home_ccy, true],
        ['未實現匯損益（現金）', a.unrealized_fx_cash, a.home_ccy, true],
        ['未實現匯損益（合計）', unrelSum, a.home_ccy, true]
      ];
      items.forEach(([k, v, ccy, isSigned]) => {
        const st = el('div', 'fx-stat');
        st.appendChild(el('span', 'k', k));
        const vv = el('span', 'v');
        if (v === null || v === undefined) {
          vv.textContent = f.NULL_GLYPH;
          vv.title = '無換匯紀錄或匯率資料不足';
          vv.classList.add('sign-nil');
        } else {
          vv.textContent = (isSigned ? f.signed : f.money)(v, ccy) + ' ' + ccy;
          if (isSigned) vv.classList.add(f.signClass(v));
        }
        st.appendChild(vv);
        stats.appendChild(st);
      });
      card.appendChild(stats);
      grid.appendChild(card);
    });

    const mk = (label, v) => {
      const s = el('span', null, label + ' ');
      const vv = el('span', 'v ' + f.signClass(v), f.signed(v, fx.reporting_currency) + ' ' + fx.reporting_currency);
      s.appendChild(vv);
      return s;
    };
    footer.appendChild(el('span', null, '報告幣別合計：'));
    footer.appendChild(mk('已實現', fx.reporting_realized_fx));
    footer.appendChild(mk('未實現', fx.reporting_unrealized_fx));

    /* collapsed-state summary chips */
    const sum = $('#fx-summary');
    if (sum) {
      sum.replaceChildren();
      [['已實現', fx.reporting_realized_fx], ['未實現', fx.reporting_unrealized_fx]].forEach(([k, v]) => {
        const chip = el('span', 'ccy-chip');
        chip.appendChild(el('span', null, k + ' '));
        chip.appendChild(el('b', f.signClass(v), f.signed(v, fx.reporting_currency) + ' ' + fx.reporting_currency));
        sum.appendChild(chip);
      });
    }
  }

  /* ============ G. 已實現損益 ============ */
  function renderRealized() {
    const tbody = $('#realized-body');
    tbody.replaceChildren();
    D.realized.rows.forEach((r) => {
      const tr = el('tr');
      const tdSym = el('td', 'col-text');
      const cell = el('div', 'sym-cell sym-link');
      cell.title = '點擊查看個股詳情';
      cell.appendChild(el('span', 'sym-code', r.symbol));
      cell.addEventListener('click', () => {
        if (window.openSymbolDrawer) window.openSymbolDrawer(r.symbol);
      });
      tdSym.appendChild(cell);
      tr.appendChild(tdSym);
      tr.appendChild(el('td', 'col-text', ACCOUNT_ZH[r.account_id] || r.account_id));
      tr.appendChild(el('td', 'num', f.num(r.shares_sold)));
      const tdProceeds = el('td', 'num', f.money(r.proceeds_net, r.quote_ccy));
      tdProceeds.appendChild(el('span', 'kpi-unit', ' ' + r.quote_ccy));
      tr.appendChild(tdProceeds);
      tr.appendChild(el('td', 'num', f.money(r.adjusted_cost_removed, r.quote_ccy)));
      tr.appendChild(el('td', 'num ' + f.signClass(r.realized), f.signed(r.realized, r.quote_ccy)));
      tbody.appendChild(tr);
    });
    const footer = $('#realized-footer');
    footer.replaceChildren();
    footer.appendChild(el('span', null, '各幣別合計'));
    Object.keys(D.realized.by_currency).forEach((ccy) => {
      const v = D.realized.by_currency[ccy];
      const chip = el('span', 'ccy-chip');
      chip.appendChild(el('span', null, ccy + ' '));
      const b = el('b', f.signClass(v), f.signed(v, ccy));
      chip.appendChild(b);
      footer.appendChild(chip);
    });
  }

  /* ============ H. 股利區 ============ */
  function renderDividendChips() {
    const wrap = $('#div-chips');
    wrap.replaceChildren();
    Object.keys(D.dividends.total_by_currency).forEach((ccy) => {
      const chip = el('span', 'ccy-chip');
      chip.appendChild(el('span', null, ccy + ' '));
      chip.appendChild(el('b', null, f.money(D.dividends.total_by_currency[ccy], ccy)));
      wrap.appendChild(chip);
    });
  }

  function renderExDivCalendar() {
    const list = $('#exdiv-list');
    list.replaceChildren();
    const sum = $('#exdiv-summary');
    if (sum) sum.replaceChildren();
    /* E3: 列表/月曆雙視圖切換 */
    ensureExdivToggle();
    if (!D.ex_dividend_calendar || D.ex_dividend_calendar.length === 0) {
      list.appendChild(emptyState('近期無除息事件'));
      if (sum) sum.appendChild(el('span', null, '近期無除息事件'));
      return;
    }
    if (sum) {
      const next = D.ex_dividend_calendar[0];
      const chip = el('span', 'ccy-chip');
      chip.appendChild(el('span', null, '下次除息 '));
      chip.appendChild(el('b', null, f.date(next.ex_date) + ' ' + next.name));
      sum.appendChild(chip);
      sum.appendChild(el('span', null, '共 ' + D.ex_dividend_calendar.length + ' 筆'));
      /* F5: 年度股利現金流預估（已宣告事件 × 持有股數，各幣別分列、不可加總） */
      const proj = {};
      D.ex_dividend_calendar.forEach((e2) => {
        const held = D.holdings.find((h) => h.symbol === e2.symbol);
        if (held && e2.cash_amount) {
          proj[e2.currency] = (proj[e2.currency] || 0) + held.shares * e2.cash_amount;
        }
      });
      Object.keys(proj).forEach((ccy) => {
        const pchip = el('span', 'ccy-chip');
        pchip.title = '年內已宣告除息事件 × 目前持有股數（稅前估算）；各幣別分列，不可跨幣加總';
        pchip.appendChild(el('span', null, '年內股利預估 '));
        pchip.appendChild(el('b', null, f.money(proj[ccy], ccy) + ' ' + ccy));
        sum.appendChild(pchip);
      });
    }
    if (exdivView === 'month') { renderExDivMonth(list); return; }
    D.ex_dividend_calendar.forEach((e) => {
      const item = el('div', 'exdiv-item');
      const dt = el('div', 'exdiv-date');
      dt.appendChild(el('span', 'mm', e.ex_date.slice(0, 7)));
      dt.appendChild(el('span', 'dd', e.ex_date.slice(8, 10)));
      dt.title = '除息日 ' + f.date(e.ex_date);
      item.appendChild(dt);
      const main = el('div', 'exdiv-main');
      const sym = el('div', 'exdiv-sym sym-link');
      sym.title = '點擊查看個股詳情';
      sym.addEventListener('click', () => {
        if (window.openSymbolDrawer) window.openSymbolDrawer(e.symbol);
      });
      sym.appendChild(el('span', 'sym-code', e.symbol));
      sym.appendChild(el('span', 'sym-name', e.name));
      main.appendChild(sym);
      main.appendChild(el('span', 'exdiv-pay', '發放日 ' + f.date(e.pay_date)));
      /* A3: 入帳預覽 — 持倉標的預估入帳金額與調整均價影響 */
      const held = D.holdings.find((h) => h.symbol === e.symbol);
      if (held && e.cash_amount) {
        const est = held.shares * e.cash_amount;
        let preview = '預估入帳 ' + f.money(est, e.currency) + ' ' + e.currency;
        if (held.account_id === 'tw_broker') {
          const newAvg = (held.adjusted_cost_total - est) / held.shares;
          preview += '・沖減後調整均價 ' + f.price(held.adjusted_avg, e.currency) + ' → ' + f.price(newAvg, e.currency);
        } else if (held.account_id === 'schwab' || held.account_id === 'moomoo_my_us') {
          preview += '（稅前；預扣 30% 後約 ' + f.money(est * 0.7, e.currency) + '）';
        }
        main.appendChild(el('span', 'exdiv-preview', preview));
      }
      item.appendChild(main);
      const amt = el('div', 'exdiv-amt');
      const a = el('span', 'a', f.price(e.cash_amount, e.currency));
      amt.appendChild(a);
      amt.appendChild(el('span', 'c', e.currency + ' / 股'));
      item.appendChild(amt);
      list.appendChild(item);
    });
  }

  /* ============ I. AI 洞察 ============ */
  function renderInsights() {
    const grid = $('#insight-grid');
    grid.replaceChildren();
    /* 額度 chip 已改為頂欄常駐（alerts.js）；面板內不再重複顯示 */
    if (!D.insights || D.insights.length === 0) {
      grid.appendChild(emptyState('尚無 AI 洞察 — 洞察卡片由排程批次產生'));
      return;
    }
    D.insights.forEach((ins) => {
      const card = el('div', 'insight-card');
      const head = el('div', 'insight-head');
      head.appendChild(el('span', 'badge badge-ai', 'AI'));
      head.appendChild(el('h3', 'insight-title', ins.title));
      card.appendChild(head);
      /* Task-1.5 card shape: summary = concise body (body_md is the full markdown). */
      card.appendChild(el('p', 'insight-body', ins.summary));
      const foot = el('div', 'insight-foot');
      foot.appendChild(el('span', 'insight-time', f.datetime(ins.created_at)));
      /* cost_usd is a Decimal STRING — format via fmt (NOT .toFixed). "0" is a valid
         truthy-safe value, so nil-check with != null (catches null + undefined only). */
      /* Unified AI attribution (2026-07-07): model · token N · $cost — via fmt.aiAttrib;
         segments degrade when absent (legacy cards lack token counts). */
      const attrib = f.aiAttrib(ins.model, ins.tokens_in, ins.tokens_out, ins.cost_usd);
      if (attrib) {
        foot.appendChild(el('span', 'insight-cost ai-attrib num', attrib));
      }
      card.appendChild(foot);
      grid.appendChild(card);
    });
  }
  /* Empty-state variant for design review: set D.insights = [] and reload,
     or run renderInsightsEmptyPreview() from the console. */
  window.renderInsightsEmptyPreview = function () {
    const saved = D.insights;
    D.insights = [];
    renderInsights();
    D.insights = saved;
  };

  /* ============ J. 資料新鮮度 ============ */
  function renderFreshness() {
    const fr = D.freshness;
    if (!fr) return;
    const chips = $('#fresh-chips');
    chips.replaceChildren();
    (fr.missing_prices || []).forEach((s) => {
      chips.appendChild(el('span', 'badge badge-missing', '缺價 ' + s));
    });
    (fr.missing_fx || []).forEach((p) => {
      chips.appendChild(el('span', 'badge badge-missing', '缺匯率 ' + p));
    });

    const priceBody = $('#fresh-prices');
    priceBody.replaceChildren();
    fr.prices.forEach((p) => {
      const tr = el('tr');
      tr.appendChild(el('td', null, p.symbol));
      tr.appendChild(el('td', null, f.date(p.as_of)));
      const td = el('td');
      if (p.stale) td.appendChild(el('span', 'badge badge-stale-mini', '過期'));
      else td.appendChild(el('span', 'sign-nil', '—'));
      tr.appendChild(td);
      priceBody.appendChild(tr);
    });

    const fxBody = $('#fresh-fx');
    fxBody.replaceChildren();
    fr.fx.forEach((p) => {
      const tr = el('tr');
      tr.appendChild(el('td', null, p.base + '/' + p.quote));
      tr.appendChild(el('td', null, f.date(p.as_of)));
      const td = el('td');
      if (p.stale) td.appendChild(el('span', 'badge badge-stale-mini', '過期'));
      else td.appendChild(el('span', 'sign-nil', '—'));
      tr.appendChild(td);
      fxBody.appendChild(tr);
    });

    const notes = $('#fresh-notes');
    notes.replaceChildren();
    [fr.xirr_unavailable_reason, fr.trend_unavailable_reason].forEach((reason) => {
      if (reason) notes.appendChild(el('div', 'fresh-note', reason));
    });
  }

  /* shared empty state */
  function emptyState(msg) {
    const wrap = el('div', 'empty-state');
    wrap.appendChild(el('div', 'glyph', '∅'));
    wrap.appendChild(el('div', 'msg', msg));
    return wrap;
  }
  window.emptyState = emptyState;

  /* ============ E3: 除息月曆視圖 ============ */
  let exdivView = 'list';
  function ensureExdivToggle() {
    if (document.getElementById('exdiv-toggle')) return;
    const sum = $('#exdiv-summary');
    if (!sum || !sum.parentElement) return;
    const seg = el('div', 'segmented');
    seg.id = 'exdiv-toggle';
    seg.style.marginLeft = '10px';
    const bList = el('button', exdivView === 'list' ? 'active' : '', '列表');
    bList.type = 'button';
    const bMonth = el('button', exdivView === 'month' ? 'active' : '', '月曆');
    bMonth.type = 'button';
    bList.addEventListener('click', () => { exdivView = 'list'; bList.classList.add('active'); bMonth.classList.remove('active'); renderExDivCalendar(); });
    bMonth.addEventListener('click', () => { exdivView = 'month'; bMonth.classList.add('active'); bList.classList.remove('active'); renderExDivCalendar(); });
    seg.appendChild(bList);
    seg.appendChild(bMonth);
    sum.parentElement.appendChild(seg);
  }

  function renderExDivMonth(host) {
    /* months covered by events (ex_date 與 pay_date 都標) */
    const months = [];
    D.ex_dividend_calendar.forEach((e) => {
      [e.ex_date, e.pay_date].forEach((d) => {
        if (d) { const m = d.slice(0, 7); if (!months.includes(m)) months.push(m); }
      });
    });
    months.sort();
    const wrap = el('div', 'exdiv-months');
    months.forEach((m) => {
      const [yy, mm] = m.split('-').map(Number);
      const first = new Date(Date.UTC(yy, mm - 1, 1));
      const daysIn = new Date(Date.UTC(yy, mm, 0)).getUTCDate();
      const startDow = first.getUTCDay(); /* 0=Sun */
      const monthBox = el('div', 'exdiv-month');
      monthBox.appendChild(el('div', 'exm-title', yy + ' 年 ' + mm + ' 月'));
      const grid = el('div', 'exm-grid');
      ['日', '一', '二', '三', '四', '五', '六'].forEach((d) => grid.appendChild(el('span', 'exm-dow', d)));
      for (let i = 0; i < startDow; i++) grid.appendChild(el('span', 'exm-cell empty'));
      for (let d = 1; d <= daysIn; d++) {
        const dateStr = m + '-' + String(d).padStart(2, '0');
        const cell = el('span', 'exm-cell');
        cell.appendChild(el('span', 'exm-day', String(d)));
        const today = (D.as_of || '').slice(0, 10);
        if (dateStr === today) cell.classList.add('today');
        D.ex_dividend_calendar.forEach((e) => {
          if (e.ex_date === dateStr) {
            const chip = el('span', 'exm-ev ev-ex sym-link', e.symbol);
            chip.title = e.name + ' 除息・每股 ' + f.price(e.cash_amount, e.currency) + ' ' + e.currency;
            chip.addEventListener('click', () => { if (window.openSymbolDrawer) window.openSymbolDrawer(e.symbol); });
            cell.appendChild(chip);
          }
          if (e.pay_date === dateStr) {
            const chip = el('span', 'exm-ev ev-pay sym-link', e.symbol);
            chip.title = e.name + ' 發放日';
            chip.addEventListener('click', () => { if (window.openSymbolDrawer) window.openSymbolDrawer(e.symbol); });
            cell.appendChild(chip);
          }
        });
        grid.appendChild(cell);
      }
      monthBox.appendChild(grid);
      wrap.appendChild(monthBox);
    });
    const legend = el('div', 'exm-legend');
    legend.appendChild(el('span', 'exm-ev ev-ex', '除息日'));
    legend.appendChild(el('span', 'exm-ev ev-pay', '發放日'));
    legend.appendChild(el('span', null, '點擊代號開啟個股詳情'));
    host.appendChild(wrap);
    host.appendChild(legend);
  }

  /* ============ E6: 鍵盤導航 (j/k/↑/↓ 移動、Enter 開抽屜) ============ */
  let kbIndex = -1;
  function kbRows() { return Array.from(document.querySelectorAll('#holdings-body tr')); }
  function kbHighlight() {
    kbRows().forEach((r, i) => r.classList.toggle('kb-focus', i === kbIndex));
  }
  document.addEventListener('keydown', (e) => {
    if (e.target.closest('input, textarea, select')) return;
    if (document.querySelector('.sd-backdrop') || document.querySelector('.search-backdrop')) return;
    const rows = kbRows();
    if (!rows.length) return;
    if (e.key === 'j' || e.key === 'ArrowDown') {
      if (e.key === 'ArrowDown' && kbIndex < 0) return; /* don't hijack page scroll until engaged */
      e.preventDefault();
      kbIndex = Math.min(kbIndex + 1, rows.length - 1);
      kbHighlight();
    } else if (e.key === 'k' || (e.key === 'ArrowUp' && kbIndex >= 0)) {
      e.preventDefault();
      kbIndex = Math.max(kbIndex - 1, 0);
      kbHighlight();
    } else if (e.key === 'Enter' && kbIndex >= 0 && kbIndex < rows.length) {
      const sym = rows[kbIndex].querySelector('.sym-code');
      if (sym && window.openSymbolDrawer) window.openSymbolDrawer(sym.textContent);
    } else if (e.key === 'Escape') {
      kbIndex = -1;
      kbHighlight();
    }
  });

  /* ============ 匯出按鈕（對帳級 CSV，直接由後端計算核心產生） ============ */
  /* Owner directive 2026-07-14: every 匯出 CSV goes through the backend reconciliation
     channel (pdApi.download → /api/export/*); the frontend no longer dumps rendered/
     display values. House style: silent on success, fail toast, pdBusy guards double
     clicks; the filename comes from the backend Content-Disposition. */
  function csvExportButton(label, path, bodyFn) {
    const b = el('button', 'btn btn-sm btn-export');
    b.type = 'button';
    b.title = '匯出對帳級 CSV（由後端計算核心產生）';
    b.appendChild(el('span', 'ico', '⬇'));
    b.appendChild(el('span', null, label));
    b.addEventListener('click', async () => {
      const restore = window.pdBusy ? window.pdBusy(b, '匯出中…') : function () {};
      try {
        await window.pdApi.download(path, bodyFn ? bodyFn() : {});
      } catch (err) {
        if (window.toast) window.toast(err && err.message ? err.message : '匯出失敗', 'fail', err && err.code);
      } finally {
        restore();
      }
    });
    return b;
  }

  function wireExports() {
    /* 持倉明細 → POST /api/export/holdings (existing reconciliation endpoint) */
    const holdingsHead = document.querySelector('#holdings-table');
    if (holdingsHead) {
      const panelHead = holdingsHead.closest('.panel').querySelector('.panel-head');
      panelHead.appendChild(csvExportButton('匯出 CSV', '/api/export/holdings', () => ({})));
      /* 匯出報告: print-optimized 持倉報告 (self-contained HTML from the backend). Server
         recomputes everything (no client math). House style: silent on success, fail toast,
         busy state guards double-clicks. Compact tier + leading ⎙ icon so the whole holdings
         action row (再平衡試算 / 匯出 CSV / 匯出報告) reads as one coherent set. */
      const reportBtn = el('button', 'btn btn-sm pd-holdings-report-btn');
      reportBtn.appendChild(el('span', 'ico', '⎙'));
      reportBtn.appendChild(el('span', null, '匯出報告'));
      reportBtn.type = 'button';
      reportBtn.title = '下載持倉報告（可列印 HTML，含 KPI、持倉明細與配置）';
      reportBtn.addEventListener('click', async () => {
        const restore = window.pdBusy(reportBtn, '產出中…');
        try {
          await window.pdApi.download('/api/export/holdings-report', {});
        } catch (err) {
          if (window.toast) {
            window.toast(err && err.message ? err.message : '匯出報告失敗', 'fail', err && err.code);
          }
        } finally {
          restore();
        }
      });
      panelHead.appendChild(reportBtn);
    }
    /* 已實現損益 → POST /api/export/realized (new reconciliation endpoint) */
    const realizedBody = document.getElementById('realized-body');
    if (realizedBody) {
      const panelHead = realizedBody.closest('.panel').querySelector('.panel-head');
      panelHead.appendChild(csvExportButton('匯出 CSV', '/api/export/realized', () => ({})));
    }
  }

  /* ============ boot ============ */
  /* All renders depend on D, so fetch the shared /api/dashboard payload first, then
     run the render sequence. The promise is shared with charts.js / alerts.js via
     window.pdDashboard so exactly ONE request is made regardless of script order.
     On failure (api.js already handles 401 → login redirect) we render a graceful
     empty state instead of letting an unhandled rejection hit the console (the e2e
     smoke asserts ZERO console errors / pageerrors). */
  async function boot() {
    try {
      D = await (window.pdDashboard || (window.pdDashboard = window.pdApi.get('/api/dashboard')));
    } catch (err) {
      bootError(err);
      return;
    }
    renderHeader();
    renderKpis();
    renderCcyReturns();
    renderFilterChips();
    renderHoldingsHead();
    renderHoldings();
    renderCurrencyView();
    renderFx();
    renderCashMini();
    renderSnapshots();
    renderRealized();
    renderDividendChips();
    renderExDivCalendar();
    renderInsights();
    renderFreshness();
    wireExports();
  }

  /* Graceful degradation when the dashboard payload cannot be loaded (non-401; 401 is
     handled by api.js). Show a single empty state in the holdings area; never throw. */
  function bootError(err) {
    const body = $('#holdings-body');
    if (body) body.replaceChildren(el('tr', null, ''));
    const host = document.querySelector('.page');
    if (host && window.emptyState && !document.getElementById('dash-load-error')) {
      const box = emptyState('儀表板資料載入失敗，請稍後重新整理。');
      box.id = 'dash-load-error';
      host.insertBefore(box, host.firstChild);
    }
    if (window.toast) {
      window.toast('儀表板資料載入失敗', 'fail', err && err.message ? err.message : undefined);
    }
  }

  boot();
})();
