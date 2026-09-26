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
  /* trades.html names its ledger tabs tab-ldiv / tab-lfx / … (the input panes took
     tab-div); ledger.html uses the bare names. Both spellings map to the same loader. */
  const TAB_IDS = ownsTabs
    ? { tx: 'tx', div: 'div', fx: 'fx', open: 'open', action: 'action', cash: 'cash' }
    : { tx: 'tx', div: 'ldiv', fx: 'lfx', open: 'lopen', action: 'laction', cash: 'lcash' };
  let activeTab = 'tx';
  function showTab(t) {
    activeTab = t;
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
  /* L16 (demo audit 2026-09-16): every filter change fired all six ledger requests although
     one pane is visible. A hidden pane is now marked DIRTY instead of fetched, and fetched
     the moment its tab is shown (ensureTab). On trades.html the tab glue lives in the page's
     own script, so the listener here only tracks the active tab and tops it up. */
  TABS.forEach((t) => {
    const b = document.getElementById('tab-' + TAB_IDS[t]);
    if (b) b.addEventListener('click', () => { activeTab = t; ensureTab(t); });
  });

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
  let accountList = []; /* [{id}] from GET /api/accounts (chip registry) */

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

  /* The 422 codes a correction door may be ACKED past, and the parameter each ack rides on.
     Until 2026-09-03 (M3-01) only `oversell` was recognised, and `negative_cash` — which
     ONLY `DELETE /api/ledgers/fx/{id}` raises (QA-10), from a control this page is the whole
     app's ONLY door to — fell through to a plain fail toast. The server's own message ends
     「確認無誤可強制寫入」, so the user was left inside an error that promised an exit no
     control implemented: that FX row could never be deleted from anywhere. A promised exit
     nobody implements is worse than no exit.

     `fx_insufficient_balance` is DELIBERATELY absent. FU-D34 makes an overdrafting conversion
     a HARD refusal with no ack, so `PUT /api/ledgers/fx/{id}` answers that code and never
     `negative_cash`; offering an ack for it here would be financing. That is also the answer
     to "does the edit door have the same defect?" — it does not, because the edit door has no
     ack-able code at all today. The table is shared by BOTH doors anyway so the two can never
     drift again: a code either is ack-able on this page or it is not, in ONE place. */
  const ACK_CODES = {
    oversell: { param: 'ack_oversell', title: '賣超確認' },
    negative_cash: { param: 'ack_negative', title: '現金將變為負數' },
  };

  /* Turn an ack-able 422 into the danger confirm; `retry(param)` re-sends carrying that ack.
     Returns true when it handled the error — the caller must then NOT also toast a failure.
     `before()` runs only when the dialog IS about to open: it closes whatever must be gone
     first (M3-04 below — the edit modal, or the delete path's progress toast). */
  function ackConfirm(err, verb, retry, before) {
    const meta = err && err.status === 422 ? ACK_CODES[err.code] : null;
    if (!meta) return false;
    if (before) before();
    window.confirmDialog({
      title: meta.title, body: err.message, danger: true,
      confirmLabel: '我了解，仍要' + verb,
      onConfirm: () => retry(meta.param),
    });
    return true;
  }
  /* DEF-049: shared with 最近匯入 › 復原 (broker-import.js), whose undo answers the same
     ack-able 422s — one dialog, one ack table, for every door that removes ledger rows. */
  window.pdAckConfirm = ackConfirm;

  /* ===== M3-04 (2026-09-06): one ledger mutation at a time, and it is visible =====
     Every correction door on this page used to close its dialog FIRST and await the request
     second — `dismiss(); await …` — so by the time the request started, the button that could
     have carried a busy state was already out of the DOM. Measured under a 2.5 s delay: no
     modal, no toast, no spinner, all 100 row buttons live, and a second 儲存 / 刪除 fired a
     second PUT / DELETE against the same row. Two mechanisms, both already on this page:

       * the edit modal STAYS OPEN with 儲存 in `pdBusy` until the request settles — the
         precedent is the 公司行動 form (corp-action-form.js: pdBusy → restore → dismiss on
         success only). A failure restores the button and leaves the modal, values intact, so
         the user corrects and re-sends instead of re-typing;
       * the delete confirm (shell.js `confirmDialog`) closes itself before `onConfirm` runs, so
         there is no button to hold: a `toastProgress` spinner stands in, and the module-level
         `inflight` flag makes every row button a no-op (`actionsCell`) until the tables have
         been rebuilt. The same runner carries every retry after an ack dialog, whose button is
         gone for the same reason.

     ORDER MATTERS on an ack-able 422: the edit modal is closed BEFORE the ack dialog opens,
     never left open beside it — `tests/e2e/test_ledger_correction_doors_flow.py` clicks the
     FIRST `.modal-foot .btn-danger` it finds, and a user's eye does the same. */
  let inflight = false;

  /* The progress toast for a mutation whose control is gone. `drop()` covers the one outcome
     the shared API has no verb for — the server answered 「needs your confirmation」: a spinner
     beside that question would say the delete is still running, and shell.js's toastProgress
     can only settle into an ok / warn / fail toast (that file is outside this change). */
  function progressToast(msg) {
    if (!window.toastProgress) return { done: () => {}, fail: () => {}, drop: () => {} };
    const p = window.toastProgress(msg, '帳本更新中，請稍候');
    const host = document.querySelector('.toast-host');
    const node = host ? host.lastElementChild : null;
    return { done: p.done, fail: p.fail, drop: () => { if (node) node.remove(); } };
  }

  function actionsCell(onEdit, onDel) {
    const td = el('td');
    const wrap = el('div', 'wl-actions');
    const e = el('button', 'btn', '編輯'); e.type = 'button';
    e.addEventListener('click', (ev) => { ev.stopPropagation(); if (!inflight) onEdit(); });
    const d = el('button', 'btn btn-row-del', '刪除'); d.type = 'button';
    d.addEventListener('click', (ev) => { ev.stopPropagation(); if (!inflight) onDel(); });
    wrap.appendChild(e); wrap.appendChild(d);
    td.appendChild(wrap);
    return td;
  }

  /* I-7 (DEF-019's last stranded writer): an edit or delete here changes the ledger every
     holdings figure on the page is read from, so it settles through the page's ONE refresh —
     window.pdLedgerRefresh, which input.js adopts (structural context, the per-account
     holdings cache the pickers and sell hints read, then these tables). It used to call
     boot(), which rebuilt only these tables: after an edit here the 交易輸入 picker kept
     annotating the pre-edit share count until the next commit made in the input pane. On a
     page without input.js the global is still this file's own table refresh, and boot() is
     the fallback when neither is present. The ledger KIND is the visible tab's. */
  function refreshAfterMutation() {
    if (typeof window.pdLedgerRefresh === 'function') {
      const kind = Object.keys(KIND_TAB).find((k) => KIND_TAB[k] === activeTab);
      return window.pdLedgerRefresh(kind);
    }
    return boot();
  }

  /* The success toast fires AFTER the tables are rebuilt — it used to fire before the
     refresh, so 「刪除完成」 stood on screen beside the row it claimed was gone. */
  async function mutationOk(kind, prog) {
    await refreshAfterMutation();
    const sub = '帳本已更新，統計將由帳本重建';
    if (prog) prog.done(kind + '完成', sub);
    else if (window.toast) window.toast(kind + '完成', 'ok', sub);
  }
  function mutationFail(err, kind, prog) {
    const msg = (err && err.message) || undefined;
    if (prog) prog.fail(kind + '失敗', msg);
    else if (window.toast) window.toast(kind + '失敗', 'fail', msg);
  }

  /* Run a mutation whose control is already gone (a delete; any retry after an ack dialog):
     progress toast + inflight guard, tables rebuilt before the settle toast. `onErr(err, prog)`
     returns true when it took over (opened a dialog — after `prog.drop()`); otherwise the
     failure settles the toast. */
  async function runMutation(kind, send, onErr) {
    if (inflight) return;
    inflight = true;
    const prog = progressToast(kind + '中…');
    try {
      await send();
    } catch (err) {
      inflight = false;
      if (!(onErr && onErr(err, prog))) mutationFail(err, kind, prog);
      return;
    }
    try { await mutationOk(kind, prog); } finally { inflight = false; }
  }

  /* Run a save from an edit modal: 儲存 goes busy and the modal stays until `send()` settles.
     Success closes the modal and rebuilds the tables before the toast. On an error `onErr`
     may take over (an ack dialog — it must `ui.dismiss()` BEFORE opening it); otherwise the
     failure is toasted and the modal stays, values intact, for the user to correct. */
  async function saveFromModal(ui, kind, send, onErr) {
    const restore = window.pdBusy ? window.pdBusy(ui.ok, '儲存中…') : () => {};
    inflight = true;
    try {
      await send();
    } catch (err) {
      inflight = false;
      restore();
      if (!(onErr && onErr(err))) mutationFail(err, kind);
      return;
    }
    restore();
    ui.dismiss();
    try { await mutationOk(kind); } finally { inflight = false; }
  }

  /* PUT with the ack retry loop. bodyFn(ack) builds the payload; the ack rides in the BODY.
     `ui` = {ok, dismiss} handed over by editModal. */
  function putWithAckGuard(path, bodyFn, kind, ui) {
    const send = (param) => {
      const body = bodyFn(param === 'ack_oversell');
      if (param) body[param] = true;
      return window.pdApi.put(path, body);
    };
    return saveFromModal(ui, kind, () => send(null), (err) => ackConfirm(
      err, '寫入', (param) => runMutation(kind, () => send(param)), ui.dismiss));
  }

  /* DELETE with confirm + the ack retry loop (the ack rides as a query param). `extra` is
     appended to the confirm body — a consequence the owner must read BEFORE confirming. */
  function delWithConfirm(path, label, extra) {
    const send = (param) => window.pdApi.del(
      param ? path + (path.indexOf('?') === -1 ? '?' : '&') + param + '=true' : path);
    window.confirmDialog({
      title: '刪除' + label,
      body: '確定刪除這筆' + label + '？統計將由其餘帳本紀錄重建。' + (extra || ''),
      confirmLabel: '刪除', danger: true,
      onConfirm: () => runMutation('刪除', () => send(null), (err, prog) => ackConfirm(
        err, '刪除', (param) => runMutation('刪除', () => send(param)), prog.drop)),
    });
  }

  /* DEF-023 (2026-09-23): deleting a trade can pull the holding out from under a LATER
     corporate action on the same account + symbol — the replay then refuses the action
     (「沒有持倉，無法套用」), XIRR goes 「— 資料不足」 portfolio-wide, and nothing in this
     dialog said so. The actions dated on/after the trade are read from the ledger API
     (`from` = the trade's own date, the row's `symbol` is its source symbol) and named
     here, one line each, BEFORE the confirm opens. A lookup failure degrades to the plain
     confirm: the warning is advice, the server's replay guard is the authority. */
  async function delTxWithWarning(t) {
    let extra = '';
    try {
      const resp = await window.pdApi.get('/api/ledgers/corporate-actions', {
        account_id: t.account_id, symbol: t.symbol, from: t.date, limit: 500 });
      const later = ((resp && resp.rows) || []).filter(
        (a) => a.symbol === t.symbol && a.date >= t.date);
      later.forEach((a) => {
        extra += '　⚠ 刪除後 ' + f.date(a.date) + ' 的 ' + a.symbol + ' ' + a.kind_label
          + ' 可能失去持倉依據而無法套用（待釐清）。';
      });
    } catch (e) { /* degrade to the plain confirm */ }
    delWithConfirm('/api/ledgers/transactions/' + t.id, '交易', extra);
  }

  /* generic edit modal: rows = [[label, inputNode]], onSave({ok, dismiss}) async — the 儲存
     button itself is handed over so the save can hold it busy (M3-04) */
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
    ok.addEventListener('click', () => onSave({ ok: ok, dismiss: dismiss }));
    document.body.appendChild(backdrop);
    /* The same handle onSave receives, returned so a dialog can gate 儲存 BEFORE any save
       (editTx: the entry door's per-warning acknowledgement, DEF-042). */
    return { ok: ok, dismiss: dismiss };
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

  /* ===== the edit modal's issue panel (M3-02 / M3-03 2026-09-03; DEF-042 2026-09-24) =====
     `editTx` previews through `POST /api/input/manual/preview` WITH `replaces_txn_id`: the
     server then runs the SAME `validate_transaction` the entry door runs, over "the ledger
     without this row + the edited row" (DEF-042, owner ruling 2026-09-24). Until then the
     preview asked the ENTRY question (「如果再新增這一筆」), so the row was its own duplicate,
     its own cover and its own 賣超, and this panel had to drop or re-word those findings — and
     a date moved before the position's opening build date saved with nothing acknowledged.

     What the panel does now, the entry door's flow (input.js renderManual) applied here:
       * every soft warning gets its OWN tick and 儲存 waits for all of them (M4-02) — keyed by
         code + sentence, so a re-worded warning is a warning not yet read;
       * `sell_exceeds_holdings` is shown WITHOUT a tick: the save answers 422 `oversell` and
         the 賣超確認 dialog is this door's one destructive acknowledgement (it also covers the
         OTHER rows an edit can strand, which no preview of this row can see);
       * advisories (info) never gate; hard findings (error) do not disable 儲存 — the server
         refuses them with the same sentence and the modal stays open (M3-04 (d));
       * `symbol_auto_register` / `symbol_needs_market` are true as a FACT and false as a
         PROMISE on this door, which answers 400 instead of auto-registering (M3-03).
     Anything else renders verbatim: a new server issue must surface by default. */
  function editIssueWire(i, symbol) {
    if (!i) return null;
    if (i.code === 'symbol_auto_register' || i.code === 'symbol_needs_market') {
      return { sev: 'error', code: i.code,
        text: '未註冊標的 ' + symbol + ' — 帳本更正不會自動註冊；請先至「標的管理」註冊，'
          + '再回來儲存這筆修改' };
    }
    return i;
  }
  const ISSUE_GLYPH = { error: '✕', warn: '⚠', info: 'ℹ' };
  const editAckKey = (i) => i.code + '\n' + i.text;
  const needsTick = (i) => i.sev === 'warn' && i.code !== 'sell_exceeds_holdings';
  /* Paint the translated list into `box`; hides its whole .field row when empty so a clean
     edit shows no stray gap. `acks` (ack key → true) holds the ticks and `onTick` repaints.
     Returns true when every warning that needs a tick has one. */
  function renderEditIssues(box, list, acks, onTick) {
    const store = acks || {};
    const live = list.filter(needsTick).map(editAckKey);
    Object.keys(store).forEach((k) => { if (live.indexOf(k) < 0) delete store[k]; });
    let allAcked = true;
    box.replaceChildren();
    list.forEach((i, idx) => {
      const sev = i.sev === 'error' || i.sev === 'warn' ? i.sev : 'info';
      const div = el('div', 'issue issue-' + sev);
      div.appendChild(el('span', null, ISSUE_GLYPH[sev]));
      if (sev !== 'warn') {
        div.appendChild(el('span', null, i.text));
        box.appendChild(div);
        return;
      }
      const col = el('div');
      col.style.cssText = 'display:flex;flex-direction:column;gap:4px;min-width:0;';
      col.appendChild(el('span', null, i.text));
      if (!needsTick(i)) {
        const sub = el('span', null, '儲存時會再請你確認賣超：確認後這個部位的成本基礎會被永久捨棄');
        sub.style.cssText = 'font-size:10px;color:var(--text-3);line-height:1.5;';
        col.appendChild(sub);
      } else {
        const key = editAckKey(i);
        const lab = el('label', 'edit-ack');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.id = 'edit-ack-' + idx;
        cb.checked = store[key] === true;
        cb.addEventListener('change', () => {
          if (cb.checked) store[key] = true; else delete store[key];
          if (onTick) onTick();
        });
        lab.appendChild(cb);
        lab.appendChild(el('span', null, '我了解，仍要儲存。'));
        col.appendChild(lab);
        if (store[key] !== true) allAcked = false;
      }
      div.appendChild(col);
      box.appendChild(div);
    });
    if (box.parentElement) box.parentElement.style.display = list.length ? '' : 'none';
    return allAcked;
  }

  /* DEF-013 (owner ruling 2026-09-24): 當沖 + 放空 together is a TW same-day short-then-cover
     and is allowed. The SAME sentence as the entry form's (input.js DAYTRADE_SHORT_NOTE): the
     tax rate that applies (當沖 outranks ETF — markets-and-fees.md, QA-19) and what the short
     declaration books. Display-only text; no figure is computed here. */
  const DAYTRADE_SHORT_NOTE = '當沖＋放空（先賣後買）：這筆賣出的證交稅以當沖 0.15% 計算'
    + '（當沖優先於 ETF 的 0.1%）；同時宣告為放空，賣出股數可以超過持股，帳本以收到的價金作為'
    + '空單成本，買回回補時才結算已實現損益 — 請記得登錄同日買回的那一筆。';

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
    /* DEF-013: the two sell-side flags, correctable here like every other field. Both are
       persisted columns; the PUT preserves them when absent, so they are always sent. */
    const flag = (id, label, on) => {
      const lab = el('label', 'hint daytrade-line');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.id = id;
      cb.checked = !!on;
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(' ' + label));
      return { line: lab, box: cb };
    };
    const fDaytrade = flag('edit-daytrade', '當沖（賣出稅 0.15%）', t.daytrade);
    const fShort = flag('edit-short', '放空（賣出股數可超過持股）', t.short_sale);
    const flags = el('div');
    flags.style.cssText = 'display:flex;flex-direction:column;gap:4px;';
    flags.appendChild(fDaytrade.line);
    flags.appendChild(fShort.line);
    const comboNote = el('div', 'hint edit-combo-note', DAYTRADE_SHORT_NOTE);
    flags.appendChild(comboNote);
    const syncFlags = () => {
      const sell = fSide.value === 'sell';
      /* Both are SELL-side (input.js setSide): on a buy they are hidden AND cleared, so a
         stale tick can never exempt a buy from anything or reprice it. */
      fDaytrade.line.hidden = !sell;
      fShort.line.hidden = !sell;
      if (!sell) { fDaytrade.box.checked = false; fShort.box.checked = false; }
      comboNote.hidden = !(sell && fDaytrade.box.checked && fShort.box.checked);
    };
    /* audit M6: track whether the user explicitly edited fee/tax. When a core field
       (帳戶/代號/方向/股數/價格/日期/當沖) changes and fee/tax are NOT dirty, the modal
       re-fetches the computed fee/tax from the entry preview seam and the backend
       recomputes them from the new account's rule set + regenerates the snapshot. An
       explicit fee/tax edit is honored as an override (snapshot tagged override:true). */
    let feeDirty = false;
    let taxDirty = false;
    fFee.addEventListener('input', () => { feeDirty = true; });
    fTax.addEventListener('input', () => { taxDirty = true; });
    /* DEF-007 / I-12: every fee/tax figure that lands after an await goes through
       window.pdField.autoFill (format.js) — it replaces only what the PAGE put in the field.
       The stored figures were put there by the page when the dialog opened, so they are
       recorded as the page's own; anything the owner types since is theirs and stays. */
    fFee.dataset.pdAuto = fFee.value;
    fTax.dataset.pdAuto = fTax.value;
    const issueBox = el('div', 'issues');
    /* The panel's state: the latest translated findings, the ticks, and whether a preview is
       in flight — 儲存 waits for the latest one, the entry door's `hasServer` rule, so a save
       can never outrun the warning its own values raise. */
    const panel = { list: [], acks: {}, pending: false };
    let ui0 = null;
    function gate() {
      const allAcked = renderEditIssues(issueBox, panel.list, panel.acks, gate);
      if (ui0 && !ui0.ok.classList.contains('is-busy')) {
        ui0.ok.disabled = panel.pending || !allAcked;
      }
    }
    /* recompute() is fired by every core-field change, so responses can land out of order; a
       monotonic token keeps the LAST request the one on screen. Harmless for fee/tax (one
       number replacing another) and load-bearing for the issue list, where a stale response
       would leave a warning standing for a value the user has already changed.
       `writeFees` is false on OPEN and on the 放空 toggle: neither is a fee-bearing change, and
       the PUT keeps the stored fee/tax when no core field moved — so those previews carry the
       field values as overrides and never write the engine's figure over a broker-supplied
       fee (data-and-pricing.md — a supplied fee is the money that actually left). */
    let previewSeq = 0;
    async function recompute(writeFees) {
      if (!window.pdApi) return;
      const seq = ++previewSeq;
      const symbol = fSym.value.trim();
      const body = {
        account_id: fAcc.value, symbol: symbol, side: fSide.value,
        date: fDate.value, shares: fShares.value || '0', price: fPrice.value || '0',
        daytrade: fDaytrade.box.checked, short_sale: fShort.box.checked,
        replaces_txn_id: t.id,
      };
      if (!writeFees) {
        if (fFee.value.trim() !== '') body.fee_override = fFee.value.trim();
        if (fTax.value.trim() !== '') body.tax_override = fTax.value.trim();
      }
      panel.pending = true;
      gate();
      try {
        const resp = await window.pdApi.post('/api/input/manual/preview', body);
        if (seq !== previewSeq) return;
        if (writeFees && resp && !feeDirty && resp.fee !== undefined) {
          window.pdField.autoFill(fFee, resp.fee);
        }
        if (writeFees && resp && !taxDirty && resp.tax !== undefined) {
          window.pdField.autoFill(fTax, resp.tax);
        }
        panel.list = ((resp && resp.issues) || [])
          .map((i) => editIssueWire(i, symbol)).filter((i) => i !== null);
      } catch (e) {
        /* best-effort; the save-time validation is the source of truth. Clear the panel so a
           warning from an earlier value never outlives the request that replaced it. */
        if (seq !== previewSeq) return;
        panel.list = [];
      }
      panel.pending = false;
      gate();
    }
    [fShares, fPrice, fDate].forEach((n) => n.addEventListener('input', () => recompute(true)));
    [fAcc, fSym, fSide, fDate].forEach((n) => n.addEventListener('change', () => {
      syncFlags();
      recompute(true);
    }));
    fDaytrade.box.addEventListener('change', () => { syncFlags(); recompute(true); });
    fShort.box.addEventListener('change', () => { syncFlags(); recompute(false); });
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
      /* 還原自動 hands the field back to the page: what is in it now is declared the page's
         own, so the recomputed figure may replace it (pdField.autoFill's own rule). */
      btn.addEventListener('click', () => {
        clearDirty();
        field.dataset.pdAuto = field.value;
        recompute(true);
      });
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
    ui0 = editModal('編輯交易 #' + t.id + ' — ' + t.symbol, [
      ['日期', fDate], ['帳戶', fAcc], ['代號', fSym], ['方向', fSide],
      ['股數', fShares], ['價格', fPrice], ['', flags],
      ['手續費', feeCell], ['交易稅', taxCell], ['備註', fNote],
      ['', issueBox],
      ['', warn],
    ], async (ui) => {
      /* values ride through as the user's raw STRINGS; the backend parses Decimal */
      await putWithAckGuard('/api/ledgers/transactions/' + t.id, (ack) => ({
        account_id: fAcc.value, symbol: fSym.value.trim(), side: fSide.value,
        date: fDate.value, shares: fShares.value, price: fPrice.value,
        fee: fFee.value, tax: fTax.value, note: fNote.value.trim() || null,
        fee_overridden: feeDirty, tax_overridden: taxDirty,
        daytrade: fDaytrade.box.checked, short_sale: fShort.box.checked,
        ack_oversell: ack,
      }), '編輯', ui);
    });
    syncFlags();
    /* DEF-042: the findings of the row AS IT STANDS are shown on open — the entry door never
       lets 確認寫入 go without a preview, and a row that already carries a warning (a future
       date, a date before its opening) must be acknowledged before it is saved again, not
       only after a field is touched. Fees are NOT written back on open (see recompute). */
    recompute(false);
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
    /* DEF-061: the correction runs the entry doors' ONE validator (validate_dividend), so the
       dialog shows its findings live and — the ⑪a contract, as the trade dialog does — holds
       儲存 until every warning is ticked; hard findings are refused by the PUT itself.
       A blank 預扣 / 淨額 is sent as null ("not stated", like a blank CSV cell), so the
       dividend model derives it exactly as the entry door would. */
    const blankNull = (n) => (n.value.trim() === '' ? null : n.value);
    const bodyOf = (ack) => ({
      account_id: fAcc.value, symbol: fSym.value.trim(), date: fDate.value,
      type: fType.value, gross: fGross.value || '0',
      withhold: blankNull(fWh), net: blankNull(fNet),
      reinvest_shares: blankNull(fReSh), reinvest_price: blankNull(fRePx),
      ack_oversell: ack,
    });
    const issueBox = el('div', 'issues div-edit-issues');
    const panel = { list: [], acks: {}, pending: false };
    let ui0 = null;
    function gate() {
      const allAcked = renderEditIssues(issueBox, panel.list, panel.acks, gate);
      if (ui0 && !ui0.ok.classList.contains('is-busy')) {
        ui0.ok.disabled = panel.pending || !allAcked;
      }
    }
    let previewSeq = 0;
    async function recompute() {
      if (!window.pdApi) return;
      const seq = ++previewSeq;
      const body = bodyOf(false);
      delete body.ack_oversell;
      body.replaces_div_id = d.id;
      panel.pending = true;
      gate();
      try {
        const resp = await window.pdApi.post('/api/ledgers/dividends/preview', body);
        if (seq !== previewSeq) return;
        panel.list = (resp && resp.issues) || [];
      } catch (e) {
        /* best-effort; the PUT's validation is the authority. A failed preview clears the
           panel so an earlier value's warning never outlives the request that replaced it. */
        if (seq !== previewSeq) return;
        panel.list = [];
      }
      panel.pending = false;
      gate();
    }
    [fGross, fWh, fNet, fReSh, fRePx, fSym].forEach((n) => n.addEventListener('input', recompute));
    [fAcc, fType, fDate].forEach((n) => n.addEventListener('change', recompute));
    ui0 = editModal('編輯股利 #' + d.id + ' — ' + d.symbol, [
      ['日期', fDate], ['帳戶', fAcc], ['代號', fSym], ['類型', fType],
      ['總額', fGross], ['預扣', fWh], ['淨額', fNet],
      ['再投資股數（DRIP／配股）', fReSh], ['再投資價格（DRIP）', fRePx],
      ['', issueBox],
    ], async (ui) => {
      await putWithAckGuard('/api/ledgers/dividends/' + d.id, bodyOf, '編輯', ui);
    });
    /* The row's findings AS IT STANDS, on open (DEF-042's rule for the trade dialog). */
    recompute();
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
    ], async (ui) => {
      await putWithAckGuard('/api/ledgers/fx/' + x.id, () => ({
        account_id: fAcc.value, date: fDate.value,
        from_ccy: fFromC.value, from_amt: fFromA.value,
        to_ccy: fToC.value, to_amt: fToA.value,
      }), '編輯', ui);
    });
  }

  function editOpen(o) {
    const fShares = inp(o.shares, 'number', 'any');
    const fAvg = inp(o.avg, 'number', 'any');
    const fDate = inp(o.date, 'date');
    editModal('編輯期初 — ' + o.symbol + '（' + acctZh(o.account_id) + '）', [
      ['股數', fShares], ['原始均價', fAvg], ['建檔日', fDate],
    ], async (ui) => {
      await putWithAckGuard(
        '/api/ledgers/openings/' + encodeURIComponent(o.account_id) + '/' + encodeURIComponent(o.symbol),
        (ack) => ({ shares: fShares.value, avg: fAvg.value, date: fDate.value, ack_oversell: ack }),
        '編輯', ui);
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

  /* DEF-056 (owner ruling 2026-09-24): a row dated after TODAY does not count yet — not in
     the holdings, 總報酬, XIRR or the cash pools — until its own date. The server decides
     (`counts_from`, set only while the row is still ahead, cut on the SERVER's clock — never
     this browser's), and the row says so, so a ledger row that the dashboard does not reflect
     explains itself. The date shown is the day it starts to count: for a dividend that is its
     effective date (the payment date; a 配股's ex-date). Rendered UNDER the date so the date
     column stays one token wide. The same badge text is in web/cash.js (its own IIFE). */
  function futureBadge(countsFrom) {
    const b = el('span', 'badge badge-stale-mini ledger-future', '未來日期：' + countsFrom + ' 起計入');
    b.title = '日期晚於今天：到 ' + countsFrom + ' 才計入持股、成本、總報酬、XIRR 與資金餘額';
    return b;
  }
  function dateCell(dateIso, countsFrom) {
    const td = el('td', 'num', f.date(dateIso));
    if (countsFrom) {
      const line = el('div', 'ledger-future-line');
      line.appendChild(futureBadge(countsFrom));
      td.appendChild(line);
    }
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
      tr.appendChild(dateCell(t.date, t.counts_from));
      tr.appendChild(el('td', 'col-text', acctZh(t.account_id)));
      tr.appendChild(symCell(t.symbol, t.name));
      const tdSide = el('td', 'col-text');
      tdSide.appendChild(dirChip(t.side));
      /* DEF-013 (owner ruling 2026-09-24): the two flags that change what this row books are
         visible on the row — both read off the row's own persisted columns (the wire's
         `daytrade` / `short_sale`), the same chips the CSV / AI previews already show. */
      if (t.daytrade) {
        const dt = el('span', 'dir-chip dir-daytrade tx-flag-daytrade', '當沖');
        dt.title = '當日沖銷：賣出證交稅以當沖稅率 0.15% 計（優先於 ETF 的 0.1%）';
        tdSide.appendChild(dt);
      }
      if (t.short_sale) {
        const sh = el('span', 'dir-chip dir-short tx-flag-short', '放空');
        sh.title = '宣告放空：帳本以收到的價金作為空單成本，買回回補時結算已實現損益';
        tdSide.appendChild(sh);
      }
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
        () => { delTxWithWarning(t); }));

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
      tr.appendChild(dateCell(d.date, d.counts_from));
      tr.appendChild(el('td', 'col-text', acctZh(d.account_id)));
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
      tr.appendChild(dateCell(x.date, x.counts_from));
      tr.appendChild(el('td', 'col-text', acctZh(x.account_id)));
      tr.appendChild(el('td', 'num', f.money(x.from_amt, x.from_ccy) + ' ' + x.from_ccy));
      tr.appendChild(el('td', 'num', f.money(x.to_amt, x.to_ccy) + ' ' + x.to_ccy));
      /* Finding 9: the implied rate is computed by the backend (from_amount / to_amount,
         home units per one foreign unit) — never recomputed here. Fixed 4 dp (M3-08): a
         rate is not money, and `rate()`'s magnitude switch gave this one column two
         precisions (4.6000 beside 28.00). */
      /* L6 (2026-09-16): the backend quotes the pair the conventional way (rate ≥ 1) and
         names the direction in implied_unit_ccy / implied_per_ccy — the row no longer
         inverts with the side that was sold. */
      tr.appendChild(el('td', 'num', '1 ' + (x.implied_unit_ccy || x.to_ccy) + ' = ' +
        f.rateExact(x.implied_rate, 4) + ' ' + (x.implied_per_ccy || x.from_ccy)));
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
      tr.appendChild(el('td', 'col-text', acctZh(o.account_id)));
      tr.appendChild(symCell(o.symbol));
      tr.appendChild(el('td', 'num', f.shares(o.shares)));
      tr.appendChild(el('td', 'num', f.price(o.avg, o.ccy)));
      tr.appendChild(el('td', 'num', f.money(o.total, o.ccy) + ' ' + o.ccy));
      tr.appendChild(dateCell(o.date, o.counts_from));
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
    /* DEF-020: the linked reorganisation fee is edited HERE, with the action it belongs
       to. The value is the server's Decimal string, shown verbatim; blank removes the fee;
       the server re-dates and re-accounts it with the action under one commit. */
    const fee = a.reorg_fee || null;
    const fFee = inp(fee ? fee.amount : '', 'number', 'any');
    const feeLabel = '重組費用（' + (fee ? fee.ccy + '，留空即刪除該筆出金' : '選填，記為出金')
      + '）';
    /* DEF-060: the SPINOFF child's seed price. Blank = leave it as it is (it still MOVES
       with a new date / child); a value re-types it — written only where the child has no
       price that day, and the server's sentence says what happened (afterActionEdit). */
    const fSeed = inp('', 'number', 'any');
    const warn = el('div', 'hint',
      '⚠ 修改公司行動會重算歷史：股數、均價、報酬率與價格基準都會依新內容重建（原值已存入稽核軌跡）。'
      + '若這筆行動同時登錄在多個帳戶，代號／日期／類型／比例必須整組一致，否則會被擋下。'
      + '重組費用會隨行動的日期與帳戶一併更新；分拆登錄時寫入的子公司起始價也會隨日期／子公司代號搬移'
      + '（新日期已有正式報價時不寫入）。');
    editModal('編輯公司行動 #' + a.id + ' — ' + a.symbol, [
      ['日期', fDate], ['帳戶', fAcc], ['類型', fKind],
      ['來源代號', fFrom], ['目的代號', fTo],
      ['每持有（股）', fRatioFrom], ['變成／換得（股）', fRatioTo],
      ['成本分攤比例（分拆用）', fCarry],
      ['子公司起始價（分拆用，選填；留空＝不變更）', fSeed],
      [feeLabel, fFee], ['備註', fNote],
      ['', warn],
    ], async (ui) => {
      const body = {
        account_id: fAcc.value, date: fDate.value, kind: fKind.value,
        from_symbol: fFrom.value.trim(), to_symbol: fTo.value.trim(),
        ratio_to: fRatioTo.value, ratio_from: fRatioFrom.value,
        cost_carry: fCarry.value.trim() || null,
        note: fNote.value.trim() || null, ack_warnings: false,
        /* Always a string on this door: '' = remove the linked fee, a value = sync it.
           (Omitting the key would mean "leave it alone", which an edit form cannot mean.) */
        reorg_fee: fFee.value.trim(),
        reorg_fee_ccy: fee ? fee.ccy : (a.ccy || null),
        /* DEF-060: only a SPINOFF takes one (the server refuses it on another kind). */
        to_symbol_price: fKind.value === 'SPINOFF' ? (fSeed.value.trim() || null) : null,
      };
      const put = async () => {
        const resp = await window.pdApi.put('/api/ledgers/corporate-actions/' + a.id, body);
        afterActionEdit(resp);
        return resp;
      };
      /* `warnings_unacknowledged` is this door's own ack code (not in ACK_CODES: the ack
         rides as a body flag, not a param), so it opens its dialog here — the modal closed
         first, same order as ackConfirm. */
      /* DEF-049: the edit is REPLAYED server-side like every other correction — a 422
         `oversell` (a later sell the old ratio/date covered) opens the shared ackConfirm;
         確認 sets the ack in the BODY (like ack_warnings) and re-sends. */
      const replayAck = (err, prog) => ackConfirm(err, '儲存', (param) => {
        body[param] = true;
        runMutation('編輯', put, replayAck);
      }, prog ? prog.drop : ui.dismiss);
      await saveFromModal(ui, '編輯', put, (err) => {
        if (replayAck(err)) return true;
        if (!(err && err.status === 422 && err.code === 'warnings_unacknowledged')) return false;
        ui.dismiss();
        window.confirmDialog({
          title: '公司行動警告確認', body: err.message,
          confirmLabel: '我了解，仍要儲存', danger: true,
          onConfirm: () => { body.ack_warnings = true; runMutation('編輯', put, replayAck); },
        });
        return true;
      });
    });
  }

  /* DEF-060: what the edit did to the SPINOFF child's seed price — the server's sentences,
     verbatim: moved / re-typed (ok), or not written / not moved and why (warn). */
  function afterActionEdit(resp) {
    if (!resp || !window.toast) return;
    const mv = resp.child_price_move;
    if (mv) {
      window.toast(mv.moved ? '子公司起始價已更新' : '子公司起始價未搬移',
        mv.moved ? 'ok' : 'warn', mv.message || '');
    } else if (resp.child_priced) {
      window.toast('已寫入子公司起始價', 'ok', resp.child_priced);
    }
    const skip = resp.child_price_skipped;
    if (skip && !(mv && mv.message && mv.message.indexOf(skip.reason) !== -1)) {
      window.toast('子公司起始價未寫入', 'warn', skip.reason);
    }
  }

  /* F-32: deleting ONE row of a multi-account set is refused server-side, because the
     price correction is global (split_factor's dedup key carries no account) while the
     share correction is per account — and the drawer would print ✓ 對帳一致 over the
     mismatch. The refusal is turned into a guided action here rather than a dead end:
     the owner who really does want the action gone is offered the WHOLE SET. */
  /* What the delete response says happened to the band (DEF-021): a toast when the band
     did NOT come back, with the server's reason — the owner then knows to visit 觀察清單. */
  function afterActionDelete(resp) {
    if (!resp || !window.toast) return;
    const br = resp.band_restore;
    if (br) {
      const band = br.band || {};
      if (br.restored) {
        window.toast('目標價已移回 ' + band.from_symbol, 'ok',
          '換股已刪除，' + band.to_symbol + ' 上的目標價（登錄換股時搬過去的）已原值移回');
      } else {
        window.toast('目標價未移回', 'warn', br.reason || '');
      }
    }
    /* I-6 (F-3): the target weight's outcome, reported the same way — the delete response
       has carried it since F-3; nothing on the page read it. */
    const wr = resp.weight_restore;
    if (wr) {
      if (wr.restored) {
        window.toast('目標權重已移回 ' + wr.from_symbol, 'ok',
          '換股已刪除，' + wr.to_symbol + ' 上的目標權重（' + f.pct(wr.weight)
          + '，登錄換股時搬過去的）已原值移回');
      } else {
        window.toast('目標權重未移回', 'warn', wr.reason || '');
      }
    }
    /* DEF-040 (owner ruling 2026-09-24): the SPINOFF child's seed price, reported the same way
       — removed with the action, or kept with the server's reason (a real quote replaced it,
       or another action still creates that child on that day). */
    const cp = resp.child_price_restore;
    if (cp) {
      if (cp.restored) {
        window.toast('已移除子公司起始價', 'ok',
          '分拆已刪除，' + cp.symbol + ' 在 ' + f.date(cp.date) + ' 的起始價（登錄時寫入的）一併移除');
      } else {
        window.toast('子公司起始價未移除', 'warn', cp.reason || '');
      }
    }
  }

  /* The sentence(s) the delete confirm adds for a corporate action: which fee leaves with
     it (DEF-020) and whether its band comes back (DEF-021) — both read off the row the
     server sent, through the SAME predicate the delete will run. */
  function actionDeleteConsequences(a) {
    let s = '';
    if (a.reorg_fee) {
      const fee = a.reorg_fee;
      s += '　會一併刪除這筆行動的重組費用出金：' + f.date(fee.date) + ' '
        + fee.kind_label + ' ' + f.money(fee.amount, fee.ccy) + ' ' + fee.ccy
        + '（' + acctZh(fee.account_id) + '）。';
    }
    if (a.band_move) {
      const m = a.band_move;
      const br = a.band_restore || {};
      const parts = [];
      /* DEF-039: a band level restated by a split ratio is an unrounded quotient — shown
         through f.exact (the D44 formatter for a level the owner may act on), not verbatim. */
      if (m.target_low !== null && m.target_low !== undefined) parts.push('下限 ' + f.exact(m.target_low));
      if (m.target_high !== null && m.target_high !== undefined) parts.push('上限 ' + f.exact(m.target_high));
      if (br.restorable) {
        s += '　登錄時移到 ' + m.to_symbol + ' 的目標價（' + parts.join('、')
          + '）未再改動，會自動移回 ' + m.from_symbol + '。';
      } else {
        s += '　目標價不會移回：' + (br.reason || '登錄時搬移的目標價已不在原狀') + '。';
      }
    }
    /* I-6 (F-3): the target weight, through the same predicate the delete runs
       (`weight_restore` on the list row) — the confirm used to be silent about it. */
    if (a.weight_move) {
      const w = a.weight_move;
      const wr = a.weight_restore || {};
      if (wr.restorable) {
        s += '　登錄時移到 ' + w.to_symbol + ' 的目標權重（' + f.pct(w.weight)
          + '）未再改動，會自動移回 ' + w.from_symbol + '。';
      } else {
        s += '　目標權重不會移回：' + (wr.reason || '登錄時搬移的目標權重已不在原狀') + '。';
      }
    }
    /* DEF-040: the SPINOFF child's seed price, through the same predicate the delete runs
       (`child_price_restore` on the list row, computed over the whole set). */
    const cp = a.child_price_restore;
    if (cp) {
      if (cp.restorable) {
        s += '　登錄時寫入的子公司起始價（' + cp.symbol + '，' + f.date(cp.date)
          + '）尚未被正式報價覆蓋，會一併移除。';
      } else {
        s += '　子公司起始價不會移除：' + (cp.reason || '') + '。';
      }
    }
    return s;
  }

  function delAction(a) {
    /* DEF-049: both deletes are REPLAYED server-side like every other ledger delete — a
       split whose shares a later sell used answers 422 `oversell`, naming each sell. That
       opens the same ackConfirm dialog the other rows use; 確認 re-sends carrying every ack
       given so far (`acks`), 取消 sends nothing. */
    const ackQs = (acks) => Object.keys(acks).map((k) => k + '=true');
    const delOne = (acks) => async () => {
      const q = ackQs(acks);
      afterActionDelete(await window.pdApi.del('/api/ledgers/corporate-actions/' + a.id
        + (q.length ? '?' + q.join('&') : '')));
    };
    const delSet = (acks) => async () => afterActionDelete(
      await window.pdApi.del('/api/ledgers/corporate-actions/set'
        + '?from_symbol=' + encodeURIComponent(a.symbol)
        + '&date=' + encodeURIComponent(a.date)
        + '&kind=' + encodeURIComponent(a.kind)
        + ackQs(acks).map((p) => '&' + p).join('')));
    const acked = (send, acks) => (err, prog) => ackConfirm(err, '刪除', (param) => {
      const next = Object.assign({}, acks, { [param]: true });
      runMutation('刪除', send(next), acked(send, next));
    }, prog.drop);
    window.confirmDialog({
      title: '刪除公司行動',
      body: '確定刪除 ' + a.symbol + ' 在 ' + a.date + ' 的' + a.kind_label
        + '？股數、成本與價格基準都會由帳本重建。' + actionDeleteConsequences(a),
      confirmLabel: '刪除', danger: true,
      onConfirm: () => runMutation('刪除', delOne({}), (err, prog) => {
        if (!(err && err.status === 422 && err.code === 'partial_action_set_change')) {
          return acked(delOne, {})(err, prog);
        }
        prog.drop();
        window.confirmDialog({
          title: '這筆行動屬於多帳戶整組紀錄',
          body: err.message + '　要整組一起刪除嗎？',
          confirmLabel: '整組刪除', danger: true,
          onConfirm: () => runMutation('刪除', delSet({}), acked(delSet, {})),
        });
        return true;
      }),
    });
  }

  function renderActions() {
    const tbody = $('#action-body');
    if (!tbody) return;
    tbody.replaceChildren();
    byKeyword(D.actions).forEach((a) => {
      const tr = el('tr');
      /* DEF-023: addressable from the dashboard banner and the drawer (deep link). */
      tr.dataset.actionId = a.id;
      /* DEF-056: the SERVER's counts_from (cut on the app clock), rendered like every other
         ledger's 「未來日期：YYYY-MM-DD 起計入」 — never decided here. */
      tr.appendChild(dateCell(a.date, a.counts_from));
      tr.appendChild(el('td', 'col-text', acctZh(a.account_id)));
      const tdKind = el('td', 'col-text', a.kind_label);
      if (a.unapplied) {
        /* DEF-023: the replay refused THIS row — marked on the row itself, not only in
           the XIRR tooltip and the drawer. The reason is the server's sentence. */
        tr.classList.add('row-stale');
        const ub = el('span', 'badge badge-missing ledger-unapplied', '未套用');
        ub.title = a.unapplied.reason || '';
        tdKind.appendChild(document.createTextNode(' '));
        tdKind.appendChild(ub);
        tr.title = '公司行動未套用（待釐清）：' + (a.unapplied.reason || '');
      }
      tr.appendChild(tdKind);
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
      /* DEF-052: the linked reorganisation fee is part of THIS event, so the row shows it —
         until now only the delete and edit dialogs read `reorg_fee`. The amount is the
         server's Decimal string through the display formatter (never computed here), the
         same 「50 TWD」 the save toast and the delete confirm print. */
      const tdNote = el('td', 'col-text');
      if (a.reorg_fee) {
        const fee = a.reorg_fee;
        tdNote.appendChild(el('span', 'badge ledger-reorg-fee',
          '重組費用 ' + f.money(fee.amount, fee.ccy) + ' ' + fee.ccy));
        if (a.note) tdNote.appendChild(document.createTextNode(' '));
      }
      if (a.note) tdNote.appendChild(document.createTextNode(a.note));
      tr.appendChild(tdNote);
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
      tr.appendChild(dateCell(m.date, m.counts_from));
      tr.appendChild(el('td', 'col-text', acctZh(m.account_id)));
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
      /* DEF-072 class: with an account chip active the owner is reading THAT account's
         actions, so the new one prefills it — it fell on the first /api/accounts entry, the
         same wrong-account landing door 2 had. 全部 keeps the form's own default. */
      window.pdCorpActionForm.open({
        account_id: state.account !== 'all' ? state.account : undefined,
        onSaved: () => boot(),
      });
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
    /* openings has no date filter server-side (GET /api/ledgers/openings takes no from/to:
       an opening is a per-(帳戶,代號) balance, not a dated flow). The SCREEN says so as of
       M3-06 — #lopen-date-note in the 期初庫存 pane + the filter tooltips — because until
       then a 2026 range emptied the other five tabs while this one stayed full and the
       tooltip called the range global. If a from/to is ever added to that endpoint, drop
       both this branch and that note together. */
    if (kind !== 'open') {
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

  const LOADERS = {
    tx: loadTx, div: loadDiv, fx: loadFx, open: loadOpen, action: loadAction, cash: loadCash,
  };
  /* Panes whose data is behind the current filter state (L16). A pane is fetched when it is
     the active tab, when a commit lands in it, or when its tab is next shown — never all
     six at once for one filter keystroke. */
  const dirty = {};
  function ensureTab(t) {
    if (!dirty[t]) return;
    dirty[t] = false;
    loadFailToasted = false;
    LOADERS[t]();
  }

  /* Refresh the visible pane (+ `also`, the pane a commit just wrote to, so the flash lands
     on a fresh row) and mark the rest dirty for their next tab switch. */
  async function loadAll(also) {
    loadFailToasted = false;
    const now = [activeTab];
    if (also && LOADERS[also] && also !== activeTab) now.push(also);
    TABS.forEach((t) => { dirty[t] = now.indexOf(t) < 0; });
    await Promise.all(now.map((t) => LOADERS[t]()));
  }

  /* FU-D45: live-refresh seam for the input panes (trades.html). After ANY successful
     input commit, input.js calls this to re-fetch the ledger tables IN PLACE (current
     account/date filters + page offsets preserved; NO page reload). The ACTIVE pane and the
     committed ledger's pane refresh now; the other panes are marked dirty and fetch on their
     next tab switch (L16) — one filter state, nothing shown stale. `kind` is the input
     ledger kind (transactions / dividends / fx / openings / corporate_actions / cash). A
     plain function assignment (not addEventListener), so repeated calls can never double-bind. */
  const KIND_TAB = {
    transactions: 'tx', dividends: 'div', fx: 'fx', openings: 'open',
    corporate_actions: 'action', cash: 'cash',
  };
  window.pdLedgerRefresh = (kind) => loadAll(kind ? KIND_TAB[kind] : undefined);

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
      /* Ids only: every chip is named by acctZh(id). The English `a.name` used to ride
         along here unread — the one context-list `.name` the widened guard in
         tests/contract/test_account_name_single_source.py would otherwise have to excuse. */
      accountList = ((resp && resp.accounts) || []).map((a) => ({ id: a.account_id }));
    } catch (err) {
      accountList = [];
    }
  }

  /* DEF-023: `trades.html?ledger=action&action_id=N` (from the dashboard's 未套用 banner
     and the drawer) opens the 公司行動 tab and flashes that row. Falls back to matching
     the (account_id, symbol, date, kind) tuple when no id was given, and to the tab alone
     when the row is not on the current page (a toast says so). Runs ONCE after boot. */
  async function openDeepLink() {
    let q;
    try { q = new URLSearchParams(window.location.search); } catch (e) { return; }
    const tab = q.get('ledger');
    if (!tab || !LOADERS[tab]) return;
    /* Not dirty BEFORE the click: the click handler's ensureTab would otherwise start a
       second, un-awaited fetch of the same pane beside the one awaited here. */
    dirty[tab] = false;
    const btn = document.getElementById('tab-' + TAB_IDS[tab]);
    if (btn) btn.click();
    activeTab = tab;
    await LOADERS[tab]();
    if (tab !== 'action') return;
    const id = q.get('action_id');
    const want = { account_id: q.get('account_id'), symbol: q.get('symbol'),
                   date: q.get('date'), kind: q.get('kind') };
    const hit = (D.actions || []).find((a) => id
      ? String(a.id) === id
      : (a.account_id === want.account_id && a.symbol === want.symbol
         && a.date === want.date && (!want.kind || a.kind === want.kind)));
    const row = hit && document.querySelector('#action-body tr[data-action-id="' + hit.id + '"]');
    if (!row) {
      if (window.toast) window.toast('找不到該筆公司行動列', 'warn', '可能不在目前頁面，請翻頁或調整篩選');
      return;
    }
    row.classList.add('ledger-added-row');
    if (row.scrollIntoView) row.scrollIntoView({ block: 'center' });
  }

  async function boot() {
    await loadAccounts();
    initFilters();
    await loadAll();
  }

  initFilters();        // account chip bar (DOM only, no data) — safe before boot
  if (ownsTabs) showTab('tx');
  boot().then(openDeepLink);
})();
