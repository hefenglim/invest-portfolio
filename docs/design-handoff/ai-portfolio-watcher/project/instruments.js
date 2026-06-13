/* portfolio-dash — 標的管理 (mock + rendering) */
window.INSTRUMENTS_DATA = {
  "as_of": "2026-06-11T14:30:00+08:00",
  "probe_example": { "symbol": "2330", "name": "台積電", "board": "TWSE", "board_label": "TWSE 上市" },
  "list": [
    { "symbol": "2330", "name": "台積電", "market": "TW", "board": "TWSE", "sector": "半導體", "ccy": "TWD",
      "held": true, "last": 612.5, "chg_pct": 0.012, "target_low": null },
    { "symbol": "0056", "name": "元大高股息", "market": "TW", "board": "TWSE", "sector": "ETF", "ccy": "TWD",
      "held": true, "last": 38.95, "chg_pct": 0.0021, "target_low": null },
    { "symbol": "6488", "name": "環球晶", "market": "TW", "board": "TPEx", "sector": "半導體", "ccy": "TWD",
      "held": false, "last": 488.00, "chg_pct": -0.008, "target_low": 450 },
    { "symbol": "8069", "name": "元太", "market": "TW", "board": null, "sector": "光電", "ccy": "TWD",
      "held": false, "last": null, "chg_pct": null, "target_low": 220 },
    { "symbol": "AAPL", "name": "Apple", "market": "US", "board": "", "sector": "科技", "ccy": "USD",
      "held": true, "last": 211.40, "chg_pct": 0.004, "target_low": null },
    { "symbol": "1155.KL", "name": "Maybank", "market": "MY", "board": ".KL", "sector": "金融", "ccy": "MYR",
      "held": true, "last": 9.870, "chg_pct": -0.003, "target_low": null }
  ]
};

(function () {
  'use strict';
  const D = window.INSTRUMENTS_DATA;
  const f = window.fmt;
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const MARKET_ZH = { TW: '台股', US: '美股', MY: '馬股' };

  /* ---- probe flow ---- */
  const probeCard = $('#probe-card');
  $('#probe-btn').addEventListener('click', () => {
    const sym = $('#new-symbol').value.trim() || D.probe_example.symbol;
    const market = $('#new-market').value;
    if (market !== 'TW') {
      probeCard.hidden = true;
      $('#detail-form').hidden = false;
      $('#df-title').textContent = sym + ' — 板別固定（' + (market === 'US' ? '美股' : '馬股 .KL') + '），直接填寫明細';
      return;
    }
    probeCard.hidden = false;
    $('#detail-form').hidden = true;
    $('#probe-text').innerHTML = '<b class="num">' + sym + '</b> → ' + D.probe_example.name +
      '，判定 <b>' + D.probe_example.board_label + '</b> — 正確嗎？';
  });
  $('#probe-ok').addEventListener('click', () => {
    probeCard.hidden = true;
    $('#detail-form').hidden = false;
    $('#df-title').textContent = D.probe_example.symbol + ' ' + D.probe_example.name + '（TWSE 上市）— 填寫明細後註冊';
  });
  $('#probe-tpex').addEventListener('click', () => {
    probeCard.hidden = true;
    $('#detail-form').hidden = false;
    $('#df-title').textContent = D.probe_example.symbol + '（改判 TPEx 上櫃）— 填寫明細後註冊';
  });
  $('#probe-fail').addEventListener('click', () => {
    probeCard.hidden = true;
    $('#unresolved-banner').hidden = false;
    window.toast('已暫存', 'ok', '8069 以預設 TWSE 抓報價，板別待確認');
  });
  $('#df-register').addEventListener('click', () =>
    window.toast('註冊成功', 'ok', '標的已加入清單（設計稿）'));

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
      .filter((i) => !q || i.symbol.toLowerCase().includes(q) || i.name.toLowerCase().includes(q))
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

        /* 現價 + 漲跌 */
        const tdLast = el('td', 'num');
        if (i.last === null) {
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
        if (i.target_low === null) {
          tdTgt.textContent = f.NULL_GLYPH;
          tdTgt.classList.add('sign-nil');
        } else {
          tdTgt.textContent = '≤ ' + f.price(i.target_low, i.ccy);
          if (i.last !== null && i.last <= i.target_low) {
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
          rp.addEventListener('click', () =>
            window.toast('探測完成', 'ok', i.symbol + ' 板別維持 ' + (i.board || '未解析') + '（設計稿）'));
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
    if (i.target_low !== null) tgtIn.value = i.target_low;
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
    ok.addEventListener('click', () => {
      i.sector = secIn.value.trim() || i.sector;
      const t = parseFloat(tgtIn.value);
      i.target_low = (tgtIn.value.trim() === '' || isNaN(t) || t <= 0) ? null : t;
      dismiss();
      render($('#inst-search').value);
      window.toast('已儲存', 'ok', i.symbol + '：目標價' + (i.target_low === null ? '已關閉' : ' ≤ ' + f.price(i.target_low, i.ccy)) + '（設計稿）');
    });
    document.body.appendChild(backdrop);
    setTimeout(() => tgtIn.focus(), 50);
  }

  render();
  $('#inst-search').addEventListener('input', (e) => render(e.target.value));
})();
