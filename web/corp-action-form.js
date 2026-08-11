/* portfolio-dash — 公司行動補登表單（spec 2026-08-06 §6.7 · W7）

   ONE form, THREE doors (§6.7). The doors are placed where the owner already is when
   they need it, because corporate actions are rare and REACTIVE — nobody wakes up
   intending to record one, they hit a wall or they are reconciling a statement:

     door 1  the 賣超 confirm dialog  (web/input.js → pdCorpActionForm.open)
     door 2  the symbol drawer footer (web/detail.js → the same call; see below)
     door 3  the 5th ledger tab       (web/ledger.js → the same call)

   This file is the single implementation; the doors differ only in what they prefill.
   Door 2 is a one-liner against the SAME entry point:

       window.pdCorpActionForm.open({
         account_id: <the drawer's account, or '' for the first holder>,
         from_symbol: <the drawer's symbol>,
         reason: '此標的的對帳結果為 ⚠ 對帳不一致',
         onSaved: function () { <re-render the drawer> }
       });

   MONEY DISCIPLINE (CLAUDE.md invariant 3). Every share count, average, cost total and
   ratio in the preview arrives from POST /api/ledgers/corporate-actions/preview as a
   Decimal STRING and is passed to window.fmt for DISPLAY only. This module performs no
   arithmetic on any of them — including the 成本不變 ✓ verdict, which is the server's
   `conserved` boolean, not a comparison done here. A conservation check computed in the
   browser would be a second opinion about the accounting model, which is exactly the
   "two numbers on one screen" failure §5.1 calls the worst kind.

   THE RATIO IS TWO INTEGER BOXES, phrased the way brokers announce it (§3.1(ii)) — and
   this form is an AFFORDANCE, NOT THE GUARD. The rounded quotient is still reachable
   through the CSV importer and the API; E6/E6a in data_ingestion/validate.py is what
   actually stops it. The boxes merely make the right thing the easy thing. */
