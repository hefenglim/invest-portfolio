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
  /* FU-D13: archived (停止追蹤) rows are hidden by default behind the toolbar toggle. */
  let showArchived = false;
  const f = window.fmt;
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const MARKET_ZH = { TW: '台股', US: '美股', MY: '馬股' };

  /* ---- one-step add via the shared quick-add dialog (FU-D23, supersedes the direct
     POST /api/instruments/quick flow) ----
     The 加入 button opens web/inst-quickadd.js: a FAST lookup (GET /api/instruments/lookup)
     provider-verifies existence (typo guard) and detects an already-registered symbol
     (「已註冊」) or an archived one to restore — network-free for both. The user confirms the
     name + picks/enters a sector (datalist of existing sectors), then 確認 registers via POST
     /api/instruments; the heavy quote/history fetch runs in the BACKGROUND. 記一筆買入 hands
     off to the manual pane (input.html?symbol=). */
  function openQuickAdd() {
    const sym = $('#new-symbol').value.trim();
    const market = $('#new-market').value;
    if (!sym) {
      if (window.toast) window.toast('請先輸入代號', 'fail');
      return;
    }
    if (!window.pdInstQuickAdd) {
      if (window.toast) window.toast('對話框載入失敗，請重新整理', 'fail');
      return;
    }
    window.pdInstQuickAdd({
      symbol: sym,
      market: market,
      /* A3/F9c: lockSymbol RETIRED — the dialog's symbol field is always editable so a wrong
         AI-parsed code can be fixed in place; passing a locked flag re-introduced that dead-end. */
      onConfirm: async () => {
        $('#new-symbol').value = '';
        await refresh();
      },
      onBuy: (resp) => {
        const s = (resp && resp.symbol) || sym.toUpperCase();
        window.location.href = 'input.html?symbol=' + encodeURIComponent(s);
      },
    });
  }
  $('#quick-add-btn').addEventListener('click', openQuickAdd);
  $('#new-symbol').addEventListener('keydown', (e) => { if (e.key === 'Enter') openQuickAdd(); });

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
    /* Toolbar toggle reflects how many archived rows exist; hidden entirely when none. */
    const archivedCount = D.list.filter((i) => i.archived).length;
    const toggle = $('#toggle-archived');
    if (toggle) {
      toggle.style.display = archivedCount ? '' : 'none';
      /* FU-D18: 移除 (soft delete) and 封存 (stop-tracking) are the SAME archived state now,
         so the toggle covers both intents. */
      toggle.textContent = showArchived
        ? '隱藏已移除／封存'
        : ('顯示已移除／封存 (' + archivedCount + ')');
    }
    D.list
      .filter((i) => showArchived || !i.archived)
      .filter((i) => !q || i.symbol.toLowerCase().includes(q) || (i.name || '').toLowerCase().includes(q))
      .forEach((i) => {
        const tr = el('tr');
        if (i.archived) tr.classList.add('inst-archived');
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

        /* 目標價提醒（下限 ≤、上限 ≥）。Display-only 觸價 flag: a boolean UI decision, not a
           money value of record — the two Decimal strings are coerced PURELY to pick the badge
           (mirrors the backend target_cross rule's ≤ / ≥); nothing money-derived is stored. */
        const tdTgt = el('td', 'num');
        const hasLow = i.target_low !== null && i.target_low !== undefined;
        const hasHigh = i.target_high !== null && i.target_high !== undefined;
        if (!hasLow && !hasHigh) {
          tdTgt.textContent = f.NULL_GLYPH;
          tdTgt.classList.add('sign-nil');
        } else {
          const priced = i.last !== null && i.last !== undefined;
          if (hasLow) {
            const line = el('div', 'tgt-line');
            line.appendChild(el('span', null, '≤ ' + f.price(i.target_low, i.ccy)));
            if (priced && Number(i.last) <= Number(i.target_low)) {
              line.appendChild(document.createTextNode(' '));
              line.appendChild(el('span', 'badge badge-stale-mini', '跌破'));
            }
            tdTgt.appendChild(line);
          }
          if (hasHigh) {
            const line = el('div', 'tgt-line');
            line.appendChild(el('span', null, '≥ ' + f.price(i.target_high, i.ccy)));
            if (priced && Number(i.last) >= Number(i.target_high)) {
              line.appendChild(document.createTextNode(' '));
              line.appendChild(el('span', 'badge badge-stale-mini', '突破'));
            }
            tdTgt.appendChild(line);
          }
        }
        tr.appendChild(tdTgt);

        const tdHeld = el('td', 'col-text');
        if (i.archived) {
          const tag = el('span', 'status-tag archived', '已移除');
          tag.title = '已移除（停止追蹤）：不抓報價/訊號/新聞；所有資料仍保留，重新加入即可還原並補抓缺口';
          tdHeld.appendChild(tag);
        } else {
          tdHeld.appendChild(el('span', 'status-tag ' + (i.held ? 'hold' : 'watch'), i.held ? '持有' : '觀察'));
        }
        tr.appendChild(tdHeld);

        const tdAct = el('td');
        const acts = el('div', 'wl-actions');
        if (i.archived) {
          const restore = el('button', 'btn', '還原'); restore.type = 'button';
          restore.title = '還原（恢復追蹤：重新納入報價、訊號與新聞範圍）';
          restore.addEventListener('click', () => archiveInstrument(i, false));
          acts.appendChild(restore);
        } else {
          const edit = el('button', 'btn', '編輯'); edit.type = 'button';
          edit.title = '編輯產業與目標價提醒';
          edit.addEventListener('click', () => openEdit(i));
          acts.appendChild(edit);
        }
        if (!i.archived && i.market === 'TW') {
          const rp = el('button', 'btn', '重新探測'); rp.type = 'button';
          rp.title = '重新探測 TWSE / TPEx 板別並儲存結果';
          /* F4: the handler is a TWO-await chain (probe → PUT). pdBusy disables the button, but
             an explicit in-flight guard makes the double-click race impossible even if a second
             click slips in before the disable paints. One try/finally clears it on every exit. */
          let probing = false;
          rp.addEventListener('click', async () => {
            if (probing) return;
            probing = true;
            const restore = window.pdBusy ? window.pdBusy(rp, '探測中…') : () => {};
            try {
              let resp;
              try {
                resp = await window.pdApi.post('/api/instruments/probe', { symbol: i.symbol });
              } catch (err) {
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
                  if (window.toast) window.toast('板別儲存失敗', 'fail', err && err.message ? err.message : undefined);
                  return;
                }
              }
              if (window.toast) {
                window.toast(board ? '板別已更新' : '探測完成',
                  board ? 'ok' : 'fail',
                  i.symbol + ' 判定 ' + (resp && resp.board_label ? resp.board_label : '未解析') +
                  (board ? '（已儲存）' : '，未變更'));
              }
              if (board) await refresh();
            } finally {
              probing = false;
              restore();
            }
          });
          acts.appendChild(rp);
        }
        const del = el('button', 'btn btn-danger', '移除'); del.type = 'button';
        del.title = '移除此標的：可選「隱藏」（保留資料、可還原）或「永久移除」（硬刪、無法復原）。持倉中不可移除';
        del.addEventListener('click', () => delInstrument(i));
        acts.appendChild(del);
        tdAct.appendChild(acts);
        tr.appendChild(tdAct);
        tbody.appendChild(tr);
      });
  }
  /* ---- edit modal — now a THIN caller of the SHARED pdInstQuickAdd builder (Wave A1,
     mode:'edit'). ONE modal definition holds 名稱／產業／產業細分／板別(TW)／ETF／目標價下限/上限
     for both flows, so a future field/validation change touches one place. 目標價下限/上限 are the
     per-symbol target_cross 門檻; the sector 「重新偵測產業」 affordance lives in the shared field.
     Save is PUT /api/instruments/{symbol}; 記一筆買入 is dropped in edit. ---- */
  function openEdit(i) {
    if (!window.pdInstQuickAdd) {
      if (window.toast) window.toast('對話框載入失敗，請重新整理', 'fail');
      return;
    }
    window.pdInstQuickAdd({
      mode: 'edit',
      symbol: i.symbol,
      market: i.market,
      name: i.name || '',
      sector: i.sector || '',
      industry: i.industry || '',
      board: i.board || null,
      is_etf: !!i.is_etf,  // F5: the wire (_element) only ever sends `is_etf`; the old `i.etf` dual-read was dead
      ccy: i.ccy,
      target_low: i.target_low,   // string | null | undefined — the builder null-guards
      target_high: i.target_high,
      onSaved: async () => { await refresh(); },
    });
  }

  /* ---- remove: TWO deletion tiers (FU-D32) ----
     The 移除 dialog offers 取消 / 移除（隱藏）/ 永久移除:
       • 移除（隱藏）= FU-D18 soft delete: DELETE archives ANY non-held symbol, no data is
         removed, so re-adding restores it and auto-backfills the gap (accumulative). The
         backend still refuses a HELD symbol (422 ``held``) → info dialog (close the position).
       • 永久移除 = HARD purge (POST …/purge): irreversible. Gated by a STRONG confirm — the
         user must TYPE the symbol EXACTLY before the button enables. A symbol with ANY ledger
         history (incl. closed positions) renders 永久移除 DISABLED with the owner's explanation
         (the backend also 422s ``has_history`` as the authoritative guard; the pre-disable is
         driven by the additive ``has_history`` wire field, so a purge with history can never
         even be attempted from the UI).
     archiveInstrument (below) is unchanged (還原 un-archives + background backfill). */
  function delInstrument(i) {
    const backdrop = el('div', 'modal-backdrop');
    const modal = el('div', 'modal');
    const head = el('div', 'modal-head');
    head.appendChild(el('h3', 'modal-title', '移除 ' + i.symbol + (i.name ? ' ' + i.name : '')));
    const close = el('button', 'modal-close', '✕'); close.type = 'button';
    head.appendChild(close);
    modal.appendChild(head);

    const body = el('div', 'modal-body');
    body.appendChild(el('div', 'hint',
      '兩種移除方式：「移除（隱藏）」自前端隱藏、保留所有資料，可再還原；' +
      '「永久移除」硬刪此標的與其衍生資料，無法復原。'));

    const hideBox = el('div', 'field');
    hideBox.appendChild(el('label', null, '移除（隱藏）— 可還原'));
    hideBox.appendChild(el('div', 'hint',
      '自前端隱藏；所有已抓取的報價與歷史資料仍保留，重新加入即可還原並自動補抓缺口。'));
    body.appendChild(hideBox);

    const purgeBox = el('div', 'field');
    purgeBox.appendChild(el('label', null, '永久移除 — 無法復原'));
    let confirmIn = null;
    if (i.has_history) {
      const warn = el('div', 'hint');
      warn.style.color = 'var(--amber)';
      warn.textContent = '此標的有歷史帳務紀錄，無法永久移除：已清倉標的的歷史交易仍被現金流回溯、'
        + 'XIRR／歷年報酬、股利紀錄、已實現損益報表引用；硬刪會導致帳目無法對帳與孤兒資料。'
        + '請改用「移除（隱藏）」。';
      purgeBox.appendChild(warn);
    } else {
      purgeBox.appendChild(el('div', 'hint',
        '硬刪此標的與其衍生資料（報價、配息事件、訊號、預警、目標權重），無法復原。'
        + '請輸入代號「' + i.symbol + '」以啟用永久移除。'));
      confirmIn = el('input', 'input purge-confirm');
      confirmIn.spellcheck = false;
      confirmIn.placeholder = i.symbol;
      confirmIn.setAttribute('aria-label', '輸入代號以確認永久移除');
      purgeBox.appendChild(confirmIn);
    }
    body.appendChild(purgeBox);
    modal.appendChild(body);

    const foot = el('div', 'modal-foot');
    const cancel = el('button', 'btn', '取消'); cancel.type = 'button';
    const hideBtn = el('button', 'btn btn-danger', '移除（隱藏）'); hideBtn.type = 'button';
    const purgeBtn = el('button', 'btn btn-danger', '永久移除'); purgeBtn.type = 'button';
    purgeBtn.disabled = true;  // STRONG confirm gate: enabled only on an exact symbol match
    foot.appendChild(cancel); foot.appendChild(hideBtn); foot.appendChild(purgeBtn);
    modal.appendChild(foot);
    backdrop.appendChild(modal);

    const dismiss = () => { document.removeEventListener('keydown', onKey); backdrop.remove(); };
    const onKey = (e) => { if (e.key === 'Escape') dismiss(); };
    document.addEventListener('keydown', onKey);
    close.addEventListener('click', dismiss);
    cancel.addEventListener('click', dismiss);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) dismiss(); });

    if (confirmIn) {
      /* Enable 永久移除 ONLY on an exact, case-sensitive symbol match. */
      confirmIn.addEventListener('input', () => {
        purgeBtn.disabled = confirmIn.value.trim() !== i.symbol;
      });
      /* No <form> here, so there is no implicit submit; still, explicitly swallow Enter so a
         default submit can never bypass the type-confirm gate (senior-review requirement). */
      confirmIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') e.preventDefault(); });
    }

    hideBtn.addEventListener('click', async () => {
      const restore = window.pdBusy ? window.pdBusy(hideBtn, '移除中…') : () => {};
      try {
        await window.pdApi.del('/api/instruments/' + encodeURIComponent(i.symbol));
      } catch (err) {
        restore();
        if (err && err.status === 422 && err.code === 'held') {
          dismiss();
          window.confirmDialog({ title: '無法移除', body: err.message, confirmLabel: '我知道了' });
          return;
        }
        if (window.toast) window.toast(err && err.message ? err.message : '移除失敗', 'fail', err && err.code);
        return;
      }
      restore();
      dismiss();
      if (window.toast) window.toast('已移除 ' + i.symbol, 'ok', '資料已保留，重新加入可還原');
      await refresh();
    });

    purgeBtn.addEventListener('click', async () => {
      if (purgeBtn.disabled) return;  // defensive: the gate must hold
      const restore = window.pdBusy ? window.pdBusy(purgeBtn, '永久移除中…') : () => {};
      try {
        await window.pdApi.post('/api/instruments/' + encodeURIComponent(i.symbol) + '/purge', {});
      } catch (err) {
        restore();
        if (err && err.status === 422) {
          dismiss();
          window.confirmDialog({ title: '無法永久移除', body: err.message, confirmLabel: '我知道了' });
          return;
        }
        if (window.toast) window.toast(err && err.message ? err.message : '永久移除失敗', 'fail', err && err.code);
        return;
      }
      restore();
      dismiss();
      if (window.toast) window.toast('已永久移除 ' + i.symbol, 'ok', '此標的與其衍生資料已刪除');
      await refresh();
    });

    document.body.appendChild(backdrop);
    if (confirmIn) setTimeout(() => confirmIn.focus(), 50);
  }

  async function archiveInstrument(i, archived) {
    let resp;
    try {
      resp = await window.pdApi.put('/api/instruments/' + encodeURIComponent(i.symbol) + '/archive',
        { archived: archived });
    } catch (err) {
      if (err && err.status === 422 && err.code === 'held') {
        window.confirmDialog({ title: '無法封存', body: err.message, confirmLabel: '我知道了' });
        return;
      }
      if (window.toast) {
        window.toast(err && err.message ? err.message : (archived ? '封存失敗' : '還原失敗'),
          'fail', err && err.code);
      }
      return;
    }
    if (window.toast) {
      if (archived) {
        window.toast('已封存 ' + i.symbol, 'ok');
      } else {
        /* FU-D18: restore kicks off a background gap backfill; report the last data on file. */
        const lastDate = resp && resp.last_price_date;
        window.toast('已還原，背景補抓報價中', 'ok',
          i.symbol + (lastDate ? '（最後資料日 ' + lastDate + '）' : ''));
      }
    }
    await refresh();
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
  /* F1: debounce the search re-render — render() rebuilds every row + its listeners, so a
     per-keystroke rebuild churns the whole table; a short debounce coalesces fast typing. */
  let searchT = null;
  $('#inst-search').addEventListener('input', (e) => {
    const v = e.target.value;
    if (searchT) clearTimeout(searchT);
    searchT = setTimeout(() => render(v), 150);
  });
  $('#toggle-archived').addEventListener('click', () => {
    showArchived = !showArchived;
    render($('#inst-search').value);
  });
  refresh();
})();
