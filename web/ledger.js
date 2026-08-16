/* portfolio-dash — 帳本檢視 (read-only append-only ledgers, wired to /api/ledgers/*).

   The four ledgers are fetched in parallel through the single pdApi fetch layer
   (spec 19/11). All money / price / rate values arrive as Decimal STRINGS and are
   formatted ONLY via window.fmt — this module never computes money. The implied FX
   rate comes from the backend (`implied_rate`), never recomputed client-side. */
(function () {
  'use strict';
  /* D is set once the four ledgers resolve; render fns read it. Default to empty
     arrays so any pre-boot render (or a fetch failure) degrades to empty tables. */
  let D = { transactions: [], dividends: [], fx: [], openings: [], actions: [], cash: [] };
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
  const TABS = ['tx', 'div', 'fx', 'open', 'action', 'cash'];
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
     via the shared resolver window.pdNames (web/names.js, FU-D37 — the single frontend
     account-naming authority that superseded the per-file ACCOUNT_ZH maps).
     WPE (2026-07-07): account + date range moved SERVER-side (the /api/ledgers/*
     endpoints take account_id + from/to) so the pagers stay honest; only the keyword
     search remains a client filter over the CURRENT page (labelled 篩選本頁). */
  /* Account zh-TW display name — single source of truth is web/names.js (FU-D37,
     window.pdNames). Local delegator with a graceful no-op (id fallback) when names.js
     is absent. trades.html loads names.js before this file (ledger.html is a redirect
     stub -> trades.html). Server-side account.display_name is the planned successor. */
  const acctZh = (id) => (window.pdNames ? window.pdNames.account(id) : id);
  const state = { account: 'all', q: '', from: '', to: '' };
  const PAGE = Math.min((window.pdPrefs && window.pdPrefs.page_size) || 50, 500);
  const pageState = {
    tx: { offset: 0, total: 0 },
    div: { offset: 0, total: 0 },
    fx: { offset: 0, total: 0 },
    open: { offset: 0, total: 0 },
    action: { offset: 0, total: 0 },
    cash: { offset: 0, total: 0 },
  };
  const pagers = {};
  let accountList = []; /* [{id, name}] from GET /api/accounts (chip registry) */

  function initFilters() {
    const bar = $('#ledger-filters');
    const mk = (val, label) => {
      const c = el('button', 'chip' + (state.account === val ? ' active' : ''), label);
      c.type = 'button';
      if (val !== 'all') c.dataset.accountId = val;
      c.addEventListener('click', () => {
        state.account = val;
        bar.querySelectorAll('.chip').forEach((x) => x.classList.remove('active'));
        c.classList.add('active');
        resetOffsets();
        loadAll(); /* server-side account filter (WPE) */
      });
      return c;
    };
    bar.replaceChildren();
    bar.appendChild(el('span', 'group-label', '帳戶'));
    bar.appendChild(mk('all', '全部'));
    accountList.forEach((a) => bar.appendChild(mk(a.id, acctZh(a.id))));
  }

  /* keyword narrows the CURRENT page only (代號優先、名稱其次 — 2026-07-03 decision) */
  const byKeyword = (rows) => rows.filter((r) => {
    if (!state.q) return true;
    const sym = (r.symbol || '').toLowerCase();
    const name = (r.name || '').toLowerCase();
    return sym.includes(state.q) || name.includes(state.q);
  });

  function resetOffsets() {
    /* Derived from pageState rather than listed one ledger per line. The listed form was
       five assignments and adding a sixth ledger meant remembering to add a sixth — and a
       forgotten one has no symptom until you filter while on page 3 of that tab, then get
       an empty table. Same class as the ledger enumerations the registry removed on the
       server side (2026-08-16). */
    Object.keys(pageState).forEach((k) => { pageState[k].offset = 0; });
  }

  /* 代號搜尋與日期區間 — keyword filters the page client-side; dates hit the server */
  (function initExtraFilters() {
    const qIn = document.getElementById('ledger-sym-search');
    const fromIn = document.getElementById('ledger-date-from');
    const toIn = document.getElementById('ledger-date-to');
    if (qIn) qIn.addEventListener('input', () => { state.q = qIn.value.trim().toLowerCase(); renderAll(); });
    let dateTimer = null;
    const onDate = () => {
      if (dateTimer) clearTimeout(dateTimer);
      dateTimer = setTimeout(() => {
        state.from = fromIn ? fromIn.value : '';
        state.to = toIn ? toIn.value : '';
        resetOffsets();
        loadAll();
      }, 250);
    };
    if (fromIn) { state.from = fromIn.value; fromIn.addEventListener('input', onDate); }
    if (toIn) { state.to = toIn.value; toIn.addEventListener('input', onDate); }
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
    const ids = accountList.map((a) => a.id);
    if (current && !ids.includes(current)) ids.push(current);
    return sel(ids.map((id) => [id, acctZh(id)]), current);
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
    /* audit M6: track whether the user explicitly edited fee/tax. When a core field
       (帳戶/代號/方向/股數/價格/日期) changes and fee/tax are NOT dirty, the modal
       re-fetches the computed fee/tax from the entry preview seam and the backend
       recomputes them from the new account's rule set + regenerates the snapshot. An
       explicit fee/tax edit is honored as an override (snapshot tagged override:true). */
    let feeDirty = false;
    let taxDirty = false;
    fFee.addEventListener('input', () => { feeDirty = true; });
    fTax.addEventListener('input', () => { taxDirty = true; });
    async function recompute() {
      if (!window.pdApi) return;
      try {
        const resp = await window.pdApi.post('/api/input/manual/preview', {
          account_id: fAcc.value, symbol: fSym.value.trim(), side: fSide.value,
          date: fDate.value, shares: fShares.value || '0', price: fPrice.value || '0',
        });
        if (resp && !feeDirty && resp.fee !== undefined) fFee.value = resp.fee;
        if (resp && !taxDirty && resp.tax !== undefined) fTax.value = resp.tax;
      } catch (e) { /* best-effort; the save-time recompute is the source of truth */ }
    }
    [fShares, fPrice].forEach((n) => n.addEventListener('input', recompute));
    [fAcc, fSym, fSide, fDate].forEach((n) => n.addEventListener('change', recompute));
    /* FU-D7: a per-field 還原自動 (↺) affordance beside fee/tax. Once you type in the
       dialog the field is dirty and there is otherwise no way back within it; this clears
       the dirty flag and re-runs recompute() so the account's computed value returns and
       fee_overridden/tax_overridden save as false. */
    const revertCell = (field, clearDirty) => {
      const wrap = el('div', 'edit-revert-line');
      wrap.appendChild(field);
      const btn = el('button', 'btn btn-sm edit-revert', '↺ 還原自動');
      btn.type = 'button';
      btn.title = '清除手動費用／稅，改回依帳戶規則自動計算';
      btn.addEventListener('click', () => { clearDirty(); recompute(); });
      wrap.appendChild(btn);
      return wrap;
    };
    const feeCell = revertCell(fFee, () => { feeDirty = false; });
    const taxCell = revertCell(fTax, () => { taxDirty = false; });
    /* 改「代號 / 帳戶」= 把這筆帳移到另一個持倉：兩邊的成本與損益都會由帳本重建。
       合法（改正輸錯的代號），但要讓使用者知道影響範圍（2026-07-03, item 12）。 */
    const warn = el('div', 'hint',
      '⚠ 更改「代號」或「帳戶」會把這筆交易移到另一個持倉，兩邊的成本、損益與報酬將自動重建；' +
      '新代號必須與帳戶市場相符且已註冊，會先做賣超與孤兒紀錄檢核；未手動改費用／稅時會依新帳戶規則重算。');
    editModal('編輯交易 #' + t.id + ' — ' + t.symbol, [
      ['日期', fDate], ['帳戶', fAcc], ['代號', fSym], ['方向', fSide],
      ['股數', fShares], ['價格', fPrice], ['手續費', feeCell], ['交易稅', taxCell], ['備註', fNote],
      ['', warn],
    ], async (dismiss) => {
      dismiss();
      /* values ride through as the user's raw STRINGS; the backend parses Decimal */
      await putWithOversellGuard('/api/ledgers/transactions/' + t.id, (ack) => ({
        account_id: fAcc.value, symbol: fSym.value.trim(), side: fSide.value,
        date: fDate.value, shares: fShares.value, price: fPrice.value,
        fee: fFee.value, tax: fTax.value, note: fNote.value.trim() || null,
        fee_overridden: feeDirty, tax_overridden: taxDirty,
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
    editModal('編輯期初 — ' + o.symbol + '（' + acctZh(o.account_id) + '）', [
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
    byKeyword(D.transactions).forEach((t) => {
      const tr = el('tr', 'expandable');
      const tdCaret = el('td', 'num caret-cell', '▸');
      tr.appendChild(tdCaret);
      tr.appendChild(el('td', 'num', f.date(t.date)));
      tr.appendChild(el('td', 'col-text', t.account));
      tr.appendChild(symCell(t.symbol, t.name));
      const tdSide = el('td', 'col-text');
      tdSide.appendChild(dirChip(t.side));
      tr.appendChild(tdSide);
      tr.appendChild(el('td', 'num', f.shares(t.shares)));
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
    byKeyword(D.dividends).forEach((d) => {
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
      else tdRe.textContent = f.shares(d.reinvest_shares) + ' 股 @ ' + f.price(d.reinvest_price, d.ccy);
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
    byKeyword(D.fx).forEach((x) => {
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
    byKeyword(D.openings).forEach((o) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'col-text', o.account));
      tr.appendChild(symCell(o.symbol));
      tr.appendChild(el('td', 'num', f.shares(o.shares)));
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

  /* ===== 公司行動 (the 5th ledger — spec 2026-08-06 §6.7 door 3) =====
     Deliberately HERE and not as a 6th tab on the input pane above: the input pane is
     high-frequency capture (trades, dividends), this section is low-frequency corrective
     record-keeping, and putting a rare control behind five common ones buries it.

     Every displayed value (股數 / 比例 / 成本分攤) is a SERVER Decimal string rendered via
     window.fmt or shown verbatim — the two ratio terms are NEVER divided here, which is
     the whole point of storing them as two integers (§3.1(ii)). `ratio_label` is composed
     server-side for the same reason. */
  function editAction(a) {
    const fDate = inp(a.date, 'date');
    const fAcc = accountSel(a.account_id);
    const fFrom = inp(a.symbol);
    const fTo = inp(a.to_symbol);
    const fKind = sel([['SPLIT', '分割'], ['EXCHANGE', '換股'], ['SPINOFF', '分拆']], a.kind);
    const fRatioFrom = inp(a.ratio_from, 'number', '1');
    const fRatioTo = inp(a.ratio_to, 'number', '1');
    const fCarry = inp(a.cost_carry === null ? '' : a.cost_carry, 'number', 'any');
    const fNote = inp(a.note || '');
    const warn = el('div', 'hint',
      '⚠ 修改公司行動會重算歷史：股數、均價、報酬率與價格基準都會依新內容重建（原值已存入稽核軌跡）。'
      + '若這筆行動同時登錄在多個帳戶，代號／日期／類型／比例必須整組一致，否則會被擋下。');
    editModal('編輯公司行動 #' + a.id + ' — ' + a.symbol, [
      ['日期', fDate], ['帳戶', fAcc], ['類型', fKind],
      ['來源代號', fFrom], ['目的代號', fTo],
      ['每持有（股）', fRatioFrom], ['變成／換得（股）', fRatioTo],
      ['成本分攤比例（分拆用）', fCarry], ['備註', fNote],
      ['', warn],
    ], async (dismiss) => {
      dismiss();
      const body = {
        account_id: fAcc.value, date: fDate.value, kind: fKind.value,
        from_symbol: fFrom.value.trim(), to_symbol: fTo.value.trim(),
        ratio_to: fRatioTo.value, ratio_from: fRatioFrom.value,
        cost_carry: fCarry.value.trim() || null,
        note: fNote.value.trim() || null, ack_warnings: false,
      };
      try {
        await window.pdApi.put('/api/ledgers/corporate-actions/' + a.id, body);
        mutationOk('編輯');
      } catch (err) {
        if (err && err.status === 422 && err.code === 'warnings_unacknowledged') {
          window.confirmDialog({
            title: '公司行動警告確認', body: err.message,
            confirmLabel: '我了解，仍要儲存', danger: true,
            onConfirm: async () => {
              body.ack_warnings = true;
              try {
                await window.pdApi.put('/api/ledgers/corporate-actions/' + a.id, body);
                mutationOk('編輯');
              } catch (e2) { mutationFail(e2, '編輯'); }
            }
          });
          return;
        }
        mutationFail(err, '編輯');
      }
    });
  }

  /* F-32: deleting ONE row of a multi-account set is refused server-side, because the
     price correction is global (split_factor's dedup key carries no account) while the
     share correction is per account — and the drawer would print ✓ 對帳一致 over the
     mismatch. The refusal is turned into a guided action here rather than a dead end:
     the owner who really does want the action gone is offered the WHOLE SET. */
  function delAction(a) {
    window.confirmDialog({
      title: '刪除公司行動',
      body: '確定刪除 ' + a.symbol + ' 在 ' + a.date + ' 的' + a.kind_label
        + '？股數、成本與價格基準都會由帳本重建。',
      confirmLabel: '刪除', danger: true,
      onConfirm: async () => {
        try {
          await window.pdApi.del('/api/ledgers/corporate-actions/' + a.id);
          mutationOk('刪除');
        } catch (err) {
          if (err && err.status === 422 && err.code === 'partial_action_set_change') {
            window.confirmDialog({
              title: '這筆行動屬於多帳戶整組紀錄',
              body: err.message + '　要整組一起刪除嗎？',
              confirmLabel: '整組刪除', danger: true,
              onConfirm: async () => {
                try {
                  await window.pdApi.del('/api/ledgers/corporate-actions/set'
                    + '?from_symbol=' + encodeURIComponent(a.symbol)
                    + '&date=' + encodeURIComponent(a.date)
                    + '&kind=' + encodeURIComponent(a.kind));
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

  function renderActions() {
    const tbody = $('#action-body');
    if (!tbody) return;
    tbody.replaceChildren();
    byKeyword(D.actions).forEach((a) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'num', f.date(a.date)));
      tr.appendChild(el('td', 'col-text', a.account));
      tr.appendChild(el('td', 'col-text', a.kind_label));
      tr.appendChild(symCell(a.symbol, a.name));
      const tdTo = el('td', 'col-text');
      if (a.to_symbol && a.to_symbol !== a.symbol) {
        tdTo.appendChild(symCell(a.to_symbol, a.to_name).firstChild);
      } else {
        tdTo.textContent = f.NULL_GLYPH;
        tdTo.classList.add('sign-nil');
      }
      tr.appendChild(tdTo);
      tr.appendChild(el('td', 'col-text num', a.ratio_label));
      const tdCarry = el('td', 'num');
      if (a.cost_carry === null || a.cost_carry === undefined) {
        tdCarry.textContent = f.NULL_GLYPH;
        tdCarry.classList.add('sign-nil');
      } else {
        tdCarry.textContent = a.cost_carry;   /* a server string; never re-scaled here */
      }
      tr.appendChild(tdCarry);
      tr.appendChild(el('td', 'col-text', a.note || ''));
      tr.appendChild(actionsCell(() => editAction(a), () => delAction(a)));
      tbody.appendChild(tr);
    });
  }

  function renderCash() {
    const tbody = $('#cash-body');
    if (!tbody) return;
    tbody.replaceChildren();
    byKeyword(D.cash).forEach((m) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'num', f.date(m.date)));
      tr.appendChild(el('td', 'col-text', m.account));
      tr.appendChild(el('td', 'col-text', m.kind_label));
      tr.appendChild(el('td', 'col-text', m.ccy));
      /* signed_amount is the SERVER's figure. The sign lives in the kind
         (shared/cash_kinds.py) and this layer never computes money — printing `amount`
         here would show a broker fee as an inflow. */
      tr.appendChild(el('td', 'num', f.money(m.signed_amount, m.ccy)));
      const tdAcq = el('td', 'num');
      if (m.acq_home_amount === null || m.acq_home_amount === undefined) {
        tdAcq.textContent = f.NULL_GLYPH;
        tdAcq.classList.add('sign-nil');
      } else {
        tdAcq.textContent = m.acq_home_amount;   /* a server string; never re-scaled here */
      }
      tr.appendChild(tdAcq);
      tr.appendChild(el('td', 'col-text', m.note || ''));
      tbody.appendChild(tr);
    });
  }

  /* Door 3's entry point — the ONLY control on this page that CREATES an action. It opens
     the shared §6.7 form (web/corp-action-form.js); doors 1 and 2 open the same one with a
     different prefill. */
  (function initActionAdd() {
    const btn = document.getElementById('action-add');
    if (!btn) return;
    btn.addEventListener('click', () => {
      if (!window.pdCorpActionForm) return;
      window.pdCorpActionForm.open({ onSaved: () => boot() });
    });
  })();

  function renderAll() {
    renderTx(); renderDiv(); renderFx(); renderOpen(); renderActions(); renderCash();
  }

  /* ===== WPE (2026-07-07): per-ledger server pagination =====
     Each of the four tables fetches its OWN page (limit/offset + total_count from
     the endpoint) with the account/date filters passed server-side; each pane gets
     a shared pdPager. One failure toast per load pass (not four). */
  let loadFailToasted = false;
  function loadFail(err) {
    renderAll();
    if (!loadFailToasted && window.toast) {
      loadFailToasted = true;
      window.toast('帳本資料載入失敗', 'fail', err && err.message ? err.message : undefined);
    }
  }

  function ledgerParams(kind) {
    const p = { limit: PAGE, offset: pageState[kind].offset };
    if (state.account !== 'all') p.account_id = state.account;
    if (kind !== 'open') { /* openings has no date filter server-side */
      if (state.from) p.from = state.from;
      if (state.to) p.to = state.to;
    }
    return p;
  }

  function updatePager(kind) {
    if (pagers[kind]) {
      pagers[kind].update({
        offset: pageState[kind].offset,
        totalCount: pageState[kind].total,
      });
    }
  }

  async function loadOne(kind, path, assign, render) {
    try {
      const resp = await window.pdApi.get(path, ledgerParams(kind));
      assign((resp && resp.rows) || []);
      pageState[kind].total = (resp && resp.total_count) || 0;
    } catch (err) {
      assign([]);
      pageState[kind].total = 0;
      loadFail(err);
      updatePager(kind);
      return;
    }
    render();
    updatePager(kind);
  }

  const loadTx = () => loadOne('tx', '/api/ledgers/transactions',
    (rows) => { D.transactions = rows; }, renderTx);
  const loadDiv = () => loadOne('div', '/api/ledgers/dividends',
    (rows) => { D.dividends = rows; }, renderDiv);
  const loadFx = () => loadOne('fx', '/api/ledgers/fx',
    (rows) => { D.fx = rows; }, renderFx);
  const loadOpen = () => loadOne('open', '/api/ledgers/openings',
    (rows) => { D.openings = rows; }, renderOpen);
  const loadAction = () => loadOne('action', '/api/ledgers/corporate-actions',
    (rows) => { D.actions = rows; }, renderActions);
  const loadCash = () => loadOne('cash', '/api/ledgers/cash',
    (rows) => { D.cash = rows; }, renderCash);

  async function loadAll() {
    loadFailToasted = false;
    await Promise.all([
      loadTx(), loadDiv(), loadFx(), loadOpen(), loadAction(), loadCash(),
    ]);
  }

  /* FU-D45: live-refresh seam for the input panes (trades.html). After ANY successful
     input commit, input.js calls this to re-fetch the ledger tables IN PLACE (current
     account/date filters + page offsets preserved; NO page reload). All four panes
     refresh — they share one filter state and a hidden pane must not go stale for the
     next tab switch — so the ACTIVE tab always re-renders. A plain function assignment
     (not addEventListener), so repeated calls/loads can never double-bind. */
  window.pdLedgerRefresh = loadAll;

  /* pagers: pane hosts exist on trades.html only — guarded per the 略過 convention */
  if (window.pdPager) {
    const HOSTS = [
      ['tx', 'tx-pager', loadTx],
      ['div', 'ldiv-pager', loadDiv],
      ['fx', 'lfx-pager', loadFx],
      ['open', 'lopen-pager', loadOpen],
      ['action', 'laction-pager', loadAction],
      ['cash', 'lcash-pager', loadCash],
    ];
    HOSTS.forEach(([kind, hostId, loader]) => {
      const host = document.getElementById(hostId);
      if (!host) return;
      pagers[kind] = window.pdPager.create({
        host: host,
        limit: PAGE, offset: 0, totalCount: 0,
        onPage: (offset) => { pageState[kind].offset = offset; loader(); },
      });
    });
  }

  /* account chips come from the accounts registry (server-filterable even when a
     page shows no rows for that account); degrades to the 全部-only bar. */
  async function loadAccounts() {
    try {
      const resp = await window.pdApi.get('/api/accounts');
      accountList = ((resp && resp.accounts) || []).map((a) => ({
        id: a.account_id, name: a.name,
      }));
    } catch (err) {
      accountList = [];
    }
  }

  async function boot() {
    await loadAccounts();
    initFilters();
    await loadAll();
  }

  initFilters();        // account chip bar (DOM only, no data) — safe before boot
  if (ownsTabs) showTab('tx');
  boot();
})();
