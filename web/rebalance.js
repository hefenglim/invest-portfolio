/* portfolio-dash — E4 再平衡試算器 (compute-only, never writes).
   持倉面板「再平衡試算」按鈕 → 抽屜：調整各標的目標權重，由後端
   POST /api/rebalance/preview 算出需買/賣股數、費稅、試算後權重。
   體驗債修復：列建立一次、輸入時僅更新計算欄（250ms debounce），輸入框不重繪不失焦。

   DATA SOURCE (spec 19, Task 3.1 + defer ③): holdings come from the SHARED
   window.pdDashboard promise (GET /api/dashboard, reused from app.js / charts.js /
   alerts.js / detail.js — one fetch per page). The retired window.DASHBOARD_DATA mock
   is no longer read. Money/price values (h.market_price, h.weight) are Decimal STRINGS
   displayed via window.fmt (f.*), which coerce internally.

   BACKEND-AUTHORITATIVE (defer ③): the trade plan is NO LONGER a client-side estimate.
   On each (debounced) target edit this POSTs the user's target weight RATIOS as STRINGS
   to /api/rebalance/preview — the AUTHORITATIVE computation (REAL fee engine compute_fees,
   real FX via RateResolver, integer-share / MY-100-lot snapping). This module computes NO
   money: it renders the backend `rows` (side/shares/amount/fee+tax/new_weight) + `summary`
   (turnover_reporting / total_fees_reporting / cash_after), all Decimal STRINGS via f.*.
   The only client number is the target-weight RATIO state — a UI percentage, not money.

   Requires: api.js (window.pdApi), format.js (window.fmt). */
