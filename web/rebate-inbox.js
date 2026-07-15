/* portfolio-dash — 待確認退款（折讓款）收件匣 (wired to /api/rebates, Wave B / FE-D1).

   The TW 券商 charge-first model refunds part of each month's commission the FOLLOWING
   month. This panel lists the per-month FORECAST (Σ floor(fee × rate), computed server-side)
   as a pending-confirmation item. 確認入帳 opens a small prompt with the estimate PREFILLED
   into an EDITABLE amount field — the actual refund wins; the estimate is never money of
   record and NEVER enters cost / P&L / XIRR. On confirm the backend books a cash-pool credit
   (movement kind rebate). Amounts are Decimal STRINGS rendered via window.fmt — the frontend
   never computes money. Mirrors inbox.js (compute-on-read; nothing auto-written). */
(function () {
  'use strict';
  const f = window.fmt;
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const section = document.getElementById('rebate-section');
  if (!section) return;
  const list = $('#rebate-list');
  const countBadge = $('#rebate-count');

  function setCount(n) {
    if (countBadge) countBadge.textContent = String(n);
  }

  /* 買/賣 label from the Side wire value ("BUY"/"SELL"). */
  function sideLabel(side) {
    return side === 'SELL' ? '賣' : '買';
  }

  /* 標的 label — 名稱（代號）, or just the symbol when the name fell back to the symbol. */
  function targetLabel(t) {
    return (t.name && t.name !== t.symbol) ? (t.name + '（' + t.symbol + '）') : t.symbol;
  }

  /* A collapsed-by-default per-trade breakdown (§3.6). `d` is any object carrying
     { trades, fee_total, expected, ccy } — a pending month row OR a skipped row's detail.
     The 合計 footer mirrors the header numbers (Σ trade == month, enforced server-side).
     All money is a Decimal STRING rendered via window.fmt — the frontend never computes. */
  function buildDetail(d) {
    const wrap = el('div', 'rbt-detail');
    wrap.hidden = true;  /* collapsed by default; the [hidden] UA rule hides it */
    const table = el('table', 'rbt-table');
    const thead = el('thead');
    const htr = el('tr');
    [['日期', ''], ['標的', ''], ['買/賣', 'rbt-c'],
      ['手續費', 'rbt-num'], ['預估退款', 'rbt-num']].forEach((h) => {
      htr.appendChild(el('th', h[1] || null, h[0]));
    });
    thead.appendChild(htr);
    table.appendChild(thead);
    const tbody = el('tbody');
    (d.trades || []).forEach((t) => {
      const tr = el('tr');
      tr.appendChild(el('td', null, f.date(t.trade_date)));
      tr.appendChild(el('td', null, targetLabel(t)));
      tr.appendChild(el('td', 'rbt-c', sideLabel(t.side)));
      tr.appendChild(el('td', 'rbt-num', f.money(t.fee, d.ccy)));
      tr.appendChild(el('td', 'rbt-num', f.money(t.expected, d.ccy)));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    wrap.appendChild(el('div', 'rbt-foot',
      '合計 手續費 ' + f.money(d.fee_total, d.ccy) + ' ' + d.ccy +
      '・預估退款 ' + f.money(d.expected, d.ccy) + ' ' + d.ccy));
    return wrap;
  }

  /* A `.btn-sm` toggle (deliberately NOT `.btn-primary`, so the e2e's single-primary =
     確認入帳 invariant holds) that shows/hides a `buildDetail` block. */
  function makeToggle(detailEl) {
    const btn = el('button', 'btn btn-sm', '明細 ▸');
    btn.type = 'button';
    btn.setAttribute('aria-expanded', 'false');
    btn.addEventListener('click', () => {
      const show = detailEl.hidden;
      detailEl.hidden = !show;
      btn.textContent = show ? '明細 ▾' : '明細 ▸';
      btn.setAttribute('aria-expanded', show ? 'true' : 'false');
    });
    return btn;
  }

  /* skip / unskip — both recomputed server-side; refresh the panel after. */
  async function act(kind, r) {
    try {
      await window.pdApi.post('/api/rebates/' + kind,
        { account_id: r.account_id, month: r.month });
      if (window.toast) {
        window.toast(kind === 'skip' ? '已略過' : '已取消略過', 'ok',
          r.month + '・' + (r.account_name || ''));
      }
      await boot();
    } catch (err) {
      if (window.toast) window.toast((err && err.message) || '操作失敗', 'fail', err && err.code);
    }
  }

  /* 確認入帳: a small prompt (confirmDialog-style modal) with the estimate PREFILLED into an
     editable amount field. The user can override it (actual wins) before booking. */
  function openConfirm(r) {
    const backdrop = el('div', 'modal-backdrop');
    const modal = el('div', 'modal');
    const head = el('div', 'modal-head');
    head.appendChild(el('h3', 'modal-title', '確認折讓款入帳 — ' + r.month));
    const close = el('button', 'modal-close', '✕');
    close.type = 'button';
    head.appendChild(close);
    modal.appendChild(head);

    const body = el('div', 'modal-body');
    body.appendChild(el('div', null,
      r.account_name + '・' + r.month + '（當月 ' + r.trade_count + ' 筆交易）'));
    const fieldLabel = el('label', 'rb-cf-label', '實際入帳金額（' + r.ccy + '）');
    body.appendChild(fieldLabel);
    const inp = el('input', 'input');
    inp.type = 'number';
    inp.min = '0';
    inp.step = '1';
    inp.value = r.expected;   /* prefill with the estimate STRING (editable) */
    body.appendChild(inp);
    body.appendChild(el('div', 'inbox-rule',
      '以實際入帳金額為準,預估僅供參考;確認後記入該帳戶現金池,不影響成本。'));
    modal.appendChild(body);

    const foot = el('div', 'modal-foot');
    const cancel = el('button', 'btn', '取消');
    cancel.type = 'button';
    const ok = el('button', 'btn btn-primary', '確認入帳');
    ok.type = 'button';
    foot.appendChild(cancel);
    foot.appendChild(ok);
    modal.appendChild(foot);
    backdrop.appendChild(modal);

    const dismiss = () => backdrop.remove();
    close.addEventListener('click', dismiss);
    cancel.addEventListener('click', dismiss);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) dismiss(); });
    ok.addEventListener('click', () => {
      const amount = (inp.value || '').trim();
      if (!amount || Number(amount) <= 0) {
        if (window.toast) window.toast('請輸入大於 0 的金額', 'fail');
        return;
      }
      dismiss();
      doConfirm(r, amount);
    });
    document.body.appendChild(backdrop);
    setTimeout(() => { inp.focus(); inp.select(); }, 30);
  }

  async function doConfirm(r, amount) {
    try {
      const resp = await window.pdApi.post('/api/rebates/confirm',
        { account_id: r.account_id, month: r.month, amount: amount });
      if (window.toast) {
        window.toast('折讓款已入帳', 'ok',
          r.month + '・' + f.money(resp.amount, resp.ccy) + ' ' + resp.ccy + ' 已記入現金池');
      }
      await boot();
    } catch (err) {
      if (window.toast) window.toast((err && err.message) || '入帳失敗', 'fail', err && err.code);
    }
  }

  function render(items) {
    list.replaceChildren();
    setCount(items.length);
    if (!items.length) {
      list.appendChild(el('div', 'inbox-note',
        '目前沒有待確認的折讓款 — 有台股交易的月份,系統會在次月自動列出預估退款。'));
      return;
    }
    items.forEach((r) => {
      const item = el('div', 'inbox-item rbt-item');
      const main = el('div', 'inbox-main');
      main.appendChild(el('span', 'inbox-title',
        r.month + ' 折讓款（' + r.account_name + '）'));
      main.appendChild(el('span', 'inbox-sub',
        '當月 ' + r.trade_count + ' 筆交易・手續費合計 ' +
        f.money(r.fee_total, r.ccy) + ' ' + r.ccy + ' → 預估退款 ' +
        f.money(r.expected, r.ccy) + ' ' + r.ccy + '（不計入成本）'));
      main.appendChild(el('span', 'inbox-rule',
        '台股先收後退:預估僅供參考,確認後以實際金額記入該帳戶現金池,不計入成本／損益。'));
      item.appendChild(main);
      const acts = el('div', 'inbox-actions');
      const ok = el('button', 'btn btn-primary', '確認入帳');
      ok.type = 'button';
      ok.addEventListener('click', () => openConfirm(r));
      acts.appendChild(ok);
      /* 明細 toggle (per-trade §3.6 breakdown), collapsed by default. */
      let detail = null;
      if (r.trades && r.trades.length) {
        detail = buildDetail(r);
        acts.appendChild(makeToggle(detail));
      }
      const sk = el('button', 'btn', '略過');
      sk.type = 'button';
      sk.addEventListener('click', () => act('skip', r));
      acts.appendChild(sk);
      item.appendChild(acts);
      if (detail) item.appendChild(detail);
      list.appendChild(item);
    });
  }

  function renderSkipped(items) {
    const details = $('#rebate-skipped');
    const listEl = $('#rebate-skipped-list');
    if (!details || !listEl) return;
    const lbl = $('#rebate-skipped-label');
    if (lbl) lbl.textContent = '已略過（' + items.length + '）';
    listEl.replaceChildren();
    if (!items.length) { details.hidden = true; return; }
    details.hidden = false;
    items.forEach((s) => {
      const row = el('div', 'sk-row');
      const main = el('div', 'sk-main');
      main.appendChild(el('span', null, s.month + '（' + s.account_name + '）'));
      let sub = '已略過於 ' + (s.skipped_at ? String(s.skipped_at).slice(0, 10) : '—');
      if (s.detail && s.detail.expected != null && f) {
        sub += '・預估 ' + f.money(s.detail.expected, s.detail.ccy) + ' ' + s.detail.ccy;
      }
      main.appendChild(el('span', 'sk-sub', sub));
      row.appendChild(main);
      /* 明細 toggle mirrors the pending rows when the skipped month is still detectable. */
      let detail = null;
      if (s.detail && s.detail.trades && s.detail.trades.length) {
        detail = buildDetail(s.detail);
        row.appendChild(makeToggle(detail));
      }
      const un = el('button', 'btn btn-sm', '取消略過');
      un.type = 'button';
      un.addEventListener('click', () => act('unskip', s));
      row.appendChild(un);
      if (detail) row.appendChild(detail);
      listEl.appendChild(row);
    });
  }

  async function boot() {
    let resp;
    try {
      resp = await window.pdApi.get('/api/rebates');
    } catch (err) {
      render([]);
      renderSkipped([]);
      if (window.toast) {
        window.toast('待確認退款載入失敗', 'fail', (err && err.message) || undefined);
      }
      return;
    }
    render((resp && resp.rows) || []);
    renderSkipped((resp && resp.skipped) || []);
  }

  boot();
})();
