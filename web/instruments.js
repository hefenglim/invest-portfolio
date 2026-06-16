/* portfolio-dash — 標的管理 (wired to /api/instruments, spec 19/10).

   The instrument list is fetched from GET /api/instruments through the single
   pdApi fetch layer; the page no longer carries an inline mock. The probe step
   (TW board guess) and registration both POST through pdApi. Money values
   (last / chg_pct / target_low) arrive as Decimal STRINGS and are formatted via
   window.fmt ONLY — the frontend never computes money. */
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

  /* The confirmed board from the probe step, fed into the register POST. Set by
     the probe flow; reset whenever a new probe begins. */
  let probedSymbol = '';
  let probedName = null;
  let confirmedBoard = null;

  /* ---- probe flow ---- */
  const probeCard = $('#probe-card');
  $('#probe-btn').addEventListener('click', async () => {
    const sym = $('#new-symbol').value.trim();
    const market = $('#new-market').value;
    if (!sym) {
      if (window.toast) window.toast('請先輸入代號', 'fail');
      return;
    }
    probedSymbol = sym;
    if (market !== 'TW') {
      // US / MY: board is fixed (no TW board probe); go straight to detail form.
      probedName = null;
      confirmedBoard = market === 'US' ? '' : '.KL';
      probeCard.hidden = true;
      $('#unresolved-banner').hidden = true;
      $('#detail-form').hidden = false;
      $('#df-title').textContent =
        sym + ' — 板別固定（' + (market === 'US' ? '美股' : '馬股 .KL') + '），直接填寫明細';
      return;
    }
    // TW: probe the board through the real endpoint.
    let resp;
    try {
      resp = await window.pdApi.post('/api/instruments/probe', { symbol: sym });
    } catch (err) {
      if (window.toast) window.toast('板別探測失敗', 'fail', err && err.message ? err.message : undefined);
      return;
    }
    probedName = resp && resp.name ? resp.name : null;
    confirmedBoard = (resp && resp.board) || 'TWSE';
    probeCard.hidden = false;
    $('#detail-form').hidden = true;
    $('#unresolved-banner').hidden = true;
    const nameTxt = probedName ? '（' + probedName + '）' : '';
    $('#probe-text').innerHTML = '<b class="num">' + sym + '</b>' + nameTxt +
      ' → 判定 <b>' + (resp && resp.board_label ? resp.board_label : '未解析') + '</b> — 正確嗎？';
  });
  $('#probe-ok').addEventListener('click', () => {
    confirmedBoard = confirmedBoard || 'TWSE';
    probeCard.hidden = true;
    $('#detail-form').hidden = false;
    $('#df-title').textContent =
      probedSymbol + (probedName ? ' ' + probedName : '') + '（TWSE 上市）— 填寫明細後註冊';
  });
  $('#probe-tpex').addEventListener('click', () => {
    confirmedBoard = 'TPEx';
    probeCard.hidden = true;
    $('#detail-form').hidden = false;
    $('#df-title').textContent = probedSymbol + '（改判 TPEx 上櫃）— 填寫明細後註冊';
  });
  $('#probe-fail').addEventListener('click', () => {
    // Unresolved: register with no board; the backend defaults to TWSE for pricing.
    confirmedBoard = null;
    probeCard.hidden = true;
    $('#detail-form').hidden = false;
    $('#df-title').textContent = probedSymbol + '（板別未解析 — 以預設 TWSE 抓報價）— 填寫明細後註冊';
    $('#unresolved-banner').hidden = false;
    if (window.toast) window.toast('已暫存', 'ok', probedSymbol + ' 以預設 TWSE 抓報價，板別待確認');
  });

  /* ---- register: POST /api/instruments, then re-fetch the list ---- */
  $('#df-register').addEventListener('click', async () => {
    const market = $('#new-market').value;
    const symbol = probedSymbol || $('#new-symbol').value.trim();
    if (!symbol) {
      if (window.toast) window.toast('請先查詢代號', 'fail');
      return;
    }
    const body = {
      symbol: symbol,
      market: market,
      name: ($('#df-name').value || '').trim(),
      sector: ($('#df-sector').value || '').trim(),
    };
    // TW carries the confirmed board; US/MY leave board to the backend default.
    if (market === 'TW' && confirmedBoard) body.board = confirmedBoard;
    try {
      await window.pdApi.post('/api/instruments', body);
    } catch (err) {
      // 409 duplicate_symbol / 400 validation_error -> surface the backend message.
      if (window.toast) window.toast(err && err.message ? err.message : '註冊失敗', 'fail', err && err.code);
      return;
    }
    if (window.toast) window.toast('註冊成功', 'ok', symbol + ' 已加入清單');
    $('#detail-form').hidden = true;
    $('#unresolved-banner').hidden = true;
    await refresh();
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
          rp.title = '重新探測 TWSE / TPEx 板別';
          rp.addEventListener('click', async () => {
            let resp;
            try {
              resp = await window.pdApi.post('/api/instruments/probe', { symbol: i.symbol });
            } catch (err) {
              if (window.toast) window.toast('探測失敗', 'fail', err && err.message ? err.message : undefined);
              return;
            }
            if (window.toast) {
              window.toast('探測完成', 'ok',
                i.symbol + ' 判定 ' + (resp && resp.board_label ? resp.board_label : '未解析'));
            }
          });
          acts.appendChild(rp);
        }
        tdAct.appendChild(acts);
        tr.appendChild(tdAct);
        tbody.appendChild(tr);
      });
  }
  /* ---- edit modal（產業、目標價提醒）---- */
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
    const secIn = el('input', 'input');
    secIn.value = i.sector || '';
    body.appendChild(fld('產業', secIn));
    const tgtIn = el('input', 'input');
    tgtIn.type = 'number'; tgtIn.min = '0'; tgtIn.step = i.ccy === 'MYR' ? '0.001' : '0.01';
    tgtIn.placeholder = '留空 = 不提醒';
    if (i.target_low !== null && i.target_low !== undefined) tgtIn.value = i.target_low;
    body.appendChild(fld('目標價提醒（現價 ≤ 此值時提醒，' + i.ccy + '）', tgtIn));
    body.appendChild(el('div', 'hint', '市場與幣別由註冊流程決定，不可更改；台股板別請用「重新探測」。'));
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
      const sector = secIn.value.trim();
      const raw = tgtIn.value.trim();
      /* target_low rides through as a STRING (never parseFloat'd into money). Empty
         clears it; otherwise pass the raw string to the backend Decimal column. */
      const body2 = { sector: sector || i.sector };
      body2.target_low = raw === '' ? null : raw;
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
    setTimeout(() => tgtIn.focus(), 50);
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
