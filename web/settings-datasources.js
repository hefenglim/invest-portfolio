/* portfolio-dash — 資料來源設定頁 (mock + rendering) */
window.DATASOURCES_DATA = {
  "sources": [
    { "id": "twse",       "name": "台灣證券交易所 (TWSE)", "type": "stock",    "markets": ["TW"],     "auth": "none",  "token_masked": null,   "status": "ok",    "last_test": "2026-06-11T14:30:04+08:00", "latency_ms": 312,  "note": "台股收盤報價・免金鑰" },
    { "id": "tpex",       "name": "櫃買中心 (TPEx)",       "type": "stock",    "markets": ["TW"],     "auth": "none",  "token_masked": null,   "status": "ok",    "last_test": "2026-06-11T14:30:06+08:00", "latency_ms": 445,  "note": "上櫃股票報價・免金鑰" },
    { "id": "yfinance",   "name": "Yahoo Finance",         "type": "stock",    "markets": ["US","MY"],"auth": "none",  "token_masked": null,   "status": "ok",    "last_test": "2026-06-11T05:30:02+08:00", "latency_ms": 820,  "note": "美股、馬股、ETF 報價・免金鑰（有速率限制）" },
    { "id": "alphavantage","name": "Alpha Vantage",        "type": "stock",    "markets": ["US"],     "auth": "apikey","token_masked": "ak-•••4F2","status": "ok","last_test": "2026-06-10T12:00:00+08:00","latency_ms": 1240, "note": "美股後備來源・免費層 25 req/day" },
    { "id": "klse",       "name": "KLSE Screener",         "type": "stock",    "markets": ["MY"],     "auth": "none",  "token_masked": null,   "status": "error", "last_test": "2026-06-10T17:30:06+08:00", "latency_ms": null, "note": "馬股後備來源・HTTP 502" },
    { "id": "finmind",    "name": "FinMind",               "type": "dividend", "markets": ["TW"],     "auth": "apikey","token_masked": "fm-•••9b1","status": "ok","last_test": "2026-06-11T03:00:05+08:00","latency_ms": 650,  "note": "台股股利、除息行事曆・付費 API" },
    { "id": "divtracker", "name": "Dividend Tracker API",  "type": "dividend", "markets": ["US"],     "auth": "apikey","token_masked": null,   "status": "off",   "last_test": null, "latency_ms": null, "note": "美股股利資料・尚未設定金鑰" },
    { "id": "newsapi",    "name": "NewsAPI.org",           "type": "news",     "markets": ["ALL"],    "auth": "apikey","token_masked": null,   "status": "off",   "last_test": null, "latency_ms": null, "note": "財經新聞截取・尚未設定金鑰" },
    { "id": "fx_ecb",     "name": "ECB 歐洲央行匯率",      "type": "fx",       "markets": ["ALL"],    "auth": "none",  "token_masked": null,   "status": "ok",    "last_test": "2026-06-11T00:00:02+08:00", "latency_ms": 290,  "note": "每日匯率・免金鑰" }
  ],
  "account_fallbacks": {
    "tw_broker":    ["twse", "tpex", "yfinance"],
    "schwab":       ["yfinance", "alphavantage"],
    "moomoo_my_us": ["yfinance", "alphavantage"],
    "moomoo_my_my": ["yfinance", "klse"]
  },
  "account_names": {
    "tw_broker": "台灣券商", "schwab": "嘉信 Schwab",
    "moomoo_my_us": "Moomoo 美股", "moomoo_my_my": "Moomoo 馬股"
  }
};

