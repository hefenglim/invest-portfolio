/* portfolio-dash — shared instrument quick-add dialog (FU-D23; FU-D42 editable symbol +
   auto-lookup + AI resolve fallback).

   window.pdInstQuickAdd(opts) opens a modal that:
     1. runs GET /api/instruments/lookup — fast identify (name + suggested sector +
        board/is_etf); found:false is the typo guard and blocks the confirm. The lookup
        fires automatically on open (prefilled symbol) AND re-fires (debounced) whenever
        the user edits the symbol or market; auto-fills only overwrite fields the user
        has not touched (pristine tracking, FU-D42a),
     2. lets the user confirm the name + pick/enter a sector (canonical select + free
        text; the lookup suggestion is preselected),
     3. when the lookup finds nothing (查無報價), offers 「AI 判讀代號」 (FU-D42c): POST
        /api/instruments/ai-resolve maps the raw input to the target market's LOCAL
        exchange code; the REAL lookup then re-verifies (the quote check stays the
        registration authority — an unverified AI suggestion can never be confirmed);
        a still-unfound suggestion gets the honest 「AI 判讀後仍查無報價」 notice,
     4. registers via POST /api/instruments — the heavy quote/history fetch runs in the
        BACKGROUND (BackgroundTasks), so the confirm returns fast,
   then calls opts.onConfirm(result) (確認) or opts.onBuy(result) (記一筆買入).

   opts:
     symbol      prefill symbol (all call sites prefill it)
     market      'TW' | 'US' | 'MY' (default 'TW')
     lockSymbol  DEPRECATED (FU-D42a) — accepted but IGNORED: the symbol field is always
                 editable, so a wrong AI-parsed symbol (owner bug: 聯電 → "UMC" on a TW
                 row) can be fixed in place. No call site genuinely needs a locked field.
     onConfirm(result)  after 確認 registers
     onBuy(result)      after 記一筆買入 registers (the caller navigates to the manual pane)
   result = the POST /api/instruments response element (+ restored / last_price_date when a
   soft-deleted symbol was RESTORED rather than freshly added).

   No money is computed here (the dialog only submits metadata; prices come from the API).
   Styling reuses the shared .modal-* / .field / .btn classes (no styles.css edits). */
