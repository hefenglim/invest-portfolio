/* portfolio-dash — 資金管理 (wired to /api/cash, 2026-07-03 R6 item 7).

   One page manages the accounts' cash pools: balances per (account, ccy) with a
   clickable收支明細 (statement) surface, deposit/withdraw/opening entry, FX conversion
   entry, and the movements ledger with edit/delete. All amounts are Decimal STRINGS via
   window.fmt — the frontend never computes money; the running balance in the statement
   comes from the server. Wave 2B additions: currency dropdowns constrained to the
   account's {交割幣, 資金幣} (audit C2), an 期初資金 movement kind (C4), a transfer/pool
   statement (C5), a negative-pool banner (C1a), and a missing-rate annotation (C6).
   For deposit/opening-side CORRECTIONS (edit/delete), the negative-pool 422 (audit C3)
   surfaces as a danger confirm before re-sending with ack_negative. FX conversions
   (FU-D34, 需求五) and WITHDRAWALS (FU-D43a) are DIFFERENT: neither may overdraft, so the
   換匯中心 shows the pool's 可用餘額 (the sell ceiling), the 出金 form shows 賬戶現金 (the
   withdraw ceiling), and both amounts are HARD-blocked when they exceed it — live
   disabled-確認 + inline error, backed by the server's fx_insufficient_balance /
   withdraw_insufficient_balance 422 (no ack override).
   FU-D43b: clicking either ceiling figure fills the amount field with the full balance.
   FU-D43c: the buy amount auto-fills from GET /api/cash/fx-estimate (SERVER-computed;
   this file only places the returned strings — it never multiplies money) while the buy
   field is pristine; a manual edit stops the auto-fill (重新試算 re-runs it).
   FU-D40: the fx_conversions ledger (same /api/ledgers/fx the 交易帳本 uses) is listed
   under the 換匯中心 tab, paged like the movements ledger. */
