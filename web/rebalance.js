/* portfolio-dash — E4 再平衡試算器 (compute-only, never writes).
   持倉面板「再平衡試算」按鈕 → 抽屜：調整各標的目標權重，由後端
   POST /api/rebalance/preview 算出需買/賣股數、費稅、試算後權重。
   體驗債修復：列建立一次、輸入時僅更新計算欄（250ms debounce），輸入框不重繪不失焦。

   COMBINED CROSS-ACCOUNT (owner ruling 2026-07-13): a symbol held in >1 account is ONE row
   whose target drives the COMBINED position (account chips show the constituents). The
   backend routes the executed trade to concrete accounts and returns per-account `legs`
   (rendered in the action cell) plus the combined `current_weight` / `new_weight`. Targets
   stay SYMBOL-level; the drawer groups the priced holdings by symbol before building rows.

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
    const exportBtn = el('button', 'btn rb-export-btn', '匯出執行報告');
    exportBtn.type = 'button';
    exportBtn.title = '下載目前試算結果為可列印的執行報告（HTML，不寫入帳本）';
    bar.appendChild(capBtn);
    bar.appendChild(resetBtn);
    bar.appendChild(exportBtn);
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

    /* GROUP the priced holdings by SYMBOL. The (account × symbol) identity is preserved as
       the symbol's constituents, but the drawer shows ONE row per symbol (the rebalance
       engine is combined-aware; owner ruling 2026-07-13). A symbol held in >1 account (e.g.
       AAPL in Schwab + Moomoo US) collapses into a single row whose target drives the
       COMBINED position — the old per-holding keying orphaned the first duplicate's cells. */
    const order = [];
    const groups = {};
    priced.forEach((h) => {
      let g = groups[h.symbol];
      if (!g) { g = groups[h.symbol] = { symbol: h.symbol, name: h.name, holdings: [] }; order.push(h.symbol); }
      g.holdings.push(h);
    });
    /* Per group: sort constituents most-shares-first (the chip order + the buy/sell routing
       order the backend uses), and sum the per-holding weight RATIOS into the combined
       current weight. Number() on shares/weight here is display / UI-state math on RATIOS (a
       documented exception), never money — every money/share number of record is backend-fed. */
    Object.keys(groups).forEach((sym) => {
      const g = groups[sym];
      g.holdings.sort((a, b) =>
        (Number(b.shares) - Number(a.shares)) ||
        String(a.account_id).localeCompare(String(b.account_id)));
      g.weightSum = g.holdings.reduce((s, h) => s + (Number(h.weight) || 0), 0);
      g.multi = g.holdings.length > 1;
    });

    /* state — the what-if target weight RATIO per SYMBOL. Seed: a stored 目標配置 target
       wins; else the COMBINED current weight (sum of the constituents' weight ratios). This
       is a target PERCENTAGE (a UI weight), NOT money — the only money/share numbers come
       back from the backend preview below and render through f.*. */
    const state = {};
    order.forEach((sym) => {
      const g = groups[sym];
      state[sym] = (storedTargets[sym] !== undefined) ? storedTargets[sym] : g.weightSum;
    });

    /* build rows ONCE (one per SYMBOL); keep refs to computed cells, keyed by symbol for
       backend matching. One row per symbol removes the duplicate-object orphan bug structurally. */
    const rowsBySym = {};
    order.forEach((sym) => {
      const g = groups[sym];
      const tr = el('tr');
      const tdSym = el('td', 'col-text');
      const cell = el('div', 'sym-cell');
      const idBox = el('div', 'rb-sym-id');
      idBox.appendChild(el('span', 'sym-code', g.symbol));
      idBox.appendChild(el('span', 'sym-name', g.name));
      cell.appendChild(idBox);
      /* account chips: only when the symbol spans >1 account (single-account rows stay
         clean). Built from the dashboard rows' raw data (account_name + shares). */
      if (g.multi) {
        const chips = el('div', 'rb-acct-chips');
        g.holdings.forEach((h) => {
          chips.appendChild(el('span', 'rb-acct-chip',
            h.account_name + ' ' + f.num(h.shares) + '股'));
        });
        cell.appendChild(chips);
      }
      tdSym.appendChild(cell);
      tr.appendChild(tdSym);
      const tdCur = el('td', 'num', f.pct(g.weightSum));  /* COMBINED current weight */
      if (g.weightSum > CAP) tdCur.classList.add('sign-up');
      tr.appendChild(tdCur);
      const tdT = el('td', 'num');
      const inp = el('input', 'rb-input');
      inp.type = 'number'; inp.min = '0'; inp.max = '100'; inp.step = '0.5';
      inp.value = (state[sym] * 100).toFixed(1);  /* seed control from numeric what-if state */
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
        state[sym] = Math.max(0, Number(inp.value) / 100 || 0);
        schedule();
      });
      rowsBySym[sym] = { symbol: sym, group: g, inp, tdCur, tdAct, tdAmt, tdFee, tdNew };
    });
    const rows = order.map((sym) => rowsBySym[sym]);

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

    /* render ONE backend row (a Decimal-STRING trade) into its table row via f.*. The action
       cell renders the executing LEGS (one line per account leg, most-shares first): a single
       leg reads `買 35 股 @ 嘉信 Schwab`; a multi-leg sell shows one line per account, with
       （零股）appended on a TW odd lot. The combined current weight is refreshed from the
       backend once it lands. */
    function renderRow(r, br) {
      r.tdAct.replaceChildren();
      const legs = Array.isArray(br.legs) ? br.legs : [];
      if (legs.length) {
        legs.forEach((lg) => {
          const line = el('div', 'rb-leg');
          line.appendChild(el('span', 'dir-chip ' + (lg.side === 'buy' ? 'dir-buy' : 'dir-sell'),
            lg.side === 'buy' ? '買' : '賣'));
          line.appendChild(document.createTextNode(
            ' ' + f.num(lg.shares) + ' 股 @ ' + lg.account_name));
          if (lg.odd_lot) line.appendChild(el('span', 'rb-oddlot', '（零股）'));
          r.tdAct.appendChild(line);
        });
      } else {
        /* aggregate fallback (no per-leg detail returned): show side + total shares */
        r.tdAct.appendChild(el('span', 'dir-chip ' + (br.side === 'buy' ? 'dir-buy' : 'dir-sell'),
          br.side === 'buy' ? '買' : '賣'));
        r.tdAct.appendChild(document.createTextNode(' ' + f.num(br.shares) + ' 股'));
      }
      const ccy = br.ccy;
      r.tdAmt.textContent = f.money(br.amount, ccy) + ' ' + ccy;
      /* fee + tax are separate Decimal strings; show their combined cost (display only,
         the backend already computed both — Number() here is presentation, not money math). */
      const feeTax = Number(br.fee) + Number(br.tax);
      r.tdFee.textContent = f.money(feeTax, ccy);
      r.tdNew.textContent = f.pct(br.new_weight);
      r.tdNew.classList.toggle('sign-up', Number(br.new_weight) > CAP);
      /* prefer the backend's COMBINED current weight once the preview resolves */
      if (br.current_weight !== undefined && br.current_weight !== null) {
        r.tdCur.textContent = f.pct(br.current_weight);
        r.tdCur.classList.toggle('sign-up', Number(br.current_weight) > CAP);
      }
    }

    /* reset one symbol's computed cells to the null glyph (no trade / on target / excluded) */
    function clearRow(r) {
      r.tdAct.replaceChildren();
      r.tdAct.appendChild(el('span', 'sign-nil', f.NULL_GLYPH));
      r.tdAmt.textContent = f.NULL_GLYPH;
      r.tdFee.textContent = f.NULL_GLYPH;
      r.tdNew.textContent = f.NULL_GLYPH;
      r.tdNew.classList.remove('sign-up');
    }

    function renderFoot(summary, sumTarget) {
      foot.replaceChildren();
      const cash = 1 - sumTarget;
      /* the backend flag is authoritative for Σ>100%; keep the client check as a fallback
         so the warning still shows before the first preview resolves. */
      const over = (summary && summary.over_allocated === true) || sumTarget > 1.0001;
      const kv = (k, v, cls) => {
        const s = el('span', 'rb-kv');
        s.appendChild(el('span', 'k', k));
        s.appendChild(el('span', 'v num' + (cls ? ' ' + cls : ''), v));
        return s;
      };
      foot.appendChild(kv('目標合計', f.pct(sumTarget), over ? 'sign-up' : ''));
      foot.appendChild(kv('現金水位', f.pct(Math.max(0, cash))));
      const turnover = summary ? summary.turnover_reporting : null;
      const fees = summary ? summary.total_fees_reporting : null;
      foot.appendChild(kv('預估周轉額', f.money(turnover, REPORTING) + ' ' + REPORTING));
      foot.appendChild(kv('預估總費稅', f.money(fees, REPORTING) + ' ' + REPORTING,
        fees != null && Number(fees) > 0 ? 'sign-up' : ''));
      if (over) {
        foot.appendChild(el('span', 'rb-warn', '⚠ 目標合計超過 100% — 請下調部分標的'));
      }
      /* stored 目標配置 symbols not in the preview (not held / unpriced) — surface, don't drop */
      const ewt = summary && Array.isArray(summary.excluded_with_target)
        ? summary.excluded_with_target : [];
      if (ewt.length) {
        foot.appendChild(el('div', 'sd-chart-note',
          '已設目標但不在試算內：' + ewt.join('、') + '（未持有或缺價）'));
      }
    }

    let timer = null;
    function schedule() { clearTimeout(timer); timer = setTimeout(update, 250); }

    /* Build the target-weight RATIO dict the backend consumes — one entry per SYMBOL, as
       STRINGS so Pydantic parses EXACT Decimals (avoids JS-float drift). SHARED by the
       debounced preview (update) and the 匯出執行報告 download so both send the identical
       plan. sumTarget is a pure UI percentage (NOT money) — drives 目標合計 / 現金水位. */
    function buildTargets() {
      let sumTarget = 0;
      const targets = {};
      rows.forEach((r) => {
        const ratio = state[r.symbol];
        sumTarget += ratio;
        targets[r.symbol] = String(ratio);
      });
      return { targets: targets, sumTarget: sumTarget };
    }

    async function update() {
      const built = buildTargets();
      const sumTarget = built.sumTarget;
      const targets = built.targets;

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
        const br = byRow[r.symbol];
        if (br) renderRow(r, br);
        else clearRow(r);  /* no trade for this symbol (on target, rounds to nothing, excluded) */
      });
      renderFoot(result && result.summary, sumTarget);
    }

    capBtn.addEventListener('click', () => {
      rows.forEach((r) => {
        state[r.symbol] = Math.min(r.group.weightSum, CAP);  /* combined current weight vs cap */
        r.inp.value = (state[r.symbol] * 100).toFixed(1);
      });
      update();
    });
    resetBtn.addEventListener('click', () => {
      rows.forEach((r) => {
        state[r.symbol] = r.group.weightSum;  /* reset to combined current weight */
        r.inp.value = (state[r.symbol] * 100).toFixed(1);
      });
      update();
    });
    /* 匯出執行報告: download the CURRENT plan as a print-optimized, self-contained HTML
       execution guide. Sends the SAME targets dict as update() (buildTargets); the server
       recomputes the numbers of record (no client math). House style: silent on success,
       toast only on failure; the button shows a busy state (guards double-clicks). */
    exportBtn.addEventListener('click', async () => {
      const restore = window.pdBusy(exportBtn, '產出中…');
      try {
        const built = buildTargets();
        await window.pdApi.download('/api/export/rebalance-report', { targets: built.targets });
      } catch (err) {
        if (window.toast) {
          window.toast(err && err.message ? err.message : '匯出執行報告失敗',
            'fail', err && err.code);
        }
      } finally {
        restore();
      }
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
