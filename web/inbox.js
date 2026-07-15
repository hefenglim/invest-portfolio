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
  let confirmedSession = [];  // session-only 已入帳 undo strip (3E; cleared on reload)

  function setCount(n) {
    if (countBadge) countBadge.textContent = String(n);
  }

  async function act(kind, fingerprints, label, items) {
    /* kind: 'confirm' | 'skip' — both recomputed/validated server-side. On confirm the
       created ledger ids (resp.ids) feed the session-only 已入帳 undo strip (3E). */
    try {
      const resp = await window.pdApi.post('/api/dividend-inbox/' + kind,
        { fingerprints: fingerprints });
      if (kind === 'confirm') {
        const n = (resp && resp.written) || 0;
        if (n) addConfirmed((resp && resp.ids) || [], items || []);
        if (window.toast) {
          window.toast(n ? '已確認入帳 ' + n + ' 筆' : '無可入帳項目', n ? 'ok' : 'fail',
            label + '・已寫入股利帳本，統計將由帳本重建');
        }
      } else if (window.toast) {
        window.toast('已略過', 'ok', label + '・可於「已忽略」取消忽略');
      }
      await boot();
    } catch (err) {
      if (window.toast) window.toast((err && err.message) || '操作失敗', 'fail', err && err.code);
    }
  }

  /* ---- 已入帳 undo strip (3E): confirm→undo→resurface, session-only ---- */
  function addConfirmed(ids, items) {
    ids.forEach((id, i) => {
      const it = items[i] || {};
      confirmedSession.unshift({
        id: id, symbol: it.symbol || '', ex_date: it.ex_date || '',
        ccy: it.ccy || '', net: it.est_net,
      });
    });
    renderStrip();
  }

  function renderStrip() {
    const host = $('#inbox-confirmed-strip');
    if (!host) return;
    host.replaceChildren();
    if (!confirmedSession.length) { host.classList.remove('show'); return; }
    host.classList.add('show');
    const head = el('div', 'icf-head');
    head.appendChild(el('span', null, '✓ 本次已入帳 ' + confirmedSession.length + ' 筆'));
    host.appendChild(head);
    confirmedSession.forEach((c) => {
      const row = el('div', 'icf-row');
      let txt = c.symbol || ('#' + c.id);
      if (c.ex_date) txt += '・' + (f ? f.date(c.ex_date) : c.ex_date);
      if (c.net != null && c.ccy && f) txt += '・Net ' + f.money(c.net, c.ccy) + ' ' + c.ccy;
      row.appendChild(el('span', 'icf-label', txt));
      const undo = el('button', 'btn btn-sm', '復原');
      undo.type = 'button';
      undo.title = '刪除帳本中的這筆股利，項目會自動重新出現在收件匣';
      undo.addEventListener('click', () => undoConfirmed(c));
      row.appendChild(undo);
      host.appendChild(row);
    });
    host.appendChild(el('div', 'icf-hint',
      '反悔？按「復原」刪除帳本中的股利紀錄即可，項目會自動重新出現在收件匣。此列表僅本次瀏覽有效。'));
  }

  function undoConfirmed(c) {
    delDividendWithGuard('/api/ledgers/dividends/' + c.id, () => {
      confirmedSession = confirmedSession.filter((x) => x.id !== c.id);
      renderStrip();
      if (window.toast) window.toast('已復原', 'ok', '帳本紀錄已刪除，項目將重新出現在收件匣');
      boot();
    });
  }

  /* DELETE a dividend row with the oversell-ack retry loop (mirrors ledger.js). */
  function delDividendWithGuard(path, onDone) {
    function doDelete(ack) {
      const p = ack ? (path + (path.indexOf('?') === -1 ? '?' : '&') + 'ack_oversell=true') : path;
      window.pdApi.del(p).then(onDone).catch((err) => {
        if (err && err.status === 422 && err.code === 'oversell' && !ack && window.confirmDialog) {
          window.confirmDialog({
            title: '賣超確認', body: err.message, confirmLabel: '我了解，仍要刪除', danger: true,
            onConfirm: () => doDelete(true),
          });
          return;
        }
        if (window.toast) window.toast((err && err.message) || '刪除失敗', 'fail', err && err.code);
      });
    }
    if (window.confirmDialog) {
      window.confirmDialog({
        title: '復原（刪除股利紀錄）',
        body: '將刪除帳本中對應的股利紀錄，統計由其餘帳本重建，並讓此項目重新出現在收件匣。',
        confirmLabel: '刪除並復原', danger: true,
        onConfirm: () => doDelete(false),
      });
    } else {
      doDelete(false);
    }
  }

  /* ---- 已忽略 list (3E): un-skip → resurface ---- */
  async function loadSkipped() {
    const details = $('#inbox-skipped');
    const listEl = $('#inbox-skipped-list');
    if (!details || !listEl) return;
    let resp;
    try { resp = await window.pdApi.get('/api/dividend-inbox/skipped'); }
    catch (e) { details.hidden = true; return; }
    const skRows = (resp && resp.rows) || [];
    const lbl = $('#inbox-skipped-label');
    if (lbl) lbl.textContent = '已忽略（' + skRows.length + '）';
    listEl.replaceChildren();
    if (!skRows.length) { details.hidden = true; return; }
    details.hidden = false;
    skRows.forEach((s) => {
      const row = el('div', 'sk-row');
      const main = el('div', 'sk-main');
      const d = s.detail;
      let title = s.symbol || s.fingerprint;
      if (s.ex_date) title += '・除息 ' + (f ? f.date(s.ex_date) : s.ex_date);
      if (d && d.account_name) title += '（' + d.account_name + '）';
      main.appendChild(el('span', null, title));
      let sub = '已忽略於 ' + (s.skipped_at ? String(s.skipped_at).slice(0, 10) : '—');
      if (d && d.est_gross != null && d.ccy && f) sub += '・預估 ' + f.money(d.est_gross, d.ccy) + ' ' + d.ccy;
      else if (!d) sub += '・目前無法重建明細（僅代號與日期）';
      main.appendChild(el('span', 'sk-sub', sub));
      row.appendChild(main);
      const un = el('button', 'btn btn-sm', '取消忽略');
      un.type = 'button';
      un.addEventListener('click', () => doUnskip(s.fingerprint));
      row.appendChild(un);
      listEl.appendChild(row);
    });
  }

  function doUnskip(fp) {
    window.pdApi.post('/api/dividend-inbox/unskip', { fingerprints: [fp] }).then(() => {
      if (window.toast) window.toast('已取消忽略', 'ok', '項目將重新出現在收件匣');
      boot();
    }).catch((err) => {
      if (window.toast) window.toast((err && err.message) || '操作失敗', 'fail', err && err.code);
    });
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
      const groupBtn = el('button', 'btn btn-sm', '全部確認');
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
            symbol + ' ×' + confirmables.length, confirmables)
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
            r.symbol + ' ' + f.date(r.ex_date), [r]));
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
    renderStrip();
    loadSkipped();
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
