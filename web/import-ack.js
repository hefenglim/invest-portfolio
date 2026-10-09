/* portfolio-dash — 匯入警告逐列確認：every bulk import door, ONE mechanism (DEF-027 → DEF-025)

   Three doors commit several rows at once through /api/import/commit: the 券商對帳單 door
   (broker-import.js), the CSV 匯入 door and the AI 輸入 door (input.js). They used to ask three
   different questions. The broker door listed every warning row one by one (DEF-027); the CSV
   door asked 「部分列有警告（如賣超）— 確認後一併寫入？」 and the AI door 「AI 草稿中部分列有
   警告 — 確認後一併寫入？」 — one click under which a PRE-TICKED 賣超 row was written and its cost
   basis permanently discarded (DEF-025; owner ruling 2026-09-24: 比照手動輸入 — the row is not
   pre-ticked, the dialog names it and says 「成本基礎會被永久捨棄」, and only a row the owner
   ticks AND confirms is written).

   So there is ONE flow, and it lives here:
   1. commit UNACKNOWLEDGED;
   2. on 422 `warnings_unacknowledged` / `oversell_rows_unacknowledged`, fetch the SAME text
      through /api/import/preview and list every warning row among those being written —
      代號／日期／類型／股數, the server's own sentence, and for a 賣超 the consequence in
      words — each with a checkbox that starts UNTICKED;
   3. re-commit with `select` = the rows without a warning + the ticked warning rows, and
      `ack_rows` = the ticked rows. The SERVER refuses a 賣超 row whose index is not in
      `ack_rows` (`oversell_rows_unacknowledged`), so no page can write one by sending a flag.

   window.pdImportAck = { isOversell, labelFromPreview, dialog, commit }

   Money is never computed here: every figure shown is a string the API produced. */
