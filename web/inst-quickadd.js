/* portfolio-dash — shared instrument add/edit dialog (FU-D23; FU-D42 editable symbol +
   auto-lookup + AI resolve fallback; Wave A1 unified add + edit + one AI action).

   window.pdInstQuickAdd(opts) is ONE modal builder serving BOTH flows:

   • ADD mode (default — the register flow, UNCHANGED contract):
     1. runs GET /api/instruments/lookup — fast identify (name + suggested sector +
        board/is_etf); found:false is the typo guard and blocks the confirm. The lookup
        fires automatically on open (prefilled symbol) AND re-fires (debounced) whenever
        the user edits the symbol or market; auto-fills only overwrite fields the user
        has not touched (pristine tracking, FU-D42a),
     2. lets the user confirm the name + pick/enter a sector (canonical select + free text),
     3. one UNIFIED 「AI 辨識」 action (Wave A1) — POST /api/instruments/ai-resolve maps the raw
        input + target market to the LOCAL exchange code + name + GICS sector (+ optional
        industry) in ONE reply, applied through ONE path that ALWAYS fills 代號/名稱/產業/產業細分
        (this is why the old 產業-not-filled bug is gone). It fires AUTOMATICALLY on a lookup
        miss and is also a manual button; status:"resolved" auto-fills then re-runs the real
        lookup to re-validate, status:"candidates" renders clickable rows, status:"not_found"
        shows 「查無此標的」. The real lookup stays the sole registration authority,
     4. registers via POST /api/instruments — the heavy quote/history fetch runs in the
        BACKGROUND (BackgroundTasks), so the confirm returns fast,
     then calls opts.onConfirm(result) (確認) or opts.onBuy(result) (記一筆買入).

   • EDIT mode (opts.mode === 'edit' — folds in the old instruments.js edit modal):
     locks 代號 + 市場 (read-only display), shows the edit-only fields (目標價下限/上限 + TW 板別),
     keeps a small 「重新偵測產業」 sector-only re-detect (same applyValue path), DROPS 記一筆買入,
     and saves via PUT /api/instruments/{symbol} → opts.onSaved(result). No lookup / code-resolve
     runs (the symbol is a known, registered instrument).

   opts (ADD — the cross-agent contract, do NOT break):
     symbol      prefill symbol (all call sites prefill it)
     market      'TW' | 'US' | 'MY' (default 'TW')
     lockSymbol  RETIRED (R8.1 Wave B, A3/F9c) — the symbol field is ALWAYS editable, so a wrong
                 AI-parsed symbol (owner bug: 聯電 → "UMC" on a TW row) can be fixed in place; a
                 locked field re-introduces that dead-end, so honoring it would be wrong. The
                 param is no longer passed by this project's own caller (web/instruments.js). It
                 stays tolerated-and-ignored here ONLY until web/input.js's two callers are
                 cleaned up (Wave C); do NOT re-introduce it. No call site genuinely needs it.
     onConfirm(result)  after 確認 registers
     onBuy(result)      after 記一筆買入 registers (the caller navigates to the manual pane)
   opts (EDIT — additive):
     mode:'edit', symbol, market, name, sector, industry, board, is_etf, ccy,
     target_low, target_high, onSaved(result)
   result = the POST /api/instruments (add) or PUT /api/instruments/{symbol} (edit) response
   element (+ restored / last_price_date when a soft-deleted symbol was RESTORED, add only).

   No money is computed here (the dialog only submits metadata; prices come from the API).
   Styling reuses the shared .modal-* / .field / .btn classes + the Wave A1 .qa-* rules. */
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

  /* ---- FU-D31 / R6 / Wave A1: shared canonical-sector field (used by BOTH modes of this
     dialog via window.pdSectorField). A <select> of the canonical GICS vocabulary (dual-text
     labels 「Information Technology（資訊科技）」) + the current off-vocabulary value preserved as
     an extra option (so editing an unmigrated row never destroys it) + an 「其他…」 escape (free
     text). The sector-only AI re-detect button is OPT-IN (opts.aiDetect) — only the EDIT form
     shows it (「重新偵測產業」); it calls the UNIFIED POST /api/instruments/ai-resolve with
     sector_only=true and applies ONLY sector (+ industry via opts.setIndustry), never
     symbol/name. The frontend never hardcodes the vocabulary; the read-time donut/alert
     canonicalization is a backend concern (portfolio/dashboard.py). ---- */
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
    const sel = el('select', 'select sector-select');
    sel.style.flex = '1';
    row.appendChild(sel);
    /* Wave A1: the sector-only AI re-detect button is OPT-IN (opts.aiDetect). The ADD dialog no
       longer shows it — the unified 「AI 辨識」 action fills the sector through ONE path (which
       killed the owner's 產業-not-filled bug). Only the EDIT form keeps a small 「重新偵測產業」
       affordance, and it still routes the returned sector through the SAME applyValue path. */
    let aiBtn = null;
    if (opts.aiDetect) {
      aiBtn = el('button', 'btn sector-ai-btn', opts.aiLabel || 'AI 偵測'); aiBtn.type = 'button';
      aiBtn.title = '以 AI 依代號／名稱／市場重新判斷產業類別';
      row.appendChild(aiBtn);
    }
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
    /* The re-detect button exists only when opts.aiDetect (EDIT form). It routes any returned
       sector through applyValue — NEVER a dead-end note-only branch (the Wave A1 bug fix). */
    if (aiBtn) aiBtn.addEventListener('click', async () => {
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
      /* resolved → sector (+ industry); candidates → the first candidate's sector. Whatever the
         status, a returned sector ALWAYS reaches applyValue below (never the dead-end else). */
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
  /* Expose the shared field so web/instruments.js (edit caller) reuses it (loaded after). */
  window.pdSectorField = pdSectorField;

  window.pdInstQuickAdd = function (opts) {
    opts = opts || {};
    if (!api) return;
    /* Wave A1: ONE modal builder for BOTH flows. mode:'edit' turns this into the instrument
       editor (locked 代號/市場, edit-only 目標價 + TW 板別, PUT save, no 記一筆買入); the DEFAULT
       (add) mode is byte-for-byte the prior register flow, so the cross-agent add caller shape
       {symbol, market, onConfirm, onBuy} is unchanged (lockSymbol is RETIRED — see the header). */
    const isEdit = opts.mode === 'edit';

    const backdrop = el('div', 'modal-backdrop');
    const modal = el('div', 'modal');

    const head = el('div', 'modal-head');
    head.appendChild(el('h3', 'modal-title',
      isEdit
        ? ('編輯標的 — ' + (opts.symbol || '') + (opts.name ? ' ' + opts.name : ''))
        : '加入觀察清單'));
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

    /* 代號 — ADD: always editable (FU-D42a), every edit re-runs the real lookup below.
       EDIT: locked read-only (代號 identifies the row; market/ccy are fixed at registration). */
    const symIn = el('input', 'input qa-symbol');
    symIn.value = (opts.symbol || '').trim().toUpperCase();
    symIn.spellcheck = false;
    symIn.placeholder = '例：2330、AAPL';
    if (isEdit) { symIn.readOnly = true; symIn.classList.add('qa-readonly'); }
    body.appendChild(fld('代號', symIn));

    const mktSel = el('select', 'select');
    MARKETS.forEach(([v, label]) => {
      const o = el('option', null, label); o.value = v;
      if ((opts.market || 'TW') === v) o.selected = true;
      mktSel.appendChild(o);
    });
    if (isEdit) { mktSel.disabled = true; mktSel.classList.add('qa-readonly'); }
    body.appendChild(fld('市場', mktSel));

    const nameIn = el('input', 'input qa-name');
    nameIn.placeholder = '名稱（自動查詢，可修改）';
    nameIn.spellcheck = false;
    if (isEdit) nameIn.value = opts.name || '';
    body.appendChild(fld('名稱', nameIn));

    /* R6: optional GICS 產業細分 — AI-populated, editable, submitted on register/save. Declared
       before the sector field so its setIndustry closure can fill it. */
    const industryIn = el('input', 'input qa-industry');
    industryIn.placeholder = '例：Semiconductors（AI 可自動填入，可修改）';
    industryIn.spellcheck = false;
    if (isEdit) industryIn.value = opts.industry || '';

    const sectorField = pdSectorField({
      current: isEdit ? (opts.sector || '') : '',
      symbol: () => symIn.value,
      name: () => nameIn.value,
      market: () => mktSel.value,
      /* EDIT: the 「重新偵測產業」 re-detect fills industry unconditionally (explicit user act);
         ADD: only fill a pristine (untouched) industry so an AI re-fill never clobbers input. */
      setIndustry: (v) => { if (isEdit || industryPristine) industryIn.value = v; },
      /* EDIT-only sector-only re-detect (routes through applyValue — never a dead end); the ADD
         dialog omits it (the unified 「AI 辨識」 action fills the sector). */
      aiDetect: isEdit,
      aiLabel: '重新偵測產業',
    });
    body.appendChild(fld('產業', sectorField.element));
    body.appendChild(fld('產業細分（選填）', industryIn));

    const etfWrap = el('label', 'hint');
    const etfCb = el('input'); etfCb.type = 'checkbox';
    if (isEdit) etfCb.checked = !!opts.is_etf;
    etfWrap.appendChild(etfCb);
    etfWrap.appendChild(el('span', null, ' 此標的為 ETF（影響台股賣出稅率 0.1%）'));
    body.appendChild(fld('類別', etfWrap));

    /* EDIT-only fields: TW 板別 + 目標價下限/上限 (the per-symbol target_cross thresholds). */
    let boardSel = null;
    let tgtLowIn = null;
    let tgtHiIn = null;
    if (isEdit) {
      if (mktSel.value === 'TW') {
        boardSel = el('select', 'select');
        [['TWSE', 'TWSE 上市'], ['TPEx', 'TPEx 上櫃']].forEach(([v, label]) => {
          const o = el('option', null, label); o.value = v;
          if (opts.board === v) o.selected = true;
          boardSel.appendChild(o);
        });
        body.appendChild(fld('板別', boardSel));
      }
      const ccy = opts.ccy || '';
      const tgtStep = ccy === 'MYR' ? '0.001' : '0.01';
      tgtLowIn = el('input', 'input');
      tgtLowIn.id = 'edit-target-low';
      tgtLowIn.type = 'number'; tgtLowIn.min = '0'; tgtLowIn.step = tgtStep;
      tgtLowIn.placeholder = '留空 = 不提醒';
      if (opts.target_low !== null && opts.target_low !== undefined) tgtLowIn.value = opts.target_low;
      body.appendChild(fld('目標價下限（現價 ≤ 此值時提醒，' + ccy + '）', tgtLowIn));
      tgtHiIn = el('input', 'input');
      tgtHiIn.id = 'edit-target-high';
      tgtHiIn.type = 'number'; tgtHiIn.min = '0'; tgtHiIn.step = tgtStep;
      tgtHiIn.placeholder = '留空 = 不提醒';
      if (opts.target_high !== null && opts.target_high !== undefined) tgtHiIn.value = opts.target_high;
      body.appendChild(fld('目標價上限（現價 ≥ 此值時提醒，' + ccy + '）', tgtHiIn));
      body.appendChild(el('div', 'hint', '市場與幣別由註冊流程決定，不可更改。'));
    }

    /* ADD-only: lookup status + the unified 「AI 辨識」 action + AI candidate rows. The nodes are
       created for both modes (the ADD helpers close over them) but only APPENDED in add mode. */
    const status = el('div', 'hint');
    status.style.cssText = 'min-height:16px;';
    /* Wave A1: ONE unified AI action (renamed from the old 「AI 判讀代號」). Always visible in add
       mode — it fires automatically on a lookup miss AND is a manual re-identify/retry. */
    const aiBtn = el('button', 'btn qa-ai-resolve', 'AI 辨識'); aiBtn.type = 'button';
    aiBtn.title = '以 AI 依輸入的代號／名稱／市場辨識正確代號、名稱與產業，再以真實報價查核';
    const candBox = el('div', 'qa-candidates');
    candBox.hidden = true;
    if (!isEdit) {
      body.appendChild(status);
      body.appendChild(aiBtn);
      body.appendChild(candBox);
    }
    modal.appendChild(body);

    const foot = el('div', 'modal-foot');
    /* ADD footer: 記一筆買入 + 確認. EDIT footer: 取消 + 儲存 (記一筆買入 dropped). */
    let buyBtn = null;
    if (isEdit) {
      const cancelBtn = el('button', 'btn', '取消'); cancelBtn.type = 'button';
      cancelBtn.addEventListener('click', () => { if (!busy) dismiss(); });
      foot.appendChild(cancelBtn);
    } else {
      buyBtn = el('button', 'btn', '記一筆買入'); buyBtn.type = 'button';
      buyBtn.title = '加入後直接前往手動交易，並帶入此代號';
      foot.appendChild(buyBtn);
    }
    const okBtn = el('button', 'btn btn-primary', isEdit ? '儲存' : '確認'); okBtn.type = 'button';
    foot.appendChild(okBtn);
    modal.appendChild(foot);
    backdrop.appendChild(modal);

    /* Latest lookup result — board rides through to the POST so TW is not re-probed. */
    let lookupState = { found: false, registered: false, archived: false, board: null };
    let busy = false;
    /* FU-D42a pristine tracking (ADD): a lookup auto-fill only overwrites a field the USER has
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

    function setEnabled(ok) { okBtn.disabled = !ok; if (buyBtn) buyBtn.disabled = !ok; }
    /* EDIT: 儲存 is enabled immediately (nothing to look up). ADD: disabled until a lookup finds. */
    setEnabled(isEdit);

    const dismiss = () => {
      document.removeEventListener('keydown', onKey);
      backdrop.remove();
    };
    const onKey = (e) => { if (e.key === 'Escape' && !busy) dismiss(); };
    document.addEventListener('keydown', onKey);
    close.addEventListener('click', () => { if (!busy) dismiss(); });
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop && !busy) dismiss(); });

    document.body.appendChild(backdrop);

    if (!isEdit) {
      // -------------------- ADD mode: lookup + unified AI resolve + register --------------------
      const runLookup = async function () {
        const seq = ++lookupSeq;
        const wasAiResolve = aiResolveTried;
        aiResolveTried = false;  // consumed by THIS run only
        const sym = symIn.value.trim().toUpperCase();
        setEnabled(false);
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
          clearCandidates();
          if (wasAiResolve) {
            /* Fix #1 (owner bug #4 — the candidate-pick wipe): an AI-sourced fill (a picked
               candidate OR applyResolved) is TRUSTED. The backend provider-verifies only the
               PRIMARY symbol, so a picked candidate's live re-validation quote commonly MISSES —
               but the AI already supplied a trusted 名稱/產業, so a miss must NOT wipe them
               (the old code cleared name+sector here BEFORE this branch ran, blanking the just
               filled data). KEEP every field. A6 (no dead-end): when 代號+名稱+產業 are all
               present the user can still register — POST /api/instruments force-registers a
               quote-less symbol — so 確認 is ENABLED. Only when the fill is incomplete do we fall
               back to the honest blocked notice. */
            const canProceed = !!(sym && nameIn.value.trim() && sectorField.value());
            if (canProceed) {
              status.textContent = '報價暫無 — 已保留 AI 判讀名稱/產業，請確認代號/市場';
              setEnabled(true);
            } else {
              status.textContent = 'AI 判讀後仍查無報價 — 請確認代號與市場是否正確';
            }
            return;
          }
          /* Genuine stale-suggestion case (a USER symbol/market edit that no longer resolves):
             clear the prior AUTO-filled suggestions — pristine only, so a user-typed field is
             never touched. A5: the wipe is now SYMMETRIC across 名稱/產業/產業細分 (industry was
             previously left stale on a miss). */
          if (namePristine) nameIn.value = '';
          if (sectorPristine) sectorField.setValue('');
          if (industryPristine) industryIn.value = '';
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
      };

      /* ---- R6-B / Wave A1 unified AI resolve (AUTOMATIC on a lookup miss + manual action) -----
         aiQuery() = the settled input (symbol + name). runAiResolve() POSTs the unified
         /api/instruments/ai-resolve and dispatches on its status; the REAL lookup stays the
         sole registration authority (an unverified suggestion can never be confirmed). */
      function aiQuery() {
        return (symIn.value.trim() + ' ' + nameIn.value.trim()).trim();
      }
      /* A2 dedup: mark the CURRENT settled form state as already AI-resolved so a later NON-AI
         re-lookup MISS on the same state never redundantly re-fires the automatic resolver.
         Called (a) when any resolve STARTS — including the manual 「AI 辨識」 button, which never
         seeded lastAiKey before, so a following miss re-paid the LLM for an input the user
         already resolved — and (b) POST-fill in applyResolved + a candidate pick, so the
         re-validation lookup and any later identical-state miss are recognized as done. */
      function markAiKey() { lastAiKey = aiQuery() + '|' + mktSel.value; }
      function clearCandidates() {
        candBox.replaceChildren();
        candBox.hidden = true;
      }
      /* status:"resolved" — fill EVERY field (代號/名稱/產業/產業細分, pristine-respecting) then
         re-run the REAL lookup to re-validate the filled code (carries the board through + enables
         確認). The sector ALWAYS goes through sectorField.setValue — the one path that fixed the
         old 產業-not-filled bug. The backend already provider-verified, so the lookup finds it. */
      function applyResolved(resp) {
        symIn.value = (resp.symbol || '').trim().toUpperCase();
        if (namePristine && resp.name) nameIn.value = resp.name;
        if (sectorPristine && resp.sector) sectorField.setValue(resp.sector);
        if (industryPristine && resp.industry) industryIn.value = resp.industry;
        markAiKey();  // A2: the filled state is now AI-resolved — a re-validation miss won't re-fire
        aiResolveTried = true;
        runLookup();
      }
      /* status:"candidates" — 2-5 clickable rows (代號＋名稱＋產業); a click fills the fields
         (sector via setValue too) and re-runs the real lookup to verify the pick. */
      function renderCandidates(list) {
        candBox.replaceChildren();
        candBox.appendChild(el('div', 'hint', 'AI 提供候選，請點選正確標的：'));
        list.slice(0, 5).forEach((c) => {
          const b = el('button', 'btn qa-cand'); b.type = 'button';
          b.appendChild(el('span', 'sym-code', c.symbol || ''));
          if (c.name) b.appendChild(el('span', 'qa-cand-name', c.name));
          if (c.sector) b.appendChild(el('span', 'qa-cand-sector', c.sector));
          b.addEventListener('click', () => {
            /* Fix #2(a): a candidate the user EXPLICITLY picked is trusted. We still run the real
               lookup (it enriches board / detects already-registered / archived), but per Fix #1
               a re-validation MISS no longer wipes these fields nor blocks 確認. */
            symIn.value = (c.symbol || '').trim().toUpperCase();
            if (namePristine && c.name) nameIn.value = c.name;
            if (sectorPristine && c.sector) sectorField.setValue(c.sector);
            clearCandidates();
            markAiKey();  // A2: the picked state is AI-resolved — no redundant auto re-fire
            aiResolveTried = true;
            runLookup();
          });
          candBox.appendChild(b);
        });
        candBox.hidden = false;
        status.textContent = '請從下方候選中選擇（或直接修改代號）';
      }
      /* o.auto=true (automatic fire) degrades SILENTLY (no toast) — it is not a user action;
         the manual action DOES toast on degrade. aiSeq drops a superseded reply. */
      async function runAiResolve(o) {
        o = o || {};
        const query = aiQuery();
        if (!query) { status.textContent = '請先輸入代號或名稱'; return; }
        /* A2: seed the dedup key on ANY resolve start, INCLUDING the manual 「AI 辨識」 button
           (the automatic path also seeds it at the fire guard; the manual button never did, so a
           following non-AI miss re-fired the LLM for an already-resolved input). */
        markAiKey();
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
        }
      }

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
      aiBtn.addEventListener('click', () => runAiResolve({ auto: false }));
      mktSel.addEventListener('change', runLookup);
      /* FU-D42a: editing the symbol re-runs the lookup (debounced; always attached — the
         symbol is always editable). The seq guard in runLookup drops superseded replies. */
      let t = null;
      symIn.addEventListener('input', () => {
        if (t) clearTimeout(t);
        t = setTimeout(runLookup, 300);
      });

      runLookup();  // FU-D42b: auto-lookup on open (prefilled symbol identifies immediately)
    } else {
      // -------------------- EDIT mode: PUT save (no lookup / code resolve) ----------------------
      const doSave = async function () {
        const rawLow = tgtLowIn ? tgtLowIn.value.trim() : '';
        const rawHi = tgtHiIn ? tgtHiIn.value.trim() : '';
        /* target_low / target_high ride through as STRINGS (never parseFloat'd into money).
           Empty clears the bound (explicit null); otherwise the raw string reaches the backend
           Decimal column verbatim. */
        const body2 = {
          name: nameIn.value.trim() || (opts.name || ''),
          sector: sectorField.value(),
          industry: industryIn.value.trim() || null,  // R6: '' clears (exclude_unset ⇒ set)
          is_etf: etfCb.checked,
          target_low: rawLow === '' ? null : rawLow,
          target_high: rawHi === '' ? null : rawHi,
        };
        if (boardSel) body2.board = boardSel.value;
        busy = true;
        const restore = window.pdBusy ? window.pdBusy(okBtn, '儲存中…') : () => {};
        let resp;
        try {
          resp = await api.put('/api/instruments/' + encodeURIComponent(opts.symbol), body2);
        } catch (err) {
          busy = false;
          restore();
          if (window.toast) {
            window.toast(err && err.message ? err.message : '儲存失敗', 'fail', err && err.code);
          }
          return;
        }
        busy = false;
        restore();
        dismiss();
        if (window.toast) window.toast('已儲存', 'ok', (opts.symbol || '') + ' 已更新');
        if (opts.onSaved) opts.onSaved(resp);
      };
      okBtn.addEventListener('click', doSave);
    }

    setTimeout(() => { (isEdit ? nameIn : (symIn.value ? nameIn : symIn)).focus(); }, 50);
  };
})();