(function () {
  'use strict';
  const api = window.pdApi;
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const MARKETS = [['TW', '台股'], ['US', '美股'], ['MY', '馬股']];

  /* ---- FU-D31: shared canonical-sector field (used by BOTH this dialog and the
     instruments edit form via window.pdSectorField). A <select> of the canonical
     vocabulary (dual-text labels 「Technology（科技）」) + the current off-vocabulary value
     preserved as an extra option (so editing an unmigrated row never destroys it) + an
     「其他…」 escape (free text) + an 「AI 偵測」 button beside the select. The canonical list
     and its synonyms live SERVER-SIDE (GET /api/instruments/sectors, POST
     .../ai-sector) — the frontend never hardcodes them. Stored values stay as-is unless
     the user re-picks; the read-time donut/alert canonicalization is a backend concern
     (portfolio/dashboard.py). ---- */
  const OTHER_SECTOR = '__other__';
  let _sectorVocab = null;  // cached [{key, zh}]
  async function fetchSectorVocab() {
    if (_sectorVocab) return _sectorVocab;
    try {
      const resp = await api.get('/api/instruments/sectors');
      _sectorVocab = (resp && resp.sectors) || [];
    } catch (e) {
      _sectorVocab = [];
    }
    return _sectorVocab;
  }

  function pdSectorField(opts) {
    opts = opts || {};
    const wrap = el('div', 'sector-field');
    const row = el('div', 'sector-row');
    row.style.cssText = 'display:flex; gap:8px; align-items:center;';
    const sel = el('select', 'select sector-select');
    sel.style.flex = '1';
    const aiBtn = el('button', 'btn sector-ai-btn', 'AI 偵測'); aiBtn.type = 'button';
    aiBtn.title = '以 AI 依代號／名稱／市場判斷產業類別';
    row.appendChild(sel); row.appendChild(aiBtn);
    wrap.appendChild(row);
    const freeIn = el('input', 'input sector-free');
    freeIn.placeholder = '自訂產業';
    freeIn.spellcheck = false;
    freeIn.style.cssText = 'display:none; margin-top:6px;';
    wrap.appendChild(freeIn);
    const note = el('div', 'hint sector-note');
    note.style.minHeight = '14px';
    wrap.appendChild(note);

    let keySet = new Set();
    let current = (opts.current || '').trim();

    function toggleFree() {
      const other = sel.value === OTHER_SECTOR;
      freeIn.style.display = other ? '' : 'none';
      if (other) freeIn.focus();
    }
    function ensureOffListOption(v) {
      const prior = sel.querySelector('option[data-offlist="1"]');
      if (prior) prior.remove();
      const o = el('option', null, v + '（目前值）');
      o.value = v; o.setAttribute('data-offlist', '1');
      sel.insertBefore(o, sel.firstChild);
    }
    /* Apply a raw value: canonical keys select directly; any off-list value is preserved
       as a "current value" option (never destroyed). Blank selects the empty placeholder,
       so a blank stored sector is NOT silently migrated to the first canonical option. */
    function applyValue(v) {
      v = (v || '').trim();
      if (!v) { sel.value = ''; toggleFree(); return; }
      if (keySet.has(v)) { sel.value = v; } else { ensureOffListOption(v); sel.value = v; }
      toggleFree();
    }
    function populate(vocab) {
      keySet = new Set(vocab.map((s) => s.key));
      sel.replaceChildren();
      const ph = el('option', null, '（未設定）'); ph.value = ''; sel.appendChild(ph);
      vocab.forEach((s) => {
        const o = el('option', null, s.key + '（' + s.zh + '）'); o.value = s.key;
        sel.appendChild(o);
      });
      const oo = el('option', null, '其他…'); oo.value = OTHER_SECTOR; sel.appendChild(oo);
      applyValue(current);
    }

    sel.addEventListener('change', toggleFree);
    aiBtn.addEventListener('click', async () => {
      const symbol = (opts.symbol ? opts.symbol() : '') || '';
      if (!symbol.trim()) { note.textContent = '請先輸入代號再偵測'; return; }
      note.textContent = '';
      const restore = window.pdBusy ? window.pdBusy(aiBtn, '偵測中…') : () => {};
      let resp;
      try {
        resp = await api.post('/api/instruments/ai-sector', {
          symbol: symbol.trim(),
          name: opts.name ? (opts.name() || '') : '',
          market: opts.market ? (opts.market() || '') : '',
        });
      } catch (err) {
        restore();
        if (window.toast) {
          window.toast(err && err.message ? err.message : 'AI 偵測失敗', 'fail', err && err.code);
        }
        return;
      }
      restore();
      if (resp && resp.mapped && resp.sector) {
        current = resp.sector;
        applyValue(current);
        note.textContent = 'AI 判定：' + resp.sector;
      } else {
        /* unmappable reply → owner-specified notice; the user's selection is untouched. */
        note.textContent = 'AI 回傳類別無法對應，保留原選擇';
      }
    });

    fetchSectorVocab().then(populate);

    return {
      element: wrap,
      value: function () { return sel.value === OTHER_SECTOR ? freeIn.value.trim() : sel.value; },
      setValue: function (v) { current = (v || '').trim(); applyValue(current); },
    };
  }
  /* Expose the shared field so web/instruments.js (edit form) reuses it (loaded after). */
  window.pdSectorField = pdSectorField;

  window.pdInstQuickAdd = function (opts) {
    opts = opts || {};
    if (!api) return;

    const backdrop = el('div', 'modal-backdrop');
    const modal = el('div', 'modal');

    const head = el('div', 'modal-head');
    head.appendChild(el('h3', 'modal-title', '加入觀察清單'));
    const close = el('button', 'modal-close', '✕'); close.type = 'button';
    close.title = '關閉（Esc）';
    head.appendChild(close);
    modal.appendChild(head);

    const body = el('div', 'modal-body');
    const fld = (label, node) => {
      const w = el('div', 'field');
      w.appendChild(el('label', null, label));
      w.appendChild(node);
      return w;
    };

    /* FU-D42a: the symbol is ALWAYS editable (opts.lockSymbol is deprecated and ignored)
       — a wrong prefilled symbol (e.g. an AI-parsed US ticker on a TW row) is fixed here,
       and every edit re-runs the real lookup below. */
    const symIn = el('input', 'input qa-symbol');
    symIn.value = (opts.symbol || '').trim().toUpperCase();
    symIn.spellcheck = false;
    symIn.placeholder = '例：2330、AAPL';
    body.appendChild(fld('代號', symIn));

    const mktSel = el('select', 'select');
    MARKETS.forEach(([v, label]) => {
      const o = el('option', null, label); o.value = v;
      if ((opts.market || 'TW') === v) o.selected = true;
      mktSel.appendChild(o);
    });
    body.appendChild(fld('市場', mktSel));

    const nameIn = el('input', 'input qa-name');
    nameIn.placeholder = '名稱（自動查詢，可修改）';
    nameIn.spellcheck = false;
    body.appendChild(fld('名稱', nameIn));

    const sectorField = pdSectorField({
      current: '',
      symbol: () => symIn.value,
      name: () => nameIn.value,
      market: () => mktSel.value,
    });
    body.appendChild(fld('產業', sectorField.element));

    const etfWrap = el('label', 'hint');
    const etfCb = el('input'); etfCb.type = 'checkbox';
    etfWrap.appendChild(etfCb);
    etfWrap.appendChild(el('span', null, ' 此標的為 ETF（影響台股賣出稅率 0.1%）'));
    body.appendChild(fld('類別', etfWrap));

    const status = el('div', 'hint');
    status.style.cssText = 'min-height:16px;';
    body.appendChild(status);
    /* FU-D42c: shown only when the lookup reports not-found (查無報價). */
    const aiBtn = el('button', 'btn qa-ai-resolve', 'AI 判讀代號'); aiBtn.type = 'button';
    aiBtn.title = '以 AI 判讀輸入的代號／名稱對應的正確代號，再以真實報價查核';
    aiBtn.style.display = 'none';
    body.appendChild(aiBtn);
    modal.appendChild(body);

    const foot = el('div', 'modal-foot');
    const buyBtn = el('button', 'btn', '記一筆買入'); buyBtn.type = 'button';
    buyBtn.title = '加入後直接前往手動交易，並帶入此代號';
    const okBtn = el('button', 'btn btn-primary', '確認'); okBtn.type = 'button';
    foot.appendChild(buyBtn); foot.appendChild(okBtn);
    modal.appendChild(foot);
    backdrop.appendChild(modal);

    /* Latest lookup result — board rides through to the POST so TW is not re-probed. */
    let lookupState = { found: false, registered: false, archived: false, board: null };
    let busy = false;
    /* FU-D42a pristine tracking: a lookup auto-fill only overwrites a field the USER has
       never touched. Programmatic fills do not fire these events, so pristine survives
       auto-fill → re-lookup → re-fill chains; one user keystroke/pick pins the field. */
    let namePristine = true;
    let sectorPristine = true;
    let etfPristine = true;
    nameIn.addEventListener('input', () => { namePristine = false; });
    sectorField.element.addEventListener('change', () => { sectorPristine = false; });
    sectorField.element.addEventListener('input', () => { sectorPristine = false; });
    etfCb.addEventListener('change', () => { etfPristine = false; });
    /* Stale-response guard: only the LATEST lookup may touch the dialog. */
    let lookupSeq = 0;
    /* Set right before the post-AI-resolve verification lookup; consumed by its
       not-found branch to show the honest 「AI 判讀後仍查無報價」 notice. */
    let aiResolveTried = false;

    function setEnabled(ok) { okBtn.disabled = !ok; buyBtn.disabled = !ok; }
    setEnabled(false);

    const dismiss = () => {
      document.removeEventListener('keydown', onKey);
      backdrop.remove();
    };
    const onKey = (e) => { if (e.key === 'Escape' && !busy) dismiss(); };
    document.addEventListener('keydown', onKey);
    close.addEventListener('click', () => { if (!busy) dismiss(); });
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop && !busy) dismiss(); });

    async function runLookup() {
      const seq = ++lookupSeq;
      const wasAiResolve = aiResolveTried;
      aiResolveTried = false;  // consumed by THIS run only
      const sym = symIn.value.trim().toUpperCase();
      setEnabled(false);
      aiBtn.style.display = 'none';
      lookupState = { found: false, registered: false, archived: false, board: null };
      if (!sym) { status.textContent = '請輸入代號'; return; }
      status.textContent = '查詢中…';
      /* FU-D42b: lookup-in-flight indicator on the name field (pristine only — a
         user-typed name is never masked). */
      if (namePristine) nameIn.placeholder = '查詢中…';
      let r;
      try {
        r = await api.get('/api/instruments/lookup', { symbol: sym, market: mktSel.value });
      } catch (err) {
        if (seq !== lookupSeq) return;  // superseded by a newer edit
        nameIn.placeholder = '名稱（自動查詢，可修改）';
        status.textContent = '查詢失敗，請稍後再試';
        return;
      }
      if (seq !== lookupSeq) return;  // superseded by a newer edit
      nameIn.placeholder = '名稱（自動查詢，可修改）';
      lookupState = r || { found: false };
      if (r && r.registered) {
        status.textContent = '已註冊 — 此標的已在觀察清單中';
        return;
      }
      if (!r || !r.found) {
        /* Stale suggestions from a PREVIOUS symbol's lookup are cleared (pristine only). */
        if (namePristine) nameIn.value = '';
        if (sectorPristine) sectorField.setValue('');
        status.textContent = wasAiResolve
          ? 'AI 判讀後仍查無報價 — 請確認代號與市場是否正確'
          : '查無報價 — 請確認代號與市場是否正確';
        aiBtn.style.display = '';  // FU-D42c fallback entry
        return;
      }
      /* found & addable (a brand-new symbol, or an archived one to restore); auto-fill
         replaces prior AUTO-fills but never a user-touched field (pristine tracking). */
      if (namePristine && r.name) nameIn.value = r.name;
      if (sectorPristine) sectorField.setValue(r.sector || '');
      if (etfPristine) etfCb.checked = !!r.is_etf;
      status.textContent = r.archived
        ? '已封存 — 確認後將還原並於背景補抓缺口'
        : '已找到，確認後加入觀察清單（報價與歷史於背景抓取）';
      setEnabled(true);
    }

    /* FU-D42c: AI 判讀代號 — maps the raw input (symbol/name fields) + target market to
       the LOCAL exchange code, then AUTO re-runs the REAL lookup: the provider quote
       check remains the sole registration authority (the LLM reply is a suggestion, never
       a verification). Degrade (402/409/503) surfaces as a toast; nothing is blocked. */
    aiBtn.addEventListener('click', async () => {
      const query = (symIn.value.trim() + ' ' + nameIn.value.trim()).trim();
      if (!query) { status.textContent = '請先輸入代號或名稱'; return; }
      const restore = window.pdBusy ? window.pdBusy(aiBtn, '判讀中…') : () => {};
      let resp;
      try {
        resp = await api.post('/api/instruments/ai-resolve',
          { query: query, market: mktSel.value });
      } catch (err) {
        restore();
        if (window.toast) {
          window.toast(err && err.message ? err.message : 'AI 判讀失敗', 'fail',
            err && err.code);
        }
        return;
      }
      restore();
      const suggested = resp && (resp.symbol || '').trim().toUpperCase();
      if (!suggested) {
        status.textContent = 'AI 無法判讀 — 請確認代號與市場是否正確';
        return;
      }
      symIn.value = suggested;
      if (namePristine && resp.name) nameIn.value = resp.name;
      aiResolveTried = true;
      runLookup();  // the REAL lookup verifies the suggestion (registration authority)
    });

    async function doRegister(after) {
      const sym = symIn.value.trim().toUpperCase();
      if (!sym) return;
      const reqBody = {
        symbol: sym, market: mktSel.value,
        name: nameIn.value.trim(), sector: sectorField.value(),
        is_etf: etfCb.checked,
      };
      /* Only a TW board rides through (US/MY resolve their board server-side). */
      if (mktSel.value === 'TW' && lookupState.board) reqBody.board = lookupState.board;
      const btn = after === 'buy' ? buyBtn : okBtn;
      busy = true;
      const restore = window.pdBusy ? window.pdBusy(btn, '加入中…') : () => {};
      let resp;
      try {
        resp = await api.post('/api/instruments', reqBody);
      } catch (err) {
        busy = false;
        restore();
        if (window.toast) {
          window.toast(err && err.message ? err.message : '加入失敗', 'fail', err && err.code);
        }
        return;
      }
      busy = false;
      restore();
      if (window.toast) {
        if (resp && resp.restored) {
          window.toast('已還原 ' + sym, 'ok',
            '已還原既有資料' +
            (resp.last_price_date ? '（保留至 ' + resp.last_price_date + '）' : '') +
            '，背景補抓中');
        } else {
          window.toast('已加入 ' + sym, 'ok', '已加入觀察清單，背景抓取報價中');
        }
      }
      dismiss();
      if (after === 'buy') { if (opts.onBuy) opts.onBuy(resp); }
      else if (opts.onConfirm) { opts.onConfirm(resp); }
    }

    okBtn.addEventListener('click', () => doRegister('confirm'));
    buyBtn.addEventListener('click', () => doRegister('buy'));
    mktSel.addEventListener('change', runLookup);
    /* FU-D42a: editing the symbol re-runs the lookup (debounced; always attached — the
       symbol is always editable). The seq guard in runLookup drops superseded replies. */
    let t = null;
    symIn.addEventListener('input', () => {
      if (t) clearTimeout(t);
      t = setTimeout(runLookup, 300);
    });

    document.body.appendChild(backdrop);
    runLookup();  // FU-D42b: auto-lookup on open (prefilled symbol identifies immediately)
    setTimeout(() => { (symIn.value ? nameIn : symIn).focus(); }, 50);
  };
})();