(function () {
  'use strict';

  /* The one finding whose acknowledgement discards a cost basis (賣超 is STICKY). */
  const OVERSELL = 'sell_exceeds_holdings';
  const ACK_CODES = ['warnings_unacknowledged', 'oversell_rows_unacknowledged'];
  const SIDE_ZH = { buy: '買入', sell: '賣出' };

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  };
  const fmtShares = (v) => (window.fmt && v !== '' && v !== null && v !== undefined
    ? window.fmt.shares(v) : (v === null || v === undefined ? '' : String(v)));

  /* A preview row ({n, status, reason, kinds, data}) carries its findings' KINDS (I-13) —
     read them, never the server's sentence. */
  function isOversell(row) {
    return !!row && Array.isArray(row.kinds) && row.kinds.indexOf(OVERSELL) !== -1;
  }

  /* The dialog's four display fields, from the preview row's own `data` (the server's parse
     of the row). A door that knows better (the broker report) passes its own labeller. */
  function labelFromPreview(row) {
    const d = (row && row.data) || {};
    const side = String(d.side || '').toLowerCase();
    return {
      symbol: d.symbol || d.from_symbol || '',
      date: d.trade_date || d.date || '',
      type: SIDE_ZH[side] || d.type || d.kind || '',
      shares: d.quantity !== undefined ? fmtShares(d.quantity) : '',
    };
  }

  /* The acknowledgement dialog: every warning row, one per line, with the server's own
     message and — for a 賣超 — the consequence in words; each checkbox starts UNTICKED.
     opts = { title, warns: [{ n, label: {symbol,date,type,shares}, reason, kinds }] }.
     Resolves to { mode: 'ack', keep: Set<n> } | { mode: 'skip' } | { mode: 'cancel' }.
     Built on the app's own modal classes, never on window.confirm — one line of text
     cannot list ten rows. */
  function dialog(opts) {
    const warns = opts.warns || [];
    const oversold = warns.filter((w) => (w.kinds || []).indexOf(OVERSELL) !== -1);
    return new Promise((resolve) => {
      const backdrop = el('div', 'modal-backdrop');
      const modal = el('div', 'modal');
      modal.style.width = 'min(680px, calc(100vw - 40px))';
      const head = el('div', 'modal-head');
      head.appendChild(el('h3', 'modal-title',
        '匯入警告確認' + (opts.title ? ' —— ' + opts.title : '')));
      const x = el('button', 'modal-close', '✕');
      x.type = 'button';
      head.appendChild(x);
      modal.appendChild(head);
      const body = el('div', 'modal-body');
      body.appendChild(el('div', null,
        '⚠ 以下 ' + warns.length + ' 列在寫入前檢核時有警告。預設不寫入；只有你勾選並確認的列才會帶著警告寫入，'
        + '其餘列照常寫入。'));
      if (oversold.length) {
        /* Names every 賣超 row and the consequence — the owner's ruling, verbatim in intent. */
        const names = oversold.map((w) => '第 ' + (w.n + 1) + ' 列'
          + (w.label && w.label.symbol ? ' ' + w.label.symbol : '')).join('、');
        const warn = el('div', 'hint imp-oversell-note',
          names + ' 是賣超：勾選並確認後，這個部位的成本基礎會被永久捨棄，並在儀表板標示為待釐清；'
          + '之後再買回也不會還原。如果是分割／換股／分拆造成的，請先取消、補登公司行動，再重新匯入。');
        body.appendChild(warn);
      }
      const wrap = el('div', 'table-wrap');
      const table = el('table', 'data');
      const thead = el('thead');
      const hr = el('tr');
      hr.appendChild(el('th', null, ''));
      [['列', 'num'], ['代號', 'col-text'], ['日期', 'col-text'], ['類型', 'col-text'],
        ['股數', 'num'], ['警告', 'col-text']]
        .forEach(([label, cls]) => hr.appendChild(el('th', cls, label)));
      thead.appendChild(hr);
      table.appendChild(thead);
      const tbody = el('tbody');
      const keep = new Set();
      const ok = el('button', 'btn btn-danger', '寫入勾選的警告列');
      ok.type = 'button';
      ok.disabled = true;
      warns.forEach((w) => {
        const lab = w.label || {};
        const tr = el('tr');
        const td = el('td');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = false;                            // never pre-ticked
        /* `bk-warn-tick` is the broker door's original class (its e2e flows drive it). */
        cb.className = 'imp-warn-tick bk-warn-tick';
        cb.dataset.n = String(w.n);
        cb.addEventListener('change', () => {
          if (cb.checked) keep.add(w.n); else keep.delete(w.n);
          ok.disabled = keep.size === 0;
        });
        td.appendChild(cb);
        tr.appendChild(td);
        tr.appendChild(el('td', 'num', '#' + (w.n + 1)));
        tr.appendChild(el('td', 'col-text', lab.symbol || '—'));
        tr.appendChild(el('td', 'col-text', lab.date || '—'));
        tr.appendChild(el('td', 'col-text', lab.type || '—'));
        tr.appendChild(el('td', 'num', lab.shares || '—'));
        const why = el('td', 'col-text');
        why.appendChild(el('div', null, w.reason || '有警告'));
        if ((w.kinds || []).indexOf(OVERSELL) !== -1) {
          why.appendChild(el('div', 'hint',
            '→ 賣超：確認後這個部位的成本基礎會被永久捨棄（待釐清），之後再買回也不會還原'));
        }
        tr.appendChild(why);
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      wrap.appendChild(table);
      body.appendChild(wrap);
      modal.appendChild(body);
      const foot = el('div', 'modal-foot');
      const cancel = el('button', 'btn', '取消，停在這一步');
      cancel.type = 'button';
      const skip = el('button', 'btn', '略過所有警告列，只寫入其他列');
      skip.type = 'button';
      foot.appendChild(cancel);
      foot.appendChild(skip);
      foot.appendChild(ok);
      modal.appendChild(foot);
      backdrop.appendChild(modal);
      const finish = (decision) => { backdrop.remove(); resolve(decision); };
      x.addEventListener('click', () => finish({ mode: 'cancel' }));
      cancel.addEventListener('click', () => finish({ mode: 'cancel' }));
      backdrop.addEventListener('click', (e) => { if (e.target === backdrop) finish({ mode: 'cancel' }); });
      skip.addEventListener('click', () => finish({ mode: 'skip' }));
      ok.addEventListener('click', () => finish({ mode: 'ack', keep: new Set(keep) }));
      document.body.appendChild(backdrop);
    });
  }

  /* One kind, committed. opts = {
       body:        the commit body WITHOUT the ack fields (kind, csv_text, select?, …),
       previewBody: what /api/import/preview needs to re-derive the SAME rows (defaults to
                    { kind, csv_text } of `body`; carry date_format / pending_actions_csv),
       title:       the dialog's subtitle (the kind in words),
       label:       optional (previewRow) → { symbol, date, type, shares }.
     }
     Resolves to the commit response, { cancelled: true }, or — when every row the owner
     wanted carried a warning and none was ticked — { written: 0, skipped: <n> }. */
  async function commit(opts) {
    const api = window.pdApi;
    const base = Object.assign({}, opts.body);
    delete base.ack_warnings;
    delete base.ack_rows;
    let select = Array.isArray(base.select) ? base.select.slice() : null;
    delete base.select;
    const labelOf = opts.label || labelFromPreview;
    let ack = false;
    let ackRows = [];
    for (;;) {
      const body = Object.assign({}, base, { ack_warnings: ack });
      if (select !== null) body.select = select;
      if (ackRows.length) body.ack_rows = ackRows;
      try {
        return await api.post('/api/import/commit', body);
      } catch (err) {
        if (ack || !(err && err.status === 422 && ACK_CODES.indexOf(err.code) !== -1)) throw err;
        const pvBody = opts.previewBody || { kind: base.kind, csv_text: base.csv_text };
        const pv = await api.post('/api/import/preview', pvBody);
        const rows = (pv && pv.rows) || [];
        const chosen = select === null ? null : new Set(select);
        const warns = rows.filter((r) => r.status === 'warn' && (chosen === null || chosen.has(r.n)));
        if (!warns.length) {
          /* Every warning row is already deselected — nothing to confirm. The ack only
             releases the server's whole-file gate; the rows it would cover are not sent. */
          ack = true;
          continue;
        }
        const decision = await dialog({
          title: opts.title,
          warns: warns.map((w) => ({ n: w.n, label: labelOf(w), reason: w.reason, kinds: w.kinds || [] })),
        });
        if (decision.mode === 'cancel') return { cancelled: true };
        const warnIdx = new Set(warns.map((w) => w.n));
        const wanted = chosen === null ? rows.map((r) => r.n) : Array.from(chosen);
        select = wanted.filter((n) => !warnIdx.has(n) || (decision.mode === 'ack' && decision.keep.has(n)));
        ackRows = decision.mode === 'ack' ? Array.from(decision.keep) : [];
        ack = true;
        if (!select.length) return { written: 0, skipped: wanted.length };
      }
    }
  }

  window.pdImportAck = {
    isOversell: isOversell,
    labelFromPreview: labelFromPreview,
    dialog: dialog,
    commit: commit,
  };
})();
