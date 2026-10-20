/* portfolio-dash — 收件匣 › 待確認到期 (options spec §7.2, OPT-D13; wired to /api/option-expiry).

   The most frequent option chore: a contract whose expiry has passed while the BOOK still
   holds it. The server computes the list on read (like the rebate inbox) and writes nothing
   until the owner says so; its only state is the skip list. One item = one account × contract.

   Every sentence is the server's: `text` (with the account as an {account:<id>} token, which
   web/api.js resolves), `premium_text`, `remedy`. Nothing here forms a figure: 口數 and the
   premium arrive as strings and are printed as they came.

   Actions (spec §7.2):
     · 確認到期作廢 — POST /api/option-expiry/confirm {account_id, symbol} → {ok, txn_id}; the
       server writes the §4.1 price-0 row through the manual door's own close-row path;
     · 改登錄被指派／履約 — the manual form in option mode, prefilled (a sold contract was
       ASSIGNED, a bought one EXERCISED — the §4.1 pairing);
     · 略過 / 取消略過 — POST /skip, /unskip;
     · 全部確認 — the in-page confirm dialog, then POST /confirm-all; every item is validated
       on its own and the result is reported per item.
   An item that is not `confirmable` (a refused close on that contract, or an `expired` row
   that no longer equals the position) shows 未入帳（待釐清） and the server's remedy instead
   of 確認到期作廢. */
