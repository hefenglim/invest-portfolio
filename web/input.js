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
  /* Shared grouped 代號 picker controllers (Wave C — one component, three inputs). Assigned
     in each tab's init(); referenced by the account-change + add-new handlers. */
  let manualPicker = null;
  let divPicker = null;
  let openingPicker = null;
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
  /* `acks` is a SET of acknowledged soft warnings (key -> true), not a single boolean:
     M4-02, 2026-09-03. One `acked` flag meant one tick spoke for every soft warning on the
     draft — and, because only one of them was ever drawn, for warnings the owner had not
     been shown. `ackOversell` is the derived answer to the ONE question the commit asks
     (`ack_oversell`), so ticking 「重複交易」 can no longer claim a 賣超 was accepted. */
  const m = { side: 'buy', feeOverride: false, taxOverride: false,
              acks: Object.create(null), ackOversell: false };
  /* Latest server preview (Decimal STRINGS) — null until the first preview lands. */
  let mPreview = null;
  /* Today (local) — the natural default trade date; retires the design-stub
     2026-06-11 / 2330 / 1000 / 612.5 fake prefill (2026-07-02). */
  const TODAY = (() => {
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  })();

  /* zh account name from the single naming authority (web/names.js, FU-D37); id fallback. */
  const acctZh = (id) => (id ? (window.pdNames ? window.pdNames.account(id) : id) : '');

  function initManual() {
    const accSel = $('#m-account');
    ctx.accounts.forEach((a) => {
      const o = el('option', null, accountLabel(a));
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
      onManualAccountChange();   // #8: close the picker + re-scope holdings to the new account
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
    const msh = $('#m-short');
    if (msh) msh.addEventListener('change', schedulePreview);
    $('#m-fee-pencil').addEventListener('click', () => toggleOverride('fee'));
    $('#m-tax-pencil').addEventListener('click', () => toggleOverride('tax'));
    $('#m-fee').addEventListener('input', schedulePreview);
    $('#m-tax').addEventListener('input', schedulePreview);
    $('#m-confirm').addEventListener('click', commitManual);
    $('#m-clear').addEventListener('click', clearManual);
    initManualPicker();   // #8: grouped 已持有／未持有 代號 picker (replaces the free-text datalist)
    schedulePreview();
  }
  /* 「清除」 (audit M3, 2026-07-26): the button existed in the markup since the form was
     built but was never wired — clicking it did nothing at all. Reset the ENTRY fields to a
     pristine form: symbol/shares/price/fee/tax cleared, side back to 買進, date back to
     today, 當沖 unchecked, both fee/tax overrides released (so the auto-computed values
     return), picker closed. The ACCOUNT is deliberately KEPT — a user entering several
     trades stays in one account, and the select has no empty option. schedulePreview() then
     re-renders the neutral pristine state with 確認寫入 disabled (runManualPreview's
     empty-form branch), so the preview never keeps stale rows. */
  function clearManual() {
    ['m-symbol', 'm-shares', 'm-price', 'm-fee', 'm-tax'].forEach((id) => {
      const n = $('#' + id);
      if (n) n.value = '';
    });
    const dt = $('#m-daytrade');
    if (dt) dt.checked = false;
    const sc = $('#m-short');
    if (sc) sc.checked = false;
    applyOverrideState('fee', false);
    applyOverrideState('tax', false);
    $('#m-date').value = TODAY;
    if (manualPicker) manualPicker.close();
    setSide('buy');            // also calls schedulePreview()
    $('#m-symbol').focus();
    if (window.toast) window.toast('已清除表單', 'ok');
  }

  function setSide(s) {
    m.side = s;
    $('#m-side-buy').classList.toggle('active', s === 'buy');
    $('#m-side-buy').classList.toggle('buy-on', s === 'buy');
    $('#m-side-sell').classList.toggle('active', s === 'sell');
    $('#m-side-sell').classList.toggle('sell-on', s === 'sell');
    // 放空 only exists on the sell side; leaving a stale tick on a buy would silently
    // send short_sale=true and exempt the row from the 賣超 guard.
    const sl = $('#m-short-line'), sc = $('#m-short');
    if (sl) sl.hidden = s !== 'sell';
    if (sc && s !== 'sell') sc.checked = false;
    /* 當沖 is a SELL-tax rate (0.15% vs 0.3%); on a buy the box did nothing but could still
       be ticked (L20, demo audit 2026-09-16). Same treatment as 放空: hidden and cleared. */
    const dl = $('#m-daytrade-line'), dt = $('#m-daytrade');
    if (dl) dl.hidden = s !== 'sell';
    if (dt && s !== 'sell') dt.checked = false;
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
    const sc = $('#m-short');
    if (sc && sc.checked && m.side === 'sell') body.short_sale = true;
    /* Sent ONLY when the box is on screen — i.e. the symbol is unregistered and its market
       is one where the flag prices something. An absent box means "no answer given", which
       is exactly the `None` the server treats as AI-D40's unset. */
    const ne = $('#m-new-etf');
    if (ne) body.new_symbol_is_etf = !!ne.checked;
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
    renderSellHints();           // FU-D44: instant from cache; async fill on a cache miss
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
      /* The ETF answer, asked at the moment the user is already looking at the symbol
         (2026-08-28). Without it the row registers UNSET and the first TW sell only then
         discloses `etf_flag_unknown` — correct, but it surfaces the question after a sell has
         already been priced on the assumption.
         Shown ONLY for TW and MY, because those are the two markets where the flag changes
         money: TW sell tax 0.3% vs ETF 0.1%, and the MY stamp duty is ETF-EXEMPT. On a US
         trade it would be a field that never does anything.
         ⚠ It answers the REGISTRATION, never this trade: the body field is
         `new_symbol_is_etf` and the server reads it only on the auto-register path. */
      const mkt = accountMarket($('#m-account').value, null);
      if (mkt === 'TW' || mkt === 'MY') {
        const lab = el('label', null, null);
        lab.style.cssText = 'display:inline-flex;align-items:center;gap:4px;cursor:pointer;';
        const cb = el('input');
        cb.type = 'checkbox';
        cb.id = 'm-new-etf';
        lab.appendChild(cb);
        lab.appendChild(el('span', null, '這是 ETF'));
        lab.title = mkt === 'TW'
          ? '勾選後以 ETF 稅率註冊（賣出證交稅 0.1%，現股為 0.3%）。不勾選＝尚未回答，'
            + '首次賣出時會提示待確認，而不會靜默套用現股稅率'
          : '勾選後以 ETF 註冊（馬股印花稅 ETF 免徵）。不勾選＝尚未回答，首次賣出時會提示待確認';
        symHint.appendChild(lab);
        symHint.appendChild(el('span', null, '　'));
      }
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

  /* ---- FU-D44 sell-entry hints ----
     side=sell + a REGISTERED symbol chosen: under 股數 show 「可賣 {shares} 股」, under 價格
     show 「持有均價 {adjusted_avg}」 — clicking either fills its field. BOTH values are
     SERVER-computed (GET /api/input/holdings: shares via current_shares, adjusted_avg via
     the verified build_book cost-basis replay) and arrive as Decimal STRINGS: display goes
     through window.fmt only; the click-fill writes the RAW wire string (this module never
     computes money). Registered but not held in the selected account -> a muted
     此帳戶無持股 note (the sell preview still warns downstream as today). Buy side hides
     everything. The per-account cache is SHARED with the dividend picker
     (acctHoldingsCache) and dropped after every successful commit (FU-D45), so a
     just-committed trade updates 可賣 immediately. */
  let sellHintSeq = 0;
  function renderSellHints() {
    const sharesHint = $('#m-shares-hint');
    const priceHint = $('#m-price-hint');
    if (!sharesHint || !priceHint) return;
    const hide = (n) => { n.hidden = true; n.replaceChildren(); };
    const a = acc($('#m-account').value);
    const it = inst($('#m-symbol').value.trim());
    if (m.side !== 'sell' || !a || !it) { hide(sharesHint); hide(priceHint); return; }
    const data = acctHoldingsCache[a.id];
    if (!data) {
      /* cache miss: fetch, then re-render ONLY if the cache actually filled (a failed
         fetch must never loop) and no newer render superseded this request. */
      hide(sharesHint); hide(priceHint);
      const seq = ++sellHintSeq;
      loadAcctHoldings(a.id, false).then(() => {
        if (seq === sellHintSeq && acctHoldingsCache[a.id]) renderSellHints();
      }).catch(() => {});
      return;
    }
    const held = (data.held || []).find((h) => h.symbol === it.symbol);
    hide(priceHint);
    sharesHint.hidden = false;
    sharesHint.replaceChildren();
    if (!held) {
      sharesHint.textContent = '此帳戶無持股';
      return;
    }
    const fillBtn = (label, inputSel, raw) => {
      const b = el('button', null, label);
      b.type = 'button';
      b.title = '點擊帶入';
      b.style.cssText = 'background:none;border:none;padding:0;color:var(--accent);'
        + 'cursor:pointer;font-size:inherit;text-decoration:underline;';
      b.addEventListener('click', () => { $(inputSel).value = raw; schedulePreview(); });
      return b;
    };
    /* fractional shares (DRIP $0-cost adds) show up to 4 dp; whole counts stay integer —
       a STRING shape check on the wire value, not arithmetic. */
    const sharesTxt = f.shares(held.shares);
    sharesHint.appendChild(fillBtn('可賣 ' + sharesTxt + ' 股', '#m-shares', held.shares));
    if (held.adjusted_avg != null) {
      priceHint.hidden = false;
      priceHint.appendChild(
        fillBtn('持有均價 ' + f.price(held.adjusted_avg, it.ccy), '#m-price', held.adjusted_avg));
    }
  }

  /* Market resolution for the input forms (Batch B — merged multi-market accounts).
     - A REGISTERED symbol's market is AUTHORITATIVE: `inst(sym).market` from the context
       instruments list. F06 (per-row ccy) and the dividend model follow this.
     - The account's bound markets come from `a.markets` (the per-market /input/context wire):
       a single-market account has exactly one; a MERGED account has several with no single
       settlement-ccy answer, so the quick-add dialog's market select is the resolution path.
     `_CCY_MARKET` remains ONLY as a legacy fallback for a stale ctx lacking `a.markets`. */
  const _CCY_MARKET = { TWD: 'TW', USD: 'US', MYR: 'MY' };
  function acctMarkets(a) {
    return (a && a.markets && typeof a.markets === 'object') ? Object.keys(a.markets) : [];
  }
  function isMultiMarket(a) { return acctMarkets(a).length > 1; }
  function symbolMarket(sym) { const it = inst(sym); return it ? it.market : null; }
  /* Default market for a quick-add dialog opened on an UNREGISTERED symbol under `accId`.
     Single-market account -> its one bound market (identical to the old settlement-ccy
     inference). Merged account -> the `hint` market when it is bound, else the first bound
     one (the user can change it in the dialog). Legacy fallback when `a.markets` is absent. */
  function accountMarket(accId, hint) {
    const a = acc(accId);
    const mk = acctMarkets(a);
    if (mk.length > 1) return (hint && mk.indexOf(hint) >= 0) ? hint : mk[0];
    if (mk.length === 1) return mk[0];
    return a ? (_CCY_MARKET[a.settlement_ccy || a.ccy] || 'TW') : 'TW';
  }
  /* Account <option> label (demo audit 2026-09-16, M5 + L8). Two defects in one string:
     it printed `a.name` — the API's English name, so the select read 「TW Broker」 while the
     filter chips one panel up read 「台灣券商」 (the second spelling web/names.js exists to
     end) — and it bracketed `a.ccy`, the SETTLEMENT currency, which on the merged Moomoo
     account is USD although its funding currency is MYR and its MY trades book in MYR: the
     one currency the label named was the one the user was least likely to be entering.
     Now: the zh name from the single naming authority + the currencies the account
     actually trades in, derived from its bound markets (「Moomoo MY（USD／MYR）」).
     Re-verification 2026-09-17 (L8 partial): the derivation lived HERE, so cash.js's two
     selects never got it. It now lives in names.js (`pdNames.accountOption`) beside the
     name it decorates; this is the delegate, with the id as the no-names.js fallback. */
  function accountLabel(a) {
    if (window.pdNames && window.pdNames.accountOption) return window.pdNames.accountOption(a);
    return a.id + (a.ccy ? '（' + a.ccy + '）' : '');
  }
  /* Dividend MODEL (tw/drip/net) for the current dividend entry.
     Single-market account -> its one model (byte-identical to the old `a.div_model`).
     Merged account -> the model bound to the ENTERED SYMBOL's market (a.markets[market]).
     Returns null when a merged account has no symbol resolvable to a bound market yet
     (blank / unregistered) — the caller then prompts to pick/register first (never guesses). */
  function divModelFor(a, sym) {
    if (!a) return 'tw';
    const mk = (a.markets && typeof a.markets === 'object') ? a.markets : null;
    const keys = mk ? Object.keys(mk) : [];
    if (keys.length > 1) {
      const it = inst(sym);
      if (!it || !mk[it.market]) return null;   // symbol not yet resolved to a bound market
      return mk[it.market].div_model;
    }
    if (keys.length === 1) return mk[keys[0]].div_model;
    return a.div_model || 'tw';   // legacy fallback (markets absent from a stale ctx)
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

  /* ================= #8 manual-tab grouped 代號 picker (Wave C: shared component) =========
     The manual pane's grouped dropdown is now produced by the ONE shared component
     (window.pdSymPicker — web/sym-picker.js), which also drives the dividend and opening
     pickers. It groups 已持有 / 未持有, MARKET-FILTERS 未持有 by the selected account, EXCLUDES
     archived instruments from 未持有 (Fable F7), annotates 已持有 rows with 股數 + 均價 (SHARED
     per-account holdings cache — server Decimal strings via fmt; no money math), and shows the
     FULL list on a focus/click open (filtering only once the user types — Fable F5). A footer
     「＋新增標的」 opens the shared quick-add dialog and auto-selects the newly-registered symbol.

     ASSISTIVE ONLY: selecting a row writes #m-symbol.value and re-runs the SAME pipeline the
     rest of the form already reads (schedulePreview → renderSymbolHint + renderSellHints +
     runManualPreview), so free typing, the 未註冊 auto-register, preview, and commit are all
     untouched. The sell-side 可賣/持有均價 fill buttons (renderSellHints) stay complementary. */

  /* 「＋新增標的」: open the shared quick-add dialog (symbol editable, market inferred from the
     account). On a successful register the context reloads and the new symbol — which lands in
     未持有 — is auto-selected, driving the preview pipeline. `typed` is the picker's current
     query (so the dialog prefills what the user typed). */
  function openManualQuickAddNew(typed) {
    if (!window.pdInstQuickAdd) {
      if (window.toast) window.toast('對話框載入失敗，請重新整理', 'fail');
      return;
    }
    const sym0 = ((typed || $('#m-symbol').value || '')).trim().toUpperCase();
    if (manualPicker) manualPicker.close();
    const select = async (resp) => {
      await reloadContext();
      const sym = (resp && resp.symbol) || sym0;
      if (sym && manualPicker) manualPicker.select(sym);
      else { renderSymbolHint(); schedulePreview(); }
    };
    window.pdInstQuickAdd({
      symbol: sym0,
      market: accountMarket($('#m-account').value),
      lockSymbol: false,
      onConfirm: select,
      onBuy: select,
    });
  }

  /* Account switch: close the picker + warm the new account's holdings cache (the sell hints
     share the same cache). */
  function onManualAccountChange() {
    if (manualPicker) manualPicker.close();
    const accId = $('#m-account').value;
    if (accId) loadAcctHoldings(accId, false).catch(() => {});
  }

  function initManualPicker() {
    manualPicker = window.pdSymPicker.create({
      input: $('#m-symbol'),
      field: $('#m-symbol-field'),
      panel: $('#m-sym-picker'),
      list: $('#m-sym-list'),
      empty: $('#m-sym-empty'),
      foot: $('#m-sym-foot'),
      mode: 'held-unheld',
      marketFilter: true,
      annotateHeld: true,
      accountOf: () => acc($('#m-account').value),
      instrumentsOf: () => ctx.instruments,
      instOf: (s) => inst(s),
      loadHoldings: (id, force) => loadAcctHoldings(id, force),
      cachedHoldings: (id) => acctHoldingsCache[id],
      addNew: { button: $('#m-sym-addnew'), onAdd: (q) => openManualQuickAddNew(q) },
      onPick: () => { schedulePreview(); },
      emptyText: (built, q) => q
        ? '無相符標的 — 可直接輸入代號，或點下方「＋新增標的」'
        : '此帳戶所屬市場尚無標的 — 點下方「＋新增標的」新增',
    });
    /* Warm the default account's cache so the first focus paints instantly. */
    const accId0 = $('#m-account').value;
    if (accId0) loadAcctHoldings(accId0, false).catch(() => {});
  }

  /* Re-fetch structural context (accounts / instruments / holdings) into `ctx`, so a
     just-registered symbol resolves immediately (the 未註冊 hint clears + it appears in the
     pickers, which read ctx.instruments live). Graceful: a failed refetch keeps the prior ctx. */
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
       page — render the neutral empty state with the confirm disabled. A selected symbol
       alone does not end pristineness (L18): after a commit or 清除 the symbol may stay for
       the next entry, and 「股數必須大於 0」 before any 股數 was typed blames the user for a
       field the page itself just emptied. Both entry fields empty ⇒ neutral. */
    if ($('#m-shares').value.trim() === '' && $('#m-price').value.trim() === '') {
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
       (read-only) input fields via fmt; when overridden, the user's own value stays.
       F06: fee/tax are in the RESOLVED instrument's quote ccy (MYR for an MY draft on a
       merged account); fall back to the account ccy when unresolved. Single-market: same. */
    const it = inst(sym);
    const ccy = (it && it.ccy) || a.ccy;
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
    /* F06: a REGISTERED symbol's own quote ccy drives the preview-card money labels + prices
       (an MY draft on a merged account shows MYR + 3-dp), falling back to the account
       settlement ccy when unresolved. Single-market: it.ccy === a.ccy, so unchanged. */
    const it = inst($('#m-symbol').value);
    const ccy = (it && it.ccy) || (a ? a.ccy : '');
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

    /* ===== M4-02 (2026-09-03): EVERY soft warning is shown, and acked on its own =====

       This line used to be `soft.find(sell_exceeds_holdings) || soft[0] || null`, and only
       that ONE issue was rendered. Everything else the server warned about never entered the
       DOM. Measured on the running app: `duplicate_trade` + `etf_flag_unknown` drew the
       duplicate alone, so a TW sell was computed at 現股 0.3% with 「無法判定是否為 ETF，
       賣出稅率待確認」 dropped — which is the silence AI-D40 exists to end
       (`markets-and-fees.md`: an unknown rate is DISCLOSED, never defaulted in silence). Same
       shape for 「無 USD/MYR 匯率，印花稅未計」 hiding behind a cash overdraft.

       賣超 keeps the FIRST slot — it is the destructive one, `#m-ack` is its tick in five e2e
       flows, and §6.7 orders its own block — so a single-warning draft renders byte-identically
       to before. The rest are listed BESIDE it, never folded into it. */
    const softRank = (i) => (i.code === 'sell_exceeds_holdings' ? 0 : 1);
    const softOrdered = soft.slice().sort((a, b) => softRank(a) - softRank(b));
    /* One tick per warning, keyed by code AND the exact sentence, for two reasons: two
       different warnings can never share a tick (that is what「勾一次認掉全部」was), and a
       re-worded warning — the 賣超 text names the quantities — is a warning that has not been
       read yet, so its tick starts clear. Keys not on screen are dropped, so an ack never
       outlives the warning it answered. */
    const ackKey = (i) => i.code + '\n' + i.text;
    const liveAcks = softOrdered.map(ackKey);
    Object.keys(m.acks).forEach((k) => { if (liveAcks.indexOf(k) < 0) delete m.acks[k]; });

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
    /* Every value below is a SERVER Decimal STRING routed through window.fmt for DISPLAY
       only (thousands separators / dp) — this module performs no money arithmetic. */
    const pcRow = (k, v, signCls) => {
      const row = el('div', 'pc-row');
      row.appendChild(el('span', 'k', k));
      row.appendChild(el('span', 'v' + (signCls ? ' ' + signCls : ''), v));
      rows.appendChild(row);
    };
    /* R7 A4: OLD → NEW comparison row — two SERVER-formatted strings joined by an arrow (a
       fresh position renders old as「—」via fmt's null glyph). No money arithmetic here. */
    const pcPair = (k, oldV, newV, note) => {
      const row = el('div', 'pc-row');
      row.appendChild(el('span', 'k', k));
      const v = el('span', 'v');
      v.appendChild(el('span', 'pc-old', oldV));
      v.appendChild(el('span', 'pc-arrow', ' → '));
      v.appendChild(el('span', 'pc-new', newV));
      if (note) v.appendChild(el('span', 'pc-note', note));
      row.appendChild(v);
      rows.appendChild(row);
    };
    if (hasServer) {
      [['成交金額', preview.gross], ['手續費' + (m.feeOverride ? '（已覆寫）' : ''), preview.fee],
       ['交易稅' + (m.taxOverride ? '（已覆寫）' : ''), preview.tax]].forEach(([k, v]) => {
        pcRow(k, f.money(v, ccy) + ' ' + ccy);
      });
      /* R6-E + R7 A4: drawer-parity 試算 what-if rendered as OLD → NEW pairs (SERVER-computed;
         position_preview is null when the symbol is unregistered / inputs incomplete — the
         rows simply do not render). A fresh BUY has null old_* → old renders as「—」.
         ⚠ This block used to render the SELL averages as old → OLD, on the stated assumption
         that "a SELL leaves the averages unchanged". That holds for the ORDINARY branch and
         for no other (sweep F-01, 2026-08-27): a declared short leaves a SHORT lot whose basis
         is the proceeds received, an undeclared oversell DISCARDS the basis, and a full exit
         leaves no position at all. The server now projects all three (`new_*_avg`, null when
         there is no position), so this side never re-derives one. */
      const pp = preview.position_preview;
      /* cost_removed / realized_pnl are NULL on the two branches the ledger books no realized
         row for — extending a short, and an undeclared 賣超 (review 2026-08-24). f.* renders
         the null glyph; pp.note carries the server's explanation. The ccy suffix is dropped on
         a null so the row reads「—」and not「— USD」. */
      const amt = (v) => f.money(v, ccy) + (v == null ? '' : ' ' + ccy);
      const sgn = (v) => f.signed(v, ccy) + (v == null ? '' : ' ' + ccy);
      if (pp && pp.kind === 'sell') {
        pcPair('持股', f.shares(pp.old_shares), f.shares(pp.remain_shares));
        /* On the 賣超 branch the projected average is ZERO because the replay DISCARDS the
           basis — which is what the ledger will hold, so the number agrees with the row the
           user is about to see. The note carries the reason, so 0 cannot be read as 「your
           average cost is nothing」 (owner ruling 2026-08-27, F-01). */
        const avgNote = pp.oversell ? '基礎已捨棄' : null;
        pcPair('原始均價', f.price(pp.old_original_avg, ccy),
               f.price(pp.new_original_avg, ccy), avgNote);
        pcPair('調整均價', f.price(pp.old_adjusted_avg, ccy),
               f.price(pp.new_adjusted_avg, ccy), avgNote);
        pcRow('調整成本移除', amt(pp.cost_removed));
        pcRow('已實現損益', sgn(pp.realized_pnl), f.signClass(pp.realized_pnl));
        if (pp.short_opened) pcRow('開／加空股數', f.shares(pp.short_opened));
      } else if (pp && pp.kind === 'buy') {
        pcPair('持股', f.shares(pp.old_shares), f.shares(pp.new_shares));
        pcPair('原始均價', f.price(pp.old_original_avg, ccy), f.price(pp.new_original_avg, ccy));
        pcPair('調整均價', f.price(pp.old_adjusted_avg, ccy), f.price(pp.new_adjusted_avg, ccy));
        if (pp.covered_shares) {
          pcRow('回補空單股數', f.shares(pp.covered_shares));
          pcRow('已實現損益', sgn(pp.realized_pnl), f.signClass(pp.realized_pnl));
        }
      }
      if (pp && pp.note) {
        const n = el('div', 'pc-row');
        n.style.opacity = '.8';
        n.textContent = pp.note;
        rows.appendChild(n);
      }
      /* R6-E: DISPLAY-ONLY account cash line + R7 A3 交易後現金 (A4 rename; owner-signed: no gating).
         Visually separated from the what-if rows; both use the SAME dynamic ccy label (ac.ccy);
         null balance / cash_after -> the shared null glyph. */
      const ac = preview.account_cash;
      if (ac) {
        const row = el('div', 'pc-row');
        row.style.borderTop = '1px solid var(--border)';
        row.style.marginTop = '3px';
        row.appendChild(el('span', 'k', '該帳戶現金（' + ac.ccy + '）'));
        row.appendChild(el('span', 'v',
          ac.balance != null ? f.money(ac.balance, ac.ccy) + ' ' + ac.ccy : f.NULL_GLYPH));
        rows.appendChild(row);
        const afterRow = el('div', 'pc-row');
        afterRow.appendChild(el('span', 'k', '交易後現金（' + ac.ccy + '）'));
        afterRow.appendChild(el('span', 'v',
          preview.cash_after != null ? f.money(preview.cash_after, ac.ccy) + ' ' + ac.ccy : f.NULL_GLYPH));
        rows.appendChild(afterRow);
      }
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
    m.ackOversell = false;
    softOrdered.forEach((warn, idx) => {
      /* ===== §6.7 door 1, ON THE ORDINARY PATH (fixed 2026-08-12) =====

         This acknowledgement used to be a bare tick — 「我了解，仍要寫入。」 — and ticking it
         is what makes the commit carry `ack_oversell: true`, which is what stops the server
         answering 422 `oversell_unacknowledged`, which is the ONLY thing the three-option
         dialog below is wired to. So on the path every owner actually walks, the dialog
         never opened: the box was ticked, the position's cost basis was discarded
         permanently (the STICKY 賣超 rule), and 補登公司行動 — the repair this whole feature
         exists to offer, and the one §6.7 lists FIRST because it is the non-destructive
         reading of the same evidence — was never mentioned.

         So the repair is offered HERE, above the tick and in the same box, phrased as the
         likely cause rather than as an escape hatch: a sell exceeding holdings is far more
         often a split/exchange/spin-off nobody recorded than a data-entry error. It stays
         OPTIONAL and it does not gate anything — a declared short (which never reaches this
         branch; `short_sale` exempts it in validate.py) and a genuinely intended oversell
         are still the owner's call, one tick away, exactly as before.

         What the tick now says changed too. It is the most destructive confirmation in the
         system and it used to describe itself as 「我了解」; it now names the consequence in
         §6.7's own words, so the two options can be compared before one is taken.

         ⚠ `warn` is NOT always an oversell — `cash_overdraft`, `future_trade_date`,
         `duplicate_trade`, `etf_flag_unknown` and `stamp_fx_missing` all render through this
         same shape. Both additions below are therefore gated on the real code: a 補登公司行動
         button beside a cash overdraft would be a repair for a problem it cannot touch, and
         telling the owner that acknowledging a future-dated trade 「歸零成本基礎」 would be
         simply false. Until M4-02 this block ran ONCE for `soft[0]` and the rest of the list
         was dropped; it now runs per warning, which is why the gating matters more, not less. */
      const isOversell = warn.code === 'sell_exceeds_holdings';
      const div = el('div', 'issue issue-warn');
      div.appendChild(el('span', null, '⚠'));
      const col = el('div');
      col.style.cssText = 'display:flex;flex-direction:column;gap:6px;min-width:0;';
      col.appendChild(el('span', null, warn.text));
      if (isOversell) {
        /* Accent-weighted, like the same option in the dialog: §6.7 lists it FIRST because
           it is the recommended reading, and a neutral button next to a tick makes the two
           look equally advisable. 確認寫入 is disabled while this box is up, so there is no
           competing live primary on screen. */
        const fix = el('button', 'btn btn-sm btn-primary',
          '這是公司行動造成的（分割／換股／分拆）→ 補登公司行動');
        fix.type = 'button';
        fix.id = 'm-oversell-fix';
        fix.style.cssText = 'align-self:flex-start;text-align:left;';
        fix.addEventListener('click', () => openCorpActionRepair(warn.text, manualBody()));
        col.appendChild(fix);
        const sub = el('span', null,
          '開啟補登表單，已帶入帳戶、代號與日期範圍；補登後這筆賣出就會通過檢查，成本基礎不會被捨棄');
        sub.style.cssText = 'font-size:10px;color:var(--text-3);line-height:1.5;';
        col.appendChild(sub);
      }
      const lab = el('label');
      const cb = el('input');
      const key = ackKey(warn);
      cb.type = 'checkbox';
      /* The first tick keeps the id `#m-ack`. Five e2e flows drive it by that id, and 賣超
         sorts first, so a draft carrying one is unchanged; the rest are numbered. */
      cb.id = idx === 0 ? 'm-ack' : 'm-ack-' + idx;
      cb.checked = m.acks[key] === true;
      cb.addEventListener('change', () => {
        if (cb.checked) m.acks[key] = true; else delete m.acks[key];
        renderManual(mPreview, issues, serverOk);
      });
      lab.appendChild(cb);
      lab.appendChild(el('span', null, isOversell
        ? '確認為賣超，接受成本基礎歸零（待釐清）：這個部位的成本基礎會被永久捨棄，之後再買回也不會還原。'
        : '我了解，仍要寫入。'));
      col.appendChild(lab);
      div.appendChild(col);
      issueBox.appendChild(div);
      /* 確認寫入 waits for ALL of them: a warning is read one at a time, so it is consented
         to one at a time. `ackOversell` answers the commit's single `ack_oversell` question
         from the 賣超 tick ALONE — never from whichever box happened to be on screen. */
      if (m.acks[key] !== true) ackOk = false;
      else if (isOversell) m.ackOversell = true;
    });
    if (hasServer && !hard.length && !softOrdered.length) {
      const div = el('div', 'issue issue-ok');
      div.appendChild(el('span', null, '✓'));
      div.appendChild(el('span', null, '草稿檢核通過，可寫入'));
      issueBox.appendChild(div);
    }
    $('#m-confirm').disabled = !hasServer || hard.length > 0 || !ackOk;
  }

  /* §6.7 door 1's repair, in ONE implementation (§6.0: one owner per concept).

     TWO surfaces open it and they must prefill identically, or the same problem meets the
     owner as two different forms: the inline offer inside the preview's 賣超 warning (the
     ordinary path — the draft has not been sent yet) and the first option of the
     three-option dialog below (the stale-preview path — the server already refused with
     422). Both pass the SAME draft body, so both bound the date window by the sell's own
     trade date: the sell is the evidence that the action had already taken effect, so an
     action dated after it cannot be the explanation. */
  function openCorpActionRepair(msg, body) {
    if (!window.pdCorpActionForm) { window.location.href = 'trades.html'; return; }
    window.pdCorpActionForm.open({
      account_id: body.account_id,
      from_symbol: body.symbol,
      date: body.date,
      date_max: body.date,
      reason: '這筆賣出被判為賣超：' + msg
        + '。若原因是漏登公司行動，補登後股數就會對上，成本基礎不會被捨棄。',
      /* Re-preview rather than auto-commit: the repaired draft must come back through the
         same validation and be pressed by the owner, never written on their behalf. The
         caches the saved action made stale (holdings, a SPINOFF child in ctx) were already
         refreshed: corp-action-form.js calls window.pdLedgerRefresh before onSaved, and that
         seam IS refreshAfterLedgerChange once adoptLedgerSeam has run (DEF-019). */
      onSaved: () => { schedulePreview(); }
    });
  }

  /* ===== Door 1 (spec 2026-08-06 §6.7) — the 賣超 dialog gains a THIRD option, FIRST =====

     This is the moment the corporate-action feature exists for. 確認 discards the position's
     cost basis PERMANENTLY (the STICKY 賣超 guard), and the most common real cause of a sell
     exceeding holdings is a split/exchange/spin-off that was never recorded — not a data
     entry error and not a short. So the destructive confirmation becomes a guided repair,
     offered at the one moment the owner is already looking at the evidence.

     Deliberately NOT the plain confirmDialog: that widget has one action, and the ordering
     of the three options is the point (§6.7 lists 補登公司行動 first). Pre-filled with the
     account, the symbol and a date window bounded by the sell's own trade date. */
  function oversellDialog(msg, body) {
    const backdrop = el('div', 'modal-backdrop');
    const modal = el('div', 'modal');
    const head = el('div', 'modal-head');
    head.appendChild(el('h3', 'modal-title', '賣超確認'));
    const x = el('button', 'modal-close', '✕');
    x.type = 'button';
    head.appendChild(x);
    modal.appendChild(head);
    const bodyEl = el('div', 'modal-body');
    bodyEl.appendChild(el('div', null, '⚠ ' + msg));
    const opts = el('div', 'os-options');
    const dismiss = () => backdrop.remove();

    opts.style.cssText = 'display:flex;flex-direction:column;gap:8px;margin-top:10px;';
    /* Styled inline rather than in input.css: this dialog is also the shape door 2 will
       reuse, and index.html does not load input.css — a rule that lives in one page's
       stylesheet renders the option list unstyled on the page it is added to next. */
    const option = (label, sub, accent, onPick) => {
      const b = el('button');
      b.type = 'button';
      b.style.cssText = 'text-align:left;display:flex;flex-direction:column;gap:3px;'
        + 'padding:9px 11px;border-radius:var(--radius-sm);cursor:pointer;'
        + 'background:var(--panel-2);color:var(--text);border:1px solid '
        + (accent ? 'var(' + accent + ')' : 'var(--border)') + ';';
      const l = el('span', null, label);
      l.style.cssText = 'font-size:12px;' + (accent ? 'color:var(' + accent + ');' : '');
      const s = el('span', null, sub);
      s.style.cssText = 'font-size:10px;color:var(--text-3);line-height:1.5;';
      b.appendChild(l);
      b.appendChild(s);
      b.addEventListener('click', () => { dismiss(); onPick(); });
      opts.appendChild(b);
    };

    /* FIRST, and phrased as the likely cause rather than as an escape hatch. Same words and
       same prefill as the inline offer in the preview — one implementation, two surfaces. */
    option('這是公司行動造成的（分割／換股／分拆）→ 補登公司行動',
      '開啟補登表單，已帶入帳戶、代號與日期範圍；補登後這筆賣出就會通過檢查，成本基礎不會被捨棄',
      '--accent', () => openCorpActionRepair(msg, body));
    option('確認為賣超，接受成本基礎歸零（待釐清）',
      '這個部位的成本基礎會被永久捨棄，並在儀表板上標示為待釐清；之後再買回也不會還原',
      '--up', async () => {
        const acked = manualBody();
        acked.ack_oversell = true;
        try {
          onManualWritten(await api.post('/api/input/manual/commit', acked));
        } catch (e2) {
          if (window.toast) window.toast((e2 && e2.message) || '寫入失敗', 'fail', e2 && e2.code);
        }
      });
    option('取消', '先不要寫入這筆交易', null, () => {});
    bodyEl.appendChild(opts);
    modal.appendChild(bodyEl);
    backdrop.appendChild(modal);
    x.addEventListener('click', dismiss);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) dismiss(); });
    document.body.appendChild(backdrop);
  }

  /* Follow-up 1 (§6.7 / §3.2): the corporate-action form calls this after a save that
     produced a fractional share, so the cash-in-lieu SELL is one click away with the
     fraction pre-filled and the PRICE deliberately blank — the owner reads it off the
     statement; we never guess a price. */
  window.pdPrefillManualSell = function (p) {
    const tab = document.getElementById('tab-manual');
    if (tab) tab.click();
    const section = document.getElementById('input-section');
    if (section) section.classList.add('open');
    const set = (id, v) => { const n = $('#' + id); if (n && v !== undefined && v !== null) n.value = v; };
    set('m-account', p.account_id);
    set('m-symbol', p.symbol);
    set('m-shares', p.shares);
    set('m-date', p.date);
    set('m-price', '');
    const sellBtn = $('#m-side-sell');
    if (sellBtn) sellBtn.click();
    schedulePreview();
    if (window.toast) window.toast('已帶入零股賣出', 'ok', '請填入實際收到的金額 ÷ 股數作為價格');
  };

  /* Commit the manual transaction. 201 -> success toast + reset draft state; 422
     oversell_unacknowledged -> the three-option 賣超 dialog above;
     400 / other PdApiError -> error toast carrying the backend message + code. */
  async function commitManual() {
    const body = manualBody();
    /* M4-02: from the 賣超 tick alone. A `duplicate_trade` or `future_trade_date` tick is
       not a claim that a 賣超 was accepted, and this flag is the only thing that silences
       the commit-time 422. */
    body.ack_oversell = m.ackOversell;
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
        oversellDialog(msg, body);
        return;
      }
      if (window.toast) window.toast((err && err.message) || '寫入失敗', 'fail', err && err.code);
    }
  }

  async function onManualWritten(resp) {
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
    applyOverrideState('fee', false); applyOverrideState('tax', false);
    m.acks = Object.create(null); m.ackOversell = false;
    $('#m-shares').value = '';
    $('#m-price').value = '';
    /* L18 (demo audit 2026-09-16): 手續費 20 / 交易稅 0 stayed in their fields after a commit
       — the auto-computed values of the trade just written, beside an empty 股數 — and the
       re-preview then printed 「股數必須大於 0」 in red for a form nobody had touched yet. The
       read-only fields are cleared with the entry fields, and runManualPreview treats
       "no shares and no price" as pristine whether or not a symbol is still selected. */
    $('#m-fee').value = '';
    $('#m-tax').value = '';
    /* Fable F8: a commit that AUTO-REGISTERED an unknown symbol must refresh ctx so the 未註冊
       hint clears, the symbol resolves in the preview, and it appears in every picker (manual /
       opening read ctx.instruments live). Awaited before schedulePreview so renderSymbolHint
       resolves the now-registered symbol. */
    /* FU-D45 + #10 + DEF-019: the ONE post-change refresh — ledger tables + holdings caches
       (可賣 just changed); a manual write is always a full-success transaction row -> flash +
       auto-switch the 交易 tab. `context` only when the commit auto-registered a symbol, and
       awaited, so schedulePreview below resolves the now-registered symbol (Fable F8). */
    await refreshAfterLedgerChange('transactions',
      { context: !!(resp && resp.auto_registered) });
    schedulePreview();
  }

  /* ================= Tab 2 CSV 匯入 ================= */
  /* kind chips map the UI label to the import endpoint `kind`. */
  /* ⚠ A new kind is registered in SEVEN places (audit F-28); this is one of them, and
     CSV_HINTS below is another. The backend five are the parser module + its *_COLUMNS
     constant, TEMPLATE_KINDS / DATE_COLUMN_BY_KIND / OPTIONAL_COLUMNS / _HEADERS / _ROWS
     in data_ingestion/import_templates.py, and _BUILDERS / _WRITERS in
     api/routers/input_center.py. Miss this line and the kind exists but is unreachable.
     An EIGHTH point exists for a kind whose parser takes an injected dependency: its
     _BUILDERS entry must be the named wrapper that BINDS it (`cash` -> _cash_builder), not
     the bare parser. See data_ingestion/cash_import.py's module docstring. */
  const CSV_KINDS = [['交易', 'transactions'], ['股利', 'dividends'], ['換匯', 'fx'], ['期初', 'openings'], ['公司行動', 'corporate_actions'], ['資金', 'cash']];
  let csvKind = 'transactions';
  /* FU-D19: the pinned date format (a dateparse format id) once the user resolves an
     ambiguous date column; null = let the backend infer. Reset whenever the CSV text or
     the kind changes so a new file re-detects from scratch. */
  let csvDateFormat = null;
  /* DEF-017 (2026-09-23): the file name the paste came from, sent as the batch's
     `source_name` so 最近匯入 can say WHICH file a batch was; '' once the text is hand-edited. */
  let csvSourceName = '';

  /* Shown in #csv-kind-note: the expected date shape + the never-guess promise (FU-D19). */
  const CSV_DATE_NOTE = '日期欄位建議 YYYY-MM-DD；2026/7/10、20260710 等常見格式亦可自動辨識，'
    + '無法判斷（如 3/4/2026）時會請你選擇格式。';

  /* per-kind CSV header hints shown in the dropzone — the FULL canonical header (leads with
     the REQUIRED `account` column; matches the *_COLUMNS constants in the backend parsers +
     the downloadable 範本; date carries its YYYY-MM-DD hint, optional columns marked 選填). */
  const CSV_HINTS = {
    transactions: '欄位：account・symbol・side・date（YYYY-MM-DD）・shares・price・fee（選填）・tax（選填）・daytrade（選填）・short_sale（選填，1＝宣告放空）・note（選填）',
    dividends: '欄位：account・symbol・date（YYYY-MM-DD，發放日）・type（CASH/STOCK/DRIP/NET）・gross・withholding（選填）・net（選填）・reinvest_shares（選填）・reinvest_price（選填）・ex_date（選填，除息日；僅配股會用到）',
    fx: '欄位：account・date（YYYY-MM-DD）・from_ccy・from_amount・to_ccy・to_amount',
    openings: '欄位：account・symbol・shares・original_cost_total・build_date（YYYY-MM-DD）・original_avg_cost（選填・舊檔相容）',
    /* 比例一定是「兩個整數欄位」，不是一個算好的小數（§3.1(ii)）：0.2857 這種寫法會讓
       700 股的 2 換 7 算成 199.99 股，之後賣 200 股會被判成賣超、成本基礎被永久捨棄。 */
    corporate_actions: '欄位：account・date（YYYY-MM-DD）・kind（SPLIT/EXCHANGE/SPINOFF，也可填 分割/換股/分拆）・from_symbol・to_symbol・ratio_to・ratio_from（兩個整數，不可填小數）・cost_carry（選填・僅分拆）・note（選填）',
    /* 取得成本只收「家幣金額」，不收匯率（spec F1）：匯率是平均值，平均值不可以是帳本的
       權威來源。外幣入金若不填取得成本，該筆金額仍會計入餘額、但不會進入換匯成本均價，
       畫面上會以 covered_ratio／匯損缺口揭露 — 寧可留白，也不要猜一個匯率。 */
    cash: '欄位：account・date（YYYY-MM-DD）・kind（DEPOSIT/WITHDRAW/OPENING/REBATE/INTEREST/INTEREST_EXPENSE/BROKER_FEE，也可填 入金/出金/期初/折讓款/利息/融資利息/券商費用）・ccy・amount・acq_home_amount（選填・僅外幣入金與期初，利息費用不適用；填家幣金額不是匯率）・note（選填）',
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
        renderCsvHead(kind);       // DEF-004: this kind's own columns, before the preview lands
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
    renderCsvHead(csvKind);

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
    if (paste) paste.addEventListener('input', () => {
      csvDateFormat = null;
      csvSourceName = '';          // DEF-017: hand-edited text is no longer that file
      scheduleCsvPreview();
    });
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
        csvSourceName = f.name;    // DEF-017: the batch's source label in 最近匯入
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
      csvSourceName = '';
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
    renderCsvPreview(resp, reqBody.kind);
  }

  /* ===== DEF-004 (2026-09-23): the CSV preview's columns, PER KIND =====
     The table had ONE header — # 日期 帳戶 買賣 代號 股數 價格 — written for trades and reused
     by all six kinds, so a 資金 row printed 「DEPOSIT — —」 with its 600,000 TWD nowhere, a 股利
     row lost its gross, a 換匯 row both of its amounts, an 期初 row its build date and its cost,
     and a 公司行動 row its ratio. Every one of those values was already in `rows[].data`; no
     column asked for it. Each kind now declares its own columns as [header, td class,
     cell(d)]: the cell reads ONLY that kind's own `data` keys and returns text or a node.
     Amounts are server Decimal STRINGS rendered through window.fmt — never arithmetic — and
     the date column reads the kind's OWN date key (build_date for 期初).
     tests/contract/test_def004_csv_preview_columns.py holds every key the backend emits for a
     kind to a column here (or to a named exclusion), so a new payload field cannot go dark. */
  const csvAmt = (v, ccy) => (v === undefined || v === null || v === ''
    ? f.NULL_GLYPH : (ccy ? f.money(v, ccy) : f.exact(v)));
  /* One currency leg of a conversion: 「32,000 TWD」. */
  const csvLeg = (v, ccy) => (v === undefined || v === null || v === ''
    ? f.NULL_GLYPH : csvAmt(v, ccy) + (ccy ? ' ' + ccy : ''));
  /* A row's display currency: the registered instrument's quote ccy, else the account's. */
  const csvCcy = (d) => {
    const it = inst(d.symbol || '');
    if (it && it.ccy) return it.ccy;
    const a = acc(d.account_id);
    return a ? (a.ccy || a.settlement_ccy || '') : '';
  };
  const csvFundingCcy = (d) => {
    const a = acc(d.account_id);
    return a ? (a.funding_ccy || a.settlement_ccy || a.ccy || '') : '';
  };
  /* Display-only zh labels for the two kinds whose preview row carries a bare code. Each is a
     COPY of its backend owner (shared/corporate_actions.py KIND_ZH, shared/cash_kinds.py
     CASH_KIND_ZH), and the contract test above compares them key for key, so the copy cannot
     drift; a server-supplied `kind_label` (the AI door's cash rows) still wins. */
  const CSV_CASH_KIND_ZH = {
    DEPOSIT: '入金', WITHDRAW: '出金', OPENING: '期初資金', REBATE: '折讓款',
    INTEREST: '利息', INTEREST_EXPENSE: '融資利息', BROKER_FEE: '券商費用',
  };
  /* The 買/賣 chip, plus the two flags that move money (當沖 halves the TW sell tax; a
     declared short is exempt from the 賣超 guard) — a flag the owner cannot see is one they
     cannot correct. */
  function csvSideChips(sideRaw, daytrade, shortSale) {
    const frag = document.createDocumentFragment();
    const side = String(sideRaw || '').toLowerCase();
    if (!side) return frag;
    frag.appendChild(el('span', 'dir-chip ' + (side === 'buy' ? 'dir-buy' : 'dir-sell'),
      side === 'buy' ? '買' : '賣'));
    if (String(daytrade) === '1') frag.appendChild(el('span', 'dir-chip dir-daytrade', '當沖'));
    if (String(shortSale) === '1') frag.appendChild(el('span', 'dir-chip dir-short', '放空'));
    return frag;
  }
  /* The two ratio terms side by side in the ledger's own phrasing (ledgers.py ratio_label) —
     never divided: they are stored as two integers precisely so nobody has to (§3.1(ii)). */
  const csvRatio = (from, to) => (from && to
    ? '每 ' + f.exact(from) + ' 股 → ' + f.exact(to) + ' 股' : f.NULL_GLYPH);
  const CSV_COLS = {
    transactions: [
      ['日期', 'num', (d) => f.date(d.trade_date)],
      ['帳戶', 'col-text', (d) => acctZh(d.account_id)],
      ['買賣', 'col-text', (d) => csvSideChips(d.side, d.daytrade, d.short_sale)],
      ['代號', 'col-text num', (d) => d.symbol || ''],
      ['股數', 'num', (d) => f.shares(d.quantity)],
      ['價格', 'num', (d) => f.price(d.price, csvCcy(d))],
      ['手續費', 'num', (d) => csvAmt(d.fee, csvCcy(d))],
      ['稅', 'num', (d) => csvAmt(d.tax, csvCcy(d))],
    ],
    dividends: [
      ['發放日', 'num', (d) => f.date(d.date)],
      ['帳戶', 'col-text', (d) => acctZh(d.account_id)],
      ['代號', 'col-text num', (d) => d.symbol || ''],
      ['類型', 'col-text', (d) => {
        const ty = String(d.type || '').toUpperCase();
        return AI_DIV_TYPE_ZH[ty] || ty;
      }],
      ['毛額', 'num', (d) => csvAmt(d.gross, csvCcy(d))],
      ['扣繳', 'num', (d) => csvAmt(d.withholding, csvCcy(d))],
      ['淨額', 'num', (d) => csvAmt(d.net, csvCcy(d))],
      ['再投資股數', 'num', (d) => (d.reinvest_shares ? f.shares(d.reinvest_shares) : f.NULL_GLYPH)],
      ['再投資價格', 'num', (d) => (d.reinvest_price ? f.price(d.reinvest_price, csvCcy(d)) : f.NULL_GLYPH)],
      ['除息日', 'num', (d) => (d.ex_date ? f.date(d.ex_date) : f.NULL_GLYPH)],
    ],
    fx: [
      ['日期', 'num', (d) => f.date(d.date)],
      ['帳戶', 'col-text', (d) => acctZh(d.account_id)],
      ['換匯', 'num', (d) => (d.from_amount === undefined && d.to_amount === undefined
        ? f.NULL_GLYPH
        : csvLeg(d.from_amount, d.from_ccy) + ' → ' + csvLeg(d.to_amount, d.to_ccy))],
    ],
    openings: [
      ['建檔日', 'num', (d) => f.date(d.build_date)],
      ['帳戶', 'col-text', (d) => acctZh(d.account_id)],
      ['代號', 'col-text num', (d) => d.symbol || ''],
      ['股數', 'num', (d) => f.shares(d.shares)],
      ['原始總成本', 'num', (d) => csvAmt(d.original_cost_total, csvCcy(d))],
    ],
    corporate_actions: [
      ['日期', 'num', (d) => f.date(d.date)],
      ['帳戶', 'col-text', (d) => acctZh(d.account_id)],
      /* I-14: the server's word for the kind (preview payload `kind_label`). */
      ['類型', 'col-text', (d) => d.kind_label || d.kind || ''],
      ['來源代號', 'col-text num', (d) => d.from_symbol || ''],
      ['目的代號', 'col-text num', (d) => d.to_symbol || ''],
      ['比例', 'col-text num', (d) => csvRatio(d.ratio_from, d.ratio_to)],
      ['成本分攤', 'num', (d) => (d.cost_carry !== undefined && d.cost_carry !== null && d.cost_carry !== ''
        ? f.exact(d.cost_carry) : f.NULL_GLYPH)],
      ['備註', 'col-text', (d) => d.note || ''],
    ],
    cash: [
      ['日期', 'num', (d) => f.date(d.date)],
      ['帳戶', 'col-text', (d) => acctZh(d.account_id)],
      ['類型', 'col-text', (d) => d.kind_label || CSV_CASH_KIND_ZH[d.kind] || d.kind || ''],
      ['幣別', 'col-text', (d) => d.ccy || ''],
      ['金額', 'num', (d) => csvAmt(d.amount, d.ccy)],
      ['取得成本（家幣）', 'num', (d) => csvAmt(d.acq_home_amount, csvFundingCcy(d))],
      ['備註', 'col-text', (d) => d.note || ''],
    ],
  };

  /* The header row for `kind` (called on every chip switch and every render). */
  function renderCsvHead(kind) {
    const head = $('#csv-head');
    if (!head) return;
    const tr = el('tr');
    tr.appendChild(el('th'));
    tr.appendChild(el('th', null, '#'));
    (CSV_COLS[kind] || CSV_COLS.transactions).forEach((c) => {
      tr.appendChild(el('th', c[1].indexOf('col-text') >= 0 ? 'col-text' : null, c[0]));
    });
    tr.appendChild(el('th', 'col-text', '狀態'));
    tr.appendChild(el('th', 'col-text', '原因'));
    head.replaceChildren(tr);
  }

  /* DEF-024 (2026-09-23): a row's GATING reason and its ADVISORIES (`info`, DEF-014) are
     different things — the first is why the row needs a tick or cannot be written, the second
     is a grey note that asks for nothing. The wire puts an advisory into `reason` only when
     nothing gates the row, so an advisory is recognised by membership in `info`, not by its
     position, and it is never painted as a warning. */
  function rowReasonParts(r) {
    const info = Array.isArray(r.info) ? r.info : [];
    const gating = r.reason && info.indexOf(r.reason) < 0 ? r.reason : '';
    return { gating: gating, info: info };
  }
  function appendInfoLines(td, info) {
    info.forEach((t) => td.appendChild(el('div', 'row-info', 'ⓘ ' + t)));
  }

  /* Render the REAL preview table from the server rows {n, status, reason, info, data} with
     the columns of the kind the preview was made FOR (a chip switch while a request is in
     flight must not print one kind's rows under another's header). */
  function renderCsvPreview(preview, kind) {
    $('#csv-file').textContent = csvSourceName || '貼上 CSV';
    const k = CSV_COLS[kind] ? kind : 'transactions';
    renderCsvHead(k);
    const cols = CSV_COLS[k];
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
      /* The row's own PreviewRow.index, which the commit sends back as `select` (F-03).
         These boxes existed, were pre-ticked, and were bound to nothing: unticking two of
         three rows still wrote all three, under a button labelled 「確認寫入勾選列」. */
      cb.dataset.n = String(r.n || 0);
      cb.addEventListener('change', refreshCsvConfirm);
      tdCb.appendChild(cb);
      tr.appendChild(tdCb);
      tr.appendChild(el('td', 'num', '#' + ((r.n || 0) + 1)));
      cols.forEach((c) => {
        const td = el('td', c[1]);
        const v = c[2](d);
        if (v && typeof v === 'object') td.appendChild(v);
        else td.textContent = v === undefined || v === null ? '' : String(v);
        tr.appendChild(td);
      });
      const st = ST[r.status] || ST.ok;
      tr.appendChild(el('td', 'col-text ' + st[1], st[0]));
      const why = rowReasonParts(r);
      const tdWhy = el('td', 'err-msg', why.gating);
      appendInfoLines(tdWhy, why.info);
      tr.appendChild(tdWhy);
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
    refreshCsvConfirm();
  }

  /* The ticked rows, as PreviewRow.index values — the server filters on these. Indices
     rather than a rebuilt CSV: a quoted field may contain a newline, so DictReader row n is
     not text line n+1, and this layer has no CSV parser (and must not grow one). */
  function csvSelectedRows() {
    const body = $('#csv-body');
    const boxes = body ? body.querySelectorAll('input[type=checkbox]') : [];
    const picked = [];
    Array.prototype.forEach.call(boxes, (cb) => {
      if (cb.checked && !cb.disabled) picked.push(parseInt(cb.dataset.n, 10));
    });
    return picked.filter((n) => !Number.isNaN(n));
  }

  /* Confirm enables only when something is actually ticked — mirrors refreshAiWriteBtn.
     Previously it enabled on "anything non-error exists", so a user who unticked every row
     was still invited to press a button that would write all of them. */
  function refreshCsvConfirm() {
    const btn = $('#csv-confirm');
    if (btn) btn.disabled = csvSelectedRows().length === 0;
  }

  /* Commit the pasted CSV. The backend re-derives from csv_text (re-validates vs the
     current ledger) and returns {written, skipped} as ints (safe). 422
     warnings_unacknowledged -> confirmDialog -> re-commit with ack_warnings:true. */
  async function commitCsv() {
    const paste = $('#csv-paste');
    const csvText = paste ? paste.value.trim() : '';
    if (!csvText) return;
    const commitBody = (ack) => {
      const b = { kind: csvKind, csv_text: csvText, ack_warnings: ack,
                  select: csvSelectedRows(),
                  source_name: csvSourceName || '貼上 CSV' };   // DEF-017
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
          body: '部分列有警告（如賣超）— 確認後一併寫入？',
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

  /* ===== DEF-024 (2026-09-23): what a commit response says happened =====
     `skipped` used to be ONE number for two opposite events: a row the owner left unticked
     (their choice) and a ticked row the server's re-check dropped because a finding surfaced
     that the preview never showed (the QA-01 / FIX-A1 narrowing — e.g. an unticked buy no
     longer covering the ticked sell). This page rendered both as 「跳過」 under a green
     「✓ 寫入成功」, and a commit that wrote nothing announced 「成功 0 筆・跳過 2 筆」.
     `skipped_rows[].code` now says which: `deselected` is the owner's choice (「未勾選」);
     anything else was BLOCKED and is listed row by row with the server's own sentence, next
     to the rows the importer refused outright (`rejected_rows`). A response WITHOUT
     `skipped_rows` (an older server) keeps the old meaning — every skip a deselection —
     rather than inventing a reason. Shared by the CSV, AI and one-row doors. */
  function commitOutcome(resp) {
    const r = resp || {};
    const skippedRows = Array.isArray(r.skipped_rows) ? r.skipped_rows : [];
    const blocked = skippedRows.filter((x) => x && x.code !== 'deselected');
    const skipped = r.skipped !== undefined ? r.skipped : 0;
    return {
      written: r.written !== undefined ? r.written : 0,
      deselected: Math.max(0, skipped - blocked.length),
      blocked: blocked,
      rejected: r.rejected !== undefined ? r.rejected : 0,
      rejectedRows: Array.isArray(r.rejected_rows) ? r.rejected_rows : [],
      duplicates: r.duplicates !== undefined ? r.duplicates : 0,
    };
  }
  /* Rows that did not reach the ledger although the owner wanted them to. */
  const outcomeStopped = (o) => o.rejected + o.blocked.length;
  /* 「成功 3 筆・未勾選 1 筆・被擋下 1 筆・已匯入過 2 筆」 — each part only when non-zero. */
  function outcomeText(o) {
    const stopped = outcomeStopped(o);
    return '成功 ' + o.written + ' 筆'
      + (o.deselected > 0 ? '・未勾選 ' + o.deselected + ' 筆' : '')
      + (stopped > 0 ? '・被擋下 ' + stopped + ' 筆' : '')
      + (o.duplicates > 0 ? '・已匯入過 ' + o.duplicates + ' 筆' : '');
  }
  /* One line per stopped row: 「第 3 列 2884：賣出 150 股，超過…」 (the server's sentence). */
  function outcomeLines(o) {
    const lines = o.rejectedRows.map((r) => '第 ' + r.row + ' 列：' + r.message);
    o.blocked.forEach((b) => {
      lines.push('第 ' + b.row + ' 列' + (b.symbol ? ' ' + b.symbol : '') + '：' + b.message);
    });
    return lines;
  }

  /* CSV-import success handler (C7): full success (nothing unwritten) clears the paste + resets
     the date-format select; a PARTIAL result keeps the ENTIRE raw paste (the user's data is
     never rewritten) plus the banner. Failure paths return before reaching here. The AI 寫入
     path has its OWN handler (onAiCommitted) so its banner + clear target the AI pane. */
  function onCsvWritten(resp) {
    const o = commitOutcome(resp);
    const stopped = outcomeStopped(o);
    const summary = outcomeText(o);
    const lines = outcomeLines(o);
    const banner = $('#csv-result');
    if (banner) {
      banner.hidden = false;
      banner.replaceChildren();
      banner.appendChild(el('div', null,
        (stopped > 0 ? '⚠ 寫入完成（有列被擋下）：' : '✓ 寫入完成：') + summary));
      if (o.duplicates > 0) {
        banner.appendChild(
          el('div', 'panel-sub', '重複的列來自先前的匯入批次，已自動略過，帳本沒有變成兩筆。')
        );
      }
      if (o.rejected > 0) {
        banner.appendChild(el('div', 'panel-sub',
          '被擋下的列沒有寫入帳本 —— 這不是「你沒有勾選」，是這幾列無法登錄。'
          + '修正後重新上傳整個檔案即可（已寫入的列會自動略過，不會重複）。'));
      }
      if (o.blocked.length > 0) {
        banner.appendChild(el('div', 'panel-sub',
          '有勾選的列在寫入前重新檢核時出現了預覽沒有顯示的問題（例如沒被勾選的買入不再支撐'
          + '這筆賣出），因此沒有寫入 —— 請重新預覽，確認後再寫入。'));
      }
      lines.slice(0, 20).forEach((line) => banner.appendChild(el('div', 'panel-sub', line)));
      if (lines.length > 20) {
        banner.appendChild(el('div', 'panel-sub',
          '…另有 ' + (lines.length - 20) + ' 列，內容相同的問題不再逐列列出。'));
      }
    }
    if (window.toast) {
      if (stopped > 0) {
        window.toast('⚠ 寫入完成（有列被擋下）', 'warn',
          summary + '：' + lines.slice(0, 3).join('；') + (lines.length > 3 ? '；…' : ''));
      } else {
        window.toast('寫入成功', 'ok', summary);
      }
    }
    /* FU-D45 + #10 + DEF-019: refresh always; flash + auto-switch only on FULL success,
       matching the Batch-A clear-on-success rule (a partial import keeps its paste + banner).
       ⚠ The refused and blocked rows belong in this test. Splitting them out of the old
       `skipped` would otherwise make a file with refused rows look like a clean run and CLEAR
       the paste box — taking with it the text the owner needs to fix those rows. */
    const clean = o.deselected === 0 && stopped === 0;
    refreshAfterLedgerChange(csvKind, { highlight: clean });
    if (clean) {
      /* full success -> clear the input so a second identical commit is impossible. */
      const paste = $('#csv-paste');
      if (paste) paste.value = '';
      csvDateFormat = null;
      csvSourceName = '';
      hideDateFmtChooser();
      const tbody = $('#csv-body');
      if (tbody) tbody.replaceChildren();
      $('#csv-counts').textContent = '';
      $('#csv-file').textContent = '';
      $('#csv-confirm').disabled = true;
    }
    /* partial -> keep the entire pasted text + preview + banner (never rewrite raw paste). */
  }

  /* I-8: the ONE reading of a commit response, shared with broker-import.js (the 券商匯出檔
     door on this page), so both doors split 「未勾選」 from 「被擋下」 the same way and list the
     same per-row reasons. Read at call time there — this file loads after it. */
  window.pdCommitOutcome = { read: commitOutcome, lines: outcomeLines };

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
    /* F-07: the 重試 button on the 「LLM 服務暫時無法連線」 degrade card had no listener at
       all — and it is that card's ONLY action, so the two sibling cards offered a way out
       (前往 AI 與額度設定) and this one left the user on a dead end. Same handler as 解析,
       because retrying IS parsing again. */
    const aiRetry = $('#ai-retry');
    if (aiRetry) aiRetry.addEventListener('click', runAiPreview);
    const writeAll = $('#ai-write-all');
    if (writeAll) writeAll.addEventListener('click', commitAi);
    refreshAiWriteBtn();   // no parse yet -> 寫入 starts disabled (no empty commit)
    initAiImages();   // FU-D20: dropzone click / drag-drop / clipboard-paste screenshot intake
    loadAiModels();   // FU-D20: per-run model picker (enabled models + 自動; last-used persisted)
  }

  /* W4 (AI-D17/D18/D21): one prompt now returns ONE preview + ONE commit CSV per import
     kind — transactions / dividends / cash (a real statement is mixed) — plus an
     `unparsed` confession list. aiCsvTexts[kind] holds that kind's canonical CSV;
     aiRows[kind] its rendered rows {n, status, reason, code, data} so an inline register
     can heal ONE row locally (Fable F4d) without a second vision parse. renderAiRows is
     the single writer. */
  const AI_KINDS = ['transactions', 'dividends', 'cash'];
  const AI_KIND_ZH = { transactions: '交易', dividends: '股利', cash: '資金' };
  const AI_DIV_TYPE_ZH = { CASH: '現金', STOCK: '配股', DRIP: 'DRIP', NET: '淨額' };
  let aiCsvTexts = {};
  let aiRows = { transactions: [], dividends: [], cash: [] };
  /* DEF-035 (2026-09-23): the EDITABLE drafts, row-aligned with aiRows[kind] (row n is
     aiDrafts[kind][n]); the server-owned cash-kind vocabulary for the 類型 select; and the
     per-kind edit state — typed values that do not parse (aiFieldErr), an in-flight
     re-validation (aiPending, with a sequence number so a stale answer is dropped), and a
     re-validation that failed (aiStale). A kind in any of those states is not written: its
     commit CSV no longer describes the rows on screen. */
  let aiDrafts = { transactions: [], dividends: [], cash: [] };
  let aiCashKinds = [];
  let aiFieldErr = { transactions: {}, dividends: {}, cash: {} };
  const aiPending = { transactions: false, dividends: false, cash: false };
  const aiStale = { transactions: false, dividends: false, cash: false };
  const aiRevalSeq = { transactions: 0, dividends: 0, cash: 0 };
  const aiRevalTimer = { transactions: null, dividends: null, cash: null };
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
    const before = sel.value;   // I-12: a pick made while the config loads is the owner's
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
        window.pdField.writeIfUntouched(sel, before, saved);
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
     same rule the backend uses.

     Fable F4d (Wave C): registering a symbol NO LONGER re-runs the whole paid vision parse
     (the old `await runAiPreview()` re-POSTed /api/input/ai/preview — a fresh LLM call per
     registered symbol that also discarded the user's checkbox selections). Instead the resume
     ONLY reloads the structural context and re-validates the affected row(s) LOCALLY: any row
     whose unregistered symbol now resolves against the fresh ctx heals to ✓, while every other
     row + its checkbox state is preserved. The final /api/import/commit re-validates server-side,
     so an optimistic local heal can never write a bad row. */
  function openAiQuickAdd(symbol, accId, marketHint) {
    if (!window.pdInstQuickAdd) {
      if (window.toast) window.toast('對話框載入失敗，請重新整理', 'fail');
      return;
    }
    const resume = async () => { await reloadContext(); healAiUnregisteredFromContext(); };
    /* FU-D42a: the symbol stays EDITABLE (no lockSymbol) — an AI mis-parse (e.g. a US-style
       ticker on a TW account) is corrected right in the dialog; editing re-runs the lookup.
       Batch B (F15): on a merged account the dialog's market defaults to the AI row's
       suggested `market` when present (else the first bound market — the user can change it). */
    window.pdInstQuickAdd({
      symbol: symbol,
      market: accountMarket(accId, marketHint),
      onConfirm: resume,
      onBuy: resume,
    });
  }

  /* Fable F4d: after a register, re-validate the parsed AI rows against the FRESH ctx WITHOUT a
     second vision parse. Captures the current checkbox states, flips any row whose formerly
     unregistered symbol now resolves (inst()) to a clean ✓ (auto-checked), and re-renders while
     preserving every OTHER row's checkbox state. A row whose symbol still does not resolve
     (e.g. the user registered a DIFFERENT symbol) honestly stays in error. */
  function healAiUnregisteredFromContext() {
    let healedAny = false;
    AI_KINDS.forEach((kind) => {
      const rows = aiRows[kind] || [];
      if (!rows.length) return;
      const prevChecked = {};
      const body = $('#ai-body-' + kind);
      const boxes = body ? body.querySelectorAll('input[type=checkbox]') : [];
      Array.prototype.forEach.call(boxes, (cb) => { prevChecked[cb.dataset.n] = cb.checked; });
      let healed = false;
      rows.forEach((r) => {
        if (r.code === 'unregistered_symbol' && inst((r.data && r.data.symbol) || '')) {
          r.status = 'ok';
          r.code = null;
          r.reason = null;
          prevChecked[String(r.n)] = true;   // newly valid -> auto-check
          healed = true;
        }
      });
      if (healed) {
        renderAiRows(kind, rows, prevChecked);
        healedAny = true;
        /* DEF-035: with the drafts in hand the server can confirm the local heal (fee/tax
           under the now-registered instrument's own rule) — no model call, so it is free. */
        if ((aiDrafts[kind] || []).length) scheduleAiRevalidate(kind);
      }
    });
    if (healedAny) refreshAiWriteBtn();
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

  /* Render the AI preview sections + meta (W4, AI-D21). The wire shape is
     {previews: {kind: {rows, summary}}, csv_texts: {kind: csv}, unparsed: [...], meta} —
     one section per kind, an absent kind hidden. cost_usd is a Decimal STRING -> f.num
     (never .toFixed). The per-row money in `data` is Decimal STRINGS -> fmt, same as CSV. */
  function renderAiPreview(preview) {
    $('#ai-degrade-off').hidden = true;
    $('#ai-degrade-quota').hidden = true;
    $('#ai-degrade-down').hidden = true;
    $('#ai-normal').hidden = false;
    clearAiBanner();                    // a fresh parse retires any prior success banner
    aiCsvTexts = preview.csv_texts || {};
    /* DEF-035: a fresh parse replaces every draft and retires any edit still in flight. */
    const drafts = preview.drafts || {};
    AI_KINDS.forEach((kind) => {
      aiDrafts[kind] = Array.isArray(drafts[kind]) ? drafts[kind] : [];
      aiRevalSeq[kind] += 1;
      aiPending[kind] = false;
      aiStale[kind] = false;
      if (aiRevalTimer[kind]) { clearTimeout(aiRevalTimer[kind]); aiRevalTimer[kind] = null; }
    });
    aiFieldErr = { transactions: {}, dividends: {}, cash: {} };
    aiCashKinds = Array.isArray(preview.cash_kinds) ? preview.cash_kinds : [];
    const meta = preview.meta || {};
    if ($('#ai-source')) {
      const cost = meta.cost_usd !== undefined && meta.cost_usd !== null
        ? '・成本 $' + f.num(meta.cost_usd, 4) : '';
      $('#ai-source').textContent = (meta.via || 'litellm') + cost;
    }
    if ($('#ai-model')) $('#ai-model').textContent = meta.model || '';
    /* AI-D17: the confessed unparsed rows (換匯／公司行動／選擇權…) are surfaced verbatim —
       the whole point is that they are SEEN, not silently dropped. */
    const unparsed = preview.unparsed || [];
    const ub = $('#ai-unparsed');
    if (ub) {
      ub.replaceChildren();
      if (unparsed.length) {
        ub.hidden = false;
        ub.appendChild(el('div', null,
          '有 ' + unparsed.length + ' 列無法歸類（不會寫入；換匯／公司行動請改用對應表單或 CSV）：'));
        unparsed.forEach((u) => {
          ub.appendChild(el('div', 'hint',
            '・' + (u.text || '') + (u.reason ? ' — ' + u.reason : '')));
        });
      } else {
        ub.hidden = true;
      }
    }
    let total = 0;
    AI_KINDS.forEach((kind) => {
      const pv = (preview.previews || {})[kind];
      const rows = pv ? (pv.rows || []) : [];
      total += rows.length;
      const sec = $('#ai-sec-' + kind);
      if (sec) sec.hidden = rows.length === 0;
      renderAiRows(kind, rows);
    });
    refreshAiWriteBtn();
    if (window.toast) window.toast('解析完成', 'ok', '共 ' + total + ' 筆草稿');
  }

  /* ===== DEF-035 (2026-09-23): the AI draft table is EDITABLE, per row =====
     Every cell used to be text (and the 帳戶 cell the raw id, 「tw_broker」, beside a CSV
     preview reading 「台灣券商」), so a mis-read share count could only be unticked and typed
     again in the manual form — the functional manual's F-04 ① asks for 「解析成結構化列並可
     逐列編輯」. The server now answers `drafts`, row-aligned with each kind's preview rows; an
     edit changes that draft's field and sends the kind's drafts back through the SAME door
     (`POST /api/input/ai/preview` with `drafts` — agents.revalidate_ai_drafts, no model call).
     The server re-runs the whole post-parse pipeline — fee/tax, the DEF-036 amount check, the
     AI-D21 cash label — and regenerates the commit CSV, so the row that gets written is the
     row on screen, and this file still never assembles or parses a CSV line.

     A typed value is never rewritten (DEF-005): a number that does not parse keeps its text,
     the box is flagged, the row says why, and the kind's re-validation — and every write —
     waits until it is fixed. Computed columns (費用／稅) stay read-only: they are the fee
     engine's answer to the edited shares and price, not an input; overriding a fee is the
     manual form's pencil. A response WITHOUT `drafts` (an older server) renders the old
     read-only row — there is nothing to send back. */
  const AI_NUMERIC = {
    transactions: { shares: ['股數', true], price: ['價格', true] },
    dividends: {
      gross: ['毛額', true], withholding: ['扣繳', false], net: ['淨額', false],
      reinvest_shares: ['再投資股數', false], reinvest_price: ['再投資價格', false],
    },
    cash: { amount: ['金額', true], acq_home_amount: ['取得成本', false] },
  };
  const AI_PLAIN_NUMBER = /^\d+(\.\d+)?$/;
  const AI_REQUIRED_TEXT = { date: '日期', symbol: '代號', account_id: '帳戶' };
  const AI_CCYS = ['TWD', 'USD', 'MYR'];
  const aiDraftOf = (kind, r) => {
    const list = aiDrafts[kind] || [];
    return list[r.n] || null;
  };
  const aiIsMismatch = (r) => !!(r && r.data && String(r.data.amount_mismatch) === '1');
  /* The first unresolved typed-value problem on row n of `kind`, or ''. */
  const aiRowErr = (kind, n) => {
    const errs = (aiFieldErr[kind] || {})[n];
    if (!errs) return '';
    const keys = Object.keys(errs);
    return keys.length ? errs[keys[0]] : '';
  };
  const aiKindHasFieldErr = (kind) => Object.keys(aiFieldErr[kind] || {}).length > 0;

  /* The checkbox cell: dataset.n is the 0-based draft index IN ITS KIND — commitAi rebuilds
     each kind's committed csv from ONLY its checked rows (csv data line n+1), so an
     unchecked row is never written (C7, per kind). `prevChecked` (Fable F4d) preserves the
     state across a LOCAL re-render (an inline register or an edit re-validation).
     DEF-036: a row whose own text contradicts its shares × price is never PRE-ticked — the
     owner ticks it after reading why, and commitAi asks once more before writing it. */
  function aiCheckboxCell(kind, r, prevChecked) {
    const td = el('td');
    const cb = el('input'); cb.type = 'checkbox';
    const localErr = !!aiRowErr(kind, r.n);
    cb.disabled = r.status === 'error' || localErr;
    const wanted = prevChecked && (String(r.n) in prevChecked)
      ? !!prevChecked[String(r.n)] : (r.status !== 'error' && !aiIsMismatch(r));
    cb.checked = cb.disabled ? false : wanted;
    /* A typo in one box disables the tick for as long as it stands; the owner's own choice
       is remembered on the box and comes back when the value is fixed. */
    if (localErr && r.status !== 'error') cb.dataset.wanted = wanted ? '1' : '0';
    cb.dataset.n = String(r.n || 0);
    cb.addEventListener('change', refreshAiWriteBtn);
    td.appendChild(cb);
    return td;
  }

  function aiSymbolCell(symbol, it) {
    const td = el('td', 'col-text');
    const cell = el('div', 'sym-cell');
    cell.appendChild(el('span', 'sym-code', symbol || ''));
    cell.appendChild(el('span', 'sym-name', it ? it.name : ''));
    td.appendChild(cell);
    return td;
  }

  function aiStatusCell(kind, r) {
    const td = el('td', 'err-msg');
    const localErr = aiRowErr(kind, r.n);
    const why = rowReasonParts(r);
    if (localErr) td.appendChild(el('span', 'st-error', '✕ ' + localErr));
    else if (r.status === 'error') td.appendChild(el('span', 'st-error', '✕ ' + (why.gating || r.reason || '無法寫入')));
    else if (why.gating) td.appendChild(el('span', 'st-warn', '⚠ ' + why.gating));
    else td.appendChild(el('span', 'st-ok', '✓ 解析完整'));
    /* DEF-036: the contradiction must stay visible even when another finding leads the row. */
    if (!localErr && aiIsMismatch(r) && why.gating.indexOf('金額矛盾') !== 0) {
      td.appendChild(el('div', 'st-warn', '⚠ 金額矛盾：文字寫成交金額 '
        + f.exact(r.data.stated_amount) + '，與股數 × 價格不符'));
    }
    appendInfoLines(td, why.info);
    return td;
  }

  /* FU-D33: an unregistered-symbol row gets an inline 立即註冊 action opening the SHARED
     quick-add dialog (symbol prefilled + market inferred from the row's account). On success
     the rows heal LOCALLY (Fable F4d) — no second paid parse. */
  function aiActionCell(r) {
    const td = el('td');
    const symbol = (r.data && r.data.symbol) || '';
    if (r.code === 'unregistered_symbol' && symbol) {
      const reg = el('button', 'btn', '立即註冊'); reg.type = 'button';
      reg.title = '註冊此標的後自動重新解析';
      reg.addEventListener('click',
        () => openAiQuickAdd(symbol, r.data.account_id, r.data.market));
      td.appendChild(reg);
    }
    return td;
  }

  /* ---- DEF-035 editors: every control writes ONE draft field through onAiEdit ---- */
  const tdOf = (node, cls) => { const td = el('td', cls || null); td.appendChild(node); return td; };
  function aiEditInput(kind, n, field, value, opts) {
    const o = opts || {};
    const inp = el('input', 'ai-edit' + (o.num ? ' num' : '') + (o.cls ? ' ' + o.cls : ''));
    inp.type = o.type || 'text';
    if (o.num) inp.inputMode = 'decimal';
    inp.value = value === null || value === undefined ? '' : String(value);
    if (o.placeholder) inp.placeholder = o.placeholder;
    inp.dataset.field = field;
    inp.setAttribute('aria-label', o.label || field);
    const errs = (aiFieldErr[kind] || {})[n];
    if (errs && errs[field]) { inp.classList.add('invalid'); inp.title = errs[field]; }
    inp.addEventListener('change', () => onAiEdit(kind, n, field, inp.value));
    return inp;
  }
  function aiEditSelect(kind, n, field, options, value, label) {
    const sel = el('select', 'ai-edit');
    options.forEach((op) => { const o = el('option', null, op[1]); o.value = op[0]; sel.appendChild(o); });
    /* A value outside the list (an id the model invented, a kind spelled in zh) is SHOWN, never
       silently swapped for the first option — that would be an edit nobody made. */
    if (value && !options.some((op) => op[0] === value)) {
      const o = el('option', null, value + '（不在清單中）'); o.value = value; sel.appendChild(o);
    }
    sel.value = value || '';
    sel.dataset.field = field;
    sel.setAttribute('aria-label', label);
    sel.addEventListener('change', () => onAiEdit(kind, n, field, sel.value));
    return sel;
  }
  function aiAccountSelect(kind, n, accountId) {
    const opts = ctx.accounts.map((a) => [a.id,
      window.pdNames ? window.pdNames.accountOption(a) : acctZh(a.id)]);
    return aiEditSelect(kind, n, 'account_id', opts, accountId, '帳戶');
  }
  function aiSymbolEditCell(kind, r, dr) {
    const td = el('td', 'col-text');
    const cell = el('div', 'sym-cell');
    cell.appendChild(aiEditInput(kind, r.n, 'symbol', dr.symbol, { cls: 'ai-edit-sym', label: '代號' }));
    const it = inst((r.data && r.data.symbol) || dr.symbol);
    cell.appendChild(el('span', 'sym-name', it ? it.name : ''));
    td.appendChild(cell);
    return td;
  }
  /* The server's computed value as the placeholder of an OPTIONAL box left blank (扣繳 auto-
     computed from the account's model, for instance) — shown, not written back. */
  const aiAutoHint = (v, ccy) => (v === undefined || v === null || v === ''
    ? '' : '自動 ' + csvAmt(v, ccy));

  function aiTxnCells(tr, r) {
    const d = r.data || {};
    const dr = aiDraftOf('transactions', r);
    const symbol = d.symbol || (dr && dr.symbol) || '';
    const it = inst(symbol);
    const ccy = it ? it.ccy : '';
    if (dr) {
      tr.appendChild(tdOf(aiAccountSelect('transactions', r.n, dr.account_id), 'col-text'));
      tr.appendChild(tdOf(aiEditInput('transactions', r.n, 'date', dr.date,
        { type: 'date', cls: 'ai-edit-date', label: '日期' }), 'col-text'));
    } else {
      tr.appendChild(el('td', 'col-text', acctZh(d.account_id)));
      tr.appendChild(el('td', 'col-text', f.date(d.trade_date || d.date)));
    }
    const side = String((dr ? dr.side : d.side) || '').toLowerCase();
    const tdSide = el('td', 'col-text');
    if (dr) {
      tdSide.appendChild(aiEditSelect('transactions', r.n, 'side',
        [['BUY', '買'], ['SELL', '賣']], String(dr.side || '').toUpperCase(), '買賣'));
    } else {
      tdSide.appendChild(el('span', 'dir-chip ' + (side === 'buy' ? 'dir-buy' : 'dir-sell'),
        side === 'buy' ? '買' : '賣'));
    }
    /* The model may mark a row 當沖, and that HALVES the TW sell tax (0.3% -> 0.15%). It
       used to be dropped before the write, so it was invisible and harmless; now it reaches
       the ledger, so it has to be visible — a flag the user cannot see is a flag the user
       cannot correct, and this one moves money. */
    if (String(d.daytrade) === '1') {
      const dt = el('span', 'dir-chip dir-daytrade', '當沖');
      dt.title = '證交稅以當沖稅率計（0.15%，非現股 0.3%）——若判讀有誤請取消勾選後改用手動輸入';
      tdSide.appendChild(dt);
    }
    /* W4 (AI-D19): a DECLARED short books a negative position whose basis is the proceeds —
       same visibility rule as 當沖, same reason: it moves money. */
    if (String(d.short_sale) === '1') {
      const ss = el('span', 'dir-chip dir-short', '放空');
      ss.title = '已申報放空——此賣出不被賣超擋下；若判讀有誤請取消勾選後改用手動輸入';
      tdSide.appendChild(ss);
    }
    tr.appendChild(tdSide);
    if (dr) {
      tr.appendChild(aiSymbolEditCell('transactions', r, dr));
      tr.appendChild(tdOf(aiEditInput('transactions', r.n, 'shares', dr.shares,
        { num: true, label: '股數' }), 'num'));
      tr.appendChild(tdOf(aiEditInput('transactions', r.n, 'price', dr.price,
        { num: true, label: '價格' }), 'num'));
    } else {
      tr.appendChild(aiSymbolCell(symbol, it));
      tr.appendChild(el('td', 'num', f.shares(d.quantity !== undefined ? d.quantity : d.shares)));
      tr.appendChild(el('td', 'num', f.price(d.price, ccy)));        // Decimal string -> fmt
    }
    tr.appendChild(el('td', 'num', d.fee !== undefined ? f.money(d.fee, ccy) : f.NULL_GLYPH));
    tr.appendChild(el('td', 'num', d.tax !== undefined ? f.money(d.tax, ccy) : f.NULL_GLYPH));
  }

  function aiDivCells(tr, r) {
    const d = r.data || {};
    const dr = aiDraftOf('dividends', r);
    const symbol = d.symbol || (dr && dr.symbol) || '';
    const it = inst(symbol);
    const ccy = it ? it.ccy : '';
    if (!dr) {
      tr.appendChild(el('td', 'col-text', acctZh(d.account_id)));
      tr.appendChild(el('td', 'col-text', f.date(d.date)));
      tr.appendChild(aiSymbolCell(symbol, it));
      const ty = (d.type || '').toString().toUpperCase();
      tr.appendChild(el('td', 'col-text', AI_DIV_TYPE_ZH[ty] || ty));
      tr.appendChild(el('td', 'num', d.gross !== undefined ? f.money(d.gross, ccy) : f.NULL_GLYPH));
      tr.appendChild(el('td', 'num', d.withholding ? f.money(d.withholding, ccy) : f.NULL_GLYPH));
      tr.appendChild(el('td', 'num', d.net ? f.money(d.net, ccy) : f.NULL_GLYPH));
      tr.appendChild(el('td', 'num', d.reinvest_shares ? f.shares(d.reinvest_shares) : f.NULL_GLYPH));
      tr.appendChild(el('td', 'num', d.reinvest_price ? f.price(d.reinvest_price, ccy) : f.NULL_GLYPH));
      return;
    }
    const k = 'dividends';
    tr.appendChild(tdOf(aiAccountSelect(k, r.n, dr.account_id), 'col-text'));
    tr.appendChild(tdOf(aiEditInput(k, r.n, 'date', dr.date,
      { type: 'date', cls: 'ai-edit-date', label: '發放日' }), 'col-text'));
    tr.appendChild(aiSymbolEditCell(k, r, dr));
    tr.appendChild(tdOf(aiEditSelect(k, r.n, 'type',
      Object.keys(AI_DIV_TYPE_ZH).map((t) => [t, AI_DIV_TYPE_ZH[t]]),
      String(dr.type || '').toUpperCase(), '類型'), 'col-text'));
    tr.appendChild(tdOf(aiEditInput(k, r.n, 'gross', dr.gross, { num: true, label: '毛額' }), 'num'));
    tr.appendChild(tdOf(aiEditInput(k, r.n, 'withholding', dr.withholding,
      { num: true, label: '扣繳', placeholder: aiAutoHint(d.withholding, ccy) }), 'num'));
    tr.appendChild(tdOf(aiEditInput(k, r.n, 'net', dr.net,
      { num: true, label: '淨額', placeholder: aiAutoHint(d.net, ccy) }), 'num'));
    tr.appendChild(tdOf(aiEditInput(k, r.n, 'reinvest_shares', dr.reinvest_shares,
      { num: true, label: '再投資股數', placeholder: d.reinvest_shares ? '自動 ' + f.shares(d.reinvest_shares) : '' }), 'num'));
    tr.appendChild(tdOf(aiEditInput(k, r.n, 'reinvest_price', dr.reinvest_price,
      { num: true, label: '再投資價格' }), 'num'));
  }

  function aiCashCells(tr, r) {
    const d = r.data || {};
    const dr = aiDraftOf('cash', r);
    const acct = acc((dr && dr.account_id) || d.account_id);
    const fundCcy = acct ? (acct.funding_ccy || acct.settlement_ccy || acct.ccy) : undefined;
    if (dr) {
      tr.appendChild(tdOf(aiAccountSelect('cash', r.n, dr.account_id), 'col-text'));
      tr.appendChild(tdOf(aiEditInput('cash', r.n, 'date', dr.date,
        { type: 'date', cls: 'ai-edit-date', label: '日期' }), 'col-text'));
    } else {
      tr.appendChild(el('td', 'col-text', acctZh(d.account_id)));
      tr.appendChild(el('td', 'col-text', f.date(d.date)));
    }
    /* AI-D21: the direction lives in the KIND (amounts are unsigned) — render the
       server-owned zh label (kind_label) AND an explicit sign, so a mislabelled kind
       (券商費用 read as 入金) is visible before it moves the pool by twice the amount.
       Editable, the options carry the same label AND sign, from the server's vocabulary. */
    const label = d.kind_label || d.kind || '';
    const debit = String(d.sign) === '-1';
    const tdKind = el('td', 'col-text');
    if (dr && aiCashKinds.length) {
      tdKind.appendChild(aiEditSelect('cash', r.n, 'cash_kind',
        aiCashKinds.map((v) => [v.kind, (String(v.sign) === '-1' ? '− ' : '＋ ') + v.label]),
        d.kind || String(dr.cash_kind || ''), '類型'));
    } else {
      const chip = el('span', 'dir-chip ' + (debit ? 'dir-sell' : 'dir-buy'),
        (debit ? '− ' : '＋ ') + label);
      chip.title = debit ? '資金流出（金額以無號存入，方向由此類型決定）'
                         : '資金流入（金額以無號存入，方向由此類型決定）';
      tdKind.appendChild(chip);
    }
    tr.appendChild(tdKind);
    if (dr) {
      tr.appendChild(tdOf(aiEditSelect('cash', r.n, 'ccy', AI_CCYS.map((c) => [c, c]),
        String(dr.ccy || ''), '幣別'), 'col-text'));
      tr.appendChild(tdOf(aiEditInput('cash', r.n, 'amount', dr.amount,
        { num: true, label: '金額' }), 'num'));
      tr.appendChild(tdOf(aiEditInput('cash', r.n, 'acq_home_amount', dr.acq_home_amount,
        { num: true, label: '取得成本（家幣）' }), 'num'));
      return;
    }
    tr.appendChild(el('td', 'col-text', d.ccy || ''));
    tr.appendChild(el('td', 'num', d.amount !== undefined
      ? (debit ? '−' : '＋') + f.money(d.amount, d.ccy) : f.NULL_GLYPH));
    tr.appendChild(el('td', 'num',
      d.acq_home_amount ? f.money(d.acq_home_amount, fundCcy) : f.NULL_GLYPH));
  }

  /* The checkbox state of `kind`'s rendered rows, keyed by draft index. */
  function aiCapturedChecks(kind) {
    const out = {};
    const body = $('#ai-body-' + kind);
    const boxes = body ? body.querySelectorAll('input[type=checkbox]') : [];
    Array.prototype.forEach.call(boxes, (cb) => {
      out[cb.dataset.n] = cb.disabled && cb.dataset.wanted !== undefined
        ? cb.dataset.wanted === '1' : cb.checked;
    });
    return out;
  }

  /* Render one kind's preview rows {n, status, reason, info, code, data} into its section
     tbody. An editor the owner is typing in survives the re-render (a re-validation of a
     sibling row can land mid-keystroke): its row + field are found again and refocused with
     the text as it stood. */
  function renderAiRows(kind, rows, prevChecked) {
    aiRows[kind] = rows || [];
    const tbody = $('#ai-body-' + kind);
    if (!tbody) return;
    const active = document.activeElement;
    let keep = null;
    if (active && tbody.contains(active) && active.dataset && active.dataset.field) {
      const tr0 = active.closest('tr');
      keep = { n: tr0 ? tr0.dataset.n : null, field: active.dataset.field, value: active.value };
    }
    tbody.replaceChildren();
    (rows || []).forEach((r) => {
      const tr = el('tr');
      tr.dataset.n = String(r.n || 0);
      tr.appendChild(aiCheckboxCell(kind, r, prevChecked));
      if (kind === 'transactions') aiTxnCells(tr, r);
      else if (kind === 'dividends') aiDivCells(tr, r);
      else aiCashCells(tr, r);
      tr.appendChild(aiStatusCell(kind, r));
      tr.appendChild(aiActionCell(r));
      tbody.appendChild(tr);
    });
    if (keep && keep.n !== null) {
      const again = tbody.querySelector('tr[data-n="' + keep.n + '"] [data-field="' + keep.field + '"]');
      if (again) { again.value = keep.value; again.focus(); }
    }
  }

  /* One edited field. The typed text is kept exactly as typed (DEF-005); a value that cannot
     be a quantity or an amount flags its box and holds the whole kind back — its CSV no
     longer describes the row on screen — until it is fixed. Otherwise the kind goes back to
     the server for a re-validation (debounced: tabbing through three boxes is one request). */
  function onAiEdit(kind, n, field, raw) {
    const d = (aiDrafts[kind] || [])[n];
    if (!d) return;
    const v = String(raw === null || raw === undefined ? '' : raw).trim();
    const spec = (AI_NUMERIC[kind] || {})[field];
    let err = '';
    if (spec) {
      if (v === '') {
        if (spec[1]) err = spec[0] + '為必填';
      } else if (!AI_PLAIN_NUMBER.test(v)) {
        err = spec[0] + '「' + v + '」不是有效的數字——請只填數字與小數點，不要千分位逗號或正負號';
      }
    } else if (AI_REQUIRED_TEXT[field] && v === '') {
      err = AI_REQUIRED_TEXT[field] + '為必填';
    }
    d[field] = (spec && !spec[1] && v === '') ? null : v;
    const errs = aiFieldErr[kind][n] || {};
    if (err) errs[field] = err; else delete errs[field];
    if (Object.keys(errs).length) aiFieldErr[kind][n] = errs; else delete aiFieldErr[kind][n];
    if (aiKindHasFieldErr(kind)) {
      if (aiRevalTimer[kind]) { clearTimeout(aiRevalTimer[kind]); aiRevalTimer[kind] = null; }
      aiRevalSeq[kind] += 1;            // an answer for the previous values is now stale
      aiPending[kind] = false;
      renderAiRows(kind, aiRows[kind], aiCapturedChecks(kind));
      refreshAiWriteBtn();
      return;
    }
    scheduleAiRevalidate(kind);
  }

  function scheduleAiRevalidate(kind) {
    aiPending[kind] = true;
    refreshAiWriteBtn();
    if (aiRevalTimer[kind]) clearTimeout(aiRevalTimer[kind]);
    aiRevalTimer[kind] = setTimeout(() => {
      aiRevalTimer[kind] = null;
      revalidateAiKind(kind);
    }, 250);
  }

  /* Send `kind`'s drafts back through the AI door (no model call) and re-render from the
     answer. `fresh` = the row set itself changed (a partial commit kept only the unwritten
     rows), so the old checkbox states do not map onto it. */
  async function revalidateAiKind(kind, fresh) {
    const seq = ++aiRevalSeq[kind];
    aiPending[kind] = true;
    refreshAiWriteBtn();
    let resp;
    try {
      resp = await api.post('/api/input/ai/preview', { drafts: { rows: aiDrafts[kind] || [] } });
    } catch (err) {
      if (seq !== aiRevalSeq[kind]) return;
      aiPending[kind] = false;
      aiStale[kind] = true;             // the CSV on hand no longer matches the drafts
      refreshAiWriteBtn();
      if (window.toast) {
        window.toast((err && err.message) || '重新檢核失敗', 'fail',
          '修正後再改一次欄位即可重新檢核；在那之前這一類不會寫入');
      }
      return;
    }
    if (seq !== aiRevalSeq[kind]) return;   // a newer edit superseded this answer
    aiPending[kind] = false;
    aiStale[kind] = false;
    const pv = (resp.previews || {})[kind];
    const rows = pv ? (pv.rows || []) : [];
    const before = aiRows[kind] || [];
    const prev = fresh ? null : aiCapturedChecks(kind);
    if (prev) {
      rows.forEach((r) => {
        const old = before[r.n];
        if (!old) return;
        const key = String(r.n);
        /* newly valid -> ticked; newly contradicted (DEF-036) -> unticked */
        if (old.status === 'error' && r.status !== 'error' && !aiIsMismatch(r)) prev[key] = true;
        if (aiIsMismatch(r) && !aiIsMismatch(old)) prev[key] = false;
      });
    }
    aiCsvTexts[kind] = ((resp.csv_texts || {})[kind]) || '';
    const back = (resp.drafts || {})[kind];
    if (Array.isArray(back)) aiDrafts[kind] = back;
    if (Array.isArray(resp.cash_kinds) && resp.cash_kinds.length) aiCashKinds = resp.cash_kinds;
    const sec = $('#ai-sec-' + kind);
    if (sec) sec.hidden = rows.length === 0;
    renderAiRows(kind, rows, prev);
    refreshAiWriteBtn();
  }

  /* Rebuild ONE KIND's committed csv from ONLY its checked rows (header + their source
     lines). Each kind's AI csv is one-line-per-draft in draft order, and each checkbox's
     dataset.n is that draft's 0-based index IN THE KIND, so checked row n -> csv data line
     n+1 (C7, per kind). Returns {count, text}; count 0 means nothing is selected. */
  function aiCheckedCsv(kind) {
    const lines = (aiCsvTexts[kind] || '').split('\n');
    const header = lines[0] || '';
    const picked = [];
    const body = $('#ai-body-' + kind);
    const boxes = body ? body.querySelectorAll('input[type=checkbox]') : [];
    Array.prototype.forEach.call(boxes, (cb) => {
      if (!cb.checked || cb.disabled) return;
      const n = parseInt(cb.dataset.n, 10);
      if (!Number.isNaN(n) && lines[n + 1] !== undefined) picked.push({ n: n, line: lines[n + 1] });
    });
    picked.sort((a, b) => a.n - b.n);
    const text = picked.length
      ? header + '\n' + picked.map((p) => p.line).join('\n') + '\n'
      : '';
    /* `ns`: the draft index of each committed line, in order — a commit response's 1-based
       `row` is a position in THIS text, and ns maps it back to the draft it came from. */
    return { count: picked.length, text: text, ns: picked.map((p) => p.n) };
  }

  /* DEF-035: a kind whose drafts were edited and not yet re-validated — a box that does not
     parse, a request in flight, or one that failed — has a commit CSV that no longer
     describes the rows on screen, so NOTHING is written until it settles. */
  const aiKindBlocked = (kind) => !!(aiPending[kind] || aiStale[kind] || aiKindHasFieldErr(kind));

  /* 寫入 is disabled unless at least one kind has a parsed csv AND a checked row (no empty
     commit, no double-submit after a full-success clear), and no kind is mid-edit. */
  function refreshAiWriteBtn() {
    const btn = $('#ai-write-all');
    if (!btn) return;
    const blocked = AI_KINDS.some(aiKindBlocked);
    btn.disabled = blocked || !AI_KINDS.some(
      (kind) => aiCsvTexts[kind] && aiCheckedCsv(kind).count > 0);
    btn.title = blocked ? '草稿有欄位待修正或正在重新檢核，完成後才能寫入' : '';
  }

  function aiBanner(text) {
    const b = $('#ai-result');
    if (!b) return;
    b.hidden = false;
    b.replaceChildren();
    b.appendChild(el('div', null, text));
  }
  function clearAiBanner() {
    const b = $('#ai-result');
    if (b) { b.hidden = true; b.replaceChildren(); }
  }

  /* Full-success reset (C7): wipe the pasted text, the parsed csvs, attached screenshots, and
     every section table so a second identical commit is impossible. The banner is left to the
     caller (it shows the success summary). */
  function clearAiInputs() {
    const t = $('#ai-text'); if (t) t.value = '';
    aiCsvTexts = {};
    AI_KINDS.forEach((kind) => {
      aiDrafts[kind] = [];
      aiRevalSeq[kind] += 1;
      aiPending[kind] = false;
      aiStale[kind] = false;
    });
    aiFieldErr = { transactions: {}, dividends: {}, cash: {} };
    aiImages = [];
    renderAiThumbs();
    AI_KINDS.forEach((kind) => {
      aiRows[kind] = [];
      const tb = $('#ai-body-' + kind); if (tb) tb.replaceChildren();
      const sec = $('#ai-sec-' + kind); if (sec) sec.hidden = true;
    });
    const ub = $('#ai-unparsed'); if (ub) { ub.hidden = true; ub.replaceChildren(); }
    if ($('#ai-source')) $('#ai-source').textContent = '';
    if ($('#ai-model')) $('#ai-model').textContent = '';
    refreshAiWriteBtn();
  }

  /* Write the AI-parsed drafts — ONLY the CHECKED rows, PER KIND through the three existing
     doors (W4, AI-D18: no new endpoint; each kind's commit is its own all-or-nothing batch,
     so undo stays per-kind granular). Kinds that trip 422 warnings-unacknowledged are
     collected and retried together after ONE confirm dialog; kinds that committed clean
     finish immediately — partial progress is reported, never silently skipped.

     DEF-036: a ticked transaction whose own text states a total that contradicts its
     shares × price is asked about ONCE MORE before anything is written. The flag is
     preview-only (the stated total is evidence, not a ledger column, so the commit door
     cannot see it) — which is exactly why the acknowledgement has to happen here. */
  function commitAi() {
    if (AI_KINDS.some(aiKindBlocked)) {
      if (window.toast) window.toast('草稿尚未檢核完成', 'fail', '有欄位待修正或正在重新檢核，完成後再寫入');
      return;
    }
    const plan = AI_KINDS
      .map((kind) => ({ kind: kind, picked: aiCheckedCsv(kind) }))
      .filter((p) => aiCsvTexts[p.kind] && p.picked.count > 0);
    if (!plan.length) {
      if (window.toast) window.toast('請先解析並勾選至少一列', 'fail');
      return;
    }
    const contradicted = [];
    plan.forEach((p) => {
      if (p.kind !== 'transactions') return;
      p.picked.ns.forEach((n) => {
        const r = (aiRows.transactions || []).find((x) => x.n === n);
        if (aiIsMismatch(r)) {
          contradicted.push((r.data.symbol || '') + ' ' + f.shares(r.data.quantity) + ' 股 × '
            + f.exact(r.data.price) + '，文字寫成交金額 ' + f.exact(r.data.stated_amount));
        }
      });
    });
    if (!contradicted.length) { runAiCommit(plan); return; }
    window.confirmDialog({
      title: '金額矛盾確認',
      body: '以下勾選的交易，文字所寫的成交金額與股數 × 價格不符：' + contradicted.join('；')
        + '。系統不會替你選擇哪一個數字正確——請確認股數與價格無誤後再寫入。',
      confirmLabel: '股數價格無誤，仍要寫入',
      onConfirm: () => { runAiCommit(plan); },
    });
  }

  /* DEF-017: every AI batch says where it came from in 最近匯入. */
  const aiCommitBody = (p, ack) => ({ kind: p.kind, csv_text: p.picked.text,
    ack_warnings: ack, source_name: 'AI 輸入' });

  async function runAiCommit(plan) {
    const restore = window.pdBusy ? window.pdBusy($('#ai-write-all'), '寫入中…') : () => {};
    const done = [];
    const needAck = [];
    let failed = null;
    for (const p of plan) {
      try {
        const resp = await api.post('/api/import/commit', aiCommitBody(p, false));
        done.push({ kind: p.kind, resp: resp, text: p.picked.text, ns: p.picked.ns });
      } catch (err) {
        if (err && err.status === 422 && err.code === 'warnings_unacknowledged') {
          needAck.push(p);
          continue;
        }
        failed = err;
        break;
      }
    }
    restore();
    /* A mid-loop hard failure must still SETTLE the kinds that already wrote (section
       cleanup + ledger refresh + counts) — their rows are in the ledger, so leaving them
       on screen looking uncommitted invites a retry that re-posts them (the server's
       content-hash dedupe absorbs the double write, but the UI would be lying). The
       failed + never-attempted kinds keep their rows + csv, retry-able. */
    if (done.length) await finishAiCommits(done);
    if (failed) {
      if (window.toast) {
        window.toast((failed && failed.message) || '寫入失敗', 'fail', failed && failed.code);
      }
      return;
    }
    if (!needAck.length) return;
    window.confirmDialog({
      title: '匯入警告確認',
      body: 'AI 草稿中部分列有警告 — 確認後一併寫入？',
      confirmLabel: '確認寫入',
      onConfirm: async () => {
        const r2 = window.pdBusy ? window.pdBusy($('#ai-write-all'), '寫入中…') : () => {};
        const acked = [];
        let failed2 = null;
        for (const p of needAck) {
          try {
            const resp = await api.post('/api/import/commit', aiCommitBody(p, true));
            acked.push({ kind: p.kind, resp: resp, text: p.picked.text, ns: p.picked.ns });
          } catch (e2) {
            failed2 = e2;
            break;
          }
        }
        r2();
        /* Same settlement rule as the main loop above: kinds whose ack-commit already
           wrote are retired + reported BEFORE the failure toast, never stranded. */
        if (acked.length) await finishAiCommits(acked);
        if (failed2 && window.toast) {
          window.toast((failed2 && failed2.message) || '寫入失敗', 'fail',
            failed2 && failed2.code);
        }
      }
    });
  }

  /* Apply each kind's commit result: toast + per-kind ledger refresh + section cleanup.
     When every kind's section is empty afterwards, the whole pane resets (C7).
     DEF-024: a row the importer refused, or dropped at its re-check, is a WARNING with its
     reason — never folded into a green 「寫入成功」. */
  async function finishAiCommits(commits) {
    let totalWritten = 0;
    let totalDup = 0;
    let totalNotWritten = 0;
    const lines = [];
    for (const c of commits) {
      const o = commitOutcome(c.resp);
      totalWritten += o.written;
      totalDup += o.duplicates;
      /* only ticked rows are sent here, so ANY skip is a row that did not land */
      totalNotWritten += o.deselected + outcomeStopped(o);
      outcomeLines(o).forEach((line) => lines.push(AI_KIND_ZH[c.kind] + ' ' + line));
      await onAiCommitted(c.kind, c.resp, c.text, c.ns);
    }
    const dupText = totalDup > 0 ? '・已匯入過 ' + totalDup + ' 筆' : '';
    if (window.toast) {
      if (totalNotWritten > 0) {
        window.toast('⚠ 寫入完成（有列被擋下）', 'warn',
          '成功 ' + totalWritten + ' 筆・未寫入 ' + totalNotWritten + ' 筆' + dupText
          + (lines.length ? '：' + lines.slice(0, 3).join('；') + (lines.length > 3 ? '；…' : '')
            : ''));
      } else {
        window.toast('寫入成功', 'ok', '成功 ' + totalWritten + ' 筆' + dupText);
      }
    }
    const anyRowsLeft = AI_KINDS.some((kind) => (aiRows[kind] || []).length > 0);
    if (!anyRowsLeft) {
      clearAiInputs();
      aiBanner('✓ 寫入完成：成功 ' + totalWritten + ' 筆' + dupText);
    }
    refreshAiWriteBtn();
  }

  /* Per-kind AI commit success handler (C7). Full success clears THAT kind's section + csv.
     On a PARTIAL result only the rows that were NOT written stay (re-indexed), so a retry
     targets the remainder without re-writing the rows already committed. Which rows those
     are comes from the response itself (`rejected_rows` / `skipped_rows`, 1-based over the
     committed text; `ns` maps them back to drafts) — the old way, a re-preview keeping every
     row not `ok`, also kept acknowledged WARNING rows that had in fact been written. With
     drafts in hand the remainder is re-validated through the AI door (DEF-035); without, the
     committed csv is re-previewed. A failed re-preview keeps the current table (never
     fabricate). */
  async function onAiCommitted(kind, resp, committedCsv, ns) {
    const o = commitOutcome(resp);
    const stoppedAt = o.rejectedRows.map((x) => x.row - 1)
      .concat(o.blocked.map((x) => x.row - 1));
    const partial = o.deselected > 0 || outcomeStopped(o) > 0;
    /* FU-D45 + #10 + DEF-019: flash + auto-switch only on FULL success. `cash` has no ledger
       tab on this page (highlightCommitted no-ops for it by design); the refresh still runs. */
    refreshAfterLedgerChange(kind, { highlight: !partial });
    if (!partial) {
      aiCsvTexts[kind] = '';
      aiRows[kind] = [];
      aiDrafts[kind] = [];
      const tb = $('#ai-body-' + kind); if (tb) tb.replaceChildren();
      const sec = $('#ai-sec-' + kind); if (sec) sec.hidden = true;
      return;
    }
    /* The AI door commits only ticked rows, so a skip here was never the owner's choice:
       the banner says 未寫入, with each row's reason when the server gave one. */
    aiBanner(AI_KIND_ZH[kind] + '：已寫入 ' + o.written + ' 筆／未寫入 '
      + (o.deselected + outcomeStopped(o)) + ' 筆'
      + (outcomeLines(o).length ? '——' + outcomeLines(o).join('；') : ''));
    const drafts = aiDrafts[kind] || [];
    if (stoppedAt.length && Array.isArray(ns) && drafts.length) {
      aiDrafts[kind] = stoppedAt.map((i) => drafts[ns[i]]).filter((d) => d);
      aiFieldErr[kind] = {};
      await revalidateAiKind(kind, true);
      return;
    }
    try {
      const pv = await api.post('/api/import/preview',
        { kind: kind, csv_text: committedCsv });
      const lines = committedCsv.split('\n');
      const header = lines[0] || '';
      const remaining = (pv.rows || []).filter((r) => (stoppedAt.length
        ? stoppedAt.indexOf(r.n) >= 0 : r.status !== 'ok'));
      const kept = remaining.map((r) => lines[(r.n || 0) + 1]).filter((l) => l !== undefined);
      aiCsvTexts[kind] = kept.length ? header + '\n' + kept.join('\n') + '\n' : '';
      aiDrafts[kind] = [];              // no drafts to keep aligned: this path renders read-only
      renderAiRows(kind, remaining.map((r, i) => Object.assign({}, r, { n: i })));
      refreshAiWriteBtn();
    } catch (e) { /* degrade: keep the current table + banner (never fabricate) */ }
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

  /* ---- FU-D45 ledger live refresh ----
     Called from EVERY successful commit path (manual / CSV / AI 寫入全部 / dividend /
     opening) — and ONLY on success; every failure path returns before reaching it.
     (1) re-fetches the lower 帳本記錄 tables in place via the seam ledger.js exposes
     (window.pdLedgerRefresh — a plain function reference, no event binding, so repeated
     commits can never double-fire), and (2) drops the per-account holdings cache so the
     可賣/均價 sell hints and the dividend picker reflect the just-committed rows, then
     re-warms the two panes' selected accounts. */
  /* #10: map an import `kind` to its lower 帳本記錄 tab id + tbody. The import kind `openings`
     (plural) normalizes to the singular `lopen` tab / #open-body table. */
  /* ⚠ `cash` is ABSENT on purpose, and the absence has to be written down because an
     unmapped kind degrades SILENTLY (highlightCommitted returns early) — which is
     indistinguishable from a forgotten registration. This page's lower ledger has five
     tabs and none of them is 資金收支: cash movements are shown on the 資金 page, not
     here. There is no tab to switch to, so a mapping would point at a null element.
     Consequence, and it is a real gap: a cash CSV commits and toasts, but nothing on this
     page shows the rows that landed. Closing it means adding a 6th ledger tab (trades.html
     + ledger.js), which is a bigger change than the import kind. */
  const LEDGER_KIND = {
    transactions: { tab: 'tx', body: 'tx-body' },
    dividends: { tab: 'ldiv', body: 'div-body' },
    fx: { tab: 'lfx', body: 'fx-body' },
    openings: { tab: 'lopen', body: 'open-body' },
    corporate_actions: { tab: 'laction', body: 'action-body' },
  };

  /* #10: after the committed ledger's table has refreshed, auto-switch the lower ledger to
     that tab and soft-pulse (~8×, wn-flash-pulse) its newest top row. Called ONLY on FULL
     success. Strip-then-re-add forces the animation to restart even when the same top row
     flashes twice (a repeat commit landing on the same row). */
  function highlightCommitted(kind) {
    const map = LEDGER_KIND[kind];
    if (!map) return;
    const tabBtn = document.getElementById('tab-' + map.tab);
    if (tabBtn) tabBtn.click();   // switch the lower ledger tab to match the committed type
    const tbody = document.getElementById(map.body);
    const row = tbody && tbody.querySelector('tr');
    if (!row) return;
    row.classList.remove('ledger-added-row');
    void row.offsetWidth;         // reflow so the pulse retriggers on a repeat flash
    row.classList.add('ledger-added-row');
  }

  /* ===== DEF-019 (2026-09-23): ONE answer to "the ledger just changed — what is stale?" =====
     Measured: 最近匯入 › 復原 took back a 0.028-share DRIP, and the manual picker went on
     annotating the symbol 「85.067255 股 均價 158.87」 until a page reload, while
     /api/input/holdings already said 85.039255 / 158.9266. The undo lives in broker-import.js
     and refreshed what IT knew about — its batch list and the ledger tables — while this
     file's per-account holdings cache, read by the manual picker, the sell hints and the
     dividend/opening pickers, was dropped only by THIS file's own commits. Every write path
     had grown its own list of things to re-fetch (the manual commit re-read the context only
     after an auto-register; the corporate-action repair re-read nothing), and the lists
     drifted.

     Every ledger change on this page now ends here:
       * this file's commits call it directly — manual (and its 賣超 ack), CSV, AI, and the
         股利／期初 one-row forms;
       * every OTHER module reaches it through `window.pdLedgerRefresh`, the seam
         broker-import.js (最近匯入 › 復原, the one-click import) and corp-action-form.js
         (公司行動 save) already call after a write. adoptLedgerSeam() re-points that global
         at this function and keeps ledger.js's own table refresh as `ledgerTables`, so those
         callers need no change and cannot skip the holdings refresh.
     In order: the structural context when an instrument may have been registered (an
     auto-register here; any change made elsewhere, e.g. a SPINOFF child), the holdings cache
     (dropped, then re-warmed for the selected accounts, the open picker re-rendered), the
     最近匯入 list (unless the caller already reloaded it), the ledger tables, the sell hints;
     the flash + auto-switch only for a full-success commit made here.
     tests/contract/test_def019_single_ledger_refresh.py pins every path to it. */
  let ledgerTables = null;
  let ledgerSeamAdopted = false;
  function adoptLedgerSeam() {
    if (ledgerSeamAdopted) return;
    const own = window.pdLedgerRefresh;
    if (typeof own !== 'function') return;   // ledger.js not loaded (yet) — retried on use
    ledgerSeamAdopted = true;
    ledgerTables = own;
    window.pdLedgerRefresh = (kind) =>
      refreshAfterLedgerChange(kind, { external: true, context: true, highlight: false });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', adoptLedgerSeam);
  } else {
    adoptLedgerSeam();
  }

  async function refreshAfterLedgerChange(kind, opts) {
    const o = opts || {};
    adoptLedgerSeam();
    if (o.context) await reloadContext();
    Object.keys(acctHoldingsCache).forEach((k) => { delete acctHoldingsCache[k]; });
    /* The 最近匯入 card (broker-import.js) serves BOTH import modes, so an ordinary CSV
       commit has to refresh it too — otherwise the undo control goes stale at exactly the
       moment it is wanted, right after an import that looks wrong. An EXTERNAL caller
       (broker-import.js itself) has already reloaded it. */
    if (!o.external && window.pdReloadImportBatches) {
      try { await window.pdReloadImportBatches(); } catch (e) { /* degrade silently */ }
    }
    if (ledgerTables) {
      /* AWAIT the in-place table refresh so the flash targets the ACTUAL new row (was a
         fixed-300ms guess wired to #m-confirm). A refresh failure must not break the commit
         flow — the caller already toasted success. */
      try { await ledgerTables(kind); } catch (e) { /* degrade silently */ }
    }
    renderSellHints();   // cache miss -> refetch for the selected manual account
    /* M4 (demo audit 2026-09-16): warm the account the commit was written TO (the manual
       select), then re-render the picker if it is open so the annotation changes in front of
       the user; the dividend and opening accounts are re-warmed too. */
    const mSel = $('#m-account');
    if (mSel && mSel.value) {
      loadAcctHoldings(mSel.value, false).then(() => {
        if (manualPicker && manualPicker.render) manualPicker.render();
      }).catch(() => {});
    }
    ['#d-account', '#o-account'].forEach((sel) => {
      const node = $(sel);
      if (node && node.value) loadAcctHoldings(node.value, false).catch(() => {});
    });
    /* #10: flash + auto-switch on FULL success only (highlight !== false); partial/failed
       commits still refresh the tables above but never switch tabs or flash. */
    if (!o.external && o.highlight !== false) highlightCommitted(kind);
  }

  /* The one-row forms (股利, 期初庫存) commit a single-row CSV through the import door.
     DEF-017 (2026-09-23): the batch they create says it was typed by hand — 最近匯入 lists
     every batch, and one with no source reads as an unexplained import.
     DEF-024: a commit that wrote nothing says WHY (the refused row's reason, the re-check's
     finding, or 「已寫入過」), instead of the fixed 「資料列被跳過，請檢查欄位」. */
  const ONE_ROW_SOURCE = '手動輸入';
  function oneRowNotWrittenReason(resp) {
    const o = commitOutcome(resp);
    if (o.rejectedRows.length) return o.rejectedRows[0].message;
    if (o.blocked.length) return o.blocked[0].message;
    if (o.duplicates > 0) return '這筆資料先前已寫入過帳本，未重複寫入';
    return '資料列未寫入，請檢查欄位';
  }
  async function commitOneRow(kind, csvText, btn, okSub, onDone) {
    const restore = window.pdBusy ? window.pdBusy(btn, '寫入中…') : () => {};
    const finishOk = (resp) => {
      if (resp && resp.written >= 1) {
        if (window.toast) window.toast('寫入成功', 'ok', okSub);
        /* FU-D45 + #10 + DEF-019: a written>=1 row here is a full success, so flash +
           auto-switch the matching ledger tab (kind in scope). */
        refreshAfterLedgerChange(kind);
        if (onDone) onDone();
      } else if (window.toast) {
        window.toast('未寫入', 'fail', oneRowNotWrittenReason(resp));
      }
    };
    const body = (ack) => ({ kind: kind, csv_text: csvText, ack_warnings: ack,
      source_name: ONE_ROW_SOURCE });
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
              finishOk(await api.post('/api/import/commit', body(true)));
            } catch (e2) {
              if (window.toast) window.toast((e2 && e2.message) || '寫入失敗', 'fail', e2 && e2.code);
            }
          }
        });
        return;
      }
      const resp = await api.post('/api/import/commit', body(false));
      restore();
      finishOk(resp);
    } catch (err) {
      restore();
      if (window.toast) window.toast((err && err.message) || '寫入失敗', 'fail', err && err.code);
    }
  }

  /* ================= Tab 4 股利 ================= */
  /* P1b: the US pane's type + withholding-override state, published by initDiv so the
     commit handler and renderDivForm can read it without re-querying the DOM's class
     attributes (a `.active` class read from two places drifts the moment one is renamed). */
  let divUsState = null;
  function initDiv() {
    const accSel = $('#d-account');
    ctx.accounts.forEach((a) => {
      const o = el('option', null, accountLabel(a));
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
      /* R6: the ex-date field appears ONLY for 配股 — it is the only dividend type whose
         effective date moves (Dividend.effective_date). Showing it on a cash payout would
         invite filling a value the ledger deliberately ignores. */
      const exf = $('#d-exdate-field');
      if (exf) {
        exf.hidden = !stock;
        if (!stock) { const n = $('#d-exdate'); if (n) n.value = ''; }
      }
      $('#d-model-note').textContent = stock
        ? '台股模式（配股）：以 $0 成本股數入帳，調整均價下降。填入除權日後，股數會在除權日就入帳'
          + '（股價當天已經反映），不填則沿用發放日。'
        : '台股模式：現金股利沖減成本（調整均價下降）；配股以 $0 成本股數入帳。';
    }));

    /* ---- P1b: the US pane's DRIP / 現金股利 switch + the withholding override ---------
       A US payout that is NOT reinvested had no manual door at all before this: the pane
       showed the reinvest fields and nothing else, so the only way in was a CSV row that
       then needed a per-row confirmation. Both types keep the 30% W-8BEN default — the
       withholding applies to the PAYOUT, not to the reinvestment — and both may override
       it, because a broker's own rounding puts the statement a cent away from gross x 0.30
       and a readonly field cannot reproduce a statement it must reconcile to. */
    const usSeg = document.querySelectorAll('#d-drip .segmented button');
    const usIsCash = () => {
      const b = document.querySelector('#d-us-cash');
      return !!(b && b.classList.contains('active'));
    };
    usSeg.forEach((b) => b.addEventListener('click', () => {
      usSeg.forEach((x) => x.classList.toggle('active', x === b));
      renderUsDivType();
    }));
    function renderUsDivType() {
      const cash = usIsCash();
      /* 現金股利 moves no shares, so the reinvest pair is not merely blank but absent —
         a visible field the commit ignores is how a user comes to believe it was recorded. */
      $('#d-drip-shares-field').hidden = cash;
      $('#d-drip-price-field').hidden = cash;
      $('#d-model-note').textContent = cash
        ? '美股現金股利：預扣 30% 後的淨額沖減成本（調整均價下降），與台股現金股利同一套會計（D35）。'
        : 'DRIP 模式：預扣 30%，net 將以 $0 成本股數入帳（再投資股數 × 再投資價格僅供對帳）。';
    }
    /* The withholding override — the SAME true-toggle the manual trade's fee/tax use
       (FU-D7), not a new interaction. OFF restores the auto 30%. */
    let whOverride = false;
    function applyWhOverride(on) {
      whOverride = on;
      const field = $('#d-drip-wh');
      const pencil = $('#d-drip-wh-pencil');
      field.readOnly = !on;
      pencil.setAttribute('aria-pressed', on ? 'true' : 'false');
      pencil.title = on ? '取消覆寫（回自動 30%）' : '覆寫';
      $('#d-drip-wh-ovr').hidden = !on;
      $('#d-drip-wh-label').firstChild.nodeValue = on ? '預扣（已覆寫） ' : '預扣 30%（自動） ';
      if (!on) recomputeDripAmounts();   // auto value returns
    }
    $('#d-drip-wh-pencil').addEventListener('click', () => {
      applyWhOverride(!whOverride);
      if (whOverride) $('#d-drip-wh').focus();
    });
    /* USER-INPUT estimate only (the value of record is computed by the backend on commit).
       Bound ONCE here rather than re-assigned on every renderDivForm() call, which is what
       the old `$('#d-drip-gross').oninput = …` inside renderDivForm did. */
    function recomputeDripAmounts() {
      const g = parseFloat($('#d-drip-gross').value) || 0;
      if (!whOverride) $('#d-drip-wh').value = (g * 0.30).toFixed(2);
      const wh = parseFloat($('#d-drip-wh').value) || 0;
      $('#d-drip-net').value = (g - wh).toFixed(2);
    }
    $('#d-drip-gross').addEventListener('input', recomputeDripAmounts);
    $('#d-drip-wh').addEventListener('input', () => { if (whOverride) recomputeDripAmounts(); });
    divUsState = { isCash: usIsCash, whOverride: () => whOverride,
      reset: () => { applyWhOverride(false); renderUsDivType(); } };
    renderUsDivType();
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
      /* F01: the committed row `type` follows the model of the ENTERED SYMBOL's market on a
         merged account (single-market accounts keep their one model). A merged account whose
         symbol is blank/unregistered -> null -> prompt to pick a registered symbol first. */
      const model = divModelFor(a, sym);
      if (model === null) {
        if (window.toast) window.toast('此帳戶橫跨多個市場，請先輸入已註冊的標的', 'fail');
        return;
      }
      /* R6: ex_date is LAST, matching DIVIDEND_COLUMNS. The CSV door parses by header, so
         the order only has to agree with the values built below — but keeping it aligned
         with the canonical list is what stops the next column from being appended in two
         different places. Only 配股 sends it; see Dividend.effective_date. */
      const header = ['account', 'symbol', 'date', 'type', 'gross', 'withholding', 'net',
        'reinvest_shares', 'reinvest_price', 'ex_date'];
      const exd = (($('#d-exdate') || {}).value || '').trim();
      let values;
      if (model === 'tw') {
        if (isStock()) {
          const shares = $('#d-tw-gross').value.trim();
          if (!shares) { if (window.toast) window.toast('請輸入配股股數', 'fail'); return; }
          values = [a.id, sym, dte, 'STOCK', '0', '', '', shares, '', exd];
        } else {
          const gross = $('#d-tw-gross').value.trim();
          if (!gross) { if (window.toast) window.toast('請輸入股利總額', 'fail'); return; }
          values = [a.id, sym, dte, 'CASH', gross, '', $('#d-tw-net').value.trim(), '', '', ''];
        }
      } else if (model === 'drip') {
        const gross = $('#d-drip-gross').value.trim();
        if (!gross) { if (window.toast) window.toast('請輸入股利總額', 'fail'); return; }
        /* The withholding is sent EXPLICITLY on both types, always. ``apply_dividend_model``
           keys on the dividend TYPE, not on the account's model, so a CASH row with a blank
           withholding would book 0 — correct for a TW/MY payout, wrong for a US one under
           W-8BEN. Sending the number makes it a stated ledger fact rather than something
           inferred from which account it happened to land in. */
        const wh = $('#d-drip-wh').value.trim();
        values = divUsState && divUsState.isCash()
          ? [a.id, sym, dte, 'CASH', gross, wh, '', '', '', '']
          : [a.id, sym, dte, 'DRIP', gross, wh, '',
            $('#d-drip-shares').value.trim(), $('#d-drip-price').value.trim(), ''];
      } else {
        const amt = $('#d-net-amt').value.trim();
        if (!amt) { if (window.toast) window.toast('請輸入淨額', 'fail'); return; }
        values = [a.id, sym, dte, 'NET', amt, '', '', '', '', ''];
      }
      commitOneRow('dividends', oneRowCsv(header, values), $('#d-confirm'),
        sym + ' 股利已寫入帳本（' + acctZh(a.id) + '）', () => {
          ['d-tw-gross', 'd-tw-net', 'd-drip-gross', 'd-drip-wh', 'd-drip-net',
            'd-drip-shares', 'd-drip-price', 'd-net-amt', 'd-exdate'].forEach((id) => {
            const n = $('#' + id); if (n) n.value = '';
          });
          /* Clear the OVERRIDE too, not just the value: a pencil left pressed over an
             empty field silently sends a blank withholding on the NEXT dividend. */
          if (divUsState) divUsState.reset();
          /* holdings refresh (STOCK/DRIP can grow shares) rides refreshAfterLedgerChange
             (FU-D45, DEF-019): the shared cache is dropped + this account re-warmed. */
        });
    });
  }
  function renderDivForm() {
    const a = acc($('#d-account').value) || ctx.accounts[0];
    /* F01: on a MERGED account the model follows the ENTERED SYMBOL's market; single-market
       accounts get their one model (byte-identical to the old `a.div_model`). */
    const sym = ($('#d-symbol') && $('#d-symbol').value || '').trim();
    const model = divModelFor(a, sym);
    ['d-tw', 'd-drip', 'd-net'].forEach((id) => { $('#' + id).hidden = true; });
    const note = $('#d-model-note');
    if (model === null) {
      /* Merged account, no symbol resolvable to a bound market yet: hide every model form and
         prompt the user to enter/pick a REGISTERED symbol first (its market picks the model). */
      if (note) {
        note.textContent = '此帳戶橫跨多個市場，請先輸入或選擇已註冊標的，表單將依標的所屬市場切換股利模式。';
      }
      return;
    }
    if (model === 'tw') {
      $('#d-tw').hidden = false;
      note.textContent = '台股模式：現金股利沖減成本（調整均價下降）；配股以 $0 成本股數入帳。';
    } else if (model === 'drip') {
      $('#d-drip').hidden = false;
      /* The note now depends on the pane's OWN type switch (DRIP vs 現金股利), so it is
         written by renderUsDivType rather than here — two writers of one string is how the
         note comes to contradict the form it describes. */
      if (divUsState) divUsState.reset();
    } else {
      $('#d-net').hidden = false;
      note.textContent = '馬股模式：單一淨額入帳（無預扣層級）。';
    }
  }

  /* ---- FU-D35 dividend 代號 picker (owner 需求六) — Wave C: the shared component ----
     After an account is chosen, activating 代號 lists that account's CURRENTLY-HELD symbols
     for point-and-click (dividends normally come from a live position). Held rows now ALSO
     show 股數 + 均價 (Wave C parity with the manual picker). The 「顯示已清倉標的」 toggle
     additionally lists symbols the account historically held but has since closed — a closed
     position can still pay a dividend after its ex-date (owner 假設 2). Held/closed come from
     GET /api/input/holdings?account=… (server-side Decimal share math), cached per account +
     refetched after a successful commit. ASSISTIVE ONLY: it never overwrites what the user
     types (the commit reads #d-symbol.value directly — an unlisted symbol still submits). */
  /* Shared per-account holdings cache (dividend/manual/opening pickers + FU-D44 sell hints).
     Held entries carry shares + adjusted_avg as Decimal STRINGS; dropped after every successful
     commit (FU-D45; since DEF-019 by refreshAfterLedgerChange, whoever made the change). */
  const acctHoldingsCache = {};   // { [accountId]: {held:[{symbol,name,shares,adjusted_avg}], closed:[{symbol,name}]} }
  /* Fable F9a in-flight dedup: focus + click on a cold cache both call the picker's open, which
     each call loadAcctHoldings; a shared in-flight promise collapses those into ONE fetch. */
  const acctHoldingsInflight = {};

  /* Fetch (or return the cached) {held, closed} for an account. Graceful: a failed fetch
     returns the last cache (or empties) so the picker degrades to a plain typed input. */
  async function loadAcctHoldings(accountId, force) {
    if (!accountId) return { held: [], closed: [] };
    if (!force && acctHoldingsCache[accountId]) return acctHoldingsCache[accountId];
    if (!force && acctHoldingsInflight[accountId]) return acctHoldingsInflight[accountId];
    const p = (async () => {
      let resp;
      try {
        resp = await api.get('/api/input/holdings?account=' + encodeURIComponent(accountId));
      } catch (e) {
        return acctHoldingsCache[accountId] || { held: [], closed: [] };
      }
      const data = { held: (resp && resp.held) || [], closed: (resp && resp.closed) || [] };
      acctHoldingsCache[accountId] = data;
      return data;
    })();
    acctHoldingsInflight[accountId] = p;
    try { return await p; } finally { delete acctHoldingsInflight[accountId]; }
  }

  /* Account switch: reset the toggle (held-first per account), close, warm the new cache. */
  function onDivAccountChange() {
    const toggle = $('#d-sym-closed-toggle');
    if (toggle) toggle.checked = false;
    if (divPicker) divPicker.close();
    const accId = $('#d-account').value;
    if (accId) loadAcctHoldings(accId, false).catch(() => {});
  }

  function initDivPicker() {
    divPicker = window.pdSymPicker.create({
      input: $('#d-symbol'),
      field: $('#d-symbol-field'),
      panel: $('#d-sym-picker'),
      list: $('#d-sym-list'),
      empty: $('#d-sym-empty'),
      foot: $('#d-sym-foot'),
      mode: 'held-closed',
      annotateHeld: true,
      accountOf: () => acc($('#d-account').value),
      instOf: (s) => inst(s),
      loadHoldings: (id, force) => loadAcctHoldings(id, force),
      cachedHoldings: (id) => acctHoldingsCache[id],
      closedToggle: { checkbox: $('#d-sym-closed-toggle') },
      /* Merged accounts switch the dividend MODEL by the entered/picked symbol's market (F01);
         gated on multi-market so a single-market account keeps today's behaviour. */
      onType: () => { if (isMultiMarket(acc($('#d-account').value))) renderDivForm(); },
      onPick: () => { if (isMultiMarket(acc($('#d-account').value))) renderDivForm(); },
      emptyText: (built, q) => {
        const held = built.held || [];
        const closed = built.closed || [];
        if (held.length === 0 && closed.length === 0) return '此帳戶尚無標的紀錄 — 可直接輸入代號';
        if (held.length === 0 && !built.showClosed) {
          return '此帳戶目前無持有標的；勾選「顯示已清倉標的」可挑選歷史標的';
        }
        return '無相符標的 — 可直接輸入代號';
      },
    });
    /* Warm the default account's cache so the first focus paints instantly. */
    const accId0 = $('#d-account').value;
    if (accId0) loadAcctHoldings(accId0, false).catch(() => {});
  }

  /* ================= Tab 5 期初庫存 =================
     (換匯已移至「資金管理」統一管理 — 2026-07-03 R6 item 7；opening 單筆仍走
     one-row-CSV import path。) */
  function initFxOpen() {
    const oAccSel = $('#o-account');
    ctx.accounts.forEach((a) => {
      const o = el('option', null, accountLabel(a)); o.value = a.id;  /* zh + trading ccys (M5/L8) */
      oAccSel.appendChild(o);
    });
    $('#o-date').value = TODAY;
    /* A6: 原始總成本 is the money of record (required); 均價 is a live READ-ONLY display hint
       (total / shares). This division is the ONE sanctioned client-side money math — it is a
       DISPLAY of two user-entered raw numbers, never a value of record (the backend stores the
       total verbatim; the average is computed on read everywhere). */
    function updateAvgView() {
      const view = $('#o-avg-view');
      if (!view) return;
      const a = acc($('#o-account').value) || ctx.accounts[0];
      /* F06: the average-cost hint uses the RESOLVED symbol's quote ccy (3-dp for an MY
         counter on a merged account), falling back to the account ccy when unresolved.
         Single-market: it.ccy === a.ccy, so the label is unchanged. */
      const it = inst($('#o-symbol').value);
      const ccy = (it && it.ccy) || (a ? a.ccy : '');
      const sharesRaw = $('#o-shares').value.trim();
      const totalRaw = $('#o-total').value.trim();
      const shares = Number(sharesRaw);
      const total = Number(totalRaw);
      if (sharesRaw !== '' && totalRaw !== '' && shares > 0 && isFinite(total)) {
        view.textContent = f.price(String(total / shares), ccy);
      } else {
        view.textContent = f.NULL_GLYPH;
      }
    }
    ['o-account', 'o-symbol', 'o-shares', 'o-total'].forEach((id) => {
      const n = $('#' + id);
      if (n) n.addEventListener('input', updateAvgView);
    });

    /* Wave C: 期初庫存 代號 now uses the SAME shared grouped picker (was a native datalist) —
       已持有 / 未持有, market-filtered, archived excluded, held rows annotated 股數 + 均價. */
    function openOpeningQuickAddNew(typed) {
      if (!window.pdInstQuickAdd) {
        if (window.toast) window.toast('對話框載入失敗，請重新整理', 'fail');
        return;
      }
      const sym0 = ((typed || $('#o-symbol').value || '')).trim().toUpperCase();
      if (openingPicker) openingPicker.close();
      const select = async (resp) => {
        await reloadContext();
        const sym = (resp && resp.symbol) || sym0;
        if (sym && openingPicker) openingPicker.select(sym);
        else updateAvgView();
      };
      window.pdInstQuickAdd({
        symbol: sym0,
        market: accountMarket($('#o-account').value),
        lockSymbol: false,
        onConfirm: select,
        onBuy: select,
      });
    }
    openingPicker = window.pdSymPicker.create({
      input: $('#o-symbol'),
      field: $('#o-symbol-field'),
      panel: $('#o-sym-picker'),
      list: $('#o-sym-list'),
      empty: $('#o-sym-empty'),
      foot: $('#o-sym-foot'),
      mode: 'held-unheld',
      marketFilter: true,
      annotateHeld: true,
      accountOf: () => acc($('#o-account').value),
      instrumentsOf: () => ctx.instruments,
      instOf: (s) => inst(s),
      loadHoldings: (id, force) => loadAcctHoldings(id, force),
      cachedHoldings: (id) => acctHoldingsCache[id],
      addNew: { button: $('#o-sym-addnew'), onAdd: (q) => openOpeningQuickAddNew(q) },
      onPick: () => { updateAvgView(); },
      emptyText: (built, q) => q
        ? '無相符標的 — 可直接輸入代號，或點下方「＋新增標的」'
        : '此帳戶所屬市場尚無標的 — 點下方「＋新增標的」新增',
    });

    if (oAccSel) oAccSel.addEventListener('change', () => {
      updateAvgView();
      if (openingPicker) openingPicker.close();   // re-scope the picker to the new account
      const accId = $('#o-account').value;
      if (accId) loadAcctHoldings(accId, false).catch(() => {});
    });
    updateAvgView();
    /* Warm the default account's cache so the first focus paints instantly. */
    const oAccId0 = $('#o-account').value;
    if (oAccId0) loadAcctHoldings(oAccId0, false).catch(() => {});
    $('#o-confirm').addEventListener('click', () => {
      const accId = $('#o-account').value || (ctx.accounts[0] && ctx.accounts[0].id) || '';
      const sym = $('#o-symbol').value.trim();
      const shares = $('#o-shares').value.trim();
      const total = $('#o-total').value.trim();
      const dte = $('#o-date').value;
      if (!accId || !sym || !shares || !total || !dte) {
        if (window.toast) window.toast('請填寫帳戶、代號、股數、原始總成本與建檔日', 'fail');
        return;
      }
      const csv = oneRowCsv(
        ['account', 'symbol', 'shares', 'original_cost_total', 'build_date'],
        [accId, sym, shares, total, dte]);
      commitOneRow('openings', csv, $('#o-confirm'),
        sym + ' 期初庫存已建檔（同鍵覆蓋更新）', () => {
          ['o-symbol', 'o-shares', 'o-total'].forEach((id) => {
            const n = $('#' + id); if (n) n.value = '';
          });
          updateAvgView();
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
