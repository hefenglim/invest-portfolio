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

  /* ===== row corrections: edit / delete (2026-07-02) =====
     Explicit corrections through PUT/DELETE /api/ledgers/*. The backend replays the
     would-be ledger first: a correction that would strand a later sell answers 422
     "oversell" — surfaced here as a second, danger-styled confirm before re-sending
     with ack_oversell (the dashboard then shows the flagged 賣超 state). */

  function actionsCell(onEdit, onDel) {
    const td = el('td');
    const wrap = el('div', 'wl-actions');
    const e = el('button', 'btn', '編輯'); e.type = 'button';
    e.addEventListener('click', (ev) => { ev.stopPropagation(); onEdit(); });
    const d = el('button', 'btn btn-row-del', '刪除'); d.type = 'button';
    d.addEventListener('click', (ev) => { ev.stopPropagation(); onDel(); });
    wrap.appendChild(e); wrap.appendChild(d);
    td.appendChild(wrap);
    return td;
  }

  function mutationOk(kind) {
    if (window.toast) window.toast(kind + '完成', 'ok', '帳本已更新，統計將由帳本重建');
    boot();
  }
  function mutationFail(err, kind) {
    if (window.toast) window.toast(kind + '失敗', 'fail', (err && err.message) || undefined);
  }

  /* PUT with the oversell-ack retry loop. bodyFn(ack) builds the payload. */
  async function putWithOversellGuard(path, bodyFn, kind) {
    try {
      await window.pdApi.put(path, bodyFn(false));
      mutationOk(kind);
    } catch (err) {
      if (err && err.status === 422 && err.code === 'oversell') {
        window.confirmDialog({
          title: '賣超確認', body: err.message, confirmLabel: '我了解，仍要寫入', danger: true,
          onConfirm: async () => {
            try { await window.pdApi.put(path, bodyFn(true)); mutationOk(kind); }
            catch (e2) { mutationFail(e2, kind); }
          }
        });
        return;
      }
      mutationFail(err, kind);
    }
  }

  /* DELETE with confirm + the oversell-ack retry loop (ack rides as a query param). */
  function delWithConfirm(path, label) {
    window.confirmDialog({
      title: '刪除' + label, body: '確定刪除這筆' + label + '？統計將由其餘帳本紀錄重建。',
      confirmLabel: '刪除', danger: true,
      onConfirm: async () => {
        try {
          await window.pdApi.del(path);
          mutationOk('刪除');
        } catch (err) {
          if (err && err.status === 422 && err.code === 'oversell') {
            window.confirmDialog({
              title: '賣超確認', body: err.message, confirmLabel: '我了解，仍要刪除', danger: true,
              onConfirm: async () => {
                try {
                  await window.pdApi.del(path + (path.indexOf('?') === -1 ? '?' : '&') + 'ack_oversell=true');
                  mutationOk('刪除');
                } catch (e2) { mutationFail(e2, '刪除'); }
              }
            });
            return;
          }
          mutationFail(err, '刪除');
        }
      }
    });
  }

  /* generic edit modal: rows = [[label, inputNode]], onSave(dismiss) async */
  function editModal(title, rows, onSave) {
    const backdrop = el('div', 'modal-backdrop');
    const modal = el('div', 'modal');
    const head = el('div', 'modal-head');
    head.appendChild(el('h3', 'modal-title', title));
    const close = el('button', 'modal-close', '✕'); close.type = 'button';
    head.appendChild(close);
    modal.appendChild(head);
    const body = el('div', 'modal-body');
    rows.forEach(([label, node]) => {
      const w = el('div', 'field');
      w.appendChild(el('label', null, label));
      w.appendChild(node);
      body.appendChild(w);
    });
    modal.appendChild(body);
    const foot = el('div', 'modal-foot');
    const cancel = el('button', 'btn', '取消'); cancel.type = 'button';
    const ok = el('button', 'btn btn-primary', '儲存'); ok.type = 'button';
    foot.appendChild(cancel); foot.appendChild(ok);
    modal.appendChild(foot);
    backdrop.appendChild(modal);
    const dismiss = () => backdrop.remove();
    close.addEventListener('click', dismiss);
    cancel.addEventListener('click', dismiss);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) dismiss(); });
    ok.addEventListener('click', () => onSave(dismiss));
    document.body.appendChild(backdrop);
  }
  const inp = (value, type, step) => {
    const n = el('input', 'input');
    n.type = type || 'text';
    if (step) n.step = step;
    if (value !== null && value !== undefined) n.value = value;
    return n;
  };
  const sel = (options, current) => {
    const s = el('select', 'select');
    options.forEach(([v, label]) => {
      const o = el('option', null, label); o.value = v;
      if (v === current) o.selected = true;
      s.appendChild(o);
    });
    return s;
  };
  const accountSel = (current) => {
    const ids = [];
    [D.transactions, D.dividends, D.fx, D.openings].forEach((rows) => {
      rows.forEach((r) => { if (r.account_id && !ids.includes(r.account_id)) ids.push(r.account_id); });
    });
    if (current && !ids.includes(current)) ids.push(current);
    return sel(ids.map((id) => [id, ACCOUNT_ZH[id] || id]), current);
  };

  function editTx(t) {
    const fDate = inp(t.date, 'date');
    const fAcc = accountSel(t.account_id);
    const fSym = inp(t.symbol);
    const fSide = sel([['buy', '買入'], ['sell', '賣出']], t.side);
    const fShares = inp(t.shares, 'number', 'any');
    const fPrice = inp(t.price, 'number', 'any');
    const fFee = inp(t.fee, 'number', 'any');
    const fTax = inp(t.tax, 'number', 'any');
    const fNote = inp(t.note || '');
    editModal('編輯交易 #' + t.id + ' — ' + t.symbol, [
      ['日期', fDate], ['帳戶', fAcc], ['代號', fSym], ['方向', fSide],
      ['股數', fShares], ['價格', fPrice], ['手續費', fFee], ['交易稅', fTax], ['備註', fNote],
    ], async (dismiss) => {
      dismiss();
      /* values ride through as the user's raw STRINGS; the backend parses Decimal */
      await putWithOversellGuard('/api/ledgers/transactions/' + t.id, (ack) => ({
        account_id: fAcc.value, symbol: fSym.value.trim(), side: fSide.value,
        date: fDate.value, shares: fShares.value, price: fPrice.value,
        fee: fFee.value, tax: fTax.value, note: fNote.value.trim() || null,
        ack_oversell: ack,
      }), '編輯');
    });
  }

  const DIV_TYPE_OPTS = [['cash', '現金'], ['stock', '配股'], ['drip', 'DRIP'], ['net', '淨額']];
  function editDiv(d) {
    const fDate = inp(d.date, 'date');
    const fAcc = accountSel(d.account_id);
    const fSym = inp(d.symbol);
    const fType = sel(DIV_TYPE_OPTS, d.type);
    const fGross = inp(d.gross, 'number', 'any');
    const fWh = inp(d.withhold, 'number', 'any');
    const fNet = inp(d.net, 'number', 'any');
    const fReSh = inp(d.reinvest_shares, 'number', 'any');
    const fRePx = inp(d.reinvest_price, 'number', 'any');
    editModal('編輯股利 #' + d.id + ' — ' + d.symbol, [
      ['日期', fDate], ['帳戶', fAcc], ['代號', fSym], ['類型', fType],
      ['總額', fGross], ['預扣', fWh], ['淨額', fNet],
      ['再投資股數（DRIP）', fReSh], ['再投資價格（DRIP）', fRePx],
    ], async (dismiss) => {
      dismiss();
      await putWithOversellGuard('/api/ledgers/dividends/' + d.id, (ack) => ({
        account_id: fAcc.value, symbol: fSym.value.trim(), date: fDate.value,
        type: fType.value, gross: fGross.value || '0', withhold: fWh.value || '0',
        net: fNet.value || '0',
        reinvest_shares: fReSh.value === '' ? null : fReSh.value,
        reinvest_price: fRePx.value === '' ? null : fRePx.value,
        ack_oversell: ack,
      }), '編輯');
    });
  }

  const CCY_OPTS = [['TWD', 'TWD'], ['USD', 'USD'], ['MYR', 'MYR']];
  function editFx(x) {
    const fDate = inp(x.date, 'date');
    const fAcc = accountSel(x.account_id);
    const fFromC = sel(CCY_OPTS, x.from_ccy);
    const fFromA = inp(x.from_amt, 'number', 'any');
    const fToC = sel(CCY_OPTS, x.to_ccy);
    const fToA = inp(x.to_amt, 'number', 'any');
    editModal('編輯換匯 #' + x.id, [
      ['日期', fDate], ['帳戶', fAcc],
      ['換出幣別', fFromC], ['換出金額', fFromA],
      ['換入幣別', fToC], ['換入金額', fToA],
    ], async (dismiss) => {
      dismiss();
      await putWithOversellGuard('/api/ledgers/fx/' + x.id, () => ({
        account_id: fAcc.value, date: fDate.value,
        from_ccy: fFromC.value, from_amt: fFromA.value,
        to_ccy: fToC.value, to_amt: fToA.value,
      }), '編輯');
    });
  }

  function editOpen(o) {
    const fShares = inp(o.shares, 'number', 'any');
    const fAvg = inp(o.avg, 'number', 'any');
    const fDate = inp(o.date, 'date');
    editModal('編輯期初 — ' + o.symbol + '（' + (ACCOUNT_ZH[o.account_id] || o.account_id) + '）', [
      ['股數', fShares], ['原始均價', fAvg], ['建檔日', fDate],
    ], async (dismiss) => {
      dismiss();
      await putWithOversellGuard(
        '/api/ledgers/openings/' + encodeURIComponent(o.account_id) + '/' + encodeURIComponent(o.symbol),
        (ack) => ({ shares: fShares.value, avg: fAvg.value, date: fDate.value, ack_oversell: ack }),
        '編輯');
    });
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
      tr.appendChild(actionsCell(
        () => editTx(t),
        () => delWithConfirm('/api/ledgers/transactions/' + t.id, '交易')));

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
      tr.appendChild(actionsCell(
        () => editDiv(d),
        () => delWithConfirm('/api/ledgers/dividends/' + d.id, '股利')));
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
      tr.appendChild(actionsCell(
        () => editFx(x),
        () => delWithConfirm('/api/ledgers/fx/' + x.id, '換匯')));
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
      const openPath = '/api/ledgers/openings/' + encodeURIComponent(o.account_id) +
        '/' + encodeURIComponent(o.symbol);
      tr.appendChild(actionsCell(
        () => editOpen(o),
        () => delWithConfirm(openPath, '期初')));
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
