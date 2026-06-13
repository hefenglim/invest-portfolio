/* portfolio-dash — 帳本檢視 (mock + rendering, read-only append-only ledgers) */
window.LEDGER_DATA = {
  "transactions": [
    { "date": "2026-06-09", "account": "台灣券商", "symbol": "0056", "name": "元大高股息", "side": "buy",
      "shares": 2000, "price": 38.60, "fee": 110, "tax": 0, "total": -77310, "ccy": "TWD",
      "fee_snapshot": { "rate": "0.1425%", "discount": "1.0", "min_fee": "NT$20", "rounding": "整數 NT$" }, "note": null },
    { "date": "2026-06-05", "account": "嘉信 Schwab", "symbol": "AAPL", "name": "Apple", "side": "sell",
      "shares": 5, "price": 200.50, "fee": 0, "tax": 0.04, "total": 1002.46, "ccy": "USD",
      "fee_snapshot": { "commission": "$0", "sec_fee": "$0.04" }, "note": "減碼" },
    { "date": "2026-05-28", "account": "Moomoo 美股", "symbol": "NVDA", "name": "NVIDIA", "side": "buy",
      "shares": 10, "price": 165.20, "fee": 0.99, "tax": 0, "total": -1652.99, "ccy": "USD",
      "fee_snapshot": { "platform_fee": "USD 0.99/筆" }, "note": null },
    { "date": "2026-05-28", "account": "Moomoo 馬股", "symbol": "1155.KL", "name": "Maybank", "side": "buy",
      "shares": 300, "price": 9.620, "fee": 3.00, "tax": 2.89, "total": -2891.89, "ccy": "MYR",
      "fee_snapshot": { "clearing": "0.03% (cap RM1,000)", "stamp_duty": "0.1%" }, "note": null },
    { "date": "2026-05-15", "account": "台灣券商", "symbol": "2330", "name": "台積電", "side": "sell",
      "shares": 200, "price": 598.00, "fee": 170, "tax": 359, "total": 119071, "ccy": "TWD",
      "fee_snapshot": { "rate": "0.1425%", "discount": "1.0", "tax": "證交稅 0.3%" }, "note": "AI 輸入" },
    { "date": "2026-05-02", "account": "台灣券商", "symbol": "00919", "name": "群益台灣精選高息", "side": "buy",
      "shares": 5000, "price": 23.50, "fee": 167, "tax": 0, "total": -117667, "ccy": "TWD",
      "fee_snapshot": { "rate": "0.1425%", "discount": "1.0", "min_fee": "NT$20" }, "note": null }
  ],
  "dividends": [
    { "date": "2026-06-03", "account": "台灣券商", "symbol": "2330", "type": "現金",
      "gross": 5000, "withhold": 0, "net": 5000, "reinvest_shares": null, "reinvest_price": null, "ccy": "TWD" },
    { "date": "2026-05-20", "account": "嘉信 Schwab", "symbol": "AAPL", "type": "DRIP",
      "gross": 7.50, "withhold": 2.25, "net": 5.25, "reinvest_shares": 0.0248, "reinvest_price": 211.40, "ccy": "USD" },
    { "date": "2026-04-28", "account": "Moomoo 馬股", "symbol": "1155.KL", "type": "淨額",
      "gross": null, "withhold": null, "net": 170.00, "reinvest_shares": null, "reinvest_price": null, "ccy": "MYR" },
    { "date": "2026-04-15", "account": "台灣券商", "symbol": "0056", "type": "現金",
      "gross": 8500, "withhold": 0, "net": 8500, "reinvest_shares": null, "reinvest_price": null, "ccy": "TWD" }
  ],
  "fx": [
    { "date": "2026-05-26", "account": "嘉信 Schwab", "from_ccy": "TWD", "from_amt": 32000, "to_ccy": "USD", "to_amt": 1000.00 },
    { "date": "2026-04-08", "account": "Moomoo 美股", "from_ccy": "MYR", "from_amt": 4450, "to_ccy": "USD", "to_amt": 1000.00 }
  ],
  "openings": [
    { "account": "台灣券商", "symbol": "2330", "shares": 500, "avg": 480.00, "total": 240000, "ccy": "TWD", "date": "2026-01-02" },
    { "account": "嘉信 Schwab", "symbol": "MSFT", "shares": 12, "avg": 405.00, "total": 4860.00, "ccy": "USD", "date": "2026-01-02" }
  ]
};

(function () {
  'use strict';
  const D = window.LEDGER_DATA;
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

  /* ===== filter chips (shared bar; 帳戶 + 代號搜尋 + 日期區間) ===== */
  const state = { account: 'all', q: '', from: '', to: '' };
  function initFilters() {
    const bar = $('#ledger-filters');
    const accounts = ['台灣券商', '嘉信 Schwab', 'Moomoo 美股', 'Moomoo 馬股'];
    const mk = (val, label) => {
      const c = el('button', 'chip' + (state.account === val ? ' active' : ''), label);
      c.type = 'button';
      c.addEventListener('click', () => {
        state.account = val;
        bar.querySelectorAll('.chip').forEach((x) => x.classList.remove('active'));
        c.classList.add('active');
        renderAll();
      });
      return c;
    };
    bar.appendChild(el('span', 'group-label', '帳戶'));
    bar.appendChild(mk('all', '全部'));
    accounts.forEach((a) => bar.appendChild(mk(a, a)));
  }
  const byAccount = (rows) => rows.filter((r) => {
    if (state.account !== 'all' && r.account !== state.account) return false;
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
      tr.appendChild(el('td', 'num', t.date));
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
      Object.keys(t.fee_snapshot).forEach((k) => {
        const item = el('span', 'num', k + ': ' + t.fee_snapshot[k]);
        kv.appendChild(item);
      });
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
  function renderDiv() {
    const tbody = $('#div-body');
    tbody.replaceChildren();
    const TYPE_CLS = { '現金': 'chip-cash', '配股': 'chip-stock', 'DRIP': 'chip-drip', '淨額': 'chip-net' };
    byAccount(D.dividends).forEach((d) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'num', d.date));
      tr.appendChild(el('td', 'col-text', d.account));
      tr.appendChild(symCell(d.symbol));
      const tdType = el('td', 'col-text');
      tdType.appendChild(el('span', 'type-chip ' + (TYPE_CLS[d.type] || ''), d.type));
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
      else tdRe.textContent = d.reinvest_shares + ' 股 @ ' + f.price(d.reinvest_price, d.ccy);
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
      tr.appendChild(el('td', 'num', x.date));
      tr.appendChild(el('td', 'col-text', x.account));
      tr.appendChild(el('td', 'num', f.money(x.from_amt, x.from_ccy) + ' ' + x.from_ccy));
      tr.appendChild(el('td', 'num', f.money(x.to_amt, x.to_ccy) + ' ' + x.to_ccy));
      tr.appendChild(el('td', 'num', '1 ' + x.to_ccy + ' = ' + (x.from_amt / x.to_amt).toFixed(4) + ' ' + x.from_ccy));
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
      tr.appendChild(el('td', 'num', o.date));
      const tdAct = el('td');
      tdAct.appendChild(correctBtn(o));
      tr.appendChild(tdAct);
      tbody.appendChild(tr);
    });
  }

  function renderAll() { renderTx(); renderDiv(); renderFx(); renderOpen(); }
  initFilters();
  renderAll();
  if (ownsTabs) showTab('tx');
})();