(function () {
  'use strict';
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const acctZh = (id) => (id ? (window.pdNames ? window.pdNames.account(id) : id) : '');
  const section = document.getElementById('expiry-section');
  if (!section) return;
  const list = $('#expiry-list');
  const countBadge = $('#expiry-count');
  const allBtn = $('#expiry-confirm-all');
  const results = $('#expiry-results');
  let items = [];

  function refreshBadge() {
    if (window.pdRefreshInboxBadge) window.pdRefreshInboxBadge();
  }

  function errText(err) {
    if (!err) return '寫入失敗';
    if (typeof err === 'string') return err;
    return err.message || '寫入失敗';
  }

  /* The manual form, option mode, prefilled (spec §7.2 「改登錄被指派／履約」). */
  function manualHref(it) {
    const q = new URLSearchParams();
    q.set('option_close', it.direction === 'long' ? 'exercised' : 'assigned');
    q.set('account_id', it.account_id);
    q.set('symbol', it.symbol);
    if (it.contracts) q.set('contracts', it.contracts);
    if (it.expiry) q.set('date', it.expiry);
    return 'trades.html?' + q.toString();
  }

  async function confirmOne(it, btn) {
    const restore = window.pdBusy ? window.pdBusy(btn, '寫入中…') : () => {};
    try {
      const resp = await window.pdApi.post('/api/option-expiry/confirm',
        { account_id: it.account_id, symbol: it.symbol });
      restore();
      if (window.toast) {
        window.toast('已登錄到期作廢', 'ok', (it.contract || it.symbol)
          + (resp && resp.txn_id !== undefined ? '（#' + resp.txn_id + '）' : ''));
      }
      await boot();
      refreshBadge();
    } catch (err) {
      restore();
      if (window.toast) window.toast(errText(err), 'fail', err && err.code);
    }
  }

  async function act(kind, it) {
    try {
      await window.pdApi.post('/api/option-expiry/' + kind,
        { account_id: it.account_id, symbol: it.symbol });
      if (window.toast) {
        window.toast(kind === 'skip' ? '已略過' : '已取消略過', 'ok',
          (it.contract || it.symbol) + '・' + acctZh(it.account_id));
      }
      await boot();
      refreshBadge();
    } catch (err) {
      if (window.toast) window.toast(errText(err), 'fail', err && err.code);
    }
  }

  /* 全部確認 — per-item results: the batch never hides a failure behind a total. */
  function renderResults(rows) {
    if (!results) return;
    results.replaceChildren();
    const failed = rows.filter((r) => !r.ok);
    if (!failed.length) { results.hidden = true; return; }
    results.hidden = false;
    results.appendChild(el('div', 'xp-results-head',
      failed.length + ' 筆未寫入（其餘已登錄）'));
    failed.forEach((r) => {
      const hit = items.find((i) => i.account_id === r.account_id && i.symbol === r.symbol);
      results.appendChild(el('div', 'xp-result-fail',
        '✕ ' + acctZh(r.account_id) + '・' + ((hit && hit.contract) || r.symbol) + '：'
        + errText(r.error)));
    });
  }

  function confirmAll() {
    const ready = items.filter((i) => i.confirmable);
    if (!ready.length) return;
    const run = async () => {
      const restore = window.pdBusy && allBtn ? window.pdBusy(allBtn, '寫入中…') : () => {};
      try {
        const resp = await window.pdApi.post('/api/option-expiry/confirm-all', {});
        restore();
        const rows = (resp && resp.results) || [];
        const okN = rows.filter((r) => r.ok).length;
        if (window.toast) {
          window.toast('已登錄到期作廢 ' + okN + ' 筆', okN === rows.length ? 'ok' : 'warn',
            okN === rows.length ? '' : (rows.length - okN) + ' 筆未寫入，原因列在下方');
        }
        await boot();
        renderResults(rows);
        refreshBadge();
      } catch (err) {
        restore();
        if (window.toast) window.toast(errText(err), 'fail', err && err.code);
      }
    };
    if (!window.confirmDialog) { run(); return; }
    window.confirmDialog({
      title: '全部確認到期作廢',
      body: '將為下列 ' + ready.length + ' 個已過到期日的部位各寫入一筆「到期作廢」（價格 0、'
        + '不收手續費與稅）：' + ready.map((i) => i.contract || i.symbol).join('、')
        + '。每一筆分別檢核，未通過的會列出原因。',
      confirmLabel: '全部確認',
      onConfirm: run,
    });
  }

  function render() {
    list.replaceChildren();
    if (countBadge) countBadge.textContent = String(items.length);
    const anyReady = items.some((i) => i.confirmable);
    if (allBtn) allBtn.hidden = !anyReady;
    if (!items.length) {
      list.appendChild(el('div', 'inbox-note',
        '目前沒有待確認的到期合約 — 到期日已過而帳本仍持有的選擇權部位會列在這裡。'));
      return;
    }
    items.forEach((it) => {
      const item = el('div', 'inbox-item xp-item');
      item.dataset.symbol = it.symbol;
      item.dataset.accountId = it.account_id;
      const main = el('div', 'inbox-main');
      main.appendChild(el('span', 'inbox-title', it.text));
      if (it.premium_text) main.appendChild(el('span', 'inbox-sub', it.premium_text));
      if (!it.confirmable) {
        const line = el('span', 'inbox-rule xp-unbooked');
        line.appendChild(el('span', 'badge badge-missing', '未入帳（待釐清）'));
        line.appendChild(document.createTextNode(' ' + (it.remedy || '')));
        main.appendChild(line);
      }
      item.appendChild(main);
      const acts = el('div', 'inbox-actions');
      if (it.confirmable) {
        const ok = el('button', 'btn btn-primary xp-confirm', '確認到期作廢');
        ok.type = 'button';
        ok.addEventListener('click', () => confirmOne(it, ok));
        acts.appendChild(ok);
      }
      const alt = el('a', 'btn xp-assign', '改登錄被指派／履約');
      alt.href = manualHref(it);
      acts.appendChild(alt);
      const sk = el('button', 'btn xp-skip', '略過');
      sk.type = 'button';
      sk.addEventListener('click', () => act('skip', it));
      acts.appendChild(sk);
      item.appendChild(acts);
      list.appendChild(item);
    });
  }

  function renderSkipped(rows) {
    const details = $('#expiry-skipped');
    const listEl = $('#expiry-skipped-list');
    if (!details || !listEl) return;
    const lbl = $('#expiry-skipped-label');
    if (lbl) lbl.textContent = '已略過（' + rows.length + '）';
    listEl.replaceChildren();
    if (!rows.length) { details.hidden = true; return; }
    details.hidden = false;
    rows.forEach((s) => {
      const row = el('div', 'sk-row');
      const main = el('div', 'sk-main');
      main.appendChild(el('span', null, (s.contract || s.symbol) + '（' + acctZh(s.account_id) + '）'));
      main.appendChild(el('span', 'sk-sub', '到期日 ' + (s.expiry || '—') + '・已略過於 '
        + (s.skipped_at ? String(s.skipped_at).slice(0, 10) : '—')));
      row.appendChild(main);
      const un = el('button', 'btn btn-sm xp-unskip', '取消略過');
      un.type = 'button';
      un.addEventListener('click', () => act('unskip', s));
      row.appendChild(un);
      listEl.appendChild(row);
    });
  }

  async function boot() {
    let resp;
    let skipped;
    try {
      [resp, skipped] = await Promise.all([
        window.pdApi.get('/api/option-expiry'),
        window.pdApi.get('/api/option-expiry/skipped'),
      ]);
    } catch (err) {
      items = [];
      render();
      renderSkipped([]);
      section.hidden = true;
      if (window.pdReportInboxCount) window.pdReportInboxCount('expiry', null);
      return;
    }
    items = (resp && resp.items) || [];
    /* PERF §13: the badge's 「待確認到期」 share is this list's length (the same detection). */
    if (window.pdReportInboxCount) window.pdReportInboxCount('expiry', items.length);
    const sk = (skipped && skipped.items) || [];
    /* The panel exists only where it has something to say — a ledger with no expired
       contract (every stock-only ledger) keeps the inbox page exactly as it was. */
    section.hidden = !items.length && !sk.length;
    render();
    renderSkipped(sk);
  }

  if (allBtn) allBtn.addEventListener('click', confirmAll);
  window.pdExpiryInboxReload = boot;
  boot();
})();