(function () {
  'use strict';
  const f = window.fmt;
  const api = window.pdApi;
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const TODAY = (() => {
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  })();

  const KIND_LABEL = {
    deposit: '入金', withdraw: '出金', opening: '期初資金', rebate: '折讓款',
    fx_in: '換入', fx_out: '換出', buy: '買入', sell: '賣出', dividend: '股利',
  };
  const settlementCcy = (a) => (a && (a.settlement_ccy || a.ccy)) || '';

  /* FU-D34: Decimal-safe compare of two numeric STRINGS — the sell amount vs. the pool
     balance. This is a display-only UX HINT (the backend re-validates as the authority),
     but comparing the raw strings (not Number()) keeps the EXACT-balance case (a == b)
     from ever being falsely blocked by binary-float drift. Returns 1 if a>b, −1 if a<b,
     0 if equal; falls back to Number() only for non-plain-decimal input (e.g. "3e5"). */
  const DEC_RE = /^[+-]?\d*\.?\d+$/;
  function parseDec(s) {
    let neg = false;
    if (s[0] === '+') s = s.slice(1);
    else if (s[0] === '-') { neg = true; s = s.slice(1); }
    const dot = s.indexOf('.');
    let int = dot < 0 ? s : s.slice(0, dot);
    let frac = dot < 0 ? '' : s.slice(dot + 1);
    int = int.replace(/^0+(?=\d)/, '') || '0';
    frac = frac.replace(/0+$/, '');
    if (int === '0' && frac === '') neg = false;  // normalize −0
    return { neg: neg, int: int, frac: frac };
  }
  function cmpMag(a, b) {
    if (a.int.length !== b.int.length) return a.int.length > b.int.length ? 1 : -1;
    if (a.int !== b.int) return a.int > b.int ? 1 : -1;
    const len = Math.max(a.frac.length, b.frac.length);
    const fa = a.frac.padEnd(len, '0');
    const fb = b.frac.padEnd(len, '0');
    if (fa === fb) return 0;
    return fa > fb ? 1 : -1;
  }
  function decCmp(a, b) {
    a = String(a).trim(); b = String(b).trim();
    if (!DEC_RE.test(a) || !DEC_RE.test(b)) {
      const na = Number(a);
      const nb = Number(b);
      return na > nb ? 1 : na < nb ? -1 : 0;
    }
    const pa = parseDec(a);
    const pb = parseDec(b);
    if (pa.neg !== pb.neg) return pa.neg ? -1 : 1;
    const m = cmpMag(pa, pb);
    return pa.neg ? -m : m;
  }

  let D = { balances: [], movements: [], negative_pools: [] };
  let accounts = [];  // from /api/input/context (id, name, ccy, settlement_ccy, funding_ccy)
  let cmKind = 'deposit';
  let booted = false;  // FU-D25: gate pd-cash-tab re-renders until the first boot() populated D
  /* FU-D43c estimate state — MODULE scope, not initForms: syncFxCcy() calls
     resetEstimate() early in initForms(), and `let` declarations further down initForms
     would sit in their temporal dead zone at that call (the exact TDZ-abort class the
     cash page smoke guards against). */
  let estPristine = true;  // buy field untouched -> the estimate may write it
  let estSeq = 0;          // stale-response fence (bumped on reset + every request)
  let estTimer = null;     // debounce so typing does not spray requests

  /* WPE (2026-07-07): movements ledger pages via the endpoint's limit/offset */
  const PAGE = Math.min((window.pdPrefs && window.pdPrefs.page_size) || 50, 500);
  const cmState = { offset: 0, limit: PAGE, total: 0 };
  let cmPager = null;
  if (window.pdPager) {
    cmPager = window.pdPager.create({
      host: document.getElementById('cm-pager'),
      limit: cmState.limit, offset: 0, totalCount: 0,
      onPage: (offset) => { cmState.offset = offset; boot(); },
    });
  }

  /* ---- C5: cash statement (per account+ccy pool) ---- */
  const stmt = { account: null, ccy: null, offset: 0, limit: PAGE, total: 0 };
  let stmtPager = null;
  if (window.pdPager) {
    stmtPager = window.pdPager.create({
      host: document.getElementById('cash-stmt-pager'),
      limit: stmt.limit, offset: 0, totalCount: 0,
      onPage: (offset) => { stmt.offset = offset; loadStatement(); },
    });
  }

  /* ---- FU-D40: fx-conversions ledger under 換匯中心 (paged like the movements ledger) ---- */
  const cfxLed = { offset: 0, limit: PAGE, total: 0, rows: [] };
  let cfxLedPager = null;
  if (window.pdPager) {
    cfxLedPager = window.pdPager.create({
      host: document.getElementById('cfx-ledger-pager'),
      limit: cfxLed.limit, offset: 0, totalCount: 0,
      onPage: (offset) => { cfxLed.offset = offset; loadFxLedger(); },
    });
  }

  async function loadFxLedger() {
    let resp;
    try {
      resp = await api.get('/api/ledgers/fx', { limit: cfxLed.limit, offset: cfxLed.offset });
    } catch (err) {
      cfxLed.rows = []; cfxLed.total = 0;
      renderFxLedger();
      if (cfxLedPager) cfxLedPager.update({ offset: cfxLed.offset, totalCount: 0 });
      if (window.toast) window.toast('換匯紀錄載入失敗', 'fail', (err && err.message) || undefined);
      return;
    }
    cfxLed.rows = (resp && resp.rows) || [];
    cfxLed.total = (resp && resp.total_count) || 0;
    renderFxLedger();
    if (cfxLedPager) cfxLedPager.update({ offset: cfxLed.offset, totalCount: cfxLed.total });
  }

  function renderFxLedger() {
    const tbody = $('#cfx-ledger-body');
    if (!tbody) return;
    tbody.replaceChildren();
    cfxLed.rows.forEach((x) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'num', f.date(x.date)));
      tr.appendChild(el('td', 'col-text', x.account));
      tr.appendChild(el('td', 'num', f.money(x.from_amt, x.from_ccy) + ' ' + x.from_ccy));
      tr.appendChild(el('td', 'num', f.money(x.to_amt, x.to_ccy) + ' ' + x.to_ccy));
      /* implied rate is computed by the backend (from/to, home per foreign) — never here */
      tr.appendChild(el('td', 'num',
        '1 ' + x.to_ccy + ' = ' + f.rate(x.implied_rate) + ' ' + x.from_ccy));
      tbody.appendChild(tr);
    });
    if (!cfxLed.rows.length) {
      const tr = el('tr');
      const td = el('td', 'hint', '尚無換匯紀錄 — 上方表單寫入的換匯會列在這裡');
      td.colSpan = 5;
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
  }

  /* ---- A. balance cards (clickable -> statement) ---- */
  function renderCards() {
    const wrap = $('#cash-cards');
    wrap.replaceChildren();
    const byAcct = new Map();
    D.balances.forEach((b) => {
      if (!byAcct.has(b.account_id)) byAcct.set(b.account_id, { name: b.account, lines: [] });
      byAcct.get(b.account_id).lines.push(b);
    });
    accounts.forEach((a) => {
      const card = el('div', 'cash-card');
      /* Account name = clickable header -> the ACCOUNT-LEVEL all-currency statement
         (openStatement with ccy=null); active when that combined view is open. */
      const acctDiv = el('div', 'acct clickable', a.name);
      if (stmt.account === a.id && stmt.ccy == null) acctDiv.classList.add('active');
      acctDiv.title = '點擊查看全部幣別收支明細';
      acctDiv.addEventListener('click', () => openStatement(a.id, null));
      card.appendChild(acctDiv);
      const entry = byAcct.get(a.id);
      if (!entry || !entry.lines.length) {
        card.appendChild(el('div', 'hint', '尚無現金紀錄'));
      } else {
        entry.lines.forEach((b) => {
          const line = el('div', 'cash-line clickable');
          if (stmt.account === b.account_id && stmt.ccy === b.ccy) line.classList.add('active');
          line.appendChild(el('span', 'ccy', b.ccy));
          const amt = el('span', 'amt', f.money(b.amount, b.ccy));
          if (String(b.amount).indexOf('-') === 0) {
            amt.classList.add('neg');
            amt.title = '負現金 — 通常代表漏記入金或換匯';
          }
          line.appendChild(amt);
          line.title = '點擊查看收支明細';
          line.addEventListener('click', () => openStatement(b.account_id, b.ccy));
          card.appendChild(line);
        });
      }
      wrap.appendChild(card);
    });
    renderTotal();
    renderBanner();
  }

  function renderTotal() {
    const totalEl = $('#cash-total');
    totalEl.replaceChildren();
    if (D.reporting_total != null) {
      totalEl.appendChild(document.createTextNode(
        '合併現金（' + D.reporting_currency + '，依最新匯率換算）: ' +
        f.money(D.reporting_total, D.reporting_currency) + ' ' + D.reporting_currency));
      if (D.reporting_total_unavailable_reason) {
        totalEl.appendChild(el('span', 'excl', '　（' + D.reporting_total_unavailable_reason + '）'));
      }
    } else {
      totalEl.textContent = '合併現金暫無法換算：' + (D.reporting_total_unavailable_reason || '');
    }
  }

  /* ---- C1a: negative-pool banner ---- */
  function renderBanner() {
    const banner = $('#cash-neg-banner');
    const negs = D.negative_pools || [];
    if (!negs.length) { banner.hidden = true; return; }
    banner.hidden = false;
    banner.textContent = '⚠ 資金池透支 — 可能漏登入金或換匯：' + negs.map((p) =>
      (p.account + ' ' + f.money(p.amount, p.ccy) + ' ' + p.ccy)).join('；');
  }

  /* ---- C5: statement table ---- */
  async function openStatement(account, ccy) {
    stmt.account = account; stmt.ccy = ccy; stmt.offset = 0;
    renderCards();  // refresh active highlight
    await loadStatement();
  }

  async function loadStatement() {
    if (!stmt.account) return;
    let resp;
    try {
      resp = await api.get('/api/cash/statement', {
        account: stmt.account, ccy: stmt.ccy, limit: stmt.limit, offset: stmt.offset,
      });
    } catch (err) {
      if (window.toast) window.toast('明細載入失敗', 'fail', (err && err.message) || undefined);
      return;
    }
    stmt.total = (resp && resp.total_count) || 0;
    renderStatement(resp);
    if (stmtPager) stmtPager.update({ offset: stmt.offset, totalCount: stmt.total });
  }

  /* Compose the 說明 line from a row's structured detail (display-only formatting via the
     f.* helpers). Falls back to the note/ref for movements; empty note -> 「（無備註）」. */
  function describe(r) {
    const ccy = r.ccy;
    if (r.kind === 'buy' || r.kind === 'sell') {
      const verb = r.kind === 'buy' ? '買入' : '賣出';
      const nm = r.name ? r.name + '（' + r.symbol + '）' : (r.symbol || '');
      return verb + ' ' + nm + ' ' + f.num(r.qty, 0) + ' 股 @ ' + f.price(r.price, ccy) +
        '（費 ' + f.money(r.fee, ccy) + '・稅 ' + f.money(r.tax, ccy) + '）';
    }
    if (r.kind === 'dividend') {
      const nm = r.name ? r.name + '（' + r.symbol + '）' : (r.symbol || '');
      return '配息 ' + nm;
    }
    if (r.kind === 'fx_in' || r.kind === 'fx_out') {
      let s = '換匯 ' + (r.ref || '');
      if (r.fx_rate != null) s += ' @ ' + f.rate(r.fx_rate);
      if (r.counter_amount != null && r.counter_ccy) {
        s += '（對應 ' + f.signed(r.counter_amount, r.counter_ccy) + ' ' + r.counter_ccy + '）';
      }
      return s;
    }
    const note = (r.ref || '').trim();
    return note || '（無備註）';
  }

  function renderStatement(resp) {
    const combined = resp.ccy == null;  // account-level all-currency view
    const sub = $('#cash-stmt-sub');
    if (sub) {
      if (combined) {
        const bals = (resp.balances || [])
          .map((b) => b.ccy + ' ' + f.money(b.balance, b.ccy)).join('　·　');
        sub.textContent = (resp.account || stmt.account) + '・全部幣別' +
          (bals ? '　目前餘額 ' + bals : '');
      } else {
        sub.textContent = (resp.account || stmt.account) + '・' + resp.ccy +
          '　目前餘額 ' + f.money(resp.current_balance, resp.ccy) + ' ' + resp.ccy;
      }
    }
    const ccyTh = $('#cash-stmt-ccy-th');
    if (ccyTh) ccyTh.hidden = !combined;
    const tbody = $('#cash-stmt-body');
    tbody.replaceChildren();
    (resp.rows || []).forEach((r) => {
      const rowCcy = r.ccy || resp.ccy;
      const tr = el('tr');
      tr.appendChild(el('td', 'num', f.date(r.date)));
      const tdKind = el('td', 'col-text');
      tdKind.appendChild(el('span', 'stmt-chip', KIND_LABEL[r.kind] || r.kind));
      tr.appendChild(tdKind);
      if (combined) tr.appendChild(el('td', 'num col-ccy', rowCcy));
      tr.appendChild(el('td', 'col-text', describe(r)));
      const tdDelta = el('td', 'num');
      tdDelta.textContent = f.signed(r.delta, rowCcy);
      if (String(r.delta).indexOf('-') === 0) tdDelta.classList.add('sign-down');
      tr.appendChild(tdDelta);
      const tdBal = el('td', 'num');
      tdBal.textContent = f.money(r.balance, rowCcy);
      if (String(r.balance).indexOf('-') === 0) { tdBal.style.color = 'var(--amber)'; }
      tr.appendChild(tdBal);
      tbody.appendChild(tr);
    });
    if (!(resp.rows || []).length) {
      tbody.appendChild(stmtEmptyRow('empty', combined));
    }
  }

  /* FU-D25 statement empty state — window.emptyState() glyph + one-line explanation, plus
     a hint pointing back at the pools above. Two honest variants:
       'pre'   — no pool chosen yet (initial / cleared);
       'empty' — a pool is selected but the ledger has no rows.
     Rendered inside a full-width <td> so the table structure (ids) is unchanged. */
  function stmtEmptyRow(kind, combined) {
    const tr = el('tr');
    const td = el('td');
    td.colSpan = combined ? 6 : 5;
    let main;
    let hint;
    if (kind === 'pre') {
      main = '尚未選擇資金池';
      hint = '點擊上方任一資金池即可檢視該池收支明細與滾動餘額';
    } else {
      main = '此資金池尚無收支紀錄';
      hint = '入金／出金、換匯或買賣收付都會列在這裡（點上方切換其他資金池）';
    }
    const es = window.emptyState ? window.emptyState(main) : el('div', 'empty-state', main);
    es.appendChild(el('div', 'stmt-empty-hint', hint));
    td.appendChild(es);
    tr.appendChild(td);
    return tr;
  }

  /* Pre-selection state: no pool chosen yet -> show guidance, not a blank table. */
  function renderStatementPre() {
    const sub = $('#cash-stmt-sub');
    if (sub) sub.textContent = '點帳戶名稱看全部幣別，或點某個幣別看單一資金池的收支明細與滾動餘額';
    const ccyTh = $('#cash-stmt-ccy-th');
    if (ccyTh) ccyTh.hidden = true;
    const tbody = $('#cash-stmt-body');
    if (tbody) tbody.replaceChildren(stmtEmptyRow('pre', false));
  }

  /* ---- statement exports: server-side reconciliation channel (匯出 CSV / 匯出報告) ----
     Both post the CURRENT statement scope {account, ccy} (ccy null = all-currency view).
     House style: pdBusy guards double-clicks, fail toast, filename from Content-Disposition. */
  function makeStmtExportButton(label, icon, path) {
    const b = el('button', 'btn btn-sm');
    b.type = 'button';
    b.appendChild(el('span', 'ico', icon));
    b.appendChild(el('span', null, label));
    b.addEventListener('click', async () => {
      if (!stmt.account) { if (window.toast) window.toast('請先選擇資金池', 'fail'); return; }
      const restore = window.pdBusy ? window.pdBusy(b, '匯出中…') : () => {};
      try {
        await api.download(path, { account: stmt.account, ccy: stmt.ccy });
      } catch (err) {
        if (window.toast) window.toast((err && err.message) || '匯出失敗', 'fail', err && err.code);
      } finally {
        restore();
      }
    });
    return b;
  }

  function initStmtExports() {
    const host = $('#cash-stmt-export');
    if (!host) return;
    host.replaceChildren();
    host.appendChild(makeStmtExportButton('匯出 CSV', '⬇', '/api/export/cash-statement'));
    host.appendChild(makeStmtExportButton('匯出報告', '⎙', '/api/export/cash-statement-report'));
  }

  /* ---- C. movements ledger ---- */
  function renderMovements() {
    const tbody = $('#cm-body');
    tbody.replaceChildren();
    D.movements.forEach((m) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'num', f.date(m.date)));
      tr.appendChild(el('td', 'col-text', m.account));
      const tdKind = el('td', 'col-text');
      const chipCls = m.kind === 'withdraw' ? 'dir-sell' : 'dir-buy';
      tdKind.appendChild(el('span', 'dir-chip ' + chipCls, KIND_LABEL[m.kind] || m.kind));
      tr.appendChild(tdKind);
      tr.appendChild(el('td', 'num', f.money(m.amount, m.ccy) + ' ' + m.ccy));
      tr.appendChild(el('td', 'col-text', m.note || ''));
      const tdAct = el('td');
      const acts = el('div', 'wl-actions');
      const edit = el('button', 'btn', '編輯'); edit.type = 'button';
      edit.addEventListener('click', () => openEdit(m));
      const rm = el('button', 'btn btn-row-del', '刪除'); rm.type = 'button';
      rm.addEventListener('click', () => removeMovement(m));
      acts.appendChild(edit); acts.appendChild(rm);
      tdAct.appendChild(acts);
      tr.appendChild(tdAct);
      tbody.appendChild(tr);
    });
  }

  function negConfirmRetry(err, retry) {
    if (err && err.status === 422 && err.code === 'negative_cash') {
      window.confirmDialog({
        title: '現金將變為負數', body: err.message, confirmLabel: '我了解，仍要寫入',
        danger: true, onConfirm: retry
      });
      return true;
    }
    return false;
  }

  /* ---- C2: constrain a ccy <select> to the account's {交割幣, 資金幣} ---- */
  function ccyOptions(accountId) {
    const a = accounts.find((x) => x.id === accountId);
    if (!a) return [];
    const out = [];
    const seen = new Set();
    const add = (ccy, role) => { if (ccy && !seen.has(ccy)) { seen.add(ccy); out.push([ccy, role]); } };
    add(settlementCcy(a), '交割幣');
    add(a.funding_ccy, '資金幣');
    return out;
  }
  function fillCcySelect(sel, accountId, preferred) {
    if (!sel) return;
    const opts = ccyOptions(accountId);
    sel.replaceChildren();
    opts.forEach(([ccy, role]) => {
      const o = el('option', null, ccy + '（' + role + '）');
      o.value = ccy;
      sel.appendChild(o);
    });
    if (preferred && opts.some(([c]) => c === preferred)) sel.value = preferred;
  }

  function openEdit(m) {
    const fDate = el('input', 'input'); fDate.type = 'date'; fDate.value = m.date;
    const fKind = el('select', 'select');
    [['deposit', '入金'], ['withdraw', '出金'], ['opening', '期初資金']].forEach(([v, label]) => {
      const o = el('option', null, label); o.value = v;
      if (m.kind === v) o.selected = true;
      fKind.appendChild(o);
    });
    const fAmt = el('input', 'input'); fAmt.type = 'number'; fAmt.step = '0.01';
    fAmt.value = m.amount;
    const fNote = el('input', 'input'); fNote.value = m.note || '';
    const backdrop = el('div', 'modal-backdrop');
    const modal = el('div', 'modal');
    const head = el('div', 'modal-head');
    head.appendChild(el('h3', 'modal-title', '編輯資金紀錄 #' + m.id + '（' + m.account + '・' + m.ccy + '）'));
    const close = el('button', 'modal-close', '✕'); close.type = 'button';
    head.appendChild(close);
    modal.appendChild(head);
    const body = el('div', 'modal-body');
    [['日期', fDate], ['方向', fKind], ['金額', fAmt], ['備註', fNote]].forEach(([label, node]) => {
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
    ok.addEventListener('click', async () => {
      dismiss();
      const send = async (ack) => api.put('/api/cash/movements/' + m.id, {
        account_id: m.account_id, date: fDate.value, kind: fKind.value,
        ccy: m.ccy, amount: fAmt.value, note: fNote.value.trim() || null,
        ack_negative: ack,
      });
      try {
        await send(false);
        if (window.toast) window.toast('已更新', 'ok', '資金紀錄 #' + m.id);
        await boot();
      } catch (err) {
        if (!negConfirmRetry(err, async () => {
          try { await send(true); if (window.toast) window.toast('已更新', 'ok'); await boot(); }
          catch (e2) { if (window.toast) window.toast((e2 && e2.message) || '更新失敗', 'fail'); }
        })) {
          if (window.toast) window.toast((err && err.message) || '更新失敗', 'fail', err && err.code);
        }
      }
    });
    document.body.appendChild(backdrop);
  }

  function removeMovement(m) {
    window.confirmDialog({
      title: '刪除資金紀錄',
      body: f.date(m.date) + '・' + (KIND_LABEL[m.kind] || m.kind) + ' ' +
        f.money(m.amount, m.ccy) + ' ' + m.ccy + '（' + m.account + '）',
      confirmLabel: '刪除', danger: true,
      onConfirm: async () => {
        const send = (ack) => api.del('/api/cash/movements/' + m.id + (ack ? '?ack_negative=true' : ''));
        try {
          await send(false);
          if (window.toast) window.toast('已刪除', 'ok');
          await boot();
        } catch (err) {
          if (!negConfirmRetry(err, async () => {
            try { await send(true); if (window.toast) window.toast('已刪除', 'ok'); await boot(); }
            catch (e2) { if (window.toast) window.toast((e2 && e2.message) || '刪除失敗', 'fail'); }
          })) {
            if (window.toast) window.toast((err && err.message) || '刪除失敗', 'fail', err && err.code);
          }
        }
      }
    });
  }

  /* ---- FU-D34 / FU-D43a: balance ceiling lines + live oversell guards ---- */
  /* Current balance of the (account, ccy) pool from the already-fetched /api/cash data.
     null = unknown (balances not loaded yet) → the hint stays neutral and never blocks;
     an absent pool AFTER load is a real 0 (an empty pool cannot fund a conversion). */
  function poolBalance(accountId, ccy) {
    if (!accountId || !ccy || !booted) return null;
    const row = D.balances.find((b) => b.account_id === accountId && b.ccy === ccy);
    return row ? row.amount : '0';
  }

  /* FU-D43b: render one ceiling line — 「{label}：{figure} {ccy}」. The FIGURE is a
     max-fill affordance when positive (.can-fill + title 「點擊帶入全額」); the RAW Decimal
     string rides on line.dataset.raw so the click handler fills the input with the exact
     server value (never the display-formatted one). */
  function renderCeilingLine(line, label, bal, ccy) {
    line.replaceChildren();
    line.classList.remove('warn');
    if (bal == null) { line.dataset.raw = ''; return; }
    const neg = String(bal).indexOf('-') === 0;
    const fillable = !neg && decCmp(String(bal), '0') > 0;  // a 0/negative pool fills nothing
    line.dataset.raw = fillable ? String(bal) : '';
    line.appendChild(document.createTextNode(label + '：'));
    const fig = el('span', null, f.money(bal, ccy) + ' ' + ccy);
    if (fillable) {
      fig.classList.add('can-fill');
      fig.title = '點擊帶入全額';
    }
    line.appendChild(fig);
    line.classList.toggle('warn', neg);
  }

  /* Shared live ceiling validation: over-balance → inline field error + disabled 確認.
     Display-only — the backend HARD-blocks the same case and is the authority; this
     only saves a round-trip. */
  function updCeiling(opts) {
    const line = $(opts.line);
    const errEl = $(opts.err);
    const confirm = $(opts.confirm);
    if (!line || !errEl || !confirm) return;
    const bal = opts.active ? poolBalance(opts.account, opts.ccy) : null;
    renderCeilingLine(line, opts.label, bal, opts.ccy);
    const amtStr = opts.active ? $(opts.amount).value.trim() : '';
    const over = bal != null && amtStr !== '' && decCmp(amtStr, bal) > 0;
    errEl.hidden = !over;
    if (over) {
      errEl.textContent = opts.overMsg + '（' + f.money(bal, opts.ccy) + ' ' + opts.ccy +
        '）— ' + opts.overTail;
    }
    confirm.disabled = over;
  }

  /* FU-D34: the 換匯中心 可用餘額 line (sell ceiling) for the chosen 帳戶+賣出幣別. */
  function updFxBalance() {
    updCeiling({
      line: '#cfx-balance', err: '#cfx-amt-err', confirm: '#cfx-confirm',
      amount: '#cfx-from-amt', active: true,
      account: $('#cfx-account').value, ccy: $('#cfx-from-ccy').value,
      label: '可用餘額', overMsg: '換出金額超過可用餘額', overTail: '換匯不可透支',
    });
  }

  /* FU-D43a: the 出金 賬戶現金 line (withdraw ceiling) — shown only for kind=出金;
     deposit/opening entry has no ceiling (credits need no balance). */
  function updCmBalance() {
    updCeiling({
      line: '#cm-balance', err: '#cm-amt-err', confirm: '#cm-confirm',
      amount: '#cm-amount', active: cmKind === 'withdraw',
      account: $('#cm-account').value, ccy: $('#cm-ccy').value,
      label: '賬戶現金', overMsg: '出金金額超過賬戶現金', overTail: '出金不可透支',
    });
  }

  /* ---- B. forms ---- */
  function initForms() {
    const accSelects = [$('#cm-account'), $('#cfx-account')];
    accSelects.forEach((sel) => {
      accounts.forEach((a) => {
        const o = el('option', null, a.name + '（' + settlementCcy(a) + '）');
        o.value = a.id;
        sel.appendChild(o);
      });
    });
    $('#cm-date').value = TODAY;
    $('#cfx-date').value = TODAY;

    /* C2: movement ccy dropdown tracks the selected account */
    const syncMovementCcy = () => {
      fillCcySelect($('#cm-ccy'), $('#cm-account').value);
      updCmBalance();  // FU-D43a: refresh the 賬戶現金 ceiling for the new account/ccy
    };
    $('#cm-account').addEventListener('change', syncMovementCcy);
    $('#cm-ccy').addEventListener('change', updCmBalance);
    $('#cm-amount').addEventListener('input', updCmBalance);
    syncMovementCcy();

    /* C2: fx ccy dropdowns track the account; default funding -> settlement */
    const syncFxCcy = () => {
      const a = accounts.find((x) => x.id === $('#cfx-account').value);
      fillCcySelect($('#cfx-from-ccy'), $('#cfx-account').value, a && a.funding_ccy);
      fillCcySelect($('#cfx-to-ccy'), $('#cfx-account').value, a && settlementCcy(a));
      updImplied();
      updFxBalance();  // FU-D34: refresh the 可用餘額 ceiling for the new account/from-ccy
      resetEstimate();     // FU-D43c: an account change makes the buy field pristine again
      scheduleEstimate();
    };
    $('#cfx-account').addEventListener('change', syncFxCcy);
    syncFxCcy();

    $('#cm-kind-in').addEventListener('click', () => setKind('deposit'));
    $('#cm-kind-out').addEventListener('click', () => setKind('withdraw'));
    const openBtn = $('#cm-kind-open');
    if (openBtn) openBtn.addEventListener('click', () => setKind('opening'));
    function setKind(k) {
      cmKind = k;
      $('#cm-kind-in').classList.toggle('active', k === 'deposit');
      $('#cm-kind-out').classList.toggle('active', k === 'withdraw');
      if (openBtn) openBtn.classList.toggle('active', k === 'opening');
      updCmBalance();  // FU-D43a: the ceiling line exists only while kind=出金
    }

    /* FU-D43b: clicking a ceiling FIGURE fills the amount field with the full raw
       balance (delegated to the line so the re-rendered span needs no re-binding). */
    $('#cm-balance').addEventListener('click', (e) => {
      const raw = $('#cm-balance').dataset.raw;
      if (!e.target.closest('.can-fill') || !raw || raw.indexOf('-') === 0) return;
      $('#cm-amount').value = raw;
      updCmBalance();
    });
    $('#cfx-balance').addEventListener('click', (e) => {
      const raw = $('#cfx-balance').dataset.raw;
      if (!e.target.closest('.can-fill') || !raw || raw.indexOf('-') === 0) return;
      $('#cfx-from-amt').value = raw;
      updImplied();
      updFxBalance();
      scheduleEstimate();  // a programmatic sell-amount change re-estimates like typing
    });

    /* FU-D43a: NO ack-retry on movement POST — a withdrawal may never overdraft (the
       backend answers a hard 422 withdraw_insufficient_balance; deposits/openings are
       credits and need no balance guard, so POST can no longer answer negative_cash).
       The live check already disables 確認 while the amount exceeds 賬戶現金; a backend
       422 renders inline under the amount as the authority (mirrors the FX form). */
    $('#cm-confirm').addEventListener('click', async () => {
      const amount = $('#cm-amount').value.trim();
      if (!amount) { if (window.toast) window.toast('請輸入金額', 'fail'); return; }
      const restore = window.pdBusy ? window.pdBusy($('#cm-confirm'), '寫入中…') : () => {};
      try {
        await api.post('/api/cash/movements', {
          account_id: $('#cm-account').value, date: $('#cm-date').value, kind: cmKind,
          ccy: $('#cm-ccy').value, amount: amount,
          note: $('#cm-note').value.trim() || null,
        });
        restore();
        if (window.toast) window.toast('寫入成功', 'ok', (KIND_LABEL[cmKind] || '') + ' ' + amount);
        $('#cm-amount').value = ''; $('#cm-note').value = '';
        await boot();
      } catch (err) {
        restore();
        if (err && err.status === 422 && err.code === 'withdraw_insufficient_balance') {
          // Backend authority: render ITS message inline (verbatim) and keep 確認 blocked.
          // Editing the amount/帳戶/幣別 fires updCmBalance(), which re-validates against
          // the pool and clears the block once the amount is within 賬戶現金.
          const errEl = $('#cm-amt-err');
          if (errEl) { errEl.hidden = false; errEl.textContent = err.message; }
          $('#cm-confirm').disabled = true;
        }
        if (window.toast) window.toast((err && err.message) || '寫入失敗', 'fail', err && err.code);
      }
    });

    /* Hoisted function declaration: syncFxCcy() above runs during initForms() BEFORE
       this point — a `const` arrow here is in its temporal dead zone at that call and
       aborts the whole init (no click handlers attached). */
    function updImplied() {
      const fromA = parseFloat($('#cfx-from-amt').value) || 0;
      const toA = parseFloat($('#cfx-to-amt').value) || 0;
      /* implied-rate what-if on the USER's own entry (input-side calc exception) */
      $('#cfx-implied').textContent = (fromA > 0 && toA > 0)
        ? '1 ' + $('#cfx-to-ccy').value + ' = ' + (fromA / toA).toFixed(4) + ' ' + $('#cfx-from-ccy').value
        : f.NULL_GLYPH;
    }

    /* ---- FU-D43c: server-computed buy-amount estimate --------------------------------
       While the buy field is PRISTINE (untouched since the last account/ccy change or
       submit), a sell-amount change asks GET /api/cash/fx-estimate and places the
       returned strings — the ESTIMATE into the buy field and the caption 「以 {date}
       匯率 {rate} 試算…」. A manual edit of the buy field flips it dirty: auto-fill stops
       (it can never fight the user) and the 重新試算 affordance appears; erasing the buy
       field to empty makes it pristine again (filling an EMPTY field destroys nothing).
       All hoisted function declarations — syncFxCcy() above calls them during init;
       the estPristine/estSeq/estTimer state lives at MODULE scope (TDZ, see top). */

    function setReestimateVisible(vis) {
      const b = $('#cfx-reestimate');
      if (b) b.hidden = !vis;
    }

    function resetEstimate() {
      estSeq += 1;  // drop any in-flight response
      if (estTimer) { clearTimeout(estTimer); estTimer = null; }
      estPristine = true;
      setReestimateVisible(false);
      const cap = $('#cfx-estimate');
      if (cap) { cap.hidden = true; cap.textContent = ''; }
    }

    async function runEstimate() {
      if (!estPristine) return;
      const cap = $('#cfx-estimate');
      const from = $('#cfx-from-ccy').value;
      const to = $('#cfx-to-ccy').value;
      const amt = $('#cfx-from-amt').value.trim();
      if (!from || !to || from === to || amt === '' || decCmp(amt, '0') <= 0) {
        if (cap) { cap.hidden = true; cap.textContent = ''; }
        return;
      }
      const seq = ++estSeq;
      let resp;
      try {
        resp = await api.get('/api/cash/fx-estimate',
          { from_ccy: from, to_ccy: to, amount: amt });
      } catch (err) {
        return;  // the estimate is a hint — fail silent, never block entry
      }
      if (seq !== estSeq || !estPristine) return;  // stale, or the user took over
      if (!resp || resp.available === false) {
        if (cap) { cap.hidden = false; cap.textContent = (resp && resp.reason) || '暫無法試算'; }
        return;
      }
      $('#cfx-to-amt').value = resp.estimate;  // server Decimal string, placed verbatim
      if (cap) {
        cap.hidden = false;
        cap.textContent = '以 ' + f.date(resp.rate_as_of) + ' 匯率 ' + f.rate(resp.rate) +
          ' 試算，可依實際成交調整';
      }
      updImplied();
      updFxBalance();
    }

    function scheduleEstimate() {
      if (!estPristine) return;
      if (estTimer) clearTimeout(estTimer);
      estTimer = setTimeout(() => { estTimer = null; runEstimate(); }, 250);
    }

    /* manual edit of the buy field -> dirty (stop auto-overwrite); erased-empty -> pristine */
    function markBuyEdited() {
      estPristine = $('#cfx-to-amt').value.trim() === '';
      setReestimateVisible(!estPristine);
      if (estPristine) {
        const cap = $('#cfx-estimate');
        if (cap) { cap.hidden = true; cap.textContent = ''; }
      }
    }

    $('#cfx-reestimate').addEventListener('click', () => {
      estPristine = true;  // explicit user request: the estimate may overwrite again
      setReestimateVisible(false);
      runEstimate();
    });

    $('#cfx-from-amt').addEventListener('input', () => {
      updImplied(); updFxBalance(); scheduleEstimate();
    });
    $('#cfx-to-amt').addEventListener('input', () => {
      updImplied(); updFxBalance(); markBuyEdited();
    });
    ['cfx-from-ccy', 'cfx-to-ccy'].forEach((id) =>
      $('#' + id).addEventListener('input', () => {
        updImplied(); updFxBalance(); resetEstimate(); scheduleEstimate();
      }));
    updImplied();
    updFxBalance();

    $('#cfx-confirm').addEventListener('click', async () => {
      const fromA = $('#cfx-from-amt').value.trim();
      const toA = $('#cfx-to-amt').value.trim();
      if (!fromA || !toA) { if (window.toast) window.toast('請填兩側金額', 'fail'); return; }
      /* FU-D34: NO ack-retry — a conversion may never overdraft the pool. The live check
         already disables 確認 when the amount exceeds 可用餘額; the backend re-validates as
         the authority and a 422 fx_insufficient_balance renders inline under the amount. */
      const restore = window.pdBusy ? window.pdBusy($('#cfx-confirm'), '寫入中…') : () => {};
      try {
        await api.post('/api/cash/fx', {
          account_id: $('#cfx-account').value, date: $('#cfx-date').value,
          from_ccy: $('#cfx-from-ccy').value, from_amt: fromA,
          to_ccy: $('#cfx-to-ccy').value, to_amt: toA,
        });
        restore();
        if (window.toast) window.toast('換匯已寫入', 'ok', fromA + ' ' + $('#cfx-from-ccy').value + ' → ' + toA + ' ' + $('#cfx-to-ccy').value);
        $('#cfx-from-amt').value = ''; $('#cfx-to-amt').value = ''; updImplied();
        resetEstimate();  // FU-D43c: cleared form -> pristine again, drop in-flight estimates
        await boot();  // refresh balances → the 可用餘額 ceiling updates (+ FU-D40 ledger)
      } catch (err) {
        restore();
        if (err && err.status === 422 && err.code === 'fx_insufficient_balance') {
          // The backend is the authority: render ITS message inline (verbatim) and keep 確認
          // blocked. Editing the amount/帳戶/幣別 fires updFxBalance(), which re-validates
          // against the pool and clears the block once the amount is within 可用餘額.
          const errEl = $('#cfx-amt-err');
          if (errEl) { errEl.hidden = false; errEl.textContent = err.message; }
          $('#cfx-confirm').disabled = true;
        }
        if (window.toast) window.toast((err && err.message) || '寫入失敗', 'fail', err && err.code);
      }
    });
  }

  async function boot() {
    let resp;
    try {
      resp = await api.get('/api/cash', { limit: cmState.limit, offset: cmState.offset });
    } catch (err) {
      D = { balances: [], movements: [], negative_pools: [] };
      cmState.total = 0;
      renderCards(); renderMovements();
      if (cmPager) cmPager.update({});
      if (window.toast) window.toast('資金資料載入失敗', 'fail', (err && err.message) || undefined);
      return;
    }
    D = {
      balances: (resp && resp.balances) || [],
      movements: (resp && resp.movements && resp.movements.rows) || [],
      negative_pools: (resp && resp.negative_pools) || [],
      reporting_total: resp && resp.reporting_total,
      reporting_currency: (resp && resp.reporting_currency) || 'TWD',
      reporting_total_unavailable_reason: resp && resp.reporting_total_unavailable_reason,
    };
    cmState.total = (resp && resp.movements && resp.movements.total_count) || 0;
    renderCards();
    renderMovements();
    if (cmPager) cmPager.update({ offset: cmState.offset, totalCount: cmState.total });
    if (stmt.account) await loadStatement();  // keep the open statement fresh
    else renderStatementPre();  // FU-D25: guidance instead of a blank statement table
    await loadFxLedger();  // FU-D40: keep the 換匯中心 ledger in step with every refresh
    booted = true;
    updFxBalance();  // FU-D34: refresh the 換匯中心 可用餘額 ceiling from the fresh balances
    updCmBalance();  // FU-D43a: refresh the 出金 賬戶現金 ceiling too
  }

  /* FU-D25: re-render the activated tab's section from cached state on every tab switch.
     boot() already refetches + re-renders all sections eagerly (even while a tab is
     display:none — this ECharts-free page has no measurement path that mis-sizes when
     hidden), so this is a belt-and-braces re-render, not the source of freshness. */
  function onCashTab(e) {
    if (!booted) return;
    const tab = e && e.detail;
    if (tab === 'pools') {
      renderCards();
      if (!stmt.account) renderStatementPre();
    } else if (tab === 'flows') {
      renderMovements();
      // FU-D43a (mirrors r4's FX fix): show the 出金 ceiling from cached balances
      // IMMEDIATELY, then re-fetch so it is CURRENT.
      updCmBalance();
      boot();  // async; its trailing updCmBalance() refreshes the ceiling once balances land
    } else if (tab === 'fx') {
      // FU-D34: opening 換匯中心 shows the ceiling from cached balances IMMEDIATELY, then
      // re-fetches so it is CURRENT — the pools may have changed via other surfaces since
      // this page last booted (e.g. a tab switch is a fragment nav, not a reload).
      updFxBalance();
      boot();  // async; its trailing updFxBalance() refreshes the ceiling (+ FU-D40 ledger)
    }
  }
  window.addEventListener('pd-cash-tab', onCashTab);

  (async function init() {
    try {
      const ctx = await api.get('/api/input/context');
      accounts = (ctx && ctx.accounts) || [];
    } catch (err) {
      accounts = [];
    }
    initForms();
    initStmtExports();
    await boot();
  })();
})();
