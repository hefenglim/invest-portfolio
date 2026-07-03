/* portfolio-dash — 標的管理 (wired to /api/instruments, spec 19/10).

   The instrument list is fetched from GET /api/instruments through the single
   pdApi fetch layer; the page no longer carries an inline mock. Adding is ONE
   step (2026-07-02): POST /api/instruments/quick probes the board, requires a
   real quote (typo guard; force after explicit confirm), auto-fills the name and
   backfills ~3 months of history. Money values (last / chg_pct / target_low)
   arrive as Decimal STRINGS and are formatted via window.fmt ONLY — the frontend
   never computes money. */
(function () {
  'use strict';
  /* D.list is mutable: starts empty (any pre-fetch render shows a blank table)
     and is replaced by the fetched rows once GET /api/instruments resolves. */
  let D = { list: [] };
  const f = window.fmt;
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const MARKET_ZH = { TW: '台股', US: '美股', MY: '馬股' };

  /* ---- one-step add (2026-07-02, supersedes the probe→confirm→detail flow) ----
     Symbol + market → POST /api/instruments/quick: the backend probes the board,
     requires a REAL quote (typo guard), auto-fills the name, and backfills ~3 months
     of history in one call. 422 quote_not_found → explicit confirm → force re-send.
     The button carries a pdBusy spinner (real network work, several seconds). */
  async function quickAdd(force) {
    const sym = $('#new-symbol').value.trim();
    const market = $('#new-market').value;
    if (!sym) {
      if (window.toast) window.toast('請先輸入代號', 'fail');
      return;
    }
    const btn = $('#quick-add-btn');
    const restore = window.pdBusy ? window.pdBusy(btn, '查詢並加入中…') : () => {};
    $('#quick-add-hint').textContent = '正在查詢報價、名稱與歷史資料（數秒）…';
    let resp;
    try {
      resp = await window.pdApi.post('/api/instruments/quick',
        { symbol: sym, market: market, force: !!force });
    } catch (err) {
      restore();
      $('#quick-add-hint').textContent = '';
      if (err && err.status === 422 && err.code === 'quote_not_found') {
        window.confirmDialog({
          title: '查無報價',
          body: (err.message || '查無 ' + sym + ' 的報價') +
            '。仍要加入嗎？（加入後將顯示「缺價」，直到資料來源提供報價）',
          confirmLabel: '仍要加入',
          danger: true,
          onConfirm: () => quickAdd(true)
        });
        return;
      }
      // 409 duplicate_symbol / 400 validation_error -> surface the backend message.
      if (window.toast) window.toast(err && err.message ? err.message : '加入失敗', 'fail', err && err.code);
      return;
    }
    restore();
    $('#quick-add-hint').textContent = '';
    $('#new-symbol').value = '';
    const label = [resp.name || null, resp.board_label || null,
      resp.last != null ? '現價 ' + f.price(resp.last, resp.ccy) + ' ' + resp.ccy : '暫無報價']
      .filter(Boolean).join('・');
    if (window.toast) window.toast('已加入 ' + resp.symbol, 'ok', label);
    await refresh();
    /* item 6 (2026-07-03): 新增 → 買入 is the most common next step — offer the
       handoff (input.html prefills via ?symbol=). 稍後 keeps the add-several flow. */
    if (window.confirmDialog) {
      window.confirmDialog({
        title: '已加入 ' + resp.symbol + (resp.name ? ' ' + resp.name : ''),
        body: '要現在記一筆買入嗎？（交易輸入將自動帶入代號）',
        confirmLabel: '記一筆買入',
        onConfirm: () => {
          window.location.href = 'input.html?symbol=' + encodeURIComponent(resp.symbol);
        }
      });
    }
  }
  $('#quick-add-btn').addEventListener('click', () => quickAdd(false));
  $('#new-symbol').addEventListener('keydown', (e) => { if (e.key === 'Enter') quickAdd(false); });

  /* ---- smart history backfill (2026-07-03): prices 12mo (or since a position's
     first acquisition when older) + the reporting FX pairs since the earliest
     ledger flow — so drawer charts, the trend line, and XIRR are complete. ---- */
  $('#backfill-btn').addEventListener('click', async () => {
    const btn = $('#backfill-btn');
    const restore = window.pdBusy ? window.pdBusy(btn, '回補中…') : () => {};
    const prog = window.toastProgress
      ? window.toastProgress('歷史回補中…',
        '個股：12 個月（持倉自最早買入日）＋匯率：自帳本最早一筆 — 可能需要一分鐘')
      : { done: () => {}, fail: () => {} };
    try {
      const resp = await window.pdApi.post('/api/actions/backfill-history', {});
      prog.done('歷史回補完成', (resp && resp.detail) || '');
    } catch (err) {
      prog.fail('歷史回補失敗', (err && err.message) || '請稍後再試');
    }
    restore();
  });

  /* ---- list table ---- */
  const BOARD_BADGE = {
    'TWSE': ['TWSE', 'board-twse'], 'TPEx': ['TPEx', 'board-tpex'],
    '.KL': ['.KL', 'board-kl'], '': ['—', ''], null: ['未解析', 'board-unres']
  };
  function render(filter) {
    const tbody = $('#inst-body');
    tbody.replaceChildren();
    const q = (filter || '').trim().toLowerCase();
    D.list
      .filter((i) => !q || i.symbol.toLowerCase().includes(q) || (i.name || '').toLowerCase().includes(q))
      .forEach((i) => {
        const tr = el('tr');
        const tdSym = el('td', 'col-text');
        const cell = el('div', 'sym-cell sym-link');
        cell.title = '點擊查看個股詳情（價格與成本、配息史、試算）';
        cell.addEventListener('click', () => {
          window.pdOpenSymbol(i.symbol);
        });
        cell.appendChild(el('span', 'sym-code', i.symbol));
        cell.appendChild(el('span', 'sym-name', i.name));
        tdSym.appendChild(cell);
        tr.appendChild(tdSym);
        tr.appendChild(el('td', 'col-text', MARKET_ZH[i.market]));
        const tdBoard = el('td', 'col-text');
        const [label, cls] = BOARD_BADGE[i.board === null ? null : i.board] || ['—', ''];
        if (i.board === '') {
          tdBoard.appendChild(el('span', 'sign-nil', '—'));
        } else {
          const b = el('span', 'board-pill ' + cls, label);
          if (i.board === null) b.title = '板別未解析 — 已以預設 TWSE 抓報價，請手動確認';
          tdBoard.appendChild(b);
        }
        tr.appendChild(tdBoard);
        tr.appendChild(el('td', 'col-text', i.sector));
        tr.appendChild(el('td', 'col-text', i.ccy));

        /* 現價 + 漲跌 (Decimal strings -> via fmt, never computed in JS) */
        const tdLast = el('td', 'num');
        if (i.last === null || i.last === undefined) {
          tdLast.appendChild(el('span', 'sign-nil', f.NULL_GLYPH + ' '));
          const b = el('span', 'badge badge-missing', '缺價');
          b.title = '板別未解析或來源無資料';
          tdLast.appendChild(b);
        } else {
          tdLast.appendChild(el('span', null, f.price(i.last, i.ccy)));
          tdLast.appendChild(el('span', 'subpct ' + f.signClass(i.chg_pct), f.signedPct(i.chg_pct)));
        }
        tr.appendChild(tdLast);

        /* 目標價提醒 */
        const tdTgt = el('td', 'num');
        if (i.target_low === null || i.target_low === undefined) {
          tdTgt.textContent = f.NULL_GLYPH;
          tdTgt.classList.add('sign-nil');
        } else {
          tdTgt.textContent = '≤ ' + f.price(i.target_low, i.ccy);
          /* Display-only触價 flag: a boolean UI decision, not a money value of record.
             Coerce the two Decimal strings purely to pick the badge; nothing computed
             from money is stored or shown. */
          if (i.last !== null && i.last !== undefined &&
              Number(i.last) <= Number(i.target_low)) {
            tdTgt.appendChild(document.createTextNode(' '));
            tdTgt.appendChild(el('span', 'badge badge-stale-mini', '已觸價'));
          }
        }
        tr.appendChild(tdTgt);

        const tdHeld = el('td', 'col-text');
        tdHeld.appendChild(el('span', 'status-tag ' + (i.held ? 'hold' : 'watch'), i.held ? '持有' : '觀察'));
        tr.appendChild(tdHeld);

        const tdAct = el('td');
        const acts = el('div', 'wl-actions');
        const edit = el('button', 'btn', '編輯'); edit.type = 'button';
        edit.title = '編輯產業與目標價提醒';
        edit.addEventListener('click', () => openEdit(i));
        acts.appendChild(edit);
        if (i.market === 'TW') {
          const rp = el('button', 'btn', '重新探測'); rp.type = 'button';
          rp.title = '重新探測 TWSE / TPEx 板別並儲存結果';
          rp.addEventListener('click', async () => {
            const restore = window.pdBusy ? window.pdBusy(rp, '探測中…') : () => {};
            let resp;
            try {
              resp = await window.pdApi.post('/api/instruments/probe', { symbol: i.symbol });
            } catch (err) {
              restore();
              if (window.toast) window.toast('探測失敗', 'fail', err && err.message ? err.message : undefined);
              return;
            }
            /* persist the probe result (2026-07-02) — the old flow only toasted it,
               leaving an unresolved board unresolved forever. */
            const board = resp && resp.board;
            if (board) {
              try {
                await window.pdApi.put('/api/instruments/' + encodeURIComponent(i.symbol),
                  { board: board });
              } catch (err) {
                restore();
                if (window.toast) window.toast('板別儲存失敗', 'fail', err && err.message ? err.message : undefined);
                return;
              }
            }
            restore();
            if (window.toast) {
              window.toast(board ? '板別已更新' : '探測完成',
                board ? 'ok' : 'fail',
                i.symbol + ' 判定 ' + (resp && resp.board_label ? resp.board_label : '未解析') +
                (board ? '（已儲存）' : '，未變更'));
            }
            if (board) await refresh();
          });
          acts.appendChild(rp);
        }
        tdAct.appendChild(acts);
        tr.appendChild(tdAct);
        tbody.appendChild(tr);
      });
  }
  /* ---- edit modal（名稱、產業、板別(TW)、ETF、目標價 — 全市場一致，2026-07-03）---- */
  function openEdit(i) {
    const backdrop = el('div', 'modal-backdrop');
    const modal = el('div', 'modal');
    const head = el('div', 'modal-head');
    head.appendChild(el('h3', 'modal-title', '編輯標的 — ' + i.symbol + ' ' + i.name));
    const close = el('button', 'modal-close', '✕'); close.type = 'button';
    head.appendChild(close);
    modal.appendChild(head);
    const body = el('div', 'modal-body');
    const fld = (label, node) => {
      const w = el('div', 'field');
      w.appendChild(el('label', null, label));
      w.appendChild(node);
      return w;
    };
    const nameIn = el('input', 'input');
    nameIn.value = i.name || '';
    nameIn.placeholder = '顯示名稱（可自動查詢失敗後手動補）';
    body.appendChild(fld('名稱', nameIn));
    const secIn = el('input', 'input');
    secIn.value = i.sector || '';
    body.appendChild(fld('產業', secIn));
    /* TW 板別可直接改（重新探測仍可自動判定並儲存）；US/MY 板別固定 */
    let boardSel = null;
    if (i.market === 'TW') {
      boardSel = el('select', 'select');
      [['TWSE', 'TWSE 上市'], ['TPEx', 'TPEx 上櫃']].forEach(([v, label]) => {
        const o = el('option', null, label); o.value = v;
        if (i.board === v) o.selected = true;
        boardSel.appendChild(o);
      });
      body.appendChild(fld('板別', boardSel));
    }
    const etfWrap = el('label', 'hint');
    const etfCb = el('input');
    etfCb.type = 'checkbox';
    etfCb.checked = !!i.etf || !!i.is_etf;
    etfWrap.appendChild(etfCb);
    etfWrap.appendChild(el('span', null, ' 此標的為 ETF（影響台股賣出稅率 0.1%）'));
    body.appendChild(fld('類別', etfWrap));
    const tgtIn = el('input', 'input');
    tgtIn.type = 'number'; tgtIn.min = '0'; tgtIn.step = i.ccy === 'MYR' ? '0.001' : '0.01';
    tgtIn.placeholder = '留空 = 不提醒';
    if (i.target_low !== null && i.target_low !== undefined) tgtIn.value = i.target_low;
    body.appendChild(fld('目標價提醒（現價 ≤ 此值時提醒，' + i.ccy + '）', tgtIn));
    body.appendChild(el('div', 'hint', '市場與幣別由註冊流程決定，不可更改。'));
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
      const raw = tgtIn.value.trim();
      /* target_low rides through as a STRING (never parseFloat'd into money). Empty
         clears it; otherwise pass the raw string to the backend Decimal column. */
      const body2 = {
        name: nameIn.value.trim() || i.name,
        sector: secIn.value.trim(),
        is_etf: etfCb.checked,
        target_low: raw === '' ? null : raw,
      };
      if (boardSel) body2.board = boardSel.value;
      try {
        await window.pdApi.put('/api/instruments/' + encodeURIComponent(i.symbol), body2);
      } catch (err) {
        if (window.toast) window.toast(err && err.message ? err.message : '儲存失敗', 'fail', err && err.code);
        return;
      }
      dismiss();
      if (window.toast) window.toast('已儲存', 'ok', i.symbol + ' 已更新');
      await refresh();
    });
    document.body.appendChild(backdrop);
    setTimeout(() => nameIn.focus(), 50);
  }

  /* Fetch the instrument list and (re)render. Graceful degradation: on failure leave
     the table empty and surface ONE toast — never an unhandled rejection (the e2e smoke
     asserts zero console errors). 401 is handled inside api.js. */
  async function refresh() {
    let resp;
    try {
      resp = await window.pdApi.get('/api/instruments');
    } catch (err) {
      D = { list: [] };
      render($('#inst-search').value);
      if (window.toast) window.toast('標的清單載入失敗', 'fail', err && err.message ? err.message : undefined);
      return;
    }
    D = { list: (resp && resp.list) || [] };
    render($('#inst-search').value);
  }

  render();  // empty table before the fetch resolves
  $('#inst-search').addEventListener('input', (e) => render(e.target.value));
  refresh();
})();