(function () {
  'use strict';
  const D = window.DATASOURCES_DATA;
  const f = window.fmt;
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  const TYPE_LABEL = { stock: '報價', dividend: '股利', fx: '匯率', news: '新聞' };
  const TYPE_CLS   = { stock: 'type-chip chip-cash', dividend: 'type-chip chip-drip', fx: 'type-chip chip-net', news: 'type-chip chip-stock' };
  const STATUS_CLASS = { ok: 'dot-ok', error: 'dot-err', off: 'dot-gray', unknown: 'dot-gray' };

  /* ---- source table ---- */
  function renderSources() {
    const groups = { stock: [], dividend: [], fx: [], news: [] };
    D.sources.forEach((s) => (groups[s.type] = groups[s.type] || []).push(s));
    const wrap = $('#sources-wrap');
    wrap.replaceChildren();
    Object.keys(groups).forEach((type) => {
      if (!groups[type].length) return;
      const sec = el('div', 'ds-section');
      const secHead = el('div', 'ds-sec-head');
      secHead.appendChild(el('span', TYPE_CLS[type], TYPE_LABEL[type]));
      sec.appendChild(secHead);
      const table = el('table', 'data');
      const thead = el('thead');
      const hr = el('tr');
      ['狀態','來源','市場','認證','延遲','上次測試','備註',''].forEach((h, i) => {
        const th = el('th', i <= 1 || i === 6 ? 'col-text' : null, h);
        hr.appendChild(th);
      });
      thead.appendChild(hr);
      table.appendChild(thead);
      const tbody = el('tbody');
      groups[type].forEach((s) => {
        const tr = el('tr');
        /* status dot */
        const tdSt = el('td');
        const dot = el('span', 'run-dot ' + STATUS_CLASS[s.status]);
        dot.title = s.status === 'ok' ? '連線正常' : s.status === 'error' ? '連線失敗' : '未啟用';
        tdSt.appendChild(dot);
        tr.appendChild(tdSt);
        /* name */
        const tdName = el('td', 'col-text');
        tdName.appendChild(el('span', null, s.name));
        if (s.status === 'error') tdName.appendChild(el('div', 'err-inline', '連線失敗・見備註'));
        tr.appendChild(tdName);
        /* markets */
        const tdM = el('td', 'col-text');
        s.markets.forEach((m) => {
          const chip = el('span', 'board-badge', m);
          chip.style.marginRight = '4px';
          tdM.appendChild(chip);
        });
        tr.appendChild(tdM);
        /* auth */
        const tdAuth = el('td', 'col-text');
        if (s.auth === 'none') {
          tdAuth.appendChild(el('span', 'sign-nil', '免金鑰'));
        } else {
          const row = el('div', 'ds-key-row');
          const hasKey = !!s.token_masked;
          row.appendChild(el('span', 'cron-code', s.token_masked || '未設定'));
          const resetBtn = el('button', 'btn', hasKey ? '重設' : '設定金鑰');
          resetBtn.type = 'button';
          resetBtn.style.fontSize = '10px';
          resetBtn.style.padding = '1px 8px';
          if (!hasKey) resetBtn.classList.add('btn-primary');
          resetBtn.addEventListener('click', () =>
            window.toast(hasKey ? '金鑰已更新' : '金鑰已設定', 'ok', s.name + '（設計稿）'));
          row.appendChild(resetBtn);
          tdAuth.appendChild(row);
        }
        tr.appendChild(tdAuth);
        /* latency */
        const tdLat = el('td', 'num');
        if (!s.latency_ms) { tdLat.textContent = f.NULL_GLYPH; tdLat.classList.add('sign-nil'); }
        else tdLat.textContent = s.latency_ms + ' ms';
        tr.appendChild(tdLat);
        /* last test */
        tr.appendChild(el('td', 'num', s.last_test ? f.datetime(s.last_test) : f.NULL_GLYPH));
        /* note */
        tr.appendChild(el('td', 'col-text', s.note || ''));
        /* test button */
        const tdTest = el('td');
        const testBtn = el('button', 'btn', '測試');
        testBtn.type = 'button';
        testBtn.addEventListener('click', () => {
          testBtn.disabled = true; testBtn.textContent = '…';
          setTimeout(() => {
            testBtn.disabled = false; testBtn.textContent = '測試';
            if (s.status === 'error') window.toast('連線失敗', 'fail', s.name + '：' + (s.note || ''));
            else window.toast('連線正常', 'ok', s.name + '・' + (s.latency_ms || '?') + ' ms');
          }, 800);
        });
        tdTest.appendChild(testBtn);
        tr.appendChild(tdTest);
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      sec.appendChild(table);
      wrap.appendChild(sec);
    });
  }

  /* ---- per-account fallback order — real drag-and-drop ---- */
  function renderFallbacks() {
    const wrap = $('#fallback-wrap');
    wrap.replaceChildren();
    Object.keys(D.account_fallbacks).forEach((accId) => {
      const order = [...D.account_fallbacks[accId]]; // mutable copy
      const card = el('div', 'fallback-card');
      card.appendChild(el('div', 'fallback-acct', D.account_names[accId] || accId));
      const chips = el('div', 'fallback-chips');
      card.appendChild(chips);
      card.appendChild(el('div', 'fb-hint', '拖曳排序・順序即 fallback 優先順序'));

      function buildChips() {
        chips.replaceChildren();
        let dragSrc = null;
        order.forEach((srcId, i) => {
          const src = D.sources.find((s) => s.id === srcId);
          const chip = el('div', 'fb-chip');
          chip.draggable = true;
          chip.dataset.idx = String(i);
          chip.appendChild(el('span', 'fb-num', String(i + 1)));
          chip.appendChild(el('span', 'fb-name', src ? src.name.split(/s/)[0] : srcId));
          const dot = el('span', 'run-dot ' + STATUS_CLASS[(src && src.status) || 'unknown']);
          dot.style.marginLeft = 'auto';
          chip.appendChild(dot);
          chip.addEventListener('dragstart', (e) => {
            dragSrc = i;
            chip.style.opacity = '0.45';
            e.dataTransfer.effectAllowed = 'move';
          });
          chip.addEventListener('dragend', () => { chip.style.opacity = ''; });
          chip.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            chip.style.outline = '2px solid var(--accent)';
          });
          chip.addEventListener('dragleave', () => { chip.style.outline = ''; });
          chip.addEventListener('drop', (e) => {
            e.preventDefault();
            chip.style.outline = '';
            const from = dragSrc;
            const to = i;
            if (from === null || from === to) return;
            dragSrc = null;
            const moved = order.splice(from, 1)[0];
            order.splice(to, 0, moved);
            D.account_fallbacks[accId] = [...order];
            buildChips();
            window.toast('順序已更新', 'ok', D.account_names[accId] + ' fallback 順序已調整');
          });
          chips.appendChild(chip);
        });
      }
      buildChips();
      wrap.appendChild(card);
    });
  }

  renderSources();
  renderFallbacks();
})();
