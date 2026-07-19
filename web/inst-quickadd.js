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
     3. AUTOMATICALLY (R6-B) calls the UNIFIED AI resolve when the lookup misses — POST
        /api/instruments/ai-resolve maps the raw input + target market to the LOCAL exchange
        code + name + GICS sector (+ optional industry) in ONE reply. status:"resolved"
        auto-fills 代號/名稱/產業/產業細分 (pristine-respecting) then re-runs the real lookup
        to re-validate; status:"candidates" renders 2-5 clickable rows; status:"not_found"
        shows 「查無此標的」. The 「AI 判讀代號」 button is the manual RETRY for the same call.
        The real lookup stays the sole registration authority — an unverified AI suggestion
        can never be confirmed,
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

  /* ---- FU-D31 / R6: shared canonical-sector field (used by BOTH this dialog and the
     instruments edit form via window.pdSectorField). A <select> of the canonical GICS
     vocabulary (dual-text labels 「Information Technology（資訊科技）」) + the current
     off-vocabulary value preserved as an extra option (so editing an unmigrated row never
     destroys it) + an 「其他…」 escape (free text) + an 「AI 偵測」 button beside the select.
     The canonical list + synonyms live SERVER-SIDE (GET /api/instruments/sectors); the AI
     button calls the UNIFIED POST /api/instruments/ai-resolve with sector_only=true (R6-B) —
     for an existing instrument it applies ONLY sector (+ industry via opts.setIndustry),
     never symbol/name. The frontend never hardcodes the vocabulary; the read-time
     donut/alert canonicalization is a backend concern (portfolio/dashboard.py). ---- */
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
      const name = opts.name ? (opts.name() || '') : '';
      const query = (symbol.trim() + ' ' + name.trim()).trim();
      if (!query) { note.textContent = '請先輸入代號或名稱再偵測'; return; }
      note.textContent = '';
      const restore = window.pdBusy ? window.pdBusy(aiBtn, '偵測中…') : () => {};
      let resp;
      try {
        /* sector_only (R6-B): the merged endpoint re-classifies a KNOWN instrument (skips the
           registered short-circuit); we apply ONLY sector (+ industry), never symbol/name. */
        resp = await api.post('/api/instruments/ai-resolve', {
          query: query,
          market: opts.market ? (opts.market() || '') : '',
          sector_only: true,
        });
      } catch (err) {
        restore();
        if (window.toast) {
          window.toast(err && err.message ? err.message : 'AI 偵測失敗', 'fail', err && err.code);
        }
        return;
      }
      restore();
      /* resolved → sector (+ industry); candidates → the first candidate's sector. */
      let sector = '';
      let industry = '';
      if (resp && resp.status === 'resolved') {
        sector = resp.sector || '';
        industry = resp.industry || '';
      } else if (resp && resp.status === 'candidates' && resp.candidates && resp.candidates.length) {
        sector = resp.candidates[0].sector || '';
      }
      if (sector) {
        current = sector;
        applyValue(current);
        note.textContent = 'AI 判定：' + sector;
        if (industry && opts.setIndustry) opts.setIndustry(industry);
      } else {
        /* not_found / unmappable → owner-specified notice; the user's selection is untouched. */
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

    /* R6: optional GICS 產業細分 — AI-populated, editable, submitted on register. Declared
       before the sector field so its setIndustry closure can fill it (pristine-respecting). */
    const industryIn = el('input', 'input qa-industry');
    industryIn.placeholder = '例：Semiconductors（AI 可自動填入，可修改）';
    industryIn.spellcheck = false;

    const sectorField = pdSectorField({
      current: '',
      symbol: () => symIn.value,
      name: () => nameIn.value,
      market: () => mktSel.value,
      setIndustry: (v) => { if (industryPristine) industryIn.value = v; },
    });
    body.appendChild(fld('產業', sectorField.element));
    body.appendChild(fld('產業細分（選填）', industryIn));

    const etfWrap = el('label', 'hint');
    const etfCb = el('input'); etfCb.type = 'checkbox';
    etfWrap.appendChild(etfCb);
    etfWrap.appendChild(el('span', null, ' 此標的為 ETF（影響台股賣出稅率 0.1%）'));
    body.appendChild(fld('類別', etfWrap));

    const status = el('div', 'hint');
    status.style.cssText = 'min-height:16px;';
    body.appendChild(status);
    /* R6-B: the unified AI resolve fires AUTOMATICALLY on a lookup miss; this button is the
       manual RETRY for the SAME call (shown when the lookup reports not-found). */
    const aiBtn = el('button', 'btn qa-ai-resolve', 'AI 判讀代號'); aiBtn.type = 'button';
    aiBtn.title = '重新以 AI 判讀輸入的代號／名稱對應的正確代號＋產業，再以真實報價查核';
    aiBtn.style.display = 'none';
    body.appendChild(aiBtn);
    /* Clickable AI candidate rows (status:"candidates") render here; hidden otherwise. */
    const candBox = el('div', 'qa-candidates');
    candBox.style.cssText = 'display:none; flex-direction:column; gap:6px; margin-top:8px;';
    body.appendChild(candBox);
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
    let industryPristine = true;
    nameIn.addEventListener('input', () => { namePristine = false; });
    sectorField.element.addEventListener('change', () => { sectorPristine = false; });
    sectorField.element.addEventListener('input', () => { sectorPristine = false; });
    etfCb.addEventListener('change', () => { etfPristine = false; });
    industryIn.addEventListener('input', () => { industryPristine = false; });
    /* Stale-response guard: only the LATEST lookup may touch the dialog. */
    let lookupSeq = 0;
    /* R6-B: the unified AI resolve fires once per DISTINCT settled input (dedup key = query +
       market, so changing the market legitimately re-fires); aiSeq drops a superseded concurrent
       AI reply the same way lookupSeq guards lookups. */
    let lastAiKey = '';
    let aiSeq = 0;
    /* Set right before the post-AI-resolve verification lookup; consumed by its
       not-found branch to show the honest 「AI 判讀後仍查無報價」 notice (and to suppress a
       re-fire of the automatic AI on an AI-suggested symbol that still cannot be verified). */
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
      clearCandidates();
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
        clearCandidates();
        aiBtn.style.display = '';  // manual RETRY entry
        if (wasAiResolve) {
          /* An AI-suggested symbol the provider still cannot find — no re-fire, honest notice. */
          status.textContent = 'AI 判讀後仍查無報價 — 請確認代號與市場是否正確';
          return;
        }
        /* AUTOMATIC unified AI resolve (R6-B): fire once per DISTINCT settled input — the
           observable union of the two spec triggers (code-format miss OR registry+provider
           miss both surface here as found:false). The form stays fully editable meanwhile. */
        const q = aiQuery();
        const key = q + '|' + mktSel.value;
        if (q && key !== lastAiKey) {
          lastAiKey = key;
          runAiResolve({ auto: true });  // sets its own 「AI 判讀中…」 status
        } else {
          status.textContent = '查無報價 — 請確認代號與市場是否正確';
        }
        return;
      }
      /* found & addable (a brand-new symbol, or an archived one to restore); auto-fill
         replaces prior AUTO-fills but never a user-touched field (pristine tracking). R6-B:
         when THIS lookup is the AI-resolve re-validation (wasAiResolve), the AI already
         supplied name/sector — a sparse provider lookup (sector is blank for a brand-new
         symbol) must NOT overwrite them; fill only a field the AI left blank. */
      if (namePristine && r.name && !(wasAiResolve && nameIn.value)) nameIn.value = r.name;
      if (sectorPristine && !(wasAiResolve && sectorField.value())) {
        sectorField.setValue(r.sector || '');
      }
      if (etfPristine) etfCb.checked = !!r.is_etf;
      status.textContent = r.archived
        ? '已封存 — 確認後將還原並於背景補抓缺口'
        : '已找到，確認後加入觀察清單（報價與歷史於背景抓取）';
      setEnabled(true);
    }

    /* ---- R6-B unified AI resolve (AUTOMATIC on a lookup miss + manual retry) -------------
       aiQuery() = the settled input (symbol + name). runAiResolve() POSTs the unified
       /api/instruments/ai-resolve and dispatches on its status; the REAL lookup stays the
       sole registration authority (an unverified suggestion can never be confirmed). */
    function aiQuery() {
      return (symIn.value.trim() + ' ' + nameIn.value.trim()).trim();
    }
    function clearCandidates() {
      candBox.replaceChildren();
      candBox.style.display = 'none';
    }
    /* status:"resolved" — fill the fields (pristine-respecting) then re-run the REAL lookup
       to re-validate the filled code (also carries the board through + enables 確認 the usual
       way). The backend already provider-verified, so this lookup normally finds it. */
    function applyResolved(resp) {
      symIn.value = (resp.symbol || '').trim().toUpperCase();
      if (namePristine && resp.name) nameIn.value = resp.name;
      if (sectorPristine && resp.sector) sectorField.setValue(resp.sector);
      if (industryPristine && resp.industry) industryIn.value = resp.industry;
      aiResolveTried = true;
      runLookup();
    }
    /* status:"candidates" — 2-5 clickable rows (代號＋名稱＋產業); a click fills the fields
       and re-runs the real lookup to verify the pick. */
    function renderCandidates(list) {
      candBox.replaceChildren();
      candBox.appendChild(el('div', 'hint', 'AI 提供候選，請點選正確標的：'));
      list.slice(0, 5).forEach((c) => {
        const b = el('button', 'btn qa-cand'); b.type = 'button';
        b.style.cssText = 'display:block; width:100%; text-align:left;';
        b.appendChild(el('span', 'sym-code', c.symbol || ''));
        if (c.name) b.appendChild(el('span', null, '　' + c.name));
        if (c.sector) b.appendChild(el('span', 'hint', '　' + c.sector));
        b.addEventListener('click', () => {
          symIn.value = (c.symbol || '').trim().toUpperCase();
          if (namePristine && c.name) nameIn.value = c.name;
          if (sectorPristine && c.sector) sectorField.setValue(c.sector);
          clearCandidates();
          aiResolveTried = true;
          runLookup();
        });
        candBox.appendChild(b);
      });
      candBox.style.display = 'flex';
      status.textContent = '請從下方候選中選擇（或直接修改代號）';
      aiBtn.style.display = '';  // keep the manual retry available
    }
    /* o.auto=true (automatic fire) degrades SILENTLY (no toast) — it is not a user action;
       the manual retry (aiBtn) DOES toast on degrade. aiSeq drops a superseded reply. */
    async function runAiResolve(o) {
      o = o || {};
      const query = aiQuery();
      if (!query) { status.textContent = '請先輸入代號或名稱'; return; }
      const seq = ++aiSeq;
      clearCandidates();
      status.textContent = 'AI 判讀中…';
      const restore = window.pdBusy ? window.pdBusy(aiBtn, '判讀中…') : () => {};
      let resp;
      try {
        resp = await api.post('/api/instruments/ai-resolve',
          { query: query, market: mktSel.value });
      } catch (err) {
        restore();
        if (seq !== aiSeq) return;  // superseded by a newer AI call
        status.textContent = '查無報價 — 請確認代號與市場是否正確';
        aiBtn.style.display = '';
        if (!o.auto && window.toast) {
          window.toast(err && err.message ? err.message : 'AI 判讀失敗', 'fail', err && err.code);
        }
        return;
      }
      restore();
      if (seq !== aiSeq) return;  // superseded by a newer AI call
      const st = resp && resp.status;
      if (st === 'resolved' && resp.symbol) {
        applyResolved(resp);
      } else if (st === 'candidates' && resp.candidates && resp.candidates.length) {
        renderCandidates(resp.candidates);
      } else {
        /* not_found (or an empty candidates payload) → honest notice; entry stays unblocked. */
        status.textContent = (resp && resp.message) || '查無此標的 — 請確認名稱與市場是否正確';
        aiBtn.style.display = '';
      }
    }
    aiBtn.addEventListener('click', () => runAiResolve({ auto: false }));

    async function doRegister(after) {
      const sym = symIn.value.trim().toUpperCase();
      if (!sym) return;
      const reqBody = {
        symbol: sym, market: mktSel.value,
        name: nameIn.value.trim(), sector: sectorField.value(),
        industry: industryIn.value.trim(),  // R6: optional GICS 產業細分
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
