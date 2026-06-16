/* portfolio-dash — 帳本檢視 (read-only append-only ledgers, wired to /api/ledgers/*).

   The four ledgers are fetched in parallel through the single pdApi fetch layer
   (spec 19/11). All money / price / rate values arrive as Decimal STRINGS and are
   formatted ONLY via window.fmt — this module never computes money. The implied FX
   rate comes from the backend (`implied_rate`), never recomputed client-side. */
(function () {
  'use strict';
  /* D is set once the four ledgers resolve; render fns read it. Default to empty
     arrays so any pre-boot render (or a fetch failure) degrades to empty tables. */
  let D = { transactions: [], dividends: [], fx: [], openings: [] };
  const f = window.fmt;
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  /* ===== tabs =====
     ledger.html 使用 tab-tx/div/fx/open；trades.html 自帶 glue（tab-ldiv 等）。
     僅在獨立帳本頁（無 pane-ldiv）時於此接線，避免誤抓輸入區同名的 #tab-div / #pane-div。 */
  const TABS = ['tx', 'div', 'fx', 'open'];
  const ownsTabs = !document.getElementById('pane-ldiv');
  function showTab(t) {
    TABS.forEach((x) => {
      const p = $('#pane-' + x);
      const b = $('#tab-' + x);
      if (p) p.classList.toggle('active', x === t);
      if (b) b.classList.toggle('active', x === t);
    });
  }
  if (ownsTabs) {
    TABS.forEach((t) => {
      const b = $('#tab-' + t);
      if (b) b.addEventListener('click', () => showTab(t));
    });
  }

  /* ===== filter chips (shared bar; 帳戶 + 代號搜尋 + 日期區間) =====
     `state.account` holds an account_id ('all' = no filter), NOT the display name —
     the backend rows carry both a stable `account_id` (e.g. "tw_broker") and an English
     `account` display name; filtering/chips key on the stable id and show the zh-TW label
     via ACCOUNT_ZH (the same map app.js uses). */
  const ACCOUNT_ZH = {
    tw_broker: '台灣券商',
    schwab: '嘉信 Schwab',
    moomoo_my_us: 'Moomoo 美股',
    moomoo_my_my: 'Moomoo 馬股',
  };
  const state = { account: 'all', q: '', from: '', to: '' };
  function initFilters() {
    const bar = $('#ledger-filters');
    /* Derive the chip set from the distinct account_ids present across all four fetched
       ledgers, so empty accounts are not shown. Pre-boot (D empty) this renders just the
       全部 chip; boot() re-invokes initFilters() once the ledgers resolve. */
    const ids = [];
    [D.transactions, D.dividends, D.fx, D.openings].forEach((rows) => {
      rows.forEach((r) => { if (r.account_id && !ids.includes(r.account_id)) ids.push(r.account_id); });
    });
    const mk = (val, label) => {
      const c = el('button', 'chip' + (state.account === val ? ' active' : ''), label);
      c.type = 'button';
      if (val !== 'all') c.dataset.accountId = val;
      c.addEventListener('click', () => {
        state.account = val;
        bar.querySelectorAll('.chip').forEach((x) => x.classList.remove('active'));
        c.classList.add('active');
        renderAll();
      });
      return c;
    };
    bar.replaceChildren();
    bar.appendChild(el('span', 'group-label', '帳戶'));
    bar.appendChild(mk('all', '全部'));
    ids.forEach((id) => bar.appendChild(mk(id, ACCOUNT_ZH[id] || id)));
  }
  const byAccount = (rows) => rows.filter((r) => {
    if (state.account !== 'all' && r.account_id !== state.account) return false;
    if (state.q) {
      const sym = (r.symbol || '').toLowerCase();
      if (!sym.includes(state.q)) return false;
    }
    const d = r.date || '';
    if (state.from && d && d < state.from) return false;
    if (state.to && d && d > state.to) return false;
    return true;
  });

  /* 代號搜尋與日期區間（僅 ledger.html 有這些欄位；trades.html 無則略過） */
  (function initExtraFilters() {
    const qIn = document.getElementById('ledger-sym-search');
    const fromIn = document.getElementById('ledger-date-from');
    const toIn = document.getElementById('ledger-date-to');
    if (qIn) qIn.addEventListener('input', () => { state.q = qIn.value.trim().toLowerCase(); renderAll(); });
    if (fromIn) { state.from = fromIn.value; fromIn.addEventListener('input', () => { state.from = fromIn.value; renderAll(); }); }
    if (toIn) { state.to = toIn.value; toIn.addEventListener('input', () => { state.to = toIn.value; renderAll(); }); }
  })();

  function dirChip(side) {
    return el('span', 'dir-chip ' + (side === 'buy' ? 'dir-buy' : 'dir-sell'), side === 'buy' ? '買' : '賣');
  }
  function correctBtn(prefill) {
    const b = el('a', 'btn', '以新列更正');
    b.href = 'input.html';
    b.title = '開啟輸入中心並預填此列；原紀錄永久保留';
    return b;
  }
  function symCell(symbol, name) {
    const td = el('td', 'col-text');
    const cell = el('div', 'sym-cell sym-link');
    cell.title = '點擊查看個股詳情';
    cell.addEventListener('click', (e) => {
      e.stopPropagation();
      window.pdOpenSymbol(symbol);
    });
    cell.appendChild(el('span', 'sym-code', symbol));
    if (name) cell.appendChild(el('span', 'sym-name', name));
    td.appendChild(cell);
    return td;
  }

  /* ===== 交易 (with row expander: fee-rule snapshot) ===== */
  function renderTx() {
    const tbody = $('#tx-body');
    tbody.replaceChildren();
    byAccount(D.transactions).forEach((t) => {
      const tr = el('tr', 'expandable');
      const tdCaret = el('td', 'num caret-cell', '▸');
      tr.appendChild(tdCaret);
      tr.appendChild(el('td', 'num', f.date(t.date)));
      tr.appendChild(el('td', 'col-text', t.account));
      tr.appendChild(symCell(t.symbol, t.name));
      const tdSide = el('td', 'col-text');
      tdSide.appendChild(dirChip(t.side));
      tr.appendChild(tdSide);
      tr.appendChild(el('td', 'num', f.num(t.shares)));
      tr.appendChild(el('td', 'num', f.price(t.price, t.ccy)));
      tr.appendChild(el('td', 'num', f.money(t.fee, t.ccy)));
      tr.appendChild(el('td', 'num', f.money(t.tax, t.ccy)));
      const tdTotal = el('td', 'num');
      tdTotal.textContent = f.signed(t.total, t.ccy) + ' ' + t.ccy;
      tr.appendChild(tdTotal);
      const tdAct = el('td');
      tdAct.appendChild(correctBtn(t));
      tr.appendChild(tdAct);

      const detail = el('tr', 'detail-row');
      const td = el('td');
      td.colSpan = 11;
      const box = el('div', 'snapshot-box');
      box.appendChild(el('span', 'snap-title', '費率規則快照'));
      const kv = el('div', 'snap-kv');
      const snap = t.fee_snapshot || {};
      Object.keys(snap).forEach((k) => {
        const item = el('span', 'num', k + ': ' + snap[k]);
        kv.appendChild(item);
      });
      if (!Object.keys(snap).length) kv.appendChild(el('span', 'num', f.NULL_GLYPH));
      box.appendChild(kv);
      if (t.note) box.appendChild(el('span', 'snap-note', '備註：' + t.note));
      td.appendChild(box);
      detail.appendChild(td);
      detail.hidden = true;

      tr.addEventListener('click', (e) => {
        if (e.target.closest('a, button')) return;
        detail.hidden = !detail.hidden;
        tdCaret.textContent = detail.hidden ? '▸' : '▾';
      });
      tbody.appendChild(tr);
      tbody.appendChild(detail);
    });
  }

  /* ===== 股利 ===== */
  /* Dividend type arrives as a lowercase wire value (cash/stock/drip/net); map to a
     display label + chip class. Unknown types fall back to the raw wire string. */
  const DIV_TYPE = {
    cash: { label: '現金', cls: 'chip-cash' },
    stock: { label: '配股', cls: 'chip-stock' },
    drip: { label: 'DRIP', cls: 'chip-drip' },
    net: { label: '淨額', cls: 'chip-net' },
  };
  function renderDiv() {
    const tbody = $('#div-body');
    tbody.replaceChildren();
    byAccount(D.dividends).forEach((d) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'num', f.date(d.date)));
      tr.appendChild(el('td', 'col-text', d.account));
      tr.appendChild(symCell(d.symbol));
      const meta = DIV_TYPE[d.type] || { label: d.type, cls: '' };
      const tdType = el('td', 'col-text');
      tdType.appendChild(el('span', 'type-chip ' + meta.cls, meta.label));
      tr.appendChild(tdType);
      const mkAmt = (v) => {
        const td = el('td', 'num');
        if (v === null) { td.textContent = f.NULL_GLYPH; td.classList.add('sign-nil'); }
        else td.textContent = f.money(v, d.ccy);
        return td;
      };
      tr.appendChild(mkAmt(d.gross));
      tr.appendChild(mkAmt(d.withhold));
      tr.appendChild(mkAmt(d.net));
      const tdRe = el('td', 'num');
      if (d.reinvest_shares === null) { tdRe.textContent = f.NULL_GLYPH; tdRe.classList.add('sign-nil'); }
      else tdRe.textContent = f.num(d.reinvest_shares, 4) + ' 股 @ ' + f.price(d.reinvest_price, d.ccy);
      tr.appendChild(tdRe);
      const tdAct = el('td');
      tdAct.appendChild(correctBtn(d));
      tr.appendChild(tdAct);
      tbody.appendChild(tr);
    });
  }

  /* ===== 換匯 ===== */
  function renderFx() {
    const tbody = $('#fx-body');
    tbody.replaceChildren();
    byAccount(D.fx).forEach((x) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'num', f.date(x.date)));
      tr.appendChild(el('td', 'col-text', x.account));
      tr.appendChild(el('td', 'num', f.money(x.from_amt, x.from_ccy) + ' ' + x.from_ccy));
      tr.appendChild(el('td', 'num', f.money(x.to_amt, x.to_ccy) + ' ' + x.to_ccy));
      /* Finding 9: the implied rate is computed by the backend (from_amount / to_amount,
         home units per one foreign unit) — never recomputed here. */
      tr.appendChild(el('td', 'num', '1 ' + x.to_ccy + ' = ' + f.rate(x.implied_rate) + ' ' + x.from_ccy));
      const tdAct = el('td');
      tdAct.appendChild(correctBtn(x));
      tr.appendChild(tdAct);
      tbody.appendChild(tr);
    });
  }

  /* ===== 期初 ===== */
  function renderOpen() {
    const tbody = $('#open-body');
    tbody.replaceChildren();
    byAccount(D.openings).forEach((o) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'col-text', o.account));
      tr.appendChild(symCell(o.symbol));
      tr.appendChild(el('td', 'num', f.num(o.shares)));
      tr.appendChild(el('td', 'num', f.price(o.avg, o.ccy)));
      tr.appendChild(el('td', 'num', f.money(o.total, o.ccy) + ' ' + o.ccy));
      tr.appendChild(el('td', 'num', f.date(o.date)));
      const tdAct = el('td');
      tdAct.appendChild(correctBtn(o));
      tr.appendChild(tdAct);
      tbody.appendChild(tr);
    });
  }

  function renderAll() { renderTx(); renderDiv(); renderFx(); renderOpen(); }

  /* Graceful degradation: leave the four tables empty and surface ONE toast. Never let
     a failed fetch become an unhandled rejection (the e2e smoke asserts zero console
     errors). 401 is already handled by api.js (login redirect). */
  function bootError(err) {
    renderAll();  // render the empty default D -> four blank tables, not a half-state
    if (window.toast) {
      window.toast('帳本資料載入失敗', 'fail', err && err.message ? err.message : undefined);
    }
  }

  /* Fetch the four append-only ledgers in parallel through the single pdApi layer, then
     render. A generous `limit` is passed so the whole (small) ledger shows on one page;
     the backend caps it at 500. */
  async function boot() {
    const P = { limit: 500 };
    let tx, dv, fx, op;
    try {
      [tx, dv, fx, op] = await Promise.all([
        window.pdApi.get('/api/ledgers/transactions', P),
        window.pdApi.get('/api/ledgers/dividends', P),
        window.pdApi.get('/api/ledgers/fx', P),
        window.pdApi.get('/api/ledgers/openings', P),
      ]);
    } catch (err) {
      bootError(err);
      return;
    }
    D = {
      transactions: (tx && tx.rows) || [],
      dividends: (dv && dv.rows) || [],
      fx: (fx && fx.rows) || [],
      openings: (op && op.rows) || [],
    };
    initFilters();      // rebuild account chips from the account_ids now present in D
    renderAll();
  }

  initFilters();        // account chip bar (DOM only, no data) — safe before boot
  if (ownsTabs) showTab('tx');
  boot();
})();
