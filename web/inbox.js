/* portfolio-dash — 待確認匯入收件匣 (wired to /api/dividend-inbox, 2026-07-03 R4).

   Boot: GET /api/dividend-inbox -> {rows, total_count}. Rows GROUP BY symbol
   (易於管理 for long backfills): each group is a collapsible block with a
   per-group 全部確認; items carry 確認入帳 / 略過. 重新偵測 re-fetches events
   from the providers (refresh=1, progress toast). Amounts are Decimal STRINGS
   rendered via window.fmt — the frontend never computes money; the backend
   recomputes every confirm server-side. 絕不自動入帳. */
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
  const section = document.getElementById('inbox-section');
  if (!section) return;
  const list = $('#inbox-list');
  const countBadge = $('#inbox-count');

  let rows = [];

  function setCount(n) {
    if (countBadge) countBadge.textContent = String(n);
  }

  async function act(kind, fingerprints, label) {
    /* kind: 'confirm' | 'skip' — both recomputed/validated server-side */
    try {
      const resp = await window.pdApi.post('/api/dividend-inbox/' + kind,
        { fingerprints: fingerprints });
      if (kind === 'confirm') {
        const n = (resp && resp.written) || 0;
        if (window.toast) {
          window.toast(n ? '已確認入帳 ' + n + ' 筆' : '無可入帳項目', n ? 'ok' : 'fail',
            label + '・已寫入股利帳本，統計將由帳本重建');
        }
      } else if (window.toast) {
        window.toast('已略過', 'ok', label + '・不會再次提示');
      }
      await boot();
    } catch (err) {
      if (window.toast) window.toast((err && err.message) || '操作失敗', 'fail', err && err.code);
    }
  }

  function render() {
    list.replaceChildren();
    setCount(rows.length);
    if (!rows.length) {
      list.appendChild(el('div', 'inbox-note',
        '目前沒有待確認項目 — 按「重新偵測」可向資料源掃描持倉的歷史配息。'));
      return;
    }
    /* group by symbol, newest ex_date first inside each group */
    const groups = new Map();
    rows.forEach((r) => {
      if (!groups.has(r.symbol)) groups.set(r.symbol, []);
      groups.get(r.symbol).push(r);
    });
    groups.forEach((items, symbol) => {
      const wrap = el('details', 'inbox-group');
      wrap.open = groups.size <= 3; /* few groups -> expanded; many -> collapsed for scanning */
      const head = el('summary', 'inbox-group-head');
      head.appendChild(el('span', 'sym-code', symbol));
      head.appendChild(el('span', 'sym-name', items[0].name));
      head.appendChild(el('span', 'inbox-count-badge', String(items.length) + ' 筆'));
      const confirmables = items.filter((x) => x.confirmable !== false);
      const groupBtn = el('button', 'btn', '全部確認');
      groupBtn.type = 'button';
      if (!confirmables.length) { groupBtn.disabled = true; groupBtn.title = '本組暫無可入帳項目'; }
      groupBtn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (!confirmables.length) return;
        window.confirmDialog({
          title: '批次確認入帳 — ' + symbol,
          body: '將 ' + confirmables.length + ' 筆偵測到的配息寫入股利帳本（金額由後端依除息日持股重新計算）。',
          confirmLabel: '全部確認',
          onConfirm: () => act('confirm', confirmables.map((x) => x.fingerprint),
            symbol + ' ×' + confirmables.length)
        });
      });
      head.appendChild(groupBtn);
      wrap.appendChild(head);

      items.forEach((r) => {
        const item = el('div', 'inbox-item');
        item.appendChild(el('span', 'src-badge', r.source));
        const main = el('div', 'inbox-main');
        const KIND_TITLE = {
          cash: '偵測到配息', drip: '偵測到配息（DRIP 再投資）',
          net: '偵測到配息（淨額）', stock: '偵測到配股',
        };
        main.appendChild(el('span', 'inbox-title',
          (KIND_TITLE[r.kind] || '偵測到配息') + '：' + r.symbol + ' ' + r.name +
          '（' + r.account_name + '）'));
        let sub;
        if (r.kind === 'stock') {
          sub = '除息日 ' + f.date(r.ex_date) + '・股票股利 ' + r.per_share +
            ' 元（面額制）・除權時持有 ' + f.num(r.shares_held) + ' 股 → 預估配得 ' +
            f.num(r.est_reinvest_shares, 2) + ' 股（$0 成本）';
        } else if (r.kind === 'drip') {
          sub = '除息日 ' + f.date(r.ex_date) + '・每股 ' + f.price(r.per_share, r.ccy) +
            '・持有 ' + f.num(r.shares_held) + ' 股 → Gross ' +
            f.money(r.est_gross, r.ccy) + '・預扣 30% → Net ' + f.money(r.est_net, r.ccy) +
            (r.est_reinvest_price != null
              ? '・估再投資 ' + f.num(r.est_reinvest_shares, 4) + ' 股 @ ' +
                f.price(r.est_reinvest_price, r.ccy) + '（估值，入帳後可編輯）'
              : '');
        } else {
          sub = '除息日 ' + f.date(r.ex_date) + '・每股 ' + f.price(r.per_share, r.ccy) +
            '・除息時持有 ' + f.num(r.shares_held) + ' 股 → 預估現金股利 ' +
            f.money(r.est_gross, r.ccy) + ' ' + r.ccy;
        }
        main.appendChild(el('span', 'inbox-sub', sub));
        const RULE = {
          cash: '入帳後依台股模式沖減調整成本',
          drip: 'DRIP：淨額以 $0 成本股數入帳，調整均價下降',
          net: '馬股單層淨額入帳，沖減調整成本',
          stock: '配股：新增 $0 成本股數，調整均價下降',
        };
        main.appendChild(el('span', 'inbox-rule',
          (r.note ? r.note + '；' : '') + (RULE[r.kind] || '') + '；入帳日為' +
          (r.pay_date ? '發放日 ' + f.date(r.pay_date) : '除息日') + '。'));
        item.appendChild(main);
        const acts = el('div', 'inbox-actions');
        const ok = el('button', 'btn btn-primary', '確認入帳');
        ok.type = 'button';
        if (r.confirmable === false) {
          ok.disabled = true;
          ok.title = r.note || '暫不可入帳';
        } else {
          ok.addEventListener('click', () => act('confirm', [r.fingerprint],
            r.symbol + ' ' + f.date(r.ex_date)));
        }
        const sk = el('button', 'btn', '略過');
        sk.type = 'button';
        sk.addEventListener('click', () => act('skip', [r.fingerprint],
          r.symbol + ' ' + f.date(r.ex_date)));
        acts.appendChild(ok);
        acts.appendChild(sk);
        item.appendChild(acts);
        wrap.appendChild(item);
      });
      list.appendChild(wrap);
    });
  }

  async function boot(refresh) {
    let resp;
    try {
      resp = await window.pdApi.get('/api/dividend-inbox', refresh ? { refresh: 1 } : undefined);
    } catch (err) {
      rows = [];
      render();
      if (window.toast) window.toast('待確認匯入載入失敗', 'fail', (err && err.message) || undefined);
      return null;
    }
    rows = (resp && resp.rows) || [];
    render();
    return resp;
  }

  /* 重新偵測 button in the panel head */
  const head = section.querySelector('.panel-head');
  if (head) {
    const btn = el('button', 'btn', '重新偵測');
    btn.type = 'button';
    btn.title = '向資料源重新掃描持倉的配息事件（自每檔最早取得日起）';
    btn.style.marginLeft = 'auto';
    btn.addEventListener('click', async () => {
      const restore = window.pdBusy ? window.pdBusy(btn, '偵測中…') : () => {};
      const prog = window.toastProgress
        ? window.toastProgress('配息偵測中…', '正在向 FinMind 掃描持倉的歷史配息（數秒～數十秒）')
        : { done: () => {}, fail: () => {} };
      const resp = await boot(true);
      restore();
      if (resp) prog.done('偵測完成', (resp.refreshed || '') + '・待確認 ' + resp.total_count + ' 筆');
      else prog.fail('偵測失敗', '請稍後再試');
    });
    head.appendChild(btn);
  }

  boot();
})();
