/* portfolio-dash — 輸入中心 (wired to /api/input/* + /api/import/*, spec 19/12).

   The five input modes (manual transaction · CSV import · AI input · dividend ·
   FX/opening) all source their structural data — accounts, instruments, fee-rule
   context, holdings — from GET /api/input/context (no more window.INPUT_DATA).

   MONEY DISCIPLINE (spec data-and-pricing.md):
   - SERVER-returned amounts (manual-preview fee/tax/gross/total, CSV preview-row
     amounts, AI cost_usd) arrive as Decimal STRINGS and are rendered via window.fmt
     ONLY — never `bareString.toFixed()`. The frontend NEVER computes money of record.
   - USER-INPUT local estimates (the fee/tax prefill while typing, the DRIP net calc,
     the FX implied-rate what-if) operate on the user's own numeric entry and are SENT
     to the backend, which then computes the value of record. Those `.toFixed` calls on
     user-entered numbers are the documented input-side exception and are retained.

   Write paths:
   - Manual transaction: live preview (POST /input/manual/preview) + commit
     (POST /input/manual/commit; 422 unacked-oversell -> confirmDialog -> re-commit
     with ack_oversell:true; unknown symbols auto-register).
   - CSV import: POST /import/preview (real table) + POST /import/commit
     ({written,skipped}); the dropzone is a REAL client-side file read (2026-07-03).
   - AI input: POST /input/ai/preview (preview + meta; 402/409/503 -> degraded panels,
     driven ONLY by real API errors — the design state-switcher is retired).
   - Dividend / FX-conversion / Opening-inventory single-entry forms commit through
     the SAME import path as a one-row CSV (preview-validate -> ack warnings ->
     commit) — one write seam, no extra endpoints (2026-07-03, items 1+2). */
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

  /* Structural context from GET /api/input/context (replaces window.INPUT_DATA).
     Starts empty so any pre-fetch render is blank; populated on boot. */
  let ctx = { accounts: [], fee_rules: {}, instruments: [], holdings: {} };
  const acc = (id) => ctx.accounts.find((a) => a.id === id);
  const inst = (sym) => {
    const s = (sym || '').trim();
    const up = s.toUpperCase();
    return ctx.instruments.find((i) => i.symbol === up || i.symbol === s);
  };

  /* ===== tabs ===== */
  const TABS = ['manual', 'csv', 'ai', 'div', 'fxopen'];
  function showTab(t) {
    TABS.forEach((x) => {
      const pane = $('#pane-' + x);
      const tab = $('#tab-' + x);
      if (pane) pane.classList.toggle('active', x === t);
      if (tab) tab.classList.toggle('active', x === t);
    });
  }
  TABS.forEach((t) => {
    const tab = $('#tab-' + t);
    if (tab) tab.addEventListener('click', () => showTab(t));
  });

  /* ================= Tab 1 手動交易 ================= */
  const m = { side: 'buy', feeOverride: false, taxOverride: false, acked: false };
  /* Latest server preview (Decimal STRINGS) — null until the first preview lands. */
  let mPreview = null;
  /* Today (local) — the natural default trade date; retires the design-stub
     2026-06-11 / 2330 / 1000 / 612.5 fake prefill (2026-07-02). */
  const TODAY = (() => {
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  })();

  function initManual() {
    const accSel = $('#m-account');
    ctx.accounts.forEach((a) => {
      const o = el('option', null, a.name + '（' + a.ccy + '）');
      o.value = a.id;
      accSel.appendChild(o);
    });
    /* item 5 (2026-07-03): remember the last-used account — the alphabetical
       default (Moomoo first) forced an extra click on every TW entry. */
    try {
      const last = localStorage.getItem('pd_last_account');
      if (last && ctx.accounts.some((a) => a.id === last)) accSel.value = last;
    } catch (e) { /* noop */ }
    accSel.addEventListener('change', () => {
      try { localStorage.setItem('pd_last_account', accSel.value); } catch (e) { /* noop */ }
    });
    const dl = $('#m-symbols');
    ctx.instruments.forEach((i) => {
      const o = el('option'); o.value = i.symbol; o.label = i.name;
      dl.appendChild(o);
    });
    $('#m-date').value = TODAY;
    $('#m-date').max = TODAY;  // audit M5: discourage a future trade date (server soft-warns too)
    $('#m-symbol').value = '';
    $('#m-shares').value = '';
    $('#m-price').value = '';
    /* item 6 (2026-07-03): 新增標的 → 記一筆買入 handoff — ?symbol=XXXX prefills. */
    try {
      const pre = new URLSearchParams(window.location.search).get('symbol');
      if (pre) {
        $('#m-symbol').value = pre.trim().toUpperCase();
        setTimeout(() => { const n = $('#m-shares'); if (n) n.focus(); }, 100);
      }
    } catch (e) { /* noop */ }

    $('#m-side-buy').addEventListener('click', () => setSide('buy'));
    $('#m-side-sell').addEventListener('click', () => setSide('sell'));
    ['m-account', 'm-symbol', 'm-shares', 'm-price', 'm-date'].forEach((id) => {
      $('#' + id).addEventListener('input', schedulePreview);
    });
    const mdt = $('#m-daytrade');
    if (mdt) mdt.addEventListener('change', schedulePreview);
    $('#m-fee-pencil').addEventListener('click', () => toggleOverride('fee'));
    $('#m-tax-pencil').addEventListener('click', () => toggleOverride('tax'));
    $('#m-fee').addEventListener('input', schedulePreview);
    $('#m-tax').addEventListener('input', schedulePreview);
    $('#m-confirm').addEventListener('click', commitManual);
    schedulePreview();
  }
  function setSide(s) {
    m.side = s;
    $('#m-side-buy').classList.toggle('active', s === 'buy');
    $('#m-side-buy').classList.toggle('buy-on', s === 'buy');
    $('#m-side-sell').classList.toggle('active', s === 'sell');
    $('#m-side-sell').classList.toggle('sell-on', s === 'sell');
    schedulePreview();
  }

  /* fee/tax override is a TRUE toggle (FU-D7). ON: flag true, field editable, pencil
     pressed. OFF: flag false, field read-only, pencil released — schedulePreview() then
     repopulates the auto-computed value (runManualPreview only writes fee/tax back when
     the flag is false) and the commit body drops fee_override/tax_override. Visual state
     rides on the pencil's aria-pressed (styled in input.css) + a swapped title. */
  function applyOverrideState(kind, on) {
    const isFee = kind === 'fee';
    if (isFee) m.feeOverride = on; else m.taxOverride = on;
    const field = $(isFee ? '#m-fee' : '#m-tax');
    const pencil = $(isFee ? '#m-fee-pencil' : '#m-tax-pencil');
    field.readOnly = !on;
    pencil.setAttribute('aria-pressed', on ? 'true' : 'false');
    pencil.title = on ? '取消覆寫（回自動計算）' : '覆寫';
  }
  function toggleOverride(kind) {
    const on = !(kind === 'fee' ? m.feeOverride : m.taxOverride);
    applyOverrideState(kind, on);
    if (on) $(kind === 'fee' ? '#m-fee' : '#m-tax').focus();
    schedulePreview();  // OFF -> auto value returns; ON -> re-preview with the override
  }

  /* Build the ManualBody for /input/manual/preview & /commit. fee/tax overrides ride
     through as the user's raw string (the input-side numeric is sent; the backend
     computes the value of record). Empty/blank => omit (let the backend auto-fill). */
  function manualBody() {
    const sym = $('#m-symbol').value.trim();
    const sharesRaw = $('#m-shares').value.trim();
    const priceRaw = $('#m-price').value.trim();
    const body = {
      account_id: $('#m-account').value || (ctx.accounts[0] && ctx.accounts[0].id) || '',
      symbol: sym,
      side: m.side,
      date: $('#m-date').value || TODAY,
      shares: sharesRaw === '' ? '0' : sharesRaw,
      price: priceRaw === '' ? '0' : priceRaw,
    };
    const dt = $('#m-daytrade');
    if (dt && dt.checked) body.daytrade = true;
    if (m.feeOverride) {
      const fv = $('#m-fee').value.trim();
      if (fv !== '') body.fee_override = fv;
    }
    if (m.taxOverride) {
      const tv = $('#m-tax').value.trim();
      if (tv !== '') body.tax_override = tv;
    }
    return body;
  }

  /* Debounce the live preview so each keystroke does not fire a request. */
  let previewTimer = null;
  function schedulePreview() {
    renderSymbolHint();          // local, instant
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(runManualPreview, 180);
  }

  function renderSymbolHint() {
    const sym = $('#m-symbol').value.trim();
    const it = inst(sym);
    const symHint = $('#m-sym-hint');
    symHint.replaceChildren();
    if (sym && !it) {
      symHint.appendChild(el('span', null, '未註冊 — 寫入時將自動查詢並註冊（依帳戶判定市場）　'));
      /* FU-D23: an inline 立即註冊 action opens the shared quick-add dialog with the symbol
         pre-filled + market inferred from the account. The commit-time auto-register fallback
         stays, so this is optional convenience, not a gate. */
      const reg = el('button', null, '立即註冊');
      reg.type = 'button';
      reg.style.cssText = 'background:none;border:none;padding:0;color:var(--accent);'
        + 'cursor:pointer;font-size:inherit;text-decoration:underline;';
      reg.addEventListener('click', () => openManualQuickAdd(sym));
      symHint.appendChild(reg);
    } else if (it) {
      symHint.textContent = it.name + '・' + it.ccy + (it.etf ? '・ETF' : '');
    }
  }

  /* Market inferred from the selected account's settlement ccy — mirrors the backend
     auto-register (data_ingestion.markets.account_market): TWD→TW, USD→US, MYR→MY. */
  const _CCY_MARKET = { TWD: 'TW', USD: 'US', MYR: 'MY' };
  function accountMarket(accId) {
    const a = acc(accId);
    return a ? (_CCY_MARKET[a.settlement_ccy || a.ccy] || 'TW') : 'TW';
  }

  /* FU-D23: open the shared quick-add dialog for the manual pane's unregistered symbol.
     After a successful register (or restore), re-fetch /api/input/context so the hint clears
     and the user continues the SAME entry — the draft form is NOT cleared. */
  function openManualQuickAdd(sym) {
    if (!window.pdInstQuickAdd) {
      if (window.toast) window.toast('對話框載入失敗，請重新整理', 'fail');
      return;
    }
    const cont = async () => {
      await reloadContext();
      renderSymbolHint();
      schedulePreview();
    };
    window.pdInstQuickAdd({
      symbol: sym,
      market: accountMarket($('#m-account').value),
      lockSymbol: true,
      onConfirm: cont,
      onBuy: cont,
    });
  }

  /* Re-fetch structural context (accounts / instruments / holdings) into `ctx` + refresh the
     manual symbol datalist, so a just-registered symbol resolves immediately (the 未註冊 hint
     clears). Graceful: a failed refetch keeps the prior ctx. */
  async function reloadContext() {
    let resp;
    try {
      resp = await api.get('/api/input/context');
    } catch (e) {
      return;
    }
    ctx = {
      accounts: (resp && resp.accounts) || ctx.accounts,
      fee_rules: (resp && resp.fee_rules) || ctx.fee_rules,
      instruments: (resp && resp.instruments) || ctx.instruments,
      holdings: (resp && resp.holdings) || ctx.holdings,
    };
    const dl = $('#m-symbols');
    if (dl) {
      dl.replaceChildren();
      ctx.instruments.forEach((i) => {
        const o = el('option'); o.value = i.symbol; o.label = i.name;
        dl.appendChild(o);
      });
    }
  }

  /* Fetch the server preview (computed fee/tax + issues) and render. Local-only field
     validation (empty symbol / non-positive shares-price) short-circuits before the
     network call so the obviously-invalid draft does not spam the endpoint. */
  async function runManualPreview() {
    const a = acc($('#m-account').value) || ctx.accounts[0];
    if (!a) { renderManual(null, [], false); return; }
    const sym = $('#m-symbol').value.trim();
    const shares = Number($('#m-shares').value) || 0;
    const price = Number($('#m-price').value) || 0;

    /* pristine form (boots empty since 2026-07-02): no red errors on an untouched
       page — render the neutral empty state with the confirm disabled. */
    if (!sym && $('#m-shares').value.trim() === '' && $('#m-price').value.trim() === '') {
      mPreview = null;
      renderManual(null, [], false);
      return;
    }

    const localIssues = [];
    if (!sym) localIssues.push({ sev: 'error', text: '請輸入代號', field: 'm-symbol' });
    if (shares <= 0) localIssues.push({ sev: 'error', text: '股數必須大於 0', field: 'm-shares' });
    if (price <= 0) localIssues.push({ sev: 'error', text: '價格必須大於 0', field: 'm-price' });
    if (localIssues.length) {
      mPreview = null;
      renderManual(null, localIssues, false);
      return;
    }

    const ctrl = api.abortable('manual-preview');
    let resp;
    try {
      resp = await api.post('/api/input/manual/preview', manualBody(), { signal: ctrl.signal });
    } catch (err) {
      if (err && err.name === 'AbortError') return;  // superseded by a newer keystroke
      mPreview = null;
      renderManual(null, [{ sev: 'error', text: (err && err.message) || '預覽失敗', field: null }], false);
      return;
    }
    mPreview = resp;
    /* Server amounts are Decimal STRINGS -> reflect computed fee/tax into the
       (read-only) input fields via fmt; when overridden, the user's own value stays. */
    const ccy = a.ccy;
    if (!m.feeOverride) $('#m-fee').value = resp.fee !== undefined ? f.money(resp.fee, ccy) : '0';
    if (!m.taxOverride) $('#m-tax').value = resp.tax !== undefined ? f.money(resp.tax, ccy) : '0';
    if (resp.fee_rule_label) $('#m-fee-rule').textContent = resp.fee_rule_label;
    renderManual(resp, (resp.issues || []), true);
  }

  /* Render the preview card + issues from the SERVER preview (or local-only issues
     when the draft is too incomplete to send). `serverOk` => a valid server preview is
     present; the confirm button enables only then with no hard issues + ack satisfied. */
  function renderManual(preview, issues, serverOk) {
    const a = acc($('#m-account').value) || ctx.accounts[0];
    const ccy = a ? a.ccy : '';
    $('#m-fee-ovr').hidden = !m.feeOverride;
    $('#m-tax-ovr').hidden = !m.taxOverride;

    /* FE-D1 forecast HINT (informational, 不計入成本): the server returns rebate_estimate
       (TW charge-first next-month refund) as a Decimal STRING, or null when the account
       never rebates. Show it under the fee field only where it applies + is positive. */
    const rebateHint = $('#m-rebate-hint');
    if (rebateHint) {
      const est = (serverOk && preview) ? preview.rebate_estimate : null;
      if (est != null && Number(est) > 0) {
        rebateHint.textContent = '預估次月折讓 +' + f.money(est, ccy) + '（不計入成本）';
        rebateHint.hidden = false;
      } else {
        rebateHint.hidden = true;
        rebateHint.textContent = '';
      }
    }

    /* split server issues: hard (error) gates the confirm; soft (warn, e.g. oversell)
       needs an ack; info (e.g. 未註冊將自動註冊) is a notice only — never gates. */
    const hard = issues.filter((i) => i.sev === 'error');
    const soft = issues.filter((i) => i.sev === 'warn');
    const infos = issues.filter((i) => i.sev === 'info');
    const oversell = soft.find((i) => i.code === 'sell_exceeds_holdings') || soft[0] || null;

    /* field-error highlight from issue.field (mapped to the m-* input ids) */
    const FIELD_ID = { symbol: 'm-symbol', shares: 'm-shares', price: 'm-price' };
    ['m-symbol', 'm-shares', 'm-price'].forEach((id) => $('#' + id).classList.remove('field-error'));
    issues.forEach((i) => {
      const id = i.field && (FIELD_ID[i.field] || (i.field.indexOf('m-') === 0 ? i.field : null));
      if (id) $('#' + id).classList.add('field-error');
    });

    /* preview card big value + rows from SERVER Decimal strings (via fmt). The card
       shows the MAGNITUDE (the 總成本 / 淨收款 label carries the sign meaning); the
       backend `total` is negative for BUY (cashflow sign), so strip a leading minus as
       a STRING op — no arithmetic on the money string — before handing it to fmt. */
    const hasServer = serverOk && preview && preview.total !== undefined && preview.total !== null;
    const totalAbs = hasServer ? String(preview.total).replace(/^-/, '') : null;
    $('#m-pc-label').textContent = m.side === 'buy' ? '總成本（含費稅）' : '淨收款（扣費稅）';
    $('#m-pc-value').textContent = hasServer ? f.money(totalAbs, ccy) : f.NULL_GLYPH;
    $('#m-pc-ccy').textContent = ccy;
    const rows = $('#m-pc-rows');
    rows.replaceChildren();
    if (hasServer) {
      [['成交金額', preview.gross], ['手續費' + (m.feeOverride ? '（已覆寫）' : ''), preview.fee],
       ['交易稅' + (m.taxOverride ? '（已覆寫）' : ''), preview.tax]].forEach(([k, v]) => {
        const row = el('div', 'pc-row');
        row.appendChild(el('span', 'k', k));
        row.appendChild(el('span', 'v', f.money(v, ccy) + ' ' + ccy));
        rows.appendChild(row);
      });
    }

    /* issue list */
    const issueBox = $('#m-issues');
    issueBox.replaceChildren();
    hard.forEach((i) => {
      const div = el('div', 'issue issue-error');
      div.appendChild(el('span', null, '✕'));
      div.appendChild(el('span', null, i.text));
      issueBox.appendChild(div);
    });
    infos.forEach((i) => {
      const div = el('div', 'issue issue-info');
      div.appendChild(el('span', null, 'ℹ'));
      div.appendChild(el('span', null, i.text));
      issueBox.appendChild(div);
    });
    let ackOk = true;
    if (oversell) {
      const div = el('div', 'issue issue-warn');
      div.appendChild(el('span', null, '⚠'));
      const lab = el('label');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.id = 'm-ack';
      cb.checked = m.acked;
      cb.addEventListener('change', () => { m.acked = cb.checked; renderManual(mPreview, issues, serverOk); });
      lab.appendChild(cb);
      lab.appendChild(el('span', null, oversell.text + ' 我了解，仍要寫入。'));
      div.appendChild(lab);
      issueBox.appendChild(div);
      ackOk = m.acked;
    } else {
      m.acked = false;
    }
    if (hasServer && !hard.length && !oversell) {
      const div = el('div', 'issue issue-ok');
      div.appendChild(el('span', null, '✓'));
      div.appendChild(el('span', null, '草稿檢核通過，可寫入'));
      issueBox.appendChild(div);
    }
    $('#m-confirm').disabled = !hasServer || hard.length > 0 || !ackOk;
  }

  /* Commit the manual transaction. 201 -> success toast + reset draft state; 422
     oversell_unacknowledged -> confirmDialog -> re-commit with ack_oversell:true;
     400 / other PdApiError -> error toast carrying the backend message + code. */
  async function commitManual() {
    const body = manualBody();
    body.ack_oversell = m.acked;
    /* busy state: the commit may auto-register an unknown symbol (real provider
       fetch, seconds) — the button must show that work, not appear frozen. */
    const restore = window.pdBusy ? window.pdBusy($('#m-confirm'), '寫入中…') : () => {};
    try {
      const resp = await api.post('/api/input/manual/commit', body);
      restore();
      onManualWritten(resp);
    } catch (err) {
      restore();
      if (err && err.status === 422 && err.code === 'oversell_unacknowledged') {
        const msg = (err.issues && err.issues[0] && err.issues[0].text) || '賣出股數超過持有 — 確認後寫入？';
        window.confirmDialog({
          title: '賣超確認',
          body: msg + '（輸入錯誤還是放空？）',
          confirmLabel: '我了解，仍要寫入',
          danger: true,
          onConfirm: async () => {
            const acked = manualBody();
            acked.ack_oversell = true;
            try {
              const resp = await api.post('/api/input/manual/commit', acked);
              onManualWritten(resp);
            } catch (e2) {
              if (window.toast) window.toast((e2 && e2.message) || '寫入失敗', 'fail', e2 && e2.code);
            }
          }
        });
        return;
      }
      if (window.toast) window.toast((err && err.message) || '寫入失敗', 'fail', err && err.code);
    }
  }

  function onManualWritten(resp) {
    if (window.toast) {
      const id = resp && resp.txn_id !== undefined ? '（#' + resp.txn_id + '）' : '';
      const ar = resp && resp.auto_registered;
      const arTxt = ar
        ? '；已自動註冊 ' + ar.symbol + (ar.name ? ' ' + ar.name : '') +
          (ar.last != null ? '（現價 ' + ar.last + '）' : '')
        : '';
      window.toast('寫入成功', 'ok', '交易已寫入帳本 ' + id + arTxt);
    }
    /* reset draft state and re-preview a clean form (clears the override toggles too) */
    applyOverrideState('fee', false); applyOverrideState('tax', false); m.acked = false;
    $('#m-shares').value = '';
    $('#m-price').value = '';
    schedulePreview();
  }

  /* ================= Tab 2 CSV 匯入 ================= */
  /* kind chips map the UI label to the import endpoint `kind`. */
  const CSV_KINDS = [['交易', 'transactions'], ['股利', 'dividends'], ['換匯', 'fx'], ['期初', 'openings']];
  let csvKind = 'transactions';
  /* FU-D19: the pinned date format (a dateparse format id) once the user resolves an
     ambiguous date column; null = let the backend infer. Reset whenever the CSV text or
     the kind changes so a new file re-detects from scratch. */
  let csvDateFormat = null;

  /* Shown in #csv-kind-note: the expected date shape + the never-guess promise (FU-D19). */
  const CSV_DATE_NOTE = '日期欄位建議 YYYY-MM-DD；2026/7/10、20260710 等常見格式亦可自動辨識，'
    + '無法判斷（如 3/4/2026）時會請你選擇格式。';

  /* per-kind CSV header hints shown in the dropzone — the FULL canonical header (leads with
     the REQUIRED `account` column; matches the *_COLUMNS constants in the backend parsers +
     the downloadable 範本; date carries its YYYY-MM-DD hint, optional columns marked 選填). */
  const CSV_HINTS = {
    transactions: '欄位：account・symbol・side・date(YYYY-MM-DD)・shares・price・fee（選填）・tax（選填）・daytrade（選填）・note（選填）',
    dividends: '欄位：account・symbol・date(YYYY-MM-DD)・type(CASH/STOCK/DRIP/NET)・gross・withholding（選填）・net（選填）・reinvest_shares（選填）・reinvest_price（選填）',
    fx: '欄位：account・date(YYYY-MM-DD)・from_ccy・from_amount・to_ccy・to_amount',
    openings: '欄位：account・symbol・shares・original_avg_cost・build_date(YYYY-MM-DD)・original_cost_total（選填）',
  };

  function initCsv() {
    const bar = $('#csv-kinds');
    CSV_KINDS.forEach(([label, kind], i) => {
      const c = el('button', 'chip' + (i === 0 ? ' active' : ''), label);
      c.type = 'button';
      c.addEventListener('click', () => {
        bar.querySelectorAll('.chip').forEach((x) => x.classList.remove('active'));
        c.classList.add('active');
        csvKind = kind;
        csvDateFormat = null;      // FU-D19: a different kind re-detects the date format
        hideDateFmtChooser();
        const note = $('#csv-kind-note');
        if (note) note.textContent = CSV_DATE_NOTE + (kind === 'transactions' ? '' : '（' + label + ' CSV：解析同此模式）');
        const hint = $('#csv-dz-hint');
        if (hint) hint.textContent = CSV_HINTS[kind] || '';
        scheduleCsvPreview();      // re-run so any prior ambiguity re-evaluates for this kind
      });
      bar.appendChild(c);
    });
    /* seed the dropzone hint + date note for the default (transactions) kind — the chip
       handler above only refreshes them on a switch. */
    const dzHint0 = $('#csv-dz-hint');
    if (dzHint0) dzHint0.textContent = CSV_HINTS[csvKind] || '';
    const note0 = $('#csv-kind-note');
    if (note0) note0.textContent = CSV_DATE_NOTE;

    /* FU-D19: picking a date format pins it and re-previews (which now resolves cleanly). */
    const fmtSel = $('#csv-datefmt-select');
    if (fmtSel) fmtSel.addEventListener('change', () => {
      csvDateFormat = fmtSel.value || null;
      runCsvPreview();
    });

    /* 下載範本：GET /api/import/template?kind=… (BOM+CRLF text/csv) for the ACTIVE kind.
       pdApi.download issues a GET when no body is passed; the filename rides the endpoint's
       Content-Disposition. The template is a single-source of the parser column order. */
    const tplBtn = $('#csv-template');
    if (tplBtn) {
      tplBtn.addEventListener('click', async () => {
        const restore = window.pdBusy ? window.pdBusy(tplBtn, '下載中…') : () => {};
        try {
          await api.download('/api/import/template?kind=' + encodeURIComponent(csvKind));
        } catch (err) {
          if (window.toast) window.toast((err && err.message) || '範本下載失敗', 'fail', err && err.code);
        } finally {
          restore();
        }
      });
    }

    const paste = $('#csv-paste');
    /* a manual edit invalidates any pinned date format — re-detect from the new text. */
    if (paste) paste.addEventListener('input', () => { csvDateFormat = null; scheduleCsvPreview(); });
    $('#csv-confirm').addEventListener('click', commitCsv);
    $('#csv-confirm').disabled = true;

    /* ---- REAL file upload (2026-07-03, item 2): the dropzone reads the .csv
       client-side (FileReader) into the paste area and previews — the import
       path stays text-based, so no backend upload endpoint is needed. ---- */
    const dz = $('#csv-dropzone');
    const fileIn = $('#csv-file-input');
    const loadFile = (f) => {
      if (!f) return;
      const r = new FileReader();
      r.onload = () => {
        if (paste) paste.value = String(r.result || '').trim();
        csvDateFormat = null;      // FU-D19: a fresh file re-detects the date format
        $('#csv-file').textContent = f.name;
        if (window.toast) window.toast('已載入 ' + f.name, 'ok', '解析預覽已更新，確認後寫入');
        scheduleCsvPreview();
      };
      r.onerror = () => { if (window.toast) window.toast('檔案讀取失敗', 'fail', f.name); };
      r.readAsText(f, 'utf-8');
    };
    if (dz && fileIn) {
      dz.style.cursor = 'pointer';
      dz.addEventListener('click', () => fileIn.click());
      fileIn.addEventListener('change', () => { loadFile(fileIn.files && fileIn.files[0]); fileIn.value = ''; });
      dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('dz-over'); });
      dz.addEventListener('dragleave', () => dz.classList.remove('dz-over'));
      dz.addEventListener('drop', (e) => {
        e.preventDefault();
        dz.classList.remove('dz-over');
        loadFile(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]);
      });
    }
  }

  let csvTimer = null;
  function scheduleCsvPreview() {
    if (csvTimer) clearTimeout(csvTimer);
    csvTimer = setTimeout(runCsvPreview, 250);
  }

  /* FU-D19 date-format chooser helpers. */
  function hideDateFmtChooser() {
    const box = $('#csv-datefmt');
    if (box) box.hidden = true;
    const sel = $('#csv-datefmt-select');
    if (sel) sel.replaceChildren();
  }
  function showDateFmtChooser(amb) {
    const box = $('#csv-datefmt');
    const sel = $('#csv-datefmt-select');
    if (!box || !sel) return;
    sel.replaceChildren();
    const ph = el('option', null, '請選擇日期格式…'); ph.value = ''; sel.appendChild(ph);
    (amb.candidates || []).forEach((c) => {
      const o = el('option', null, c.label + ' — ' + c.example_in + ' → ' + c.example_out);
      o.value = c.id;
      sel.appendChild(o);
    });
    box.hidden = false;
  }

  async function runCsvPreview() {
    const paste = $('#csv-paste');
    const csvText = paste ? paste.value.trim() : '';
    const tbody = $('#csv-body');
    if (!csvText) {
      if (tbody) tbody.replaceChildren();
      $('#csv-counts').textContent = '';
      $('#csv-file').textContent = '';
      $('#csv-confirm').disabled = true;
      csvDateFormat = null;
      hideDateFmtChooser();
      return;
    }
    const reqBody = { kind: csvKind, csv_text: csvText };
    if (csvDateFormat) reqBody.date_format = csvDateFormat;  // FU-D19: pin once chosen
    let resp;
    try {
      resp = await api.post('/api/import/preview', reqBody);
    } catch (err) {
      if (window.toast) window.toast((err && err.message) || '解析失敗', 'fail', err && err.code);
      return;
    }
    renderCsvPreview(resp);
  }

  /* Render the REAL preview table from the server rows {n, status, reason, data}.
     The per-row money in `data` (price / shares / fee / tax) is Decimal STRINGS now,
     so amounts go through fmt / Number — NOT `.toFixed()` on a wire string (Finding 5). */
  function renderCsvPreview(preview) {
    $('#csv-file').textContent = '貼上 CSV';
    const tbody = $('#csv-body');
    tbody.replaceChildren();
    const ST = { ok: ['✓ 可寫入', 'st-ok'], warn: ['⚠ 警告', 'st-warn'], error: ['✕ 錯誤', 'st-error'] };
    (preview.rows || []).forEach((r) => {
      const d = r.data || {};
      const tr = el('tr', r.status === 'error' ? 'row-error' : '');
      const tdCb = el('td');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = r.status !== 'error';
      cb.disabled = r.status === 'error';
      tdCb.appendChild(cb);
      tr.appendChild(tdCb);
      tr.appendChild(el('td', 'num', '#' + ((r.n || 0) + 1)));
      tr.appendChild(el('td', 'num', f.date(d.trade_date || d.date)));
      tr.appendChild(el('td', 'col-text', d.account_id || d.account || ''));
      const side = (d.side || '').toString().toLowerCase();
      const tdSide = el('td', 'col-text');
      tdSide.appendChild(el('span', 'dir-chip ' + (side === 'buy' ? 'dir-buy' : 'dir-sell'),
        side === 'buy' ? '買' : '賣'));
      tr.appendChild(tdSide);
      const symbol = d.symbol || '';
      tr.appendChild(el('td', 'col-text num', symbol));
      const it = inst(symbol);
      const ccy = it ? it.ccy : '';
      tr.appendChild(el('td', 'num', f.num(d.quantity !== undefined ? d.quantity : d.shares)));
      tr.appendChild(el('td', 'num', f.price(d.price, ccy)));   // Decimal string -> fmt
      const st = ST[r.status] || ST.ok;
      tr.appendChild(el('td', 'col-text ' + st[1], st[0]));
      tr.appendChild(el('td', 'err-msg', r.reason || ''));
      tbody.appendChild(tr);
    });
    const s = preview.summary || { ok: 0, warn: 0, error: 0 };
    $('#csv-counts').textContent =
      '可寫入 ' + (s.ok || 0) + '・警告 ' + (s.warn || 0) + '・錯誤 ' + (s.error || 0);
    /* FU-D19: an unresolved ambiguous date column -> show the chooser + hold the confirm
       disabled until a format is pinned (all date rows are errors until then anyway). */
    const amb = preview.date_ambiguity;
    if (amb && !csvDateFormat) {
      showDateFmtChooser(amb);
      $('#csv-confirm').disabled = true;
      return;
    }
    hideDateFmtChooser();
    /* confirm enables when there is anything non-error to write */
    $('#csv-confirm').disabled = ((s.ok || 0) + (s.warn || 0)) === 0;
  }

  /* Commit the pasted CSV. The backend re-derives from csv_text (re-validates vs the
     current ledger) and returns {written, skipped} as ints (safe). 422
     warnings_unacknowledged -> confirmDialog -> re-commit with ack_warnings:true. */
  async function commitCsv() {
    const paste = $('#csv-paste');
    const csvText = paste ? paste.value.trim() : '';
    if (!csvText) return;
    const commitBody = (ack) => {
      const b = { kind: csvKind, csv_text: csvText, ack_warnings: ack };
      if (csvDateFormat) b.date_format = csvDateFormat;  // FU-D19: carry the pinned format
      return b;
    };
    try {
      const resp = await api.post('/api/import/commit', commitBody(false));
      onCsvWritten(resp);
    } catch (err) {
      /* FU-D19: server refused because the date column is still ambiguous — never a guess. */
      if (err && err.status === 422 && err.code === 'date_ambiguity_unresolved') {
        if (window.toast) window.toast('日期格式不明確', 'fail', '請先於上方選擇日期格式再寫入');
        return;
      }
      if (err && err.status === 422 && err.code === 'warnings_unacknowledged') {
        window.confirmDialog({
          title: '匯入警告確認',
          body: '部分列有警告（如賣超 / 模糊代號）— 確認後一併寫入？',
          confirmLabel: '確認寫入',
          onConfirm: async () => {
            try {
              const resp = await api.post('/api/import/commit', commitBody(true));
              onCsvWritten(resp);
            } catch (e2) {
              if (window.toast) window.toast((e2 && e2.message) || '匯入失敗', 'fail', e2 && e2.code);
            }
          }
        });
        return;
      }
      if (window.toast) window.toast((err && err.message) || '匯入失敗', 'fail', err && err.code);
    }
  }

  function onCsvWritten(resp) {
    const written = resp && resp.written !== undefined ? resp.written : 0;
    const skipped = resp && resp.skipped !== undefined ? resp.skipped : 0;
    const banner = $('#csv-result');
    if (banner) {
      banner.hidden = false;
      banner.replaceChildren();
      banner.appendChild(el('div', null, '✓ 寫入完成：成功 ' + written + ' 筆・跳過 ' + skipped + ' 筆'));
    }
    if (window.toast) window.toast('寫入成功', 'ok', '成功 ' + written + ' 筆・跳過 ' + skipped + ' 筆');
  }

  /* ================= Tab 3 AI 輸入 =================
     The design-review state switcher is RETIRED (2026-07-03, item 3): the three
     degraded panels are now driven ONLY by real API errors (402 額度 / 409 未啟用 /
     503 不可用) — they double as the usage-time hints when AI is later enabled. */
  function initAi() {
    $('#ai-normal').hidden = false;
    $('#ai-degrade-off').hidden = true;
    $('#ai-degrade-quota').hidden = true;
    $('#ai-degrade-down').hidden = true;
    $('#ai-parse').addEventListener('click', runAiPreview);
    const writeAll = $('#ai-write-all');
    if (writeAll) writeAll.addEventListener('click', commitAi);
    initAiImages();   // FU-D20: dropzone click / drag-drop / clipboard-paste screenshot intake
    loadAiModels();   // FU-D20: per-run model picker (enabled models + 自動; last-used persisted)
  }

  /* The CSV text the AI run returns; written via the import/commit path on 寫入全部. */
  let aiCsvText = '';
  /* FU-D20 attached screenshots for the current run: {name, dataUrl}. The dataUrl is the
     FileReader readAsDataURL result (a full `data:image/...;base64,` string) sent as-is —
     the server tolerates + strips the prefix. Money/quantity of record NEVER come from
     here: the LLM only extracts what the image shows, then preview→confirm→commit computes. */
  let aiImages = [];
  const AI_MAX_IMAGES = 4;

  /* Render the thumbnail strip with a per-image ✕ remove control. */
  function renderAiThumbs() {
    const strip = $('#ai-images');
    if (!strip) return;
    strip.replaceChildren();
    strip.hidden = aiImages.length === 0;
    aiImages.forEach((img, i) => {
      const cell = el('div', 'ai-thumb');
      cell.style.cssText = 'position:relative;width:64px;height:64px;border:1px solid ' +
        'var(--border,#2a2f3a);border-radius:6px;overflow:hidden;background:#0d0f14;';
      const im = el('img');
      im.src = img.dataUrl; im.alt = img.name || ('image ' + (i + 1));
      im.style.cssText = 'width:100%;height:100%;object-fit:cover;';
      const x = el('button', null, '✕'); x.type = 'button'; x.title = '移除';
      x.style.cssText = 'position:absolute;top:2px;right:2px;width:18px;height:18px;' +
        'line-height:16px;padding:0;border:none;border-radius:50%;cursor:pointer;' +
        'background:rgba(0,0,0,0.6);color:#fff;font-size:11px;';
      x.addEventListener('click', () => { aiImages.splice(i, 1); renderAiThumbs(); });
      cell.appendChild(im); cell.appendChild(x);
      strip.appendChild(cell);
    });
  }

  /* Read image Files -> base64 data URLs, capping the total at AI_MAX_IMAGES (toast on excess). */
  function addAiImages(files) {
    const list = Array.prototype.slice.call(files || [])
      .filter((fl) => fl && fl.type && fl.type.indexOf('image/') === 0);
    if (!list.length) return;
    const room = AI_MAX_IMAGES - aiImages.length;
    if (room <= 0) {
      if (window.toast) window.toast('最多 ' + AI_MAX_IMAGES + ' 張圖片', 'fail');
      return;
    }
    if (list.length > room && window.toast) {
      window.toast('最多 ' + AI_MAX_IMAGES + ' 張圖片', 'fail', '已略過多餘的圖片');
    }
    list.slice(0, room).forEach((fl) => {
      const r = new FileReader();
      r.onload = () => {
        if (aiImages.length >= AI_MAX_IMAGES) return;   // guard the async race
        aiImages.push({ name: fl.name, dataUrl: String(r.result || '') });
        renderAiThumbs();
      };
      r.onerror = () => { if (window.toast) window.toast('圖片讀取失敗', 'fail', fl.name); };
      r.readAsDataURL(fl);
    });
  }

  /* Wire the three intake paths onto the dropzone / hidden file input / pane paste. */
  function initAiImages() {
    const dz = $('#ai-dropzone');
    const fileIn = $('#ai-file-input');
    if (dz && fileIn) {
      dz.style.cursor = 'pointer';
      dz.addEventListener('click', () => fileIn.click());
      fileIn.addEventListener('change', () => { addAiImages(fileIn.files); fileIn.value = ''; });
      dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('dz-over'); });
      dz.addEventListener('dragleave', () => dz.classList.remove('dz-over'));
      dz.addEventListener('drop', (e) => {
        e.preventDefault();
        dz.classList.remove('dz-over');
        addAiImages(e.dataTransfer && e.dataTransfer.files);
      });
    }
    /* clipboard paste of an image while focus is anywhere in the AI pane. */
    const pane = $('#pane-ai');
    if (pane) {
      pane.addEventListener('paste', (e) => {
        const items = (e.clipboardData && e.clipboardData.items) || [];
        const imgs = [];
        for (let i = 0; i < items.length; i++) {
          if (items[i].kind === 'file' && items[i].type.indexOf('image/') === 0) {
            const fl = items[i].getAsFile();
            if (fl) imgs.push(fl);
          }
        }
        if (imgs.length) { e.preventDefault(); addAiImages(imgs); }
      });
    }
  }

  /* Populate the model picker from GET /api/llm/config (enabled models only). Persist the
     choice in localStorage `pd_ai_model`; a stale/disabled persisted alias silently falls
     back to 自動 (it simply won't match any option). AI-off / guest leaves 自動 only. */
  async function loadAiModels() {
    const sel = $('#ai-model-select');
    if (!sel) return;
    let cfg;
    try { cfg = await api.get('/api/llm/config'); } catch (e) { return; }
    const models = (cfg && cfg.models) || [];
    models.filter((mo) => mo.enabled).forEach((mo) => {
      const o = el('option', null, mo.alias + (mo.vision ? '・支援影像' : ''));
      o.value = mo.alias;
      o.dataset.vision = mo.vision ? '1' : '';
      sel.appendChild(o);
    });
    try {
      const saved = localStorage.getItem('pd_ai_model');
      if (saved && Array.prototype.some.call(sel.options, (o) => o.value === saved)) {
        sel.value = saved;
      }
    } catch (e) { /* noop */ }
    sel.addEventListener('change', () => {
      try { localStorage.setItem('pd_ai_model', sel.value); } catch (e) { /* noop */ }
    });
  }

  /* Map a PdApiError code to the matching degraded panel. */
  function showAiDegrade(code) {
    const id = code === 'budget_exceeded' ? 'quota'
      : code === 'ai_not_activated' ? 'off'
        : 'down';
    $('#ai-normal').hidden = false;  // keep the result region; just clear the table
    $('#ai-degrade-off').hidden = id !== 'off';
    $('#ai-degrade-quota').hidden = id !== 'quota';
    $('#ai-degrade-down').hidden = id !== 'down';
  }

  /* FU-D33: open the shared quick-add dialog for an unregistered symbol in the AI preview.
     Market is inferred from the row's account (accountMarket — TWD→TW, USD→US, MYR→MY), the
     same rule the backend uses. On a successful register (or restore) the SAME preview re-runs
     (runAiPreview rebuilds the request from the unchanged pane state: text + images + model),
     so the healed row loses its error with zero re-entry. */
  function openAiQuickAdd(symbol, accId) {
    if (!window.pdInstQuickAdd) {
      if (window.toast) window.toast('對話框載入失敗，請重新整理', 'fail');
      return;
    }
    const resume = async () => { await reloadContext(); await runAiPreview(); };
    window.pdInstQuickAdd({
      symbol: symbol,
      market: accountMarket(accId),
      lockSymbol: true,
      onConfirm: resume,
      onBuy: resume,
    });
  }

  async function runAiPreview() {
    const text = ($('#ai-text') && $('#ai-text').value || '').trim();
    if (!text && !aiImages.length) {
      if (window.toast) window.toast('請貼上對帳單文字或上傳截圖', 'fail');
      return;
    }
    /* Resolve the per-run model. If a non-vision model is chosen WITH images attached,
       fall back to 自動 (the vision role chain) + show the inline hint — this keeps the
       frontend consistent with the server rule (which 400s a non-vision alias + images),
       so we never send that invalid combination. */
    const sel = $('#ai-model-select');
    let modelAlias = sel ? sel.value : '';
    const opt = sel && sel.selectedOptions && sel.selectedOptions[0];
    const modelIsVision = !!(opt && opt.dataset && opt.dataset.vision);
    const hint = $('#ai-model-hint');
    if (modelAlias && aiImages.length && !modelIsVision) {
      modelAlias = '';
      if (hint) hint.hidden = false;
    } else if (hint) {
      hint.hidden = true;
    }
    const payload = { text: text };
    if (aiImages.length) payload.images = aiImages.map((im) => im.dataUrl);
    if (modelAlias) payload.model_alias = modelAlias;
    let resp;
    try {
      resp = await api.post('/api/input/ai/preview', payload);
    } catch (err) {
      /* graceful degradation: 402 額度 / 409 未啟用 / 503 不可用 -> degraded panel + toast */
      if (err && (err.status === 402 || err.status === 409 || err.status === 503)) {
        showAiDegrade(err.code);
      }
      if (window.toast) window.toast((err && err.message) || 'AI 解析失敗', 'fail', err && err.code);
      return;
    }
    renderAiPreview(resp);
  }

  /* Render the AI preview rows + meta. cost_usd is a Decimal STRING -> f.num (never
     .toFixed). The per-row money in `data` is Decimal STRINGS -> fmt, same as CSV. */
  function renderAiPreview(preview) {
    $('#ai-degrade-off').hidden = true;
    $('#ai-degrade-quota').hidden = true;
    $('#ai-degrade-down').hidden = true;
    $('#ai-normal').hidden = false;
    aiCsvText = preview.csv_text || '';
    const meta = preview.meta || {};
    if ($('#ai-source')) {
      const cost = meta.cost_usd !== undefined && meta.cost_usd !== null
        ? '・成本 $' + f.num(meta.cost_usd, 4) : '';
      $('#ai-source').textContent = (meta.via || 'litellm') + cost;
    }
    if ($('#ai-model')) $('#ai-model').textContent = meta.model || '';

    const tbody = $('#ai-body');
    tbody.replaceChildren();
    (preview.rows || []).forEach((r) => {
      const d = r.data || {};
      const tr = el('tr');
      const tdCb = el('td');
      const cb = el('input'); cb.type = 'checkbox'; cb.checked = r.status !== 'error';
      cb.disabled = r.status === 'error';
      tdCb.appendChild(cb);
      tr.appendChild(tdCb);
      tr.appendChild(el('td', 'col-text', d.account_id || ''));
      tr.appendChild(el('td', 'col-text', f.date(d.trade_date || d.date)));
      const side = (d.side || '').toString().toLowerCase();
      const tdSide = el('td', 'col-text');
      tdSide.appendChild(el('span', 'dir-chip ' + (side === 'buy' ? 'dir-buy' : 'dir-sell'),
        side === 'buy' ? '買' : '賣'));
      tr.appendChild(tdSide);
      const symbol = d.symbol || '';
      const it = inst(symbol);
      const ccy = it ? it.ccy : '';
      const tdSym = el('td', 'col-text');
      const cell = el('div', 'sym-cell');
      cell.appendChild(el('span', 'sym-code', symbol));
      cell.appendChild(el('span', 'sym-name', it ? it.name : ''));
      tdSym.appendChild(cell);
      tr.appendChild(tdSym);
      tr.appendChild(el('td', 'num', f.num(d.quantity !== undefined ? d.quantity : d.shares)));
      tr.appendChild(el('td', 'num', f.price(d.price, ccy)));        // Decimal string -> fmt
      tr.appendChild(el('td', 'num', d.fee !== undefined ? f.money(d.fee, ccy) : f.NULL_GLYPH));
      tr.appendChild(el('td', 'num', d.tax !== undefined ? f.money(d.tax, ccy) : f.NULL_GLYPH));
      const tdNote = el('td', 'err-msg');
      if (r.reason) tdNote.appendChild(el('span', 'st-warn', '⚠ ' + r.reason));
      else tdNote.appendChild(el('span', 'st-ok', '✓ 解析完整'));
      tr.appendChild(tdNote);
      /* FU-D33: an unregistered-symbol row gets an inline 立即註冊 action opening the SHARED
         quick-add dialog (symbol prefilled + market inferred from the row's account, exactly
         as the backend auto-register does). On success the SAME preview re-runs automatically
         (text + images + model are still in the pane state), so the healed row resumes with
         zero re-entry. The commit-time auto-register fallback stays untouched. */
      const tdAct = el('td');
      if (r.code === 'unregistered_symbol' && symbol) {
        const reg = el('button', 'btn', '立即註冊'); reg.type = 'button';
        reg.title = '註冊此標的後自動重新解析';
        reg.addEventListener('click', () => openAiQuickAdd(symbol, d.account_id));
        tdAct.appendChild(reg);
      }
      tr.appendChild(tdAct);
      tbody.appendChild(tr);
    });
    const s = preview.summary || {};
    if (window.toast) window.toast('解析完成', 'ok', '共 ' + (s.total || 0) + ' 筆草稿');
  }

  /* Write the AI-parsed drafts: the run returns a canonical csv_text, so the commit
     reuses the SAME import/commit transaction path (single write seam). */
  async function commitAi() {
    if (!aiCsvText) {
      if (window.toast) window.toast('請先解析', 'fail');
      return;
    }
    try {
      const resp = await api.post('/api/import/commit',
        { kind: 'transactions', csv_text: aiCsvText, ack_warnings: false });
      onCsvWritten(resp);
    } catch (err) {
      if (err && err.status === 422 && err.code === 'warnings_unacknowledged') {
        window.confirmDialog({
          title: '匯入警告確認',
          body: 'AI 草稿中部分列有警告 — 確認後一併寫入？',
          confirmLabel: '確認寫入',
          onConfirm: async () => {
            try {
              const resp = await api.post('/api/import/commit',
                { kind: 'transactions', csv_text: aiCsvText, ack_warnings: true });
              onCsvWritten(resp);
            } catch (e2) {
              if (window.toast) window.toast((e2 && e2.message) || '寫入失敗', 'fail', e2 && e2.code);
            }
          }
        });
        return;
      }
      if (window.toast) window.toast((err && err.message) || '寫入失敗', 'fail', err && err.code);
    }
  }

  /* ================= 單筆寫入共用：一列 CSV 走匯入通道 =================
     (2026-07-03, items 1+2) 股利/換匯/期初的單筆表單把欄位組成「一列 CSV」，
     經過與批次匯入完全相同的 /api/import/preview 檢核 → /api/import/commit 寫入
     —— 單一寫入縫隙，不新增後端端點；警告列沿用確認機制。 */
  function csvEscape(v) {
    const s = String(v === null || v === undefined ? '' : v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  function oneRowCsv(header, values) {
    return header.join(',') + '\n' + values.map(csvEscape).join(',');
  }
  async function commitOneRow(kind, csvText, btn, okSub, onDone) {
    const restore = window.pdBusy ? window.pdBusy(btn, '寫入中…') : () => {};
    const finishOk = (resp) => {
      if (resp && resp.written >= 1) {
        if (window.toast) window.toast('寫入成功', 'ok', okSub);
        if (onDone) onDone();
      } else if (window.toast) {
        window.toast('未寫入', 'fail', '資料列被跳過，請檢查欄位');
      }
    };
    try {
      const pv = await api.post('/api/import/preview', { kind: kind, csv_text: csvText });
      const row = pv && pv.rows && pv.rows[0];
      if (!row) throw new Error('預覽無資料列');
      if (row.status === 'error') {
        restore();
        if (window.toast) window.toast('資料檢核未通過', 'fail', row.reason || '');
        return;
      }
      if (row.status === 'warn') {
        restore();
        window.confirmDialog({
          title: '警告確認',
          body: (row.reason || '此筆資料有警告') + ' — 確認後仍要寫入？',
          confirmLabel: '確認寫入',
          onConfirm: async () => {
            try {
              finishOk(await api.post('/api/import/commit',
                { kind: kind, csv_text: csvText, ack_warnings: true }));
            } catch (e2) {
              if (window.toast) window.toast((e2 && e2.message) || '寫入失敗', 'fail', e2 && e2.code);
            }
          }
        });
        return;
      }
      const resp = await api.post('/api/import/commit',
        { kind: kind, csv_text: csvText, ack_warnings: false });
      restore();
      finishOk(resp);
    } catch (err) {
      restore();
      if (window.toast) window.toast((err && err.message) || '寫入失敗', 'fail', err && err.code);
    }
  }

  /* ================= Tab 4 股利 ================= */
  function initDiv() {
    const accSel = $('#d-account');
    ctx.accounts.forEach((a) => {
      const o = el('option', null, a.name + '（' + a.ccy + '）');
      o.value = a.id;
      accSel.appendChild(o);
    });
    accSel.addEventListener('change', () => { renderDivForm(); onDivAccountChange(); });
    $('#d-date').value = TODAY;
    const typeSeg = document.querySelectorAll('#d-tw .segmented button');
    const isStock = () => {
      const b = document.querySelector('#d-type-stock');
      return !!(b && b.classList.contains('active'));
    };
    typeSeg.forEach((b) => b.addEventListener('click', () => {
      typeSeg.forEach((x) => x.classList.toggle('active', x === b));
      const stock = isStock();
      /* 配股時 Gross 欄位轉為「配股股數」、Net 欄位隱藏（$0 成本入帳） */
      $('#d-tw-gross-label').textContent = stock ? '配股股數' : 'Gross（總額）';
      $('#d-tw-net-field').hidden = stock;
      $('#d-model-note').textContent = stock
        ? '台股模式（配股）：以 $0 成本股數入帳，調整均價下降。'
        : '台股模式：現金股利沖減成本（調整均價下降）；配股以 $0 成本股數入帳。';
    }));
    renderDivForm();
    initDivPicker();
    $('#d-confirm').addEventListener('click', () => {
      const a = acc($('#d-account').value) || ctx.accounts[0];
      const sym = $('#d-symbol').value.trim();
      const dte = $('#d-date').value;
      if (!a || !sym || !dte) {
        if (window.toast) window.toast('請填寫帳戶、代號與日期', 'fail');
        return;
      }
      const header = ['account', 'symbol', 'date', 'type', 'gross', 'withholding', 'net',
        'reinvest_shares', 'reinvest_price'];
      let values;
      if (a.div_model === 'tw') {
        if (isStock()) {
          const shares = $('#d-tw-gross').value.trim();
          if (!shares) { if (window.toast) window.toast('請輸入配股股數', 'fail'); return; }
          values = [a.id, sym, dte, 'STOCK', '0', '', '', shares, ''];
        } else {
          const gross = $('#d-tw-gross').value.trim();
          if (!gross) { if (window.toast) window.toast('請輸入股利總額', 'fail'); return; }
          values = [a.id, sym, dte, 'CASH', gross, '', $('#d-tw-net').value.trim(), '', ''];
        }
      } else if (a.div_model === 'drip') {
        const gross = $('#d-drip-gross').value.trim();
        if (!gross) { if (window.toast) window.toast('請輸入股利總額', 'fail'); return; }
        values = [a.id, sym, dte, 'DRIP', gross, '', '',
          $('#d-drip-shares').value.trim(), $('#d-drip-price').value.trim()];
      } else {
        const amt = $('#d-net-amt').value.trim();
        if (!amt) { if (window.toast) window.toast('請輸入淨額', 'fail'); return; }
        values = [a.id, sym, dte, 'NET', amt, '', '', '', ''];
      }
      commitOneRow('dividends', oneRowCsv(header, values), $('#d-confirm'),
        sym + ' 股利已寫入帳本（' + a.name + '）', () => {
          ['d-tw-gross', 'd-tw-net', 'd-drip-gross', 'd-drip-wh', 'd-drip-net',
            'd-drip-shares', 'd-drip-price', 'd-net-amt'].forEach((id) => {
            const n = $('#' + id); if (n) n.value = '';
          });
          /* a STOCK/DRIP dividend can grow shares (never shrinks); refresh so the picker
             reflects the latest holdings. Cheap + cached, so unconditional is fine. */
          loadDivHoldings(a.id, true).catch(() => {});
        });
    });
  }
  function renderDivForm() {
    const a = acc($('#d-account').value) || ctx.accounts[0];
    const model = a ? a.div_model : 'tw';
    ['d-tw', 'd-drip', 'd-net'].forEach((id) => { $('#' + id).hidden = true; });
    const note = $('#d-model-note');
    if (model === 'tw') {
      $('#d-tw').hidden = false;
      note.textContent = '台股模式：現金股利沖減成本（調整均價下降）；配股以 $0 成本股數入帳。';
    } else if (model === 'drip') {
      $('#d-drip').hidden = false;
      note.textContent = 'DRIP 模式：預扣 30%，net 將以 $0 成本股數入帳（再投資股數 × 再投資價格僅供對帳）。';
    } else {
      $('#d-net').hidden = false;
      note.textContent = '馬股模式：單一淨額入帳（無預扣層級）。';
    }
    /* DRIP gross live recompute — USER-INPUT estimate (documented input-side calc;
       the value of record is computed by the backend on CSV import, not here). */
    $('#d-drip-gross').oninput = () => {
      const g = parseFloat($('#d-drip-gross').value) || 0;
      const wh = g * 0.30;
      $('#d-drip-wh').value = wh.toFixed(2);
      $('#d-drip-net').value = (g - wh).toFixed(2);
    };
  }

  /* ---- FU-D35 dividend 代號 picker (owner 需求六) ----
     After an account is chosen, activating 代號 lists that account's CURRENTLY-HELD
     symbols for point-and-click (dividends normally come from a live position). The
     「顯示已清倉標的」 toggle additionally lists symbols the account historically held but
     has since closed — a closed position can still pay a dividend after its ex-date
     (owner 假設 2). Held/closed come from GET /api/input/holdings?account=… (server-side
     Decimal share math), cached per account for the page session + refetched after a
     successful commit. The picker is ASSISTIVE ONLY: it never overwrites what the user
     types, and manual free entry always remains possible (the commit reads
     #d-symbol.value directly — an unlisted symbol still submits). */
  const divHoldingsCache = {};   // { [accountId]: {held:[{symbol,name}], closed:[...]} }
  let divPickerOpen = false;

  /* Inline-style the picker shell here (keeps the styling in this wave's files — the
     dividend section owns input.js; the shell ids live in trades.html #pane-div). */
  function styleDivPicker() {
    const p = $('#d-sym-picker');
    if (p) {
      p.style.cssText = 'position:absolute;left:0;right:0;top:100%;z-index:40;margin-top:4px;'
        + 'background:var(--panel-2,#141821);border:1px solid var(--border,#2a2f3a);'
        + 'border-radius:8px;box-shadow:0 10px 30px rgba(0,0,0,.45);max-height:260px;'
        + 'overflow:auto;padding:4px;';
    }
    const empty = $('#d-sym-empty');
    if (empty) empty.style.cssText = 'padding:8px 10px;color:var(--text-3,#8a92a3);font-size:11px;';
    const foot = $('#d-sym-foot');
    if (foot) {
      foot.style.cssText = 'border-top:1px solid var(--border,#2a2f3a);margin-top:4px;'
        + 'padding:7px 10px 3px;';
    }
    const tog = document.querySelector('.sym-pick-toggle');
    if (tog) {
      tog.style.cssText = 'display:flex;align-items:center;gap:7px;font-size:11px;'
        + 'color:var(--text-2,#c2c8d2);cursor:pointer;';
    }
  }

  /* Fetch (or return the cached) {held, closed} for an account. Graceful: a failed fetch
     returns the last cache (or empties) so the picker degrades to a plain typed input. */
  async function loadDivHoldings(accountId, force) {
    if (!accountId) return { held: [], closed: [] };
    if (!force && divHoldingsCache[accountId]) return divHoldingsCache[accountId];
    let resp;
    try {
      resp = await api.get('/api/input/holdings?account=' + encodeURIComponent(accountId));
    } catch (e) {
      return divHoldingsCache[accountId] || { held: [], closed: [] };
    }
    const data = { held: (resp && resp.held) || [], closed: (resp && resp.closed) || [] };
    divHoldingsCache[accountId] = data;
    return data;
  }

  function closeDivPicker() {
    const p = $('#d-sym-picker');
    if (p) p.hidden = true;
    divPickerOpen = false;
  }

  function fillDivSymbol(sym) {
    const inp = $('#d-symbol');
    if (inp) inp.value = sym;
    closeDivPicker();
  }

  /* One selectable row: 代號 + 名稱 (+ a muted 已清倉 tag for closed positions). Selection
     rides on mousedown+preventDefault so the value lands BEFORE the input's focusout —
     otherwise the outside-focus close would race the click away. */
  function divPickRow(item, isClosed) {
    const row = el('button', null, null);
    row.type = 'button';
    row.style.cssText = 'display:flex;align-items:baseline;gap:8px;width:100%;text-align:left;'
      + 'background:none;border:none;padding:6px 8px;cursor:pointer;color:inherit;'
      + 'border-radius:6px;font:inherit;';
    row.addEventListener('mouseenter', () => { row.style.background = 'rgba(255,255,255,.06)'; });
    row.addEventListener('mouseleave', () => { row.style.background = 'none'; });
    const code = el('span', null, item.symbol);
    code.style.cssText = 'font-weight:600;font-variant-numeric:tabular-nums;';
    row.appendChild(code);
    const name = el('span', null, item.name || '');
    name.style.cssText = 'color:var(--text-3,#8a92a3);font-size:11px;overflow:hidden;'
      + 'text-overflow:ellipsis;white-space:nowrap;';
    row.appendChild(name);
    if (isClosed) {
      const tag = el('span', null, '已清倉');
      tag.style.cssText = 'margin-left:auto;color:var(--text-3,#8a92a3);font-size:10px;'
        + 'border:1px solid var(--border,#2a2f3a);border-radius:4px;padding:0 6px;flex:none;';
      row.appendChild(tag);
    }
    row.addEventListener('mousedown', (e) => { e.preventDefault(); fillDivSymbol(item.symbol); });
    return row;
  }

  /* Render the list for the current account from {held, closed}, filtered by whatever is
     typed in 代號 (also matches names). The closed list appears only when the toggle is on
     AND the account has closed history; it is visually separated by a dashed divider. */
  function renderDivPicker(data) {
    const list = $('#d-sym-list');
    const empty = $('#d-sym-empty');
    const foot = $('#d-sym-foot');
    const toggle = $('#d-sym-closed-toggle');
    if (!list || !empty || !foot) return;
    list.replaceChildren();
    const a = acc($('#d-account').value);
    if (!a) {
      empty.hidden = false;
      empty.textContent = '請先選擇帳戶';
      foot.hidden = true;
      return;
    }
    const held = (data && data.held) || [];
    const closed = (data && data.closed) || [];
    foot.hidden = closed.length === 0;   // only offer the toggle where there IS closed history
    const showClosed = !!(toggle && toggle.checked) && closed.length > 0;
    const q = ($('#d-symbol').value || '').trim().toUpperCase();
    const match = (s) => !q || (s.symbol || '').toUpperCase().indexOf(q) >= 0
      || (s.name || '').toUpperCase().indexOf(q) >= 0;
    const heldF = held.filter(match);
    const closedF = showClosed ? closed.filter(match) : [];
    heldF.forEach((it) => list.appendChild(divPickRow(it, false)));
    if (closedF.length) {
      const divider = el('div', null);
      divider.style.cssText = 'border-top:1px dashed var(--border,#2a2f3a);margin:4px 0;';
      list.appendChild(divider);
      closedF.forEach((it) => list.appendChild(divPickRow(it, true)));
    }
    /* Empty-state copy — honest about WHY the list is empty; never blocks free entry. */
    if (heldF.length + closedF.length === 0) {
      empty.hidden = false;
      if (held.length === 0 && closed.length === 0) {
        empty.textContent = '此帳戶尚無標的紀錄 — 可直接輸入代號';
      } else if (held.length === 0 && !showClosed) {
        empty.textContent = '此帳戶目前無持有標的；勾選「顯示已清倉標的」可挑選歷史標的';
      } else {
        empty.textContent = '無相符標的 — 可直接輸入代號';
      }
    } else {
      empty.hidden = true;
    }
  }

  /* Open + populate the dropdown for the chosen account (cache-first paint, then refresh
     from the fetch). Wrapped so an assistive-picker error can never surface as an
     unhandled rejection (the e2e smoke asserts ZERO console errors). */
  async function openDivPicker() {
    try {
      const p = $('#d-sym-picker');
      if (!p) return;
      p.hidden = false;
      divPickerOpen = true;
      const a = acc($('#d-account').value);
      if (!a) { renderDivPicker({ held: [], closed: [] }); return; }
      if (divHoldingsCache[a.id]) renderDivPicker(divHoldingsCache[a.id]);
      const data = await loadDivHoldings(a.id, false);
      if (divPickerOpen) renderDivPicker(data);
    } catch (e) { /* picker is assistive — degrade silently */ }
  }

  /* Account switch: reset the toggle (held-first per account), close, warm the new cache. */
  function onDivAccountChange() {
    const toggle = $('#d-sym-closed-toggle');
    if (toggle) toggle.checked = false;
    closeDivPicker();
    const accId = $('#d-account').value;
    if (accId) loadDivHoldings(accId, false).catch(() => {});
  }

  function initDivPicker() {
    styleDivPicker();
    const inp = $('#d-symbol');
    if (inp) {
      inp.addEventListener('focus', () => { openDivPicker(); });
      inp.addEventListener('click', () => { openDivPicker(); });
      inp.addEventListener('input', () => {
        if (!divPickerOpen) { openDivPicker(); return; }
        const a = acc($('#d-account').value);
        renderDivPicker((a && divHoldingsCache[a.id]) || { held: [], closed: [] });
      });
      inp.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDivPicker(); });
    }
    const toggle = $('#d-sym-closed-toggle');
    if (toggle) toggle.addEventListener('change', () => {
      const a = acc($('#d-account').value);
      renderDivPicker((a && divHoldingsCache[a.id]) || { held: [], closed: [] });
    });
    /* Close when focus leaves the 代號 field (input OR the footer toggle). focusout bubbles,
       so one listener on the container covers both; relatedTarget inside the field (e.g.
       the toggle) keeps it open. Row clicks preventDefault so focus never leaves the input. */
    const field = $('#d-symbol-field');
    if (field) field.addEventListener('focusout', (e) => {
      const to = e.relatedTarget;
      if (to && field.contains(to)) return;
      closeDivPicker();
    });
    /* Warm the default account's cache so the first focus paints instantly. */
    const accId0 = $('#d-account').value;
    if (accId0) loadDivHoldings(accId0, false).catch(() => {});
  }

  /* ================= Tab 5 期初庫存 =================
     (換匯已移至「資金管理」統一管理 — 2026-07-03 R6 item 7；opening 單筆仍走
     one-row-CSV import path。) */
  function initFxOpen() {
    const oAccSel = $('#o-account');
    ctx.accounts.forEach((a) => {
      const o = el('option', null, a.name); o.value = a.id;
      oAccSel.appendChild(o);
    });
    $('#o-date').value = TODAY;
    $('#o-confirm').addEventListener('click', () => {
      const accId = $('#o-account').value || (ctx.accounts[0] && ctx.accounts[0].id) || '';
      const sym = $('#o-symbol').value.trim();
      const shares = $('#o-shares').value.trim();
      const avg = $('#o-avg').value.trim();
      const dte = $('#o-date').value;
      if (!accId || !sym || !shares || !avg || !dte) {
        if (window.toast) window.toast('請填寫帳戶、代號、股數、均價與建檔日', 'fail');
        return;
      }
      const csv = oneRowCsv(
        ['account', 'symbol', 'shares', 'original_avg_cost', 'build_date',
          'original_cost_total'],
        [accId, sym, shares, avg, dte, $('#o-total').value.trim()]);
      commitOneRow('openings', csv, $('#o-confirm'),
        sym + ' 期初庫存已建檔（同鍵覆蓋更新）', () => {
          ['o-symbol', 'o-shares', 'o-avg', 'o-total'].forEach((id) => {
            const n = $('#' + id); if (n) n.value = '';
          });
        });
    });
  }

  /* ===== boot: fetch /input/context, then init every tab. Graceful: on failure leave
     the forms empty + surface ONE toast (never an unhandled rejection — the e2e smoke
     asserts ZERO console errors). 401 is handled inside api.js. ===== */
  async function boot() {
    try {
      const resp = await api.get('/api/input/context');
      ctx = {
        accounts: (resp && resp.accounts) || [],
        fee_rules: (resp && resp.fee_rules) || {},
        instruments: (resp && resp.instruments) || [],
        holdings: (resp && resp.holdings) || {},
      };
    } catch (err) {
      if (window.toast) window.toast('輸入中心載入失敗', 'fail', (err && err.message) || undefined);
      /* fall through with empty ctx so the page still renders an (empty) shell */
    }
    initManual();
    initCsv();
    initAi();
    initDiv();
    initFxOpen();
    showTab('manual');
  }

  boot();
  /* NOTE (FU-D20, 2026-07-17): the old "截圖解析尚未開通" AI-dropzone stub is retired — the
     dropzone is now a REAL screenshot intake wired in initAi()/initAiImages() (click /
     drag-drop / clipboard-paste → vision parse via /api/input/ai/preview). */
})();
