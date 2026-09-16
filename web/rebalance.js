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
   The only client number is the target-weight state — a UI percentage, not money — and
   since audit H1 (2026-09-16) it is an INTEGER (tenths of a percent), never a float ratio.

   Requires: api.js (window.pdApi), format.js (window.fmt), names.js (window.pdNames). */
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
  /* G-01 / M5 (2026-09-16): account display names come from web/names.js, NEVER from the
     payload's `account_name` — that is the server's ENGLISH `accounts.name` column, so the
     drawer printed 「TW Broker」 inside a page whose every other 帳戶 cell read 「台灣券商」.
     Degrades to the id if names.js has not loaded (same fallback as app.js / detail.js). */
  const acctZh = (id) => (window.pdNames ? window.pdNames.account(id) : id);

  /* THE TARGET-WEIGHT STATE IS AN INTEGER: tenths of a percent, 0..1000 (audit H1).
     It used to be a float ratio (`Number(inp.value) / 100`), POSTed via `String(ratio)` —
     which serializes an IEEE double in full: a drawer whose fields read 0.7 / 3.6 / 2.6 sent
     "0.006999999999999999" / "0.036000000000000004" / "0.026000000000000002", the backend
     summed them as EXACT Decimals to 1.000000000000000004, and its `> 1` test raised
     「目標合計超過 100%」 beside a footer that read 100.00% (measured 2026-09-16). The field is a
     one-decimal-place percent, so a tenth of a percent is the finest state it can ever hold;
     as an integer, the sum is exact and the wire string is built by digit surgery instead of
     by printing a float. The backend also gained a tolerance — both ends, because either
     alone leaves the other's arithmetic load-bearing. */
  const TENTHS_MAX = 1000;              /* 1000 tenths of a percent = 100.0% */
  const CAP_TENTHS = Math.round(CAP * TENTHS_MAX);
  /* integer tenths -> the EXACT fixed-point ratio string the wire carries: 7 -> "0.007".
     No division: `7/1000` is a double, and printing it is how H1 happened. */
  function ratioStr(tenths) {
    const t = Math.max(0, Math.round(tenths));
    const whole = Math.floor(t / TENTHS_MAX);
    const frac = String(t - whole * TENTHS_MAX);
    return whole + '.' + '000'.slice(frac.length) + frac;
  }
  /* integer tenths -> the field's one-decimal percent text (345 -> "34.5"). */
  const fieldValue = (tenths) => (Math.round(tenths) / 10).toFixed(1);
  /* a ratio (0.3456) or a percent ("34.5") -> integer tenths, clamped at 0. The rounding is
     the ONLY place a float meets the state, and it is a UI weight, never money. */
  const tenthsOfRatio = (ratio) => Math.max(0, Math.round((Number(ratio) || 0) * 1000) || 0);
  const tenthsOfPercent = (pct) => Math.max(0, Math.round(Number(pct) * 10) || 0);

  /* Why a targeted symbol produced no trade row (audit L11). The backend now says which of
     five paths it took (summary.holds + summary.excluded_reasons); an unknown/absent code
     degrades to a neutral phrase rather than to the bare 「—」 this replaced. */
  const REASON_ZH = {
    on_target: '已達目標',
    /* NOT 「差額不足一股」: an MY leg snaps to a 100-unit board lot, so for Bursa rows the
       threshold is a 手, not a 股. One phrase that is true of every market. */
    rounds_to_zero: '差額不足最小單位',
    no_price: '缺價',
    no_rate: '無匯率',
    no_fee_rule: '無費率',
    no_total: '無市值基準'
  };
  const reasonZh = (code) => REASON_ZH[code] || '無法試算';

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

    /* state — the what-if target weight per SYMBOL, in integer tenths of a percent (see the
       TENTHS_MAX block above). Seed: a stored 目標配置 target wins; else the COMBINED current
       weight (sum of the constituents' weight ratios). This is a target PERCENTAGE (a UI
       weight), NOT money — the only money/share numbers come back from the backend preview
       below and render through f.*. */
    const state = {};
    /* The 目標 % field renders ONE decimal place of a percent, so the state behind it must be
       that same number. It was not: the fields were `.toFixed(1)` views of full-precision
       weights, while 目標合計 and the plan POSTed to /api/rebalance/preview were summed from
       the unrounded values. Measured 2026-08-27 (F-06): 17 visible fields summing to 100.1%
       under a footer reading 「目標合計 100.00%」, with the over-100% warning silent — and the
       exported execution report computed from targets the user had never seen.
       Seeding THROUGH the display precision makes the two agree by construction.

       ⚠ F-06 made the two numbers AGREE; it did not make them ADD UP (audit L9, 2026-09-16).
       Seventeen weights rounded independently to 1 dp summed to 100.1%, so the drawer OPENED
       already in its own over-100% error state — a warning about a plan the user had not
       written, on a default that is supposed to be a no-op. Independent rounding cannot
       preserve a sum; an apportionment can. `seeded` below floors every current-weight seed
       and hands the leftover tenths to the largest fractional parts (largest-remainder /
       Hare), so Σ(seeds) is exactly the 1-dp rounding of Σ(raw weights): 100.0 when every
       held symbol is priced, and the honest smaller figure when some are not — never forced.
       F-06's invariant is untouched: every seed is still a whole tenth, so the fields still
       equal the POSTed plan byte for byte.
       Rejected: putting the whole residue on the last row (same sum, but one arbitrary symbol
       absorbs up to n/2 tenths of error instead of ≤1 tenth landing on the rows that were
       closest to rounding up anyway). */
    const seeded = {};
    (function apportionCurrentWeights() {
      let floorSum = 0;
      let rawSum = 0;
      const rem = [];
      order.forEach((sym, i) => {
        const raw = (Number(groups[sym].weightSum) || 0) * 1000;  /* fractional tenths */
        const fl = Math.max(0, Math.floor(raw));
        seeded[sym] = fl;
        floorSum += fl;
        rawSum += raw;
        rem.push({ sym: sym, rem: raw - fl, i: i });
      });
      /* The TARGET total: the 1-dp rounding of the raw sum, not a hard-coded 1000. */
      const totalTenths = Math.round(rawSum);
      const left = Math.min(Math.max(0, totalTenths - floorSum), rem.length);
      rem.sort((a, b) => (b.rem - a.rem) || (a.i - b.i));  /* deterministic tie-break */
      for (let k = 0; k < left; k++) seeded[rem[k].sym] += 1;
    })();
    order.forEach((sym) => {
      /* A stored 目標配置 target is the USER's number: it is expressed in the field's
         precision (F-06 — the field and the wire must agree) but never apportioned. Nudging
         it to make a sum come out would silently edit a saved decision. */
      state[sym] = (storedTargets[sym] !== undefined)
        ? tenthsOfRatio(storedTargets[sym]) : seeded[sym];
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
         clean). Shares come from the dashboard row; the NAME comes from names.js (acctZh),
         not from the row's English `account_name` — see the acctZh note at the top. */
      if (g.multi) {
        const chips = el('div', 'rb-acct-chips');
        g.holdings.forEach((h) => {
          chips.appendChild(el('span', 'rb-acct-chip',
            acctZh(h.account_id) + ' ' + f.shares(h.shares) + '股'));
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
      inp.value = fieldValue(state[sym]);  /* seed control from the integer what-if state */
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
        /* the field IS a 1-dp percent, so ×10 lands exactly on the integer state */
        state[sym] = tenthsOfPercent(inp.value);
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
            ' ' + f.shares(lg.shares) + ' 股 @ ' + acctZh(lg.account_id)));
          if (lg.odd_lot) line.appendChild(el('span', 'rb-oddlot', '（零股）'));
          r.tdAct.appendChild(line);
        });
      } else {
        /* aggregate fallback (no per-leg detail returned): show side + total shares */
        r.tdAct.appendChild(el('span', 'dir-chip ' + (br.side === 'buy' ? 'dir-buy' : 'dir-sell'),
          br.side === 'buy' ? '買' : '賣'));
        r.tdAct.appendChild(document.createTextNode(' ' + f.shares(br.shares) + ' 股'));
      }
      const ccy = br.ccy;
      r.tdAmt.textContent = f.money(br.amount, ccy) + ' ' + ccy;
      /* 費稅 is the backend's Decimal `fee_tax` (= fee + tax, summed server-side). The
         frontend never adds the two component strings — that is float money math over exact
         Decimal values, which the locked invariant forbids. Audit L1, 2026-07-26. */
      r.tdFee.textContent = f.money(br.fee_tax, ccy);
      r.tdNew.textContent = f.pct(br.new_weight);
      r.tdNew.classList.toggle('sign-up', Number(br.new_weight) > CAP);
      /* prefer the backend's COMBINED current weight once the preview resolves */
      if (br.current_weight !== undefined && br.current_weight !== null) {
        r.tdCur.textContent = f.pct(br.current_weight);
        r.tdCur.classList.toggle('sign-up', Number(br.current_weight) > CAP);
      }
    }

    /* reset one symbol's computed cells to the null glyph (no trade / on target / excluded),
       WITH the backend's reason beside the glyph when it gave one.

       Audit L11, 2026-09-16: the 動作 column printed the same bare 「—」 for five different
       outcomes. On the demo drawer AAPL (already on target) and NVDA (no usable price) were
       indistinguishable, so the honest answer 「nothing to do」 and the degradation 「I could
       not price this」 read identically — and the second one is the one the user has to act
       on. The glyph stays (there genuinely is no trade); the reason now sits next to it. */
    function clearRow(r, reason) {
      r.tdAct.replaceChildren();
      r.tdAct.appendChild(el('span', 'sign-nil',
        reason ? f.NULL_GLYPH + ' ' + reasonZh(reason) : f.NULL_GLYPH));
      r.tdAmt.textContent = f.NULL_GLYPH;
      r.tdFee.textContent = f.NULL_GLYPH;
      r.tdNew.textContent = f.NULL_GLYPH;
      r.tdNew.classList.remove('sign-up');
    }

    function renderFoot(summary, sumTenths) {
      foot.replaceChildren();
      /* 目標合計 / 現金水位 are UI PERCENTAGES over the integer state — summed as integers
         (exact), then handed to f.pct as the same fixed-point string the wire carries. The
         client check is `> TENTHS_MAX` for the same reason: `sumTarget > 1.0001` was correctly
         FALSE on the float sum that the backend, reading exact Decimals, called over 100%
         (audit H1). The backend flag stays authoritative; this covers the window before the
         first preview resolves. */
      const cashTenths = Math.max(0, TENTHS_MAX - sumTenths);
      const over = (summary && summary.over_allocated === true) || sumTenths > TENTHS_MAX;
      const kv = (k, v, cls) => {
        const s = el('span', 'rb-kv');
        s.appendChild(el('span', 'k', k));
        s.appendChild(el('span', 'v num' + (cls ? ' ' + cls : ''), v));
        return s;
      };
      foot.appendChild(kv('目標合計', f.pct(ratioStr(sumTenths)), over ? 'sign-up' : ''));
      foot.appendChild(kv('現金水位', f.pct(ratioStr(cashTenths))));
      const turnover = summary ? summary.turnover_reporting : null;
      const fees = summary ? summary.total_fees_reporting : null;
      foot.appendChild(kv('預估周轉額', f.money(turnover, REPORTING) + ' ' + REPORTING));
      foot.appendChild(kv('預估總費稅', f.money(fees, REPORTING) + ' ' + REPORTING,
        fees != null && Number(fees) > 0 ? 'sign-up' : ''));
      /* 試算後現金 (audit L10) — the BACKEND's `cash_after`, signed, in the reporting ccy.
         It is NOT 現金水位: that one is 1 − Σtarget, a pure UI percentage that knows nothing
         about fees, tax or share snapping. With targets at exactly 100% the backend returned
         −1,603.23 TWD (the fees and tax have to come from somewhere) while the footer rendered
         `f.pct(Math.max(0, cash))` = 0.00% — the overdraft was not understated, it was
         invisible, and the plan as shown was unexecutable. Both numbers now appear; only one
         of them is money, and that one is a server string rendered by f.signed. */
      const cashAfter = summary ? summary.cash_after : null;
      /* Sign test on the STRING's first character, not `Number(cashAfter) < 0`: the value is
         an exact Decimal string, and coercing it to an IEEE double to ask a question the
         leading '-' already answers is the habit invariant 3 exists to break. */
      const overdrawn = cashAfter != null && String(cashAfter).trim().charAt(0) === '-';
      foot.appendChild(kv('試算後現金', f.signed(cashAfter, REPORTING) + ' ' + REPORTING,
        overdrawn ? 'sign-down' : ''));
      if (over) {
        foot.appendChild(el('span', 'rb-warn', '⚠ 目標合計超過 100% — 請下調部分標的'));
      }
      if (overdrawn) {
        foot.appendChild(el('div', 'rb-warn',
          '⚠ 費稅使現金透支 ' + f.signed(cashAfter, REPORTING) + ' ' + REPORTING +
          ' — 請下調部分買進'));
      }
      /* FE-D1 forecast footnote (不計入成本): TW legs' expected next-month rebate, summed
         server-side (reporting ccy). Only shown when a TW leg actually rebates. */
      const rebateTotal = summary ? summary.rebate_estimate_total : null;
      if (rebateTotal != null && Number(rebateTotal) > 0) {
        foot.appendChild(el('div', 'sd-chart-note',
          '預估次月折讓合計 ' + f.money(rebateTotal, REPORTING) + ' ' + REPORTING +
          '（台股先收後退,不計入成本）'));
      }
      /* targeted symbols the engine could not act on, each with its cause (L11) — the same
         treatment `excluded_with_target` already gets, for the list that was silent. */
      const exc = summary && Array.isArray(summary.excluded) ? summary.excluded : [];
      if (exc.length) {
        const why = (summary && summary.excluded_reasons) || {};
        foot.appendChild(el('div', 'sd-chart-note',
          '不在試算內：' + exc.map((s) => s + '（' + reasonZh(why[s]) + '）').join('、')));
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
       EXACT fixed-point STRINGS (ratioStr over the integer state) so Pydantic parses the
       Decimal the field actually shows. `String(ratio)` over a float ratio is what audit H1
       measured on the wire: "0.006999999999999999" for a field reading 0.7. SHARED by the
       debounced preview (update) and the 匯出執行報告 download so both send the identical
       plan. sumTenths is a pure UI percentage in integer tenths (NOT money) — it drives
       目標合計 / 現金水位 and is summed as an integer, so it cannot drift. */
    function buildTargets() {
      let sumTenths = 0;
      const targets = {};
      rows.forEach((r) => {
        const tenths = state[r.symbol];
        sumTenths += tenths;
        targets[r.symbol] = ratioStr(tenths);
      });
      return { targets: targets, sumTenths: sumTenths };
    }

    async function update() {
      const built = buildTargets();
      const sumTenths = built.sumTenths;
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
      const summary = result && result.summary;
      const byRow = {};
      backendRows.forEach((br) => { byRow[br.symbol] = br; });
      /* symbol -> why it has no row, merged from the summary's two channels: `holds` (priced
         and computable, simply no trade) and `excluded_reasons` (could not be computed). A
         symbol appears in at most one of them — the engine takes exactly one exit per symbol. */
      const reasons = {};
      if (summary) {
        (Array.isArray(summary.holds) ? summary.holds : []).forEach((h) => {
          if (h && h.symbol) reasons[h.symbol] = h.reason;
        });
        const exr = summary.excluded_reasons;
        if (exr && typeof exr === 'object') {
          Object.keys(exr).forEach((s) => { reasons[s] = exr[s]; });
        }
      }
      rows.forEach((r) => {
        const br = byRow[r.symbol];
        if (br) renderRow(r, br);
        else clearRow(r, reasons[r.symbol]);  /* on target / rounds to nothing / excluded */
      });
      renderFoot(summary, sumTenths);
    }

    /* Both buttons re-seed from `seeded` (the apportioned current weights), never from the
       raw weightSum — otherwise 重設 would hand back a set of fields that no longer sums to
       the total the drawer opened with, reopening L9 one click later. Capping is applied to
       the apportioned seed: the released part becoming cash is the point of that button, so
       its result is NOT re-apportioned back up to 100%. */
    capBtn.addEventListener('click', () => {
      rows.forEach((r) => {
        state[r.symbol] = Math.min(seeded[r.symbol], CAP_TENTHS);
        r.inp.value = fieldValue(state[r.symbol]);
      });
      update();
    });
    resetBtn.addEventListener('click', () => {
      rows.forEach((r) => {
        state[r.symbol] = seeded[r.symbol];  /* reset to combined current weight */
        r.inp.value = fieldValue(state[r.symbol]);
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
    const btn = el('button', 'btn btn-sm rb-open-btn');
    btn.appendChild(el('span', 'ico', '⚖'));
    btn.appendChild(el('span', null, '再平衡試算'));
    btn.type = 'button';
    btn.title = '設定目標權重，試算需買賣的股數與費稅（不寫入）';
    btn.addEventListener('click', open);
    headBar.appendChild(btn);
  }
})();
