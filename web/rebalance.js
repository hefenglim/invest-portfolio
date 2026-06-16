/* portfolio-dash — E4 再平衡試算器 (compute-only, never writes).
   持倉面板「再平衡試算」按鈕 → 抽屜：調整各標的目標權重，
   即時算出需買/賣股數、預估費稅、新權重。費稅估算共用 detail.js 的 pdFeeTax。
   體驗債修復：列建立一次、輸入時僅更新計算欄（250ms debounce），輸入框不重繪不失焦。

   DATA SOURCE (spec 19, Task 3.1): holdings come from the SHARED window.pdDashboard
   promise (GET /api/dashboard, reused from app.js / charts.js / alerts.js / detail.js —
   one fetch per page). The retired window.DASHBOARD_DATA mock is no longer read.
   Money/price values (h.market_price, h.market_value, h.weight, kpis.total_market_value)
   are Decimal STRINGS displayed via window.fmt (f.*), which coerce internally. The
   interactive what-if (target weights → buy/sell qty, pdFeeTax fee estimate, new weights)
   is the documented spec-03 exception: a CLIENT-SIDE estimate over USER INPUT + approximate
   spot rates, NOT a display of backend money-of-record.

   Requires: api.js (window.pdApi), format.js, detail.js (window.pdFeeTax mirror). */
(function () {
  'use strict';
  const f = window.fmt;
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const FX_TWD = { TWD: 1, USD: 32.90, MYR: 7.05 };
  const CAP = 0.30; /* 預警門檻：單一標的上限（與 alerts.js 規則一致） */

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
    const priced = D.holdings.filter((h) => h.market_price !== null && h.market_price !== undefined && h.weight !== null);
    const unpriced = D.holdings.filter((h) => h.market_price === null || h.market_price === undefined || h.weight === null);
    const total = D.kpis.total_market_value;

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
      '費稅依各帳戶費率規則估算（買賣皆計）；金額換算採現匯參考價。整數股限制：台股/美股 1 股、馬股 100 股一手。正式版由後端 /api/rebalance/preview 計算（spec 03）。'));

    /* state — the what-if target weight per symbol, seeded from the backend weight.
       h.weight is a Decimal STRING; Number() makes the coercion into the what-if's
       numeric estimate explicit (this seed feeds the spec-03 client-side estimate, it
       is NOT a money-of-record display — those go through f.* below). */
    const state = {};
    priced.forEach((h) => { state[h.symbol] = Number(h.weight); });
    function lotOf(h) { return h.market === 'MY' ? 100 : 1; }

    /* build rows ONCE; keep refs to computed cells */
    const rows = priced.map((h) => {
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
      return { h, inp, tdAct, tdAmt, tdFee, tdNew };
    });

    let timer = null;
    function schedule() { clearTimeout(timer); timer = setTimeout(update, 250); }

    function update() {
      let sumTarget = 0;
      let totalFeeTwd = 0;
      let turnoverTwd = 0;
      rows.forEach((r) => {
        const h = r.h;
        const target = state[h.symbol];
        sumTarget += target;
        const fx = FX_TWD[h.quote_ccy] || 1;
        const curTwd = h.market_value * fx;
        const deltaTwd = total * target - curTwd;
        const lot = lotOf(h);
        const qty = Math.floor(Math.abs(deltaTwd / fx / h.market_price) / lot) * lot;
        const side = deltaTwd >= 0 ? 'buy' : 'sell';
        let ft = { fee: 0, tax: 0 };
        let amount = 0;
        if (qty > 0 && window.pdFeeTax) {
          ft = window.pdFeeTax(h, side, qty, h.market_price);
          amount = qty * h.market_price;
          totalFeeTwd += (ft.fee + ft.tax) * fx;
          turnoverTwd += amount * fx;
        }
        const newW = (curTwd + (side === 'buy' ? 1 : -1) * qty * h.market_price * fx) / total;
        r.tdAct.replaceChildren();
        if (qty <= 0) {
          r.tdAct.appendChild(el('span', 'sign-nil', '—'));
        } else {
          r.tdAct.appendChild(el('span', 'dir-chip ' + (side === 'buy' ? 'dir-buy' : 'dir-sell'), side === 'buy' ? '買' : '賣'));
          r.tdAct.appendChild(document.createTextNode(' ' + f.num(qty) + ' 股'));
        }
        r.tdAmt.textContent = qty > 0 ? f.money(amount, h.quote_ccy) + ' ' + h.quote_ccy : '—';
        r.tdFee.textContent = qty > 0 ? f.money(ft.fee + ft.tax, h.quote_ccy) : '—';
        r.tdNew.textContent = f.pct(newW);
        r.tdNew.classList.toggle('sign-up', newW > CAP);
      });

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
      foot.appendChild(kv('預估周轉額', f.money(turnoverTwd, 'TWD') + ' TWD'));
      foot.appendChild(kv('預估總費稅', f.money(totalFeeTwd, 'TWD') + ' TWD', totalFeeTwd > 0 ? 'sign-up' : ''));
      if (sumTarget > 1.0001) {
        foot.appendChild(el('span', 'rb-warn', '⚠ 目標合計超過 100% — 請下調部分標的'));
      }
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