(function () {
  'use strict';
  const f = window.fmt;
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const CAP = 0.30; /* 預警門檻：單一標的上限（與 alerts.js 規則一致） */
  /* Reporting ccy for the summary footer (turnover / fees / cash are reporting-ccy). The
     dashboard's combined view reports in TWD; the backend keys the values *_reporting. */
  const REPORTING = 'TWD';

  function close() {
    const b = document.querySelector('.rb-backdrop');
    if (b) b.remove();
    document.removeEventListener('keydown', onKey);
  }
  function onKey(e) { if (e.key === 'Escape') close(); }

  async function open() {
    close();
    let D;
    try {
      D = await (window.pdDashboard || (window.pdDashboard = window.pdApi.get('/api/dashboard')));
    } catch (e) {
      /* api.js already redirected on 401; for other failures surface a toast and bail —
         never throw (the e2e smoke asserts ZERO console errors / pageerrors). */
      if (window.toast) window.toast('無法載入持倉資料，請稍後再試', 'fail');
      return;
    }
    if (!D || !Array.isArray(D.holdings) || !D.holdings.length) {
      if (window.toast) window.toast('目前沒有可試算的持倉', 'info');
      return;
    }
    /* D8: prefill target weights from the server-side 目標配置 (single source of truth). A
       symbol with a stored target seeds its input from that ratio; the rest fall back to the
       current weight. Non-fatal — a failure just keeps the current-weight seed. */
    const storedTargets = {};
    try {
      const tw = await window.pdApi.get('/api/target-weights');
      (tw && tw.symbols || []).forEach((s) => {
        if (s.weight !== null && s.weight !== undefined) storedTargets[s.symbol] = Number(s.weight);
      });
    } catch (e) { /* fall back to current weights */ }
    const priced = D.holdings.filter((h) => h.market_price !== null && h.market_price !== undefined && h.weight !== null);
    const unpriced = D.holdings.filter((h) => h.market_price === null || h.market_price === undefined || h.weight === null);

    const backdrop = el('div', 'sd-backdrop rb-backdrop');
    const drawer = el('div', 'sd-drawer rb-drawer');
    backdrop.appendChild(drawer);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
    document.addEventListener('keydown', onKey);

    const head = el('div', 'sd-head');
    head.appendChild(el('span', 'sym-code', '再平衡試算'));
    head.appendChild(el('span', 'sd-sim-badge', '試算不寫入帳本'));
    const x = el('button', 'sd-close', '✕');
    x.type = 'button';
    x.addEventListener('click', close);
    head.appendChild(el('span', 'header-spacer'));
    head.appendChild(x);
    drawer.appendChild(head);

    const body = el('div', 'sd-body');
    drawer.appendChild(body);

    /* controls */
    const bar = el('div', 'rb-bar');
    const capBtn = el('button', 'btn', '套用預警上限（單檔 ≤ ' + (CAP * 100) + '%）');
    capBtn.type = 'button';
    capBtn.title = '將超過上限的標的設為上限值，其餘維持現權重；釋出部分視為現金';
    const resetBtn = el('button', 'btn', '重設為現權重');
    resetBtn.type = 'button';
    bar.appendChild(capBtn);
    bar.appendChild(resetBtn);
    body.appendChild(bar);

    /* table */
    const wrap = el('div', 'table-wrap');
    const table = el('table', 'data rb-table');
    table.innerHTML = '<thead><tr>' +
      '<th class="col-text">代號</th><th>現權重</th><th>目標 %</th>' +
      '<th class="col-text">動作</th><th>預估金額（原幣）</th><th>費稅（原幣）</th><th>試算後權重</th>' +
      '</tr></thead>';
    const tbody = el('tbody');
    table.appendChild(tbody);
    wrap.appendChild(table);
    body.appendChild(wrap);

    const foot = el('div', 'rb-foot');
    body.appendChild(foot);
    if (unpriced.length) {
      body.appendChild(el('div', 'sd-chart-note',
        '缺價標的不參與試算：' + unpriced.map((h) => h.symbol).join('、')));
    }
    body.appendChild(el('div', 'sd-mock-note',
      '費稅與股數由後端 /api/rebalance/preview 依各帳戶費率規則與現匯計算（買賣皆計，整數股／馬股 100 股一手；缺價標的排除）。試算不寫入帳本（spec 03）。'));

    /* state — the what-if target weight RATIO per symbol, seeded from the backend weight.
       h.weight is a Decimal STRING; Number() makes the seed into a numeric UI ratio. This
       is a target PERCENTAGE (a UI weight), NOT money — the only money/share numbers come
       back from the backend preview below and render through f.*. */
    const state = {};
    priced.forEach((h) => {
      state[h.symbol] = (storedTargets[h.symbol] !== undefined)
        ? storedTargets[h.symbol] : Number(h.weight);  /* D8 target prefill, else current */
    });

    /* build rows ONCE; keep refs to computed cells, keyed by symbol for backend matching */
    const rowsBySym = {};
    priced.forEach((h) => {
      const tr = el('tr');
      const tdSym = el('td', 'col-text');
      const cell = el('div', 'sym-cell');
      cell.appendChild(el('span', 'sym-code', h.symbol));
      cell.appendChild(el('span', 'sym-name', h.name));
      tdSym.appendChild(cell);
      tr.appendChild(tdSym);
      const tdCur = el('td', 'num', f.pct(h.weight));  /* backend weight via f.* */
      if (state[h.symbol] > CAP) tdCur.classList.add('sign-up');  /* numeric what-if seed */
      tr.appendChild(tdCur);
      const tdT = el('td', 'num');
      const inp = el('input', 'rb-input');
      inp.type = 'number'; inp.min = '0'; inp.max = '100'; inp.step = '0.5';
      inp.value = (state[h.symbol] * 100).toFixed(1);  /* seed control from numeric what-if state */
      tdT.appendChild(inp);
      tr.appendChild(tdT);
      const tdAct = el('td', 'col-text');
      const tdAmt = el('td', 'num');
      const tdFee = el('td', 'num');
      const tdNew = el('td', 'num');
      tr.appendChild(tdAct);
      tr.appendChild(tdAmt);
      tr.appendChild(tdFee);
      tr.appendChild(tdNew);
      tbody.appendChild(tr);
      inp.addEventListener('input', () => {
        state[h.symbol] = Math.max(0, Number(inp.value) / 100 || 0);
        schedule();
      });
      rowsBySym[h.symbol] = { h, inp, tdAct, tdAmt, tdFee, tdNew };
    });
    const rows = priced.map((h) => rowsBySym[h.symbol]);

    /* clear all computed cells to the null glyph (used while a preview is in flight / on error) */
    function clearComputed() {
      rows.forEach((r) => {
        r.tdAct.replaceChildren();
        r.tdAct.appendChild(el('span', 'sign-nil', f.NULL_GLYPH));
        r.tdAmt.textContent = f.NULL_GLYPH;
        r.tdFee.textContent = f.NULL_GLYPH;
        r.tdNew.textContent = f.NULL_GLYPH;
        r.tdNew.classList.remove('sign-up');
      });
    }

    /* render ONE backend row (a Decimal-STRING trade) into its table row via f.* */
    function renderRow(r, br) {
      r.tdAct.replaceChildren();
      const side = br.side;
      r.tdAct.appendChild(el('span', 'dir-chip ' + (side === 'buy' ? 'dir-buy' : 'dir-sell'),
        side === 'buy' ? '買' : '賣'));
      r.tdAct.appendChild(document.createTextNode(' ' + f.num(br.shares) + ' 股'));
      const ccy = br.ccy;
      r.tdAmt.textContent = f.money(br.amount, ccy) + ' ' + ccy;
      /* fee + tax are separate Decimal strings; show their combined cost (display only,
         the backend already computed both — Number() here is presentation, not money math). */
      const feeTax = Number(br.fee) + Number(br.tax);
      r.tdFee.textContent = f.money(feeTax, ccy);
      r.tdNew.textContent = f.pct(br.new_weight);
      r.tdNew.classList.toggle('sign-up', Number(br.new_weight) > CAP);
    }

    function renderFoot(summary, sumTarget) {
      foot.replaceChildren();
      const cash = 1 - sumTarget;
      const kv = (k, v, cls) => {
        const s = el('span', 'rb-kv');
        s.appendChild(el('span', 'k', k));
        s.appendChild(el('span', 'v num' + (cls ? ' ' + cls : ''), v));
        return s;
      };
      foot.appendChild(kv('目標合計', f.pct(sumTarget), sumTarget > 1.0001 ? 'sign-up' : ''));
      foot.appendChild(kv('現金水位', f.pct(Math.max(0, cash))));
      const turnover = summary ? summary.turnover_reporting : null;
      const fees = summary ? summary.total_fees_reporting : null;
      foot.appendChild(kv('預估周轉額', f.money(turnover, REPORTING) + ' ' + REPORTING));
      foot.appendChild(kv('預估總費稅', f.money(fees, REPORTING) + ' ' + REPORTING,
        fees != null && Number(fees) > 0 ? 'sign-up' : ''));
      if (sumTarget > 1.0001) {
        foot.appendChild(el('span', 'rb-warn', '⚠ 目標合計超過 100% — 請下調部分標的'));
      }
    }

    let timer = null;
    function schedule() { clearTimeout(timer); timer = setTimeout(update, 250); }

    async function update() {
      /* sumTarget is a pure UI percentage (NOT money) — drives the 目標合計 / 現金水位 hints. */
      let sumTarget = 0;
      const targets = {};
      rows.forEach((r) => {
        const ratio = state[r.h.symbol];
        sumTarget += ratio;
        /* send ratios as STRINGS so Pydantic parses EXACT Decimals (avoids JS-float drift). */
        targets[r.h.symbol] = String(ratio);
      });

      /* cancel any prior in-flight preview so a newer edit wins (typeahead-style). */
      const ctrl = window.pdApi.abortable('rebalance-preview');
      let result;
      try {
        result = await window.pdApi.post('/api/rebalance/preview', { targets },
          { signal: ctrl.signal });
      } catch (err) {
        if (err && err.name === 'AbortError') return;  /* superseded by a newer edit */
        if (window.toast) {
          window.toast(err && err.message ? err.message : '再平衡試算失敗',
            'fail', err && err.code);
        }
        return;
      }

      const backendRows = (result && Array.isArray(result.rows)) ? result.rows : [];
      const byRow = {};
      backendRows.forEach((br) => { byRow[br.symbol] = br; });
      rows.forEach((r) => {
        const br = byRow[r.h.symbol];
        if (br) {
          renderRow(r, br);
        } else {
          /* no trade for this symbol (on target, rounds to nothing, or excluded) */
          r.tdAct.replaceChildren();
          r.tdAct.appendChild(el('span', 'sign-nil', f.NULL_GLYPH));
          r.tdAmt.textContent = f.NULL_GLYPH;
          r.tdFee.textContent = f.NULL_GLYPH;
          r.tdNew.textContent = f.NULL_GLYPH;
          r.tdNew.classList.remove('sign-up');
        }
      });
      renderFoot(result && result.summary, sumTarget);
    }

    capBtn.addEventListener('click', () => {
      rows.forEach((r) => {
        state[r.h.symbol] = Math.min(Number(r.h.weight), CAP);  /* numeric what-if seed */
        r.inp.value = (state[r.h.symbol] * 100).toFixed(1);
      });
      update();
    });
    resetBtn.addEventListener('click', () => {
      rows.forEach((r) => {
        state[r.h.symbol] = Number(r.h.weight);  /* reset to backend weight (numeric) */
        r.inp.value = (state[r.h.symbol] * 100).toFixed(1);
      });
      update();
    });

    clearComputed();
    update();
    document.body.appendChild(backdrop);
  }

  /* mount button on holdings panel head */
  const table = document.getElementById('holdings-table');
  if (table) {
    const headBar = table.closest('.panel').querySelector('.panel-head');
    const btn = el('button', 'btn rb-open-btn', '⚖ 再平衡試算');
    btn.type = 'button';
    btn.title = '設定目標權重，試算需買賣的股數與費稅（不寫入）';
    btn.addEventListener('click', open);
    headBar.appendChild(btn);
  }
})();
