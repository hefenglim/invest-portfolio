/* portfolio-dash — 資料來源設定頁 (wired to /api/datasources/*, spec 14/20/19).

   Boot: GET /api/datasources -> { sources:[...], market_order:{TW:[srcId,...]},
   market_order_available:{TW:[srcId,...]} }. The old per-ACCOUNT fallback wire is
   superseded (2026-07-03, item 9): quote routing belongs to the MARKET, and the
   stored order is consumed by the real fetch chain (default_registry).

   A source: { id, name, type, markets, auth, provides, token_masked, status, last_test,
   latency_ms, tier, tiers, note }. `latency_ms` is a COUNT (not money) — no fmt-money
   path needed; no `.toFixed` on any value here. Real `type` set spans stock/dividend/
   sentiment/fx/macro/trends/news; `status` spans ok/error/off/unknown/pending/blocked;
   `auth` spans none/apikey/oauth — the render is robust to all of them.

   Write paths (all via pdApi; success -> toast + re-fetch; PdApiError -> toast(message,
   'fail', code); try/catch graceful so a failure never throws an unhandled rejection):
   - PUT /api/datasources/{id}/key        (set / clear API key)
   - PUT /api/datasources/{id}/tier       (mark token tier — spec 20)
   - POST /api/datasources/{id}/test      (connection test -> health row)
   - PUT /api/datasources/market-order    (per-market quote-chain reorder) */