(function () {
  'use strict';

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  };
  const api = () => window.pdApi;
  const f = () => window.fmt;

  /* The component carries its OWN styling, scoped under `.ca-modal`, injected once.
     Not a shared stylesheet edit: `.input` / `.select` / `.field` / `.form-grid` are
     defined in input.css / settings.css / trades.css, and index.html (door 2's page)
     loads NEITHER — a form that depends on whichever page happens to host it renders
     unstyled on exactly the door that is added last. Every rule below is scoped, so it
     cannot leak onto a page that does define those classes. Colours are the app's own
     tokens, so both themes follow automatically. */
  const STYLE_ID = 'pd-ca-style';
  const CSS = `
.ca-modal { max-width: 720px; width: min(720px, calc(100vw - 24px)); }
.ca-modal .field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.ca-modal label { font-size: 11px; color: var(--text-2); }
.ca-modal .hint { font-size: 10px; color: var(--text-3); line-height: 1.5; }
.ca-modal .input, .ca-modal .select {
  background: var(--panel-2); border: 1px solid var(--border); color: var(--text);
  border-radius: var(--radius-sm); padding: 6px 8px; font-size: 12px;
  font-family: var(--font-ui); width: 100%; max-width: 100%; box-sizing: border-box; }
.ca-modal .ca-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px; }
.ca-modal .ca-kinds { display: flex; flex-direction: column; gap: 6px; }
.ca-modal .ca-kind { text-align: left; background: var(--panel-2);
  border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 8px 10px;
  cursor: pointer; display: flex; flex-direction: column; gap: 2px; color: var(--text); }
.ca-modal .ca-kind.active { border-color: var(--accent); background: var(--accent-soft); }
.ca-modal .ca-kind-label { font-size: 12px; }
.ca-modal .ca-kind-hint { font-size: 10px; color: var(--text-3); }
.ca-modal .ca-ratio { display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
  font-size: 12px; }
.ca-modal .ca-ratio .input { width: 84px; }
.ca-modal .ca-preview-title { font-size: 11px; color: var(--text-2); font-weight: 700;
  border-top: 1px solid var(--border); padding-top: 10px; }
.ca-modal .ca-preview { display: flex; flex-direction: column; gap: 8px; }
.ca-modal .ca-scope { font-size: 11px; color: var(--accent);
  background: var(--accent-soft); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 6px 8px; }
.ca-modal .ca-reason { font-size: 11px; color: var(--amber);
  background: var(--amber-soft); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 8px 10px; }
.ca-modal .ca-acct { border: 1px solid var(--border); border-radius: var(--radius-sm);
  padding: 8px; overflow-x: auto; }
.ca-modal .ca-acct-name { font-size: 11px; color: var(--text-2); margin-bottom: 4px; }
.ca-modal .ca-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.ca-modal .ca-table th { color: var(--text-3); font-weight: 500; text-align: right;
  padding: 2px 6px; }
.ca-modal .ca-table th.col-text, .ca-modal .ca-table td.col-text { text-align: left; }
.ca-modal .ca-table td { padding: 2px 6px; text-align: right; }
.ca-modal .ca-table .num { font-family: var(--font-num);
  font-variant-numeric: tabular-nums; }
.ca-modal .ca-conserve { font-size: 11px; padding: 4px 8px;
  border-radius: var(--radius-sm); }
.ca-modal .ca-conserve.ok { color: var(--ok); background: var(--ok-soft); }
.ca-modal .ca-conserve.bad { color: var(--up); background: var(--up-soft); }
.ca-modal .ca-unblock { font-size: 11px; color: var(--ok); background: var(--ok-soft);
  border-radius: var(--radius-sm); padding: 4px 8px; }
.ca-modal .ca-issue { font-size: 11px; padding: 6px 8px; border-radius: var(--radius-sm);
  display: flex; gap: 6px; align-items: flex-start; line-height: 1.6; }
.ca-modal .ca-issue-error { color: var(--up); background: var(--up-soft); }
.ca-modal .ca-issue-warn { color: var(--amber); background: var(--amber-soft); }
.ca-modal .ca-issue label { display: flex; gap: 6px; align-items: flex-start;
  color: inherit; font-size: 11px; cursor: pointer; }
@media (max-width: 640px) {
  .ca-modal .ca-grid { grid-template-columns: 1fr; }
}`;

  function ensureStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const s = document.createElement('style');
    s.id = STYLE_ID;
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  /* The three questions §6.7 mandates. The owner is NEVER asked to classify the event:
     SPLIT / EXCHANGE / SPINOFF is our vocabulary, not theirs — they are asked what the
     statement shows, in terms of the observable effect. */
  const KINDS = [
    { kind: 'SPLIT', label: '同一檔股票，股數變多或變少',
      verb: '變成', hint: '例如 1 股分割成 3 股，或 10 股併成 1 股' },
    { kind: 'EXCHANGE', label: '整個部位換成另一檔股票',
      verb: '換得', hint: '例如被併購，原持股全部換成另一檔股票' },
    { kind: 'SPINOFF', label: '原持股不變，另外多拿到一檔新股票',
      verb: '另外配發', hint: '例如子公司分拆上市，原本的股票還在' }
  ];

  /* ---------------------------------------------------------------- small helpers */

  function field(label, node, hint) {
    const w = el('div', 'field');
    w.appendChild(el('label', null, label));
    w.appendChild(node);
    if (hint) w.appendChild(el('span', 'hint', hint));
    return w;
  }

  function intBox(value) {
    const n = el('input', 'input ca-int');
    n.type = 'number';
    n.step = '1';
    n.min = '1';
    n.inputMode = 'numeric';
    n.value = value === undefined ? '' : String(value);
    /* Integers only — the two terms map straight onto ratio_from / ratio_to with no
       mental arithmetic and nothing to convert (§6.7). A pasted decimal is stripped
       here as a courtesy; the REAL rejection is E6/E6a's, server-side. */
    n.addEventListener('input', () => {
      const cleaned = n.value.replace(/[^0-9]/g, '');
      if (cleaned !== n.value) n.value = cleaned;
    });
    return n;
  }

  function todayIso() {
    const d = new Date();
    const p = (x) => (x < 10 ? '0' : '') + x;
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  }

  /* ------------------------------------------------------------------ the preview */

  /* Renders the conservation law, made visible (§6.7). Three jobs: show the resulting
     share count so the owner can check it against the statement, show the average
     correcting itself, and state 成本不變 explicitly. Every value is a server string. */
  function renderPreview(host, data, state) {
    host.replaceChildren();
    if (!data) {
      host.appendChild(el('div', 'hint', '填入代號、日期與比例後，這裡會顯示試算結果。'));
      return;
    }
    const fm = f();
    const ccy = data.ccy || '';

    if (data.rows_to_write > 1) {
      /* D28: the owner arrives to fix ONE account and E13 writes N rows. That is
         correct — the action really did happen in every account at once — but it must
         not be a surprise, so the full scope is stated BEFORE they commit. */
      const banner = el('div', 'ca-scope');
      banner.appendChild(el('span', null,
        '這筆行動同時影響以下 ' + data.rows_to_write +
        ' 個帳戶（公司行動對每個持有帳戶一體適用）'));
      host.appendChild(banner);
    }

    (data.accounts || []).forEach((acct) => {
      const card = el('div', 'ca-acct');
      card.appendChild(el('div', 'ca-acct-name', acct.account || acct.account_id));
      const table = el('table', 'ca-table');
      const thead = el('thead');
      const hr = el('tr');
      ['', '代號', '股數', '均價', '成本總額'].forEach((h, i) => {
        hr.appendChild(el('th', i >= 2 ? 'num' : 'col-text', h));
      });
      thead.appendChild(hr);
      table.appendChild(thead);
      const tbody = el('tbody');
      const line = (label, rows) => {
        rows.forEach((r, i) => {
          const tr = el('tr');
          tr.appendChild(el('td', 'col-text', i === 0 ? label : ''));
          tr.appendChild(el('td', 'col-text num', r.symbol));
          tr.appendChild(el('td', 'num', fm.shares(r.shares)));
          tr.appendChild(el('td', 'num', fm.price(r.avg, ccy)));
          tr.appendChild(el('td', 'num', fm.money(r.cost_total, ccy)));
          tbody.appendChild(tr);
        });
      };
      line('行動前', acct.before || []);
      line('行動後', acct.after || []);
      table.appendChild(tbody);
      card.appendChild(table);
      const foot = el('div', 'ca-conserve ' + (acct.conserved ? 'ok' : 'bad'),
        (acct.conserved ? '✓ ' : '⚠ ') +
        ((acct.after || []).length > 1 ? '成本合計' : '成本') +
        (acct.conserved ? '不變 ' : '改變了 ') +
        fm.money(acct.cost_before, ccy) + ' → ' + fm.money(acct.cost_after, ccy));
      card.appendChild(foot);
      host.appendChild(card);
    });

    if (data.rows_to_write > 1) {
      const total = el('div', 'ca-conserve ' + (data.conserved ? 'ok' : 'bad'),
        (data.conserved ? '✓ 全部帳戶成本合計不變 ' : '⚠ 全部帳戶成本合計改變了 ') +
        fm.money(data.cost_before_total, ccy) + ' → ' +
        fm.money(data.cost_after_total, ccy));
      host.appendChild(total);
    }

    /* Naming the untouched account is not clutter — it is how the owner can tell the
       system understood their ledger rather than merely applied a rule (§6.7). */
    (data.not_affected || []).forEach((a) => {
      host.appendChild(el('div', 'hint',
        '不受影響：' + (a.account || a.account_id) + '（' + a.reason + '）'));
    });

    /* Said BEFORE saving — this is the sentence door 1 exists to produce. */
    (data.unblocks || []).forEach((u) => {
      host.appendChild(el('div', 'ca-unblock',
        '✓ 這筆行動會讓 ' + u.date + ' 的 ' + f().shares(u.shares) + ' 股賣出通過檢查'
        + '（目前為賣超）'));
    });

    (data.issues || []).forEach((i) => {
      const box = el('div', 'ca-issue ca-issue-' + (i.sev === 'error' ? 'error' : 'warn'));
      box.appendChild(el('span', null, i.sev === 'error' ? '✕' : '⚠'));
      box.appendChild(el('span', null, i.text));
      host.appendChild(box);
    });
    if (data.needs_confirm && !data.blocking) {
      const wrap = el('div', 'ca-issue ca-issue-warn');
      const lab = el('label');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = !!state.acked;
      cb.addEventListener('change', () => { state.acked = cb.checked; state.sync(); });
      lab.appendChild(cb);
      lab.appendChild(el('span', null, ' 我已確認上述警告，仍要登錄這筆公司行動。'));
      wrap.appendChild(lab);
      host.appendChild(wrap);
    }
  }

  /* ----------------------------------------------------------------- the follow-ups */

  /* §6.7's four follow-ups, offered ON SAVE so they are not forgotten. Left to the
     owner's memory, §3.2's cash-in-lieu rule and §3.3's fee stay theoretical. */
  function offerFollowUps(resp, preview, body) {
    const fractions = (preview && preview.fractions) || [];
    const unpriced = (resp && resp.unpriced_symbols) || [];

    if (unpriced.length && window.confirmDialog) {
      /* §6.6: returns.py is all-or-nothing on the terminal value — ONE unpriced holding
         blanks the WHOLE portfolio's XIRR, not just this symbol's. So the fetch is
         offered here, with the cause still on screen, rather than left to be discovered
         as a blank headline number with no visible reason. */
      window.confirmDialog({
        title: '需要更新報價',
        body: unpriced.join('、') + ' 目前沒有價格紀錄。只要有一檔持倉沒有價格，'
          + '整個投資組合的年化報酬率（XIRR）就會顯示不出來。要現在更新報價嗎？',
        confirmLabel: '立即更新報價',
        onConfirm: async () => {
          try {
            await api().post('/api/actions/refresh-quotes', {});
            if (window.toast) window.toast('已排入報價更新', 'ok', unpriced.join('、'));
          } catch (e) {
            if (window.toast) window.toast('報價更新失敗', 'fail', e && e.message);
          }
        }
      });
      return;                       // one dialog at a time; the fraction offer follows
    }

    if (fractions.length && window.confirmDialog) {
      const fr = fractions[0];
      window.confirmDialog({
        title: '產生零股',
        body: '產生 ' + f().shares(fr.shares) + ' 股 ' + fr.symbol + ' 零股，'
          + '券商通常折現（cash in lieu）。要現在補登這筆賣出嗎？'
          + '（股數已帶入，價格請填實際收到的金額 ÷ 股數）',
        confirmLabel: '補登零股賣出',
        onConfirm: () => {
          if (window.pdPrefillManualSell) {
            window.pdPrefillManualSell({
              account_id: fr.account_id, symbol: fr.symbol,
              shares: fr.shares, date: body.date
            });
          } else {
            window.location.href = 'trades.html';
          }
        }
      });
    }
  }

  /* §3.3 / D12: the reorganisation fee is a WITHDRAW cash movement, and the caveat is
     shown inline rather than filed in a manual nobody reads at entry time. */
  async function bookReorgFee(body, amount, ccy) {
    if (!amount) return;
    try {
      await api().post('/api/cash/movements', {
        account_id: body.account_id, date: body.date, kind: 'WITHDRAW',
        ccy: ccy, amount: amount,
        note: '重組費用 ' + body.from_symbol + ' ' + body.date
      });
      if (window.toast) window.toast('已登錄重組費用', 'ok', '記為現金支出，不計入成本基礎');
    } catch (e) {
      if (window.toast) window.toast('重組費用登錄失敗', 'fail', e && e.message);
    }
  }

  /* ---------------------------------------------------------------------- the form */

  function open(prefill) {
    prefill = prefill || {};
    if (!api()) return;
    const state = { kind: prefill.kind || 'SPLIT', acked: false, preview: null,
                    sync: () => {} };

    const backdrop = el('div', 'modal-backdrop');
    const modal = el('div', 'modal ca-modal');
    const head = el('div', 'modal-head');
    head.appendChild(el('h3', 'modal-title', '補登公司行動'));
    const close = el('button', 'modal-close', '✕');
    close.type = 'button';
    head.appendChild(close);
    modal.appendChild(head);

    const bodyEl = el('div', 'modal-body');
    if (prefill.reason) {
      const note = el('div', 'ca-reason');
      note.appendChild(el('span', null, prefill.reason));
      bodyEl.appendChild(note);
    }

    /* --- question 1: what does the statement show? (never "pick a kind") --- */
    const kindWrap = el('div', 'field');
    kindWrap.appendChild(el('label', null, '對帳單上這筆行動的結果是？'));
    const kindBox = el('div', 'ca-kinds');
    const kindBtns = {};
    KINDS.forEach((k) => {
      const b = el('button', 'ca-kind' + (k.kind === state.kind ? ' active' : ''));
      b.type = 'button';
      b.appendChild(el('span', 'ca-kind-label', k.label));
      b.appendChild(el('span', 'ca-kind-hint', k.hint));
      b.addEventListener('click', () => {
        state.kind = k.kind;
        Object.keys(kindBtns).forEach((x) => kindBtns[x].classList.toggle('active', x === k.kind));
        applyKind();
        schedule();
      });
      kindBtns[k.kind] = b;
      kindBox.appendChild(b);
    });
    kindWrap.appendChild(kindBox);
    bodyEl.appendChild(kindWrap);

    /* --- the identifying fields --- */
    const fAcct = el('select', 'select');
    const fSym = el('input', 'input');
    fSym.spellcheck = false;
    fSym.value = prefill.from_symbol || '';
    const fDate = el('input', 'input');
    fDate.type = 'date';
    fDate.value = prefill.date || todayIso();
    /* Door 1 bounds the date by the last reconciling date and the sell's trade date —
       the window in which the missing action must have happened. */
    if (prefill.date_min) fDate.min = prefill.date_min;
    if (prefill.date_max) fDate.max = prefill.date_max;

    const grid = el('div', 'ca-grid');
    grid.appendChild(field('帳戶', fAcct, '公司行動會自動套用到所有持有這檔股票的帳戶'));
    grid.appendChild(field('代號', fSym));
    grid.appendChild(field('行動日期', fDate));
    bodyEl.appendChild(grid);

    /* --- the ratio: two integer boxes, phrased the way brokers announce it --- */
    const fFrom = intBox(prefill.ratio_from || 1);
    const fTo = intBox(prefill.ratio_to || '');
    const fToSym = el('input', 'input');
    fToSym.spellcheck = false;
    fToSym.style.width = '120px';
    fToSym.placeholder = '新代號';
    const ratioLine = el('div', 'ca-ratio');
    ratioLine.appendChild(el('span', null, '每持有'));
    ratioLine.appendChild(fFrom);
    ratioLine.appendChild(el('span', null, '股 →'));
    const verbEl = el('span', null, '變成');
    ratioLine.appendChild(verbEl);
    ratioLine.appendChild(fTo);
    ratioLine.appendChild(el('span', null, '股'));
    ratioLine.appendChild(fToSym);
    bodyEl.appendChild(field('比例', ratioLine,
      '請照對帳單上的兩個整數填寫（例如 3 換 1、1 換 20、2 換 7），不要填算好的小數'));

    const fCarry = el('input', 'input');
    fCarry.type = 'number';
    fCarry.step = '0.0001';
    fCarry.min = '0';
    fCarry.max = '1';
    fCarry.style.width = '120px';
    const carryField = field('成本分攤比例', fCarry,
      '公司公告中移轉給子公司的成本佔比（例如 58.31% 就填 0.5831）');
    bodyEl.appendChild(carryField);

    const fNote = el('input', 'input');
    bodyEl.appendChild(field('備註', fNote));

    /* --- §3.3 / D12: the reorganisation fee, offered inline with its caveat --- */
    const fFee = el('input', 'input');
    fFee.type = 'number';
    fFee.step = '0.01';
    fFee.min = '0';
    fFee.style.width = '140px';
    bodyEl.appendChild(field('重組費用（選填）', fFee,
      '若對帳單有收取重組／換股手續費，會記成一筆現金支出（WITHDRAW）。'
      + '⚠ 它不會併入成本基礎，也不會進入 XIRR 的現金流序列'));

    /* --- the preview: ALWAYS ON, and the most important element on this form --- */
    const previewHost = el('div', 'ca-preview');
    bodyEl.appendChild(el('div', 'ca-preview-title', '試算（成本守恆檢查）'));
    bodyEl.appendChild(previewHost);
    modal.appendChild(bodyEl);

    const foot = el('div', 'modal-foot');
    const cancel = el('button', 'btn', '取消');
    cancel.type = 'button';
    const save = el('button', 'btn btn-primary', '登錄公司行動');
    save.type = 'button';
    save.disabled = true;
    foot.appendChild(cancel);
    foot.appendChild(save);
    modal.appendChild(foot);
    backdrop.appendChild(modal);

    const dismiss = () => backdrop.remove();
    close.addEventListener('click', dismiss);
    cancel.addEventListener('click', dismiss);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) dismiss(); });

    function applyKind() {
      const meta = KINDS.filter((k) => k.kind === state.kind)[0] || KINDS[0];
      verbEl.textContent = meta.verb;
      const needsTo = state.kind !== 'SPLIT';
      fToSym.hidden = !needsTo;
      carryField.hidden = state.kind !== 'SPINOFF';
    }

    function requestBody() {
      const toSym = state.kind === 'SPLIT'
        ? fSym.value.trim() : fToSym.value.trim();
      return {
        account_id: fAcct.value,
        date: fDate.value,
        kind: state.kind,
        from_symbol: fSym.value.trim(),
        to_symbol: toSym,
        ratio_to: fTo.value,
        ratio_from: fFrom.value,
        cost_carry: state.kind === 'SPINOFF' ? fCarry.value : null,
        note: fNote.value.trim() || null
      };
    }

    function ready() {
      const b = requestBody();
      if (!b.account_id || !b.from_symbol || !b.date) return false;
      if (!b.ratio_to || !b.ratio_from) return false;
      return !(state.kind !== 'SPLIT' && !b.to_symbol);
    }

    function syncSave() {
      const p = state.preview;
      save.disabled = !p || p.blocking || (p.needs_confirm && !state.acked);
    }
    state.sync = syncSave;

    let timer = null;
    function schedule() {
      if (timer) clearTimeout(timer);
      timer = setTimeout(runPreview, 250);
    }
    async function runPreview() {
      if (!ready()) {
        state.preview = null;
        renderPreview(previewHost, null, state);
        syncSave();
        return;
      }
      try {
        state.preview = await api().post(
          '/api/ledgers/corporate-actions/preview', requestBody());
      } catch (err) {
        state.preview = null;
        previewHost.replaceChildren();
        previewHost.appendChild(el('div', 'ca-issue ca-issue-error',
          (err && err.message) || '試算失敗'));
        syncSave();
        return;
      }
      renderPreview(previewHost, state.preview, state);
      syncSave();
    }

    [fSym, fToSym, fTo, fFrom, fCarry].forEach((n) => n.addEventListener('input', schedule));
    [fAcct, fDate].forEach((n) => n.addEventListener('change', schedule));

    save.addEventListener('click', async () => {
      const body = requestBody();
      body.ack_warnings = !!state.acked;
      const restore = window.pdBusy ? window.pdBusy(save, '寫入中…') : () => {};
      try {
        const resp = await api().post('/api/ledgers/corporate-actions', body);
        restore();
        dismiss();
        if (window.toast) {
          window.toast('公司行動已登錄', 'ok',
            '寫入 ' + resp.written + ' 筆（' + (resp.accounts || []).join('、') + '）'
            + (resp.prices_restated ? '；已重算 ' + resp.prices_restated + ' 筆價格' : ''));
        }
        await bookReorgFee(body, fFee.value.trim(),
          (state.preview && state.preview.ccy) || '');
        if (window.pdLedgerRefresh) { try { await window.pdLedgerRefresh(); } catch (e) { /* noop */ } }
        if (prefill.onSaved) prefill.onSaved(resp);
        offerFollowUps(resp, state.preview, body);
      } catch (err) {
        restore();
        if (window.toast) window.toast((err && err.message) || '登錄失敗', 'fail', err && err.code);
      }
    });

    ensureStyles();
    document.body.appendChild(backdrop);
    applyKind();
    renderPreview(previewHost, null, state);

    /* Accounts last: the form is already usable and the list only fills the dropdown. */
    api().get('/api/accounts').then((resp) => {
      const list = (resp && resp.accounts) || [];
      fAcct.replaceChildren();
      list.forEach((a) => {
        const o = el('option', null,
          window.pdNames ? window.pdNames.account(a.account_id) : a.account_id);
        o.value = a.account_id;
        if (a.account_id === prefill.account_id) o.selected = true;
        fAcct.appendChild(o);
      });
      if (prefill.account_id && !list.some((a) => a.account_id === prefill.account_id)) {
        const o = el('option', null, prefill.account_id);
        o.value = prefill.account_id;
        o.selected = true;
        fAcct.appendChild(o);
      }
      schedule();
    }).catch(() => { schedule(); });
  }

  window.pdCorpActionForm = { open: open };
})();
