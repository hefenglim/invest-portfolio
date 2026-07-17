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
      lockSymbol: true,
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
        const del = el('button', 'btn btn-danger', '移除'); del.type = 'button';
        del.title = '移除此標的（自前端隱藏；資料仍保留，重新加入可還原。持倉中不可移除）';
        del.addEventListener('click', () => delInstrument(i));
        acts.appendChild(del);
        tdAct.appendChild(acts);
        tr.appendChild(tdAct);
        tbody.appendChild(tr);
      });
  }
  /* ---- edit modal（名稱、產業、板別(TW)、ETF、目標價下限/上限 — 全市場一致，
     2026-07-03；上限 FU-D28）。目標價下限/上限即為 target_cross 預警規則的逐檔門檻。 ---- */
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
    const tgtStep = i.ccy === 'MYR' ? '0.001' : '0.01';
    const tgtIn = el('input', 'input');
    tgtIn.id = 'edit-target-low';
    tgtIn.type = 'number'; tgtIn.min = '0'; tgtIn.step = tgtStep;
    tgtIn.placeholder = '留空 = 不提醒';
    if (i.target_low !== null && i.target_low !== undefined) tgtIn.value = i.target_low;
    body.appendChild(fld('目標價下限（現價 ≤ 此值時提醒，' + i.ccy + '）', tgtIn));
    const tgtHiIn = el('input', 'input');
    tgtHiIn.id = 'edit-target-high';
    tgtHiIn.type = 'number'; tgtHiIn.min = '0'; tgtHiIn.step = tgtStep;
    tgtHiIn.placeholder = '留空 = 不提醒';
    if (i.target_high !== null && i.target_high !== undefined) tgtHiIn.value = i.target_high;
    body.appendChild(fld('目標價上限（現價 ≥ 此值時提醒，' + i.ccy + '）', tgtHiIn));
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
      const rawHi = tgtHiIn.value.trim();
      /* target_low / target_high ride through as STRINGS (never parseFloat'd into money).
         Empty clears the bound (explicit null); otherwise the raw string reaches the backend
         Decimal column verbatim. */
      const body2 = {
        name: nameIn.value.trim() || i.name,
        sector: secIn.value.trim(),
        is_etf: etfCb.checked,
        target_low: raw === '' ? null : raw,
        target_high: rawHi === '' ? null : rawHi,
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

  /* ---- remove / restore (FU-D18, accumulative soft delete) ----
     移除 → ONE confirm → DELETE, which now SOFT-deletes (archives) ANY non-held symbol: no
     data is removed, so re-adding restores it and auto-backfills the gap. The backend still
     refuses a HELD symbol (422 ``held``) — surfaced as an info dialog (the only fix is to
     close the position). The former has_history → 封存 branch is gone (all non-held symbols
     soft-delete alike). archiveInstrument PUTs the archive flag (還原 un-archives + triggers
     the background backfill; archiving a held symbol 422s too). */
  function delInstrument(i) {
    window.confirmDialog({
      title: '移除 ' + i.symbol + (i.name ? ' ' + i.name : ''),
      body: '移除後將自前端隱藏；所有已抓取的報價與歷史資料仍保留，' +
        '重新加入即可還原並自動補抓缺口。',
      confirmLabel: '移除', danger: true,
      onConfirm: async () => {
        try {
          await window.pdApi.del('/api/instruments/' + encodeURIComponent(i.symbol));
        } catch (err) {
          if (err && err.status === 422 && err.code === 'held') {
            window.confirmDialog({ title: '無法移除', body: err.message, confirmLabel: '我知道了' });
            return;
          }
          if (window.toast) window.toast(err && err.message ? err.message : '移除失敗', 'fail', err && err.code);
          return;
        }
        if (window.toast) window.toast('已移除 ' + i.symbol, 'ok', '資料已保留，重新加入可還原');
        await refresh();
      }
    });
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
  $('#inst-search').addEventListener('input', (e) => render(e.target.value));
  $('#toggle-archived').addEventListener('click', () => {
    showArchived = !showArchived;
    render($('#inst-search').value);
  });
  refresh();
})();