(function () {
  'use strict';
  const f = window.fmt;
  const api = window.pdApi;
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  function _toast(msg, kind, code) {
    if (window.toast) window.toast(msg, kind, code);
  }

  /* Structural data from GET /api/datasources. Starts empty so a pre-fetch render
     is blank; populated on boot. */
  let sources = [];
  let marketOrder = {};      // {TW: [srcId,...], US: [...], MY: [...]} — the REAL chain
  let marketAvailable = {};  // {TW: [srcId,...]} — providers capable of that market
  const MARKET_LABEL = { TW: '台股', US: '美股', MY: '馬股' };

  /* Display order + labels for the type groups (any unknown type falls under 其他). */
  const TYPE_ORDER = ['stock', 'dividend', 'fx', 'sentiment', 'macro', 'trends', 'news'];
  const TYPE_LABEL = {
    stock: '報價', dividend: '股利', fx: '匯率', sentiment: '情緒',
    macro: '總經', trends: '趨勢', news: '新聞', other: '其他',
  };
  const TYPE_CLS = {
    stock: 'type-chip chip-cash', dividend: 'type-chip chip-drip', fx: 'type-chip chip-net',
    sentiment: 'type-chip chip-stock', macro: 'type-chip', trends: 'type-chip',
    news: 'type-chip chip-stock', other: 'type-chip',
  };
  const STATUS_CLASS = {
    ok: 'dot-ok', error: 'dot-err', off: 'dot-gray', unknown: 'dot-gray',
    pending: 'dot-gray', blocked: 'dot-err',
  };
  const STATUS_TITLE = {
    ok: '連線正常', error: '連線失敗', off: '未啟用', unknown: '未測試',
    pending: '待測試', blocked: '受阻',
  };

  function srcById(id) { return sources.find((s) => s.id === id); }

  /* ---- source table ---- */
  function renderSources() {
    const wrap = $('#sources-wrap');
    if (!wrap) return;
    wrap.replaceChildren();
    /* group by type, preserving TYPE_ORDER then any leftover types as 其他. */
    const groups = {};
    sources.forEach((s) => {
      const key = TYPE_ORDER.indexOf(s.type) === -1 ? 'other' : s.type;
      (groups[key] = groups[key] || []).push(s);
    });
    const order = TYPE_ORDER.filter((t) => groups[t]);
    if (groups.other) order.push('other');
    order.forEach((type) => {
      const sec = el('div', 'ds-section');
      const secHead = el('div', 'ds-sec-head');
      secHead.appendChild(el('span', TYPE_CLS[type] || 'type-chip', TYPE_LABEL[type] || type));
      sec.appendChild(secHead);
      const table = el('table', 'data');
      const thead = el('thead');
      const hr = el('tr');
      ['狀態', '來源', '市場', '認證', '延遲', '上次測試', '備註', ''].forEach((h, i) => {
        hr.appendChild(el('th', i <= 1 || i === 6 ? 'col-text' : null, h));
      });
      thead.appendChild(hr);
      table.appendChild(thead);
      const tbody = el('tbody');
      groups[type].forEach((s) => tbody.appendChild(renderSourceRow(s)));
      table.appendChild(tbody);
      sec.appendChild(table);
      wrap.appendChild(sec);
    });
  }

  function renderSourceRow(s) {
    const tr = el('tr');
    /* status dot */
    const tdSt = el('td');
    const dot = el('span', 'run-dot ' + (STATUS_CLASS[s.status] || 'dot-gray'));
    dot.title = STATUS_TITLE[s.status] || s.status || '';
    tdSt.appendChild(dot);
    tr.appendChild(tdSt);
    /* name */
    const tdName = el('td', 'col-text');
    tdName.appendChild(el('span', null, s.name));
    if (s.status === 'error') tdName.appendChild(el('div', 'err-inline', '連線失敗・見備註'));
    tr.appendChild(tdName);
    /* markets */
    const tdM = el('td', 'col-text');
    (s.markets || []).forEach((m) => {
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
      resetBtn.addEventListener('click', () => openKeyModal(s));
      row.appendChild(resetBtn);
      /* tier dropdown when the source offers selectable tiers (spec 20). */
      if (s.tiers && s.tiers.length) {
        const tierSel = el('select', 'select');
        tierSel.style.fontSize = '10px';
        tierSel.style.padding = '1px 6px';
        tierSel.title = '資費等級';
        const none = el('option', null, '（未標記）'); none.value = '';
        tierSel.appendChild(none);
        s.tiers.forEach((t) => {
          const o = el('option', null, t); o.value = t;
          if (s.tier === t) o.selected = true;
          tierSel.appendChild(o);
        });
        if (s.tier == null) none.selected = true;
        tierSel.addEventListener('change', async () => {
          try {
            await api.put('/api/datasources/' + encodeURIComponent(s.id) + '/tier',
              { tier: tierSel.value || null });
            _toast('資費等級已更新', 'ok', s.name + (tierSel.value ? ' · ' + tierSel.value : ''));
            await boot();
          } catch (err) {
            _toast((err && err.message) || '更新失敗', 'fail', err && err.code);
            await boot();
          }
        });
        row.appendChild(tierSel);
      }
      tdAuth.appendChild(row);
    }
    tr.appendChild(tdAuth);
    /* latency (a count, not money) */
    const tdLat = el('td', 'num');
    if (s.latency_ms == null) { tdLat.textContent = f.NULL_GLYPH; tdLat.classList.add('sign-nil'); }
    else tdLat.textContent = f.num(s.latency_ms) + ' ms';
    tr.appendChild(tdLat);
    /* last test */
    tr.appendChild(el('td', 'num', s.last_test ? f.datetime(s.last_test) : f.NULL_GLYPH));
    /* note */
    tr.appendChild(el('td', 'col-text', s.note || ''));
    /* test button -> POST /api/datasources/{id}/test */
    const tdTest = el('td');
    const testBtn = el('button', 'btn', '測試');
    testBtn.type = 'button';
    testBtn.addEventListener('click', async () => {
      testBtn.disabled = true; testBtn.textContent = '…';
      try {
        const resp = await api.post('/api/datasources/' + encodeURIComponent(s.id) + '/test');
        const ok = resp && resp.status === 'ok';
        const lat = resp && resp.latency_ms;
        _toast(ok ? '連線正常' : '連線失敗', ok ? 'ok' : 'fail',
          s.name + (ok && lat != null ? '・' + lat + ' ms'
            : '・' + ((resp && resp.detail) || '無法連線')));
        await boot();
      } catch (err) {
        _toast((err && err.message) || '測試失敗', 'fail', err && err.code);
      } finally {
        testBtn.disabled = false; testBtn.textContent = '測試';
      }
    });
    tdTest.appendChild(testBtn);
    tr.appendChild(tdTest);
    return tr;
  }

  /* ---- API-key modal -> PUT /api/datasources/{id}/key ---- */
  function openKeyModal(s) {
    const hasKey = !!s.token_masked;
    const backdrop = el('div', 'modal-backdrop');
    const modal = el('div', 'modal');
    const head = el('div', 'modal-head');
    head.appendChild(el('h3', 'modal-title', (hasKey ? '重設金鑰 — ' : '設定金鑰 — ') + s.name));
    const close = el('button', 'modal-close', '✕'); close.type = 'button';
    head.appendChild(close);
    modal.appendChild(head);
    const body = el('div', 'modal-body');
    const field = el('div', 'field');
    field.appendChild(el('label', null, 'API Key'));
    const inp = el('input', 'input');
    inp.type = 'text'; inp.spellcheck = false; inp.placeholder = '貼上新的 API Key…';
    inp.style.fontFamily = 'var(--font-num)';
    field.appendChild(inp);
    field.appendChild(el('span', 'hint', '永不顯示既存金鑰；留空並送出可清除金鑰。'));
    body.appendChild(field);
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
      ok.disabled = true;
      try {
        await api.put('/api/datasources/' + encodeURIComponent(s.id) + '/key',
          { api_key: inp.value });
        dismiss();
        _toast(inp.value ? '金鑰已更新' : '金鑰已清除', 'ok', s.name);
        await boot();
      } catch (err) {
        ok.disabled = false;
        _toast((err && err.message) || '金鑰更新失敗', 'fail', err && err.code);
      }
    });
    document.body.appendChild(backdrop);
    setTimeout(() => inp.focus(), 50);
  }

  /* ---- per-MARKET quote order (item 9) — drag-and-drop + add/remove ->
     PUT /api/datasources/market-order. One coherent logic: the LIST above says
     which sources exist and how healthy they are; the ORDER below says, per
     market, who gets asked first when fetching quotes — and it is the REAL
     chain default_registry walks (scheduler + manual refresh alike). ---- */
  function renderMarketOrder() {
    const wrap = $('#market-order-wrap');
    if (!wrap) return;
    wrap.replaceChildren();
    ['TW', 'US', 'MY'].forEach((mkt) => {
      const order = [...(marketOrder[mkt] || [])]; /* mutable copy */
      const available = marketAvailable[mkt] || [];
      const card = el('div', 'fallback-card');
      card.appendChild(el('div', 'fallback-acct', (MARKET_LABEL[mkt] || mkt) + '（' + mkt + '）'));
      const chips = el('div', 'fallback-chips');
      card.appendChild(chips);
      const addRow = el('div', 'fb-hint');
      card.appendChild(addRow);
      card.appendChild(el('div', 'fb-hint', '拖曳排序・第 1 順位優先；✕ 移出鏈（來源仍在上方清單）'));

      function persist() {
        api.put('/api/datasources/market-order', { market: mkt, order: [...order] })
          .then((resp) => {
            marketOrder = (resp && resp.market_order) || marketOrder;
            _toast('抓取順位已更新', 'ok',
              (MARKET_LABEL[mkt] || mkt) + '：' + order.join(' → '));
            renderMarketOrder();
          })
          .catch((err) => {
            _toast((err && err.message) || '順位更新失敗', 'fail', err && err.code);
            boot(); /* re-sync to server's last-good order */
          });
      }

      function buildAddRow() {
        addRow.replaceChildren();
        const missing = available.filter((id) => order.indexOf(id) === -1);
        if (!missing.length) return;
        const sel = el('select', 'select');
        sel.style.fontSize = '10px'; sel.style.padding = '1px 6px';
        const ph = el('option', null, '＋ 加入來源…'); ph.value = '';
        sel.appendChild(ph);
        missing.forEach((id) => {
          const src = srcById(id);
          const o = el('option', null, src ? src.name : id); o.value = id;
          sel.appendChild(o);
        });
        sel.addEventListener('change', () => {
          if (!sel.value) return;
          order.push(sel.value);
          buildChips();
          buildAddRow();
          persist();
        });
        addRow.appendChild(sel);
      }

      function buildChips() {
        chips.replaceChildren();
        let dragSrc = null;
        order.forEach((srcId, i) => {
          const src = srcById(srcId);
          const chip = el('div', 'fb-chip');
          chip.draggable = true;
          chip.dataset.idx = String(i);
          chip.appendChild(el('span', 'fb-num', String(i + 1)));
          chip.appendChild(el('span', 'fb-name', src ? src.name : srcId));
          const dot = el('span', 'run-dot ' + (STATUS_CLASS[(src && src.status) || 'unknown'] || 'dot-gray'));
          dot.title = STATUS_TITLE[(src && src.status) || 'unknown'] || '';
          dot.style.marginLeft = 'auto';
          chip.appendChild(dot);
          if (order.length > 1) {
            const rm = el('button', 'modal-close', '✕');
            rm.type = 'button';
            rm.title = '從此市場的抓取鏈移除';
            rm.style.fontSize = '10px';
            rm.addEventListener('click', () => {
              order.splice(i, 1);
              buildChips();
              buildAddRow();
              persist();
            });
            chip.appendChild(rm);
          }
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
            buildChips();
            persist();
          });
          chips.appendChild(chip);
        });
      }
      buildChips();
      buildAddRow();
      wrap.appendChild(card);
    });
  }

  /* ===== boot: GET /api/datasources, then render. Graceful: on failure leave the page
     empty + surface ONE toast (never an unhandled rejection — the e2e smoke asserts ZERO
     console errors). 401 is handled inside api.js. ===== */
  async function boot() {
    try {
      const resp = await api.get('/api/datasources');
      sources = (resp && resp.sources) || [];
      marketOrder = (resp && resp.market_order) || {};
      marketAvailable = (resp && resp.market_order_available) || {};
    } catch (err) {
      _toast('資料來源載入失敗', 'fail', (err && err.message) || undefined);
      sources = []; marketOrder = {}; marketAvailable = {};
    }
    renderSources();
    renderMarketOrder();
  }

  boot();
})();
