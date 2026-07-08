/* portfolio-dash — 設定 · AI 提示詞 (system prompt + vars wired to /api/*, spec 19/06/20).
   定案模型（2026-06-12）：
   - 策略提示詞 = 純設計物件（無排程、無校正掛載），搭配數據變數系統組裝，
     可「預覽提示詞」（POST /api/prompts/preview，真實計算值、不呼叫 LLM）與
     「測試送出」（POST /api/prompts/test，走真實 LiteLLM、費用照記、422/402 錯誤照拋）。
   - 刪除策略 → 檢查組合器引用：被引用則阻擋；未引用則封存（軟刪除）。
   - 洞察類型組合器 = 排程與自我校正的唯一掛載點：系統(可選)＋1..n 策略＋自我校正開關。
     啟動排程 → 週期設定表（與排程工作表共用）→ 寫入排程工作表；刪除組合同步刪排程。
   - 校正提示詞 1:1 掛組合；版本管理器：active=手動選定版，active≠最新版時最新版自動影子評估；
     版本封存制（軟刪除），歸因鏈永不斷。校正產生與回測評分由「AI 大師模型」執行。

   WIRED (Task 2.7b): the global system prompt loads from GET /api/system-prompt and
   saves via PUT /api/system-prompt; the variable registry loads from GET /api/prompt-vars
   (via PD_VARS.load(), with per-var tier-greyout); preview/test hit the real /api/prompts/*
   endpoints. The former window.PROMPTS_DATA + PD_VARS inline mocks are RETIRED. The
   strategy cards / composer / calibration chains remain DESIGN-STAGE objects (no /api/*
   backing yet) — kept inline as local consts, NOT a window global. */

(function () {
  'use strict';
  const V = window.PD_VARS;
  const api = window.pdApi;
  const f = window.fmt;

  /* Strategies load from GET /api/strategy-prompts on boot (wired 2026-07-05 — the
     inline DESIGN-STAGE seeds are retired; the composer/calibration mocks further
     below are still design-stage). */
  const D = {
    /* system_prompt + system_updated_at are filled from GET /api/system-prompt on boot. */
    system_prompt: '',
    system_updated_at: '',
    strategies: [],
  };
  function _toast(msg, kind, code) { if (window.toast) window.toast(msg, kind, code); }
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  /* ================= 共用 modal ================= */
  function openModal(title, buildBody, wide) {
    const back = el('div', 'pv-backdrop');
    const box = el('div', 'pv-box' + (wide ? ' wide' : ''));
    const head = el('div', 'pv-head');
    head.appendChild(el('span', 'pv-title', title));
    const x = el('button', 'sd-close', '✕');
    x.type = 'button';
    x.addEventListener('click', () => back.remove());
    head.appendChild(x);
    box.appendChild(head);
    const body = el('div', 'pv-body');
    box.appendChild(body);
    buildBody(body, () => back.remove());
    back.appendChild(box);
    back.addEventListener('click', (e) => { if (e.target === back) back.remove(); });
    document.body.appendChild(back);
    return back;
  }

  /* 持倉代號（預覽/測試送出時的代入標的選單；正式執行由任務範圍逐檔代入）。 */
  const HELD_SYMBOLS = ['2330', '0056', '00919', 'AAPL', 'MSFT', 'NVDA', '1155.KL'];
  /* ===== async boot: load the variable registry (GET /api/prompt-vars, populates V in
     place) + the global system prompt (GET /api/system-prompt), THEN build the page.
     Graceful: a fetch failure surfaces ONE toast and falls through with whatever loaded
     so the page still renders (never an unhandled rejection — the e2e smoke asserts ZERO
     console errors). Everything below runs inside boot() so V.CATEGORIES is populated. ===== */
  async function boot() {
    try {
      await V.load();              // populates V.CATEGORIES / index from /api/prompt-vars
    } catch (err) {
      _toast('變數總表載入失敗', 'fail', (err && err.message) || undefined);
    }

  /* ================= 系統提示詞 (GET /api/system-prompt) ================= */
  try {
    const sp = await api.get('/api/system-prompt');
    D.system_prompt = (sp && sp.body) || '';
    D.system_updated_at = (sp && sp.updated_at) || '';
  } catch (err) {
    _toast('系統提示詞載入失敗', 'fail', (err && err.message) || undefined);
  }
  try {
    D.strategies = (await api.get('/api/strategy-prompts')) || [];
  } catch (err) {
    _toast('策略提示詞載入失敗', 'fail', (err && err.message) || undefined);
  }
  $('#sys-prompt').value = D.system_prompt;
  $('#sys-prompt-meta').textContent =
    '更新 ' + (D.system_updated_at ? f.date(D.system_updated_at) : '—') + '・套用於所有策略提示詞之前';
  /* save the global system prompt -> PUT /api/system-prompt (toast + restamp meta). */
  const sysSave = document.getElementById('prompts-save');

  /* ================= 策略提示詞卡 ================= */
  const list = $('#tpl-list');

  const hasPerSymbolVars = (text) => V.tokensIn(text).some((tk) => {
    const v = V.find(tk);
    return v && v.scope === '單一標的';
  });
  /* per_symbol 變數的代入標的選擇器（正式執行時由組合範圍逐檔代入，這裡僅供預覽/測試指定） */
  function symbolPicker(onChange) {
    const row = el('div', 'pv-field');
    row.appendChild(el('label', null, '代入標的（本策略含「單一標的」變數）'));
    const sel = el('select', 'select');
    HELD_SYMBOLS.forEach((s) => {
      const o = el('option', null, s);
      o.value = s;
      sel.appendChild(o);
    });
    sel.addEventListener('change', () => onChange(sel.value));
    row.appendChild(sel);
    return { row, sel };
  }

  /* 插入變數輔助列（策略卡與新增策略共用） */
  function varInsertRow(ta) {
    const varRow = el('div', 'tpl-vars');
    varRow.appendChild(el('span', null, '插入變數：'));
    const sel = el('select', 'select tpl-var-sel');
    const opt0 = el('option', null, '選擇數據變數…');
    opt0.value = '';
    sel.appendChild(opt0);
    V.CATEGORIES.forEach((cat) => {
      const og = document.createElement('optgroup');
      og.label = cat.name + (cat.source === 'ingest' ? '（需後端新增）' : '');
      cat.vars.forEach((v) => {
        /* tier-greyout (spec 20.15): a var whose required tier is unavailable is shown
           but DISABLED, with the tier label appended so the user sees why. */
        const tierOk = v.tier_ok !== false;
        const suffix = tierOk ? '' : '（' + (v.tier_label || '方案受限') + '）';
        const o = el('option', null, v.name + '  {{' + v.token + '}}' + suffix);
        o.value = v.token;
        if (!tierOk) o.disabled = true;
        og.appendChild(o);
      });
      sel.appendChild(og);
    });
    sel.addEventListener('change', () => {
      if (!sel.value) return;
      const ins = '{{' + sel.value + '}}';
      const pos = ta.selectionStart || ta.value.length;
      ta.value = ta.value.slice(0, pos) + ins + ta.value.slice(pos);
      sel.value = '';
      ta.focus();
    });
    varRow.appendChild(sel);
    const catLink = el('button', 'btn-link', '變數總表 ↓');
    catLink.type = 'button';
    catLink.addEventListener('click', () => {
      const p = document.getElementById('vars-panel');
      if (p) {
        p.open = true;
        const top = p.getBoundingClientRect().top + (document.scrollingElement ? document.scrollingElement.scrollTop : 0) - 60;
        window.scrollTo({ top: top, behavior: 'smooth' });
      }
    });
    varRow.appendChild(catLink);
    return varRow;
  }

  /* scope for a prompt body: per_symbol if it references any 單一標的 variable, else portfolio. */
  const scopeOf = (text) => (hasPerSymbolVars(text) ? 'per_symbol' : 'portfolio');

  /* 預覽提示詞 — POST /api/prompts/preview (always 200, real computed values, NO LLM call). */
  function previewPrompt(t, ta) {
    openModal('預覽提示詞 — ' + t.name, (body) => {
      body.appendChild(el('div', 'pv-note',
        '變數已代入目前快照的真實計算值（不呼叫 LLM，零成本）。實際送出 = 系統提示詞 ＋ 本策略；組合器執行時再附加組合的生效校正提示詞。'));
      const out = el('div'); /* re-rendered on each fetch (symbol change) */
      let sym = hasPerSymbolVars(ta.value) ? HELD_SYMBOLS[0] : null;

      const fetchAndRender = async () => {
        out.replaceChildren(el('div', 'pv-testing', '⏳ 載入預覽…'));
        try {
          const resp = await api.post('/api/prompts/preview',
            { body: ta.value, scope: scopeOf(ta.value), symbol: sym });
          out.replaceChildren();
          const sys = el('div', 'pv-section');
          sys.appendChild(el('div', 'pv-sec-label', '系統提示詞'));
          sys.appendChild(el('pre', 'pv-pre', (resp && resp.system_prompt) || ''));
          out.appendChild(sys);
          const st = el('div', 'pv-section');
          st.appendChild(el('div', 'pv-sec-label', '策略提示詞（變數代入後）'));
          st.appendChild(el('pre', 'pv-pre pv-rendered', (resp && resp.rendered) || ''));
          out.appendChild(st);
          /* token-count chip (tokens_used / est_tokens are plain JSON numbers). */
          const meta = el('div', 'pv-cost num',
            '代入變數 ' + ((resp && resp.tokens_used) || 0) + ' 個・估算 ' +
            f.num((resp && resp.est_tokens) || 0) + ' tokens');
          out.appendChild(meta);
          /* unknown / scope-violation diagnostics (preview lists them, never blocks). */
          const unknown = (resp && resp.unknown_tokens) || [];
          const violations = (resp && resp.scope_violations) || [];
          if (unknown.length || violations.length) {
            const warn = el('div', 'pv-toklist');
            warn.appendChild(el('span', 'pv-sec-label',
              '⚠ 送出時會被擋下（unknown：' + unknown.length + '・範圍不符：' + violations.length + '）'));
            unknown.forEach((tk) => {
              const chip = el('code', 'pv-tok bad', '{{' + tk + '}}');
              chip.title = '未知變數';
              warn.appendChild(chip);
            });
            violations.forEach((tk) => {
              const chip = el('code', 'pv-tok bad', '{{' + tk + '}}');
              chip.title = '單一標的變數用於全組合範圍';
              warn.appendChild(chip);
            });
            out.appendChild(warn);
          }
        } catch (err) {
          out.replaceChildren(el('div', 'pv-note', '預覽載入失敗：' + ((err && err.message) || '')));
          _toast((err && err.message) || '預覽載入失敗', 'fail', err && err.code);
        }
      };

      if (hasPerSymbolVars(ta.value)) {
        const pk = symbolPicker((v) => { sym = v; fetchAndRender(); });
        const note = el('div', 'pv-note',
          '正式排程執行時不需選 — 組合範圍為「單一標的」時，系統自動對每檔持倉跑一次並逐檔代入。');
        const wrap = el('div', 'pv-fields');
        wrap.appendChild(pk.row);
        body.appendChild(wrap);
        body.appendChild(note);
      }
      body.appendChild(out);
      fetchAndRender();
    }, true);
  }

  /* 測試送出 — POST /api/prompts/test (real LiteLLM; 422 bad tokens, 402 budget, 409 role).
     cost_usd / quota_remaining arrive as Decimal STRINGS -> rendered via window.fmt only. */
  function testSend(t, ta) {
    openModal('測試送出 — ' + t.name, (body) => {
      body.appendChild(el('div', 'pv-note',
        '以目前快照資料組裝（系統＋本策略），經真實 LiteLLM 送至 Default 模型分析；費用照記入 llm_usage 與額度。不寫入洞察卡。'));
      const out = el('div'); /* re-rendered per run */
      const run = async (sym) => {
        out.replaceChildren(
          el('div', 'pv-testing', '⏳ 送出中…（via LiteLLM' + (sym ? '・代入 ' + sym : '') + '）'));
        try {
          const resp = await api.post('/api/prompts/test',
            { body: ta.value, scope: scopeOf(ta.value), symbol: sym });
          out.replaceChildren();
          const res = el('div', 'pv-section');
          res.appendChild(el('div', 'pv-sec-label',
            '回傳洞察（' + ((resp && resp.model) || 'LLM') + (sym ? '・' + sym : '') + '）'));
          res.appendChild(el('pre', 'pv-pre', (resp && resp.reply) || ''));
          out.appendChild(res);
          /* tokens are plain numbers; cost_usd / quota_remaining are Decimal STRINGS. */
          const cost = el('div', 'pv-cost num',
            '消耗：' + f.num((resp && resp.tokens_in) || 0) + ' tokens in / ' +
            f.num((resp && resp.tokens_out) || 0) + ' out・$' + f.num((resp && resp.cost_usd) || '0', 4) +
            '・已記入額度（剩餘 $' + f.num((resp && resp.quota_remaining) || '0', 2) + '）');
          out.appendChild(cost);
          _toast('測試完成', 'ok',
            t.name + '：費用 $' + f.num((resp && resp.cost_usd) || '0', 4) + ' 已記入 llm_usage');
        } catch (err) {
          out.replaceChildren();
          const note = el('div', 'pv-note', '測試失敗：' + ((err && err.message) || ''));
          out.appendChild(note);
          /* 422 carries per-token issues (unknown_token / scope_violation). */
          const issues = (err && err.issues) || [];
          if (issues.length) {
            const tl = el('div', 'pv-toklist');
            issues.forEach((iss) => {
              const tk = iss && iss.token ? iss.token : '';
              const chip = el('code', 'pv-tok bad', '{{' + tk + '}}');
              chip.title = iss && iss.code === 'scope_violation'
                ? '單一標的變數用於全組合範圍' : '未知變數';
              tl.appendChild(chip);
            });
            out.appendChild(tl);
          }
          _toast((err && err.message) || '測試失敗', 'fail', err && err.code);
        }
      };
      if (hasPerSymbolVars(ta.value)) {
        let sym = HELD_SYMBOLS[0];
        const pk = symbolPicker((v) => { sym = v; run(sym); });
        const wrap = el('div', 'pv-fields');
        wrap.appendChild(pk.row);
        body.appendChild(wrap);
        body.appendChild(out);
        run(sym);
      } else {
        body.appendChild(out);
        run(null);
      }
    }, true);
  }

  function deleteStrategy(t, card) {
    window.confirmDialog({
      title: '封存策略 — ' + t.name,
      body: '封存後從可選清單移除；仍被洞察任務引用時後端會阻擋。歷史洞察仍可反查內文（軟刪除）。',
      confirmLabel: '確認封存', danger: true,
      onConfirm: async () => {
        try {
          await api.del('/api/strategy-prompts/' + t.id);
        } catch (err) {
          _toast((err && err.message) || '無法封存', 'fail', err && err.code);
          return;
        }
        t.archived = true;
        card.remove();
        window.toast('已封存', 'ok', t.name + ' 已移出可選清單');
      }
    });
  }

  function addStrategyCard(t) {
    const card = el('div', 'tpl-card');
    const head = el('div', 'tpl-head');
    head.appendChild(el('span', 'tpl-name', t.name));
    const perSym = hasPerSymbolVars(t.body);
    const scopeBadge = el('span', 'tpl-scope ' + (perSym ? 'scope-sym' : 'scope-pf'), perSym ? '單一標的' : '全組合');
    scopeBadge.title = perSym
      ? '含「單一標的」變數 — 只能被範圍為「單一標的」的洞察類型引用'
      : '僅使用全組合變數 — 任何範圍的洞察類型皆可引用';
    head.appendChild(scopeBadge);
    head.appendChild(el('span', 'tpl-meta', '更新 ' + f.date(t.updated_at)));
    const right = el('span', 'right');
    if (!t.enabled) right.appendChild(el('span', 'pill pill-off', '停用'));
    const tg = el('button', 'toggle' + (t.enabled ? ' on' : ''));
    tg.type = 'button';
    tg.setAttribute('role', 'switch');
    tg.title = '啟用/停用此策略：停用後，引用它的洞察類型下次執行時跳過此策略段（其餘策略照常），新組合也不可選用；不影響歷史洞察';
    tg.addEventListener('click', async () => {
      const on = !tg.classList.contains('on');
      try {
        await api.put('/api/strategy-prompts/' + t.id,
          { name: t.name, body: t.body, enabled: on });
      } catch (err) {
        _toast((err && err.message) || '切換失敗', 'fail', err && err.code);
        return;
      }
      t.enabled = on;
      tg.classList.toggle('on', on);
      window.toast(on ? '策略已啟用' : '策略已停用', 'ok',
        on ? t.name + '：引用此策略的洞察任務恢復執行此段'
           : t.name + '：引用此策略的洞察任務將跳過此段，新組合不可選用');
    });
    right.appendChild(tg);
    head.appendChild(right);
    card.appendChild(head);

    const body = el('div', 'tpl-body');
    const ta = el('textarea', 'input');
    ta.rows = 4;
    ta.value = t.body;
    body.appendChild(ta);

    /* 插入變數（讀取數據變數總表） */
    body.appendChild(varInsertRow(ta));

    const actions = el('div', 'tpl-actions');
    const mkBtn = (label, cls, fn, title) => {
      const b = el('button', 'btn' + (cls ? ' ' + cls : ''), label);
      b.type = 'button';
      if (title) b.title = title;
      b.addEventListener('click', fn);
      return b;
    };
    actions.appendChild(mkBtn('儲存', 'btn-primary', async () => {
      if (!ta.value.trim()) {
        window.toast('內文不可為空', 'fail', '請填寫提示詞內文');
        return;
      }
      try {
        const sp = await api.put('/api/strategy-prompts/' + t.id,
          { name: t.name, body: ta.value, enabled: t.enabled !== false });
        t.body = (sp && sp.body) || ta.value;
        t.updated_at = (sp && sp.updated_at) || t.updated_at;
      } catch (err) {
        _toast((err && err.message) || '儲存失敗', 'fail', err && err.code);
        return;
      }
      /* 重新檢查範圍徽章（變數可能增減） */
      const ps = hasPerSymbolVars(ta.value);
      scopeBadge.textContent = ps ? '單一標的' : '全組合';
      scopeBadge.className = 'tpl-scope ' + (ps ? 'scope-sym' : 'scope-pf');
      scopeBadge.title = ps
        ? '含「單一標的」變數 — 只能被範圍為「單一標的」的洞察類型引用'
        : '僅使用全組合變數 — 任何範圍的洞察類型皆可引用';
      window.toast('已儲存', 'ok', t.name + '：內文已寫入資料庫，下次執行生效');
    }, '寫入資料庫並依最新內文重新檢查範圍徽章'));
    actions.appendChild(mkBtn('預覽提示詞', null, () => previewPrompt(t, ta),
      '變數代入目前快照，檢視實際送出的完整提示詞'));
    actions.appendChild(mkBtn('測試送出', null, () => testSend(t, ta),
      '經 LiteLLM 實際送出一次並回傳洞察結果（費用照記）'));
    actions.appendChild(mkBtn('封存', 'btn-danger', () => deleteStrategy(t, card),
      '被洞察類型引用時將阻擋'));
    body.appendChild(actions);
    card.appendChild(body);

    head.addEventListener('click', (e) => {
      if (e.target.closest('.toggle')) return;
      card.classList.toggle('open');
    });
    list.appendChild(card);
    return card;
  }
  D.strategies.filter((t) => !t.archived).forEach(addStrategyCard);

  /* ＋ 新增策略：表單 → 推入清單（後端接線後 POST /api/strategy-prompts） */
  const tplAdd = document.getElementById('tpl-add');
  if (tplAdd) tplAdd.addEventListener('click', () => {
    openModal('新增策略提示詞', (body, close) => {
      const nameFld = el('div', 'pv-field');
      nameFld.appendChild(el('label', null, '策略名稱'));
      const nameInp = el('input', 'input');
      nameInp.placeholder = '例：買進時機體檢';
      nameFld.appendChild(nameInp);
      body.appendChild(nameFld);
      const bodyFld = el('div', 'pv-field');
      bodyFld.appendChild(el('label', null, '提示詞內文（可用 {{變數}}，見下方變數總表）'));
      const ta2 = el('textarea', 'input');
      ta2.rows = 5;
      ta2.style.width = '100%';
      ta2.placeholder = '根據 {{holdings_json}} 與 {{price_vs_cost_json}}，…';
      bodyFld.appendChild(ta2);
      body.appendChild(bodyFld);
      body.appendChild(varInsertRow(ta2));
      const acts = el('div', 'cal-actions');
      const ok = el('button', 'btn btn-primary', '建立策略');
      ok.type = 'button';
      ok.addEventListener('click', async () => {
        const nm = nameInp.value.trim();
        if (!nm || !ta2.value.trim()) {
          window.toast('請填寫完整', 'fail', '名稱與內文皆為必填');
          return;
        }
        let sp;
        try {
          sp = await api.post('/api/strategy-prompts',
            { name: nm, body: ta2.value.trim(), enabled: true });
        } catch (err) {
          _toast((err && err.message) || '建立失敗', 'fail', err && err.code);
          return;
        }
        D.strategies.push(sp);
        const card = addStrategyCard(sp);
        card.classList.add('open');
        close();
        window.toast('已建立', 'ok', sp.name + '：可在洞察任務中掛載使用');
      });
      acts.appendChild(ok);
      body.appendChild(acts);
    }, true);
  });

  /* 儲存設定：將系統提示詞寫入後端 (PUT /api/system-prompt)；策略卡仍為設計稿（無端點）。 */
  if (sysSave) sysSave.addEventListener('click', async () => {
    const body = $('#sys-prompt').value;
    sysSave.disabled = true;
    try {
      const sp = await api.put('/api/system-prompt', { body: body });
      D.system_prompt = (sp && sp.body) || body;
      D.system_updated_at = (sp && sp.updated_at) || D.system_updated_at;
      $('#sys-prompt-meta').textContent =
        '更新 ' + (D.system_updated_at ? f.date(D.system_updated_at) : '—') + '・套用於所有策略提示詞之前';
      _toast('已儲存', 'ok', '系統提示詞已更新，下次 AI 呼叫生效');
    } catch (err) {
      _toast((err && err.message) || '儲存失敗', 'fail', err && err.code);
    } finally {
      sysSave.disabled = false;
    }
  });

  /* 重置回官方版（官方模板庫 2026-07-05）：POST /api/system-prompt/reset */
  const sysReset = document.getElementById('sys-reset');
  if (sysReset) sysReset.addEventListener('click', () => {
    window.confirmDialog({
      title: '重置系統提示詞',
      body: '將以官方模板庫的最新版本覆蓋目前內容；自訂修改將遺失（策略提示詞不受影響）。',
      confirmLabel: '重置回官方版', danger: true,
      onConfirm: async () => {
        try {
          const sp = await api.post('/api/system-prompt/reset');
          D.system_prompt = (sp && sp.body) || '';
          D.system_updated_at = (sp && sp.updated_at) || '';
          $('#sys-prompt').value = D.system_prompt;
          $('#sys-prompt-meta').textContent =
            '更新 ' + (D.system_updated_at ? f.date(D.system_updated_at) : '—') +
            '・套用於所有策略提示詞之前';
          _toast('已重置', 'ok', '系統提示詞已回到官方版');
        } catch (err) {
          _toast((err && err.message) || '重置失敗', 'fail', err && err.code);
        }
      }
    });
  });

  /* 從官方模板庫新增策略副本：GET /api/prompt-templates → POST from-template */
  const tplFromLib = document.getElementById('tpl-from-lib');
  if (tplFromLib) tplFromLib.addEventListener('click', async () => {
    let lib;
    try {
      lib = await api.get('/api/prompt-templates');
    } catch (err) {
      _toast('模板庫載入失敗', 'fail', (err && err.message) || undefined);
      return;
    }
    openModal('官方模板庫 ' + (lib.library_version || ''), (body, close) => {
      (lib.strategies || []).forEach((tpl) => {
        const row = el('div', 'pv-field');
        const head = el('div', null);
        head.appendChild(el('strong', null, tpl.name + '　' + tpl.version +
          '（' + (tpl.scope === 'per_symbol' ? '單一標的' : '全組合') + '）'));
        const btn = el('button', 'btn btn-primary', '新增副本');
        btn.type = 'button';
        btn.style.marginLeft = '8px';
        btn.addEventListener('click', async () => {
          try {
            const sp = await api.post('/api/strategy-prompts/from-template',
              { name: tpl.name });
            D.strategies.push(sp);
            const card = addStrategyCard(sp);
            card.classList.add('open');
            close();
            window.toast('已新增', 'ok', sp.name + '：官方 ' + tpl.version + ' 副本，可自由修改');
          } catch (err) {
            _toast((err && err.message) || '新增失敗', 'fail', err && err.code);
          }
        });
        head.appendChild(btn);
        row.appendChild(head);
        const pre = el('pre', 'pv-pre', tpl.body);
        pre.style.maxHeight = '180px';
        pre.style.overflow = 'auto';
        row.appendChild(pre);
        body.appendChild(row);
      });
    }, true);
  });

  /* mount point — FM8 (2026-07-07): the save bar now lives INSIDE the system-prompt
     panel, so dynamic panels simply append at the page end (vars table, then the
     self-contained 自我進化設定 with its own 儲存進化設定 button). */
  const promptsView = document.getElementById('view-prompts') || document.querySelector('.page');
  if (!promptsView) return;
  const mount = (node) => { promptsView.appendChild(node); };

  /* ================= 數據變數總表 ================= */
  (function () {
    const panel = document.createElement('details');
    panel.className = 'panel freshness';
    panel.id = 'vars-panel';
    const sum = el('summary');
    sum.appendChild(el('span', 'caret', '▶'));
    sum.appendChild(el('span', null, '數據變數總表'));
    const chips = el('span', 'chips sum-chips');
    const nVars = V.all().length;
    const nIngest = V.all().filter((v) => v.source === 'ingest').length;
    const c1 = el('span', 'ccy-chip');
    c1.appendChild(el('b', null, nVars + ' 個變數'));
    chips.appendChild(c1);
    const c2 = el('span', 'ccy-chip');
    c2.appendChild(el('span', null, '其中 '));
    c2.appendChild(el('b', null, nIngest + ' 個待後端新增'));
    chips.appendChild(c2);
    sum.appendChild(chips);
    panel.appendChild(sum);

    const wrap = el('div', 'vars-wrap');
    V.CATEGORIES.forEach((cat) => {
      const sec = el('div', 'vars-cat');
      const head = el('div', 'vars-cat-head');
      head.appendChild(el('span', 'vars-cat-name', cat.name));
      head.appendChild(el('span', 'vars-src ' + (cat.source === 'ready' ? 'src-ready' : 'src-ingest'),
        cat.source === 'ready' ? '後端已具備' : '需新增資料快照（spec 06）'));
      sec.appendChild(head);
      const table = el('table', 'data vars-table');
      table.innerHTML = '<thead><tr><th class="col-text">變數</th><th class="col-text">名稱</th><th class="col-text">說明</th><th class="col-text">範圍</th><th class="col-text"></th></tr></thead>';
      const tb = el('tbody');
      cat.vars.forEach((v) => {
        /* tier-greyout (spec 20.15): vars whose required tier is unavailable are dimmed. */
        const tierOk = v.tier_ok !== false;
        const tr = el('tr');
        if (!tierOk) { tr.classList.add('tier-locked'); tr.style.opacity = '0.5'; }
        const tdTok = el('td', 'col-text');
        tdTok.appendChild(el('code', 'pv-tok', '{{' + v.token + '}}'));
        tr.appendChild(tdTok);
        tr.appendChild(el('td', 'col-text vars-name', v.name));
        const tdDesc = el('td', 'col-text vars-desc', v.desc);
        tdDesc.title = '預覽範例：' + v.sample;
        tr.appendChild(tdDesc);
        const tdScope = el('td', 'col-text vars-scope', v.scope);
        if (!tierOk) {
          tdScope.appendChild(document.createTextNode(' '));
          const lock = el('span', 'pill pill-off', v.tier_label || '方案受限');
          lock.style.fontSize = '10px';
          tdScope.appendChild(lock);
        }
        tr.appendChild(tdScope);
        const tdCopy = el('td', 'col-text');
        const cp = el('button', 'btn-link', '複製');
        cp.type = 'button';
        cp.addEventListener('click', () => {
          try { navigator.clipboard.writeText('{{' + v.token + '}}'); } catch (e) { /* noop */ }
          window.toast('已複製', 'ok', '{{' + v.token + '}}');
        });
        tdCopy.appendChild(cp);
        tr.appendChild(tdCopy);
        tb.appendChild(tr);
      });
      table.appendChild(tb);
      sec.appendChild(table);
      wrap.appendChild(sec);
    });
    wrap.appendChild(el('div', 'cmp-note',
      '所有變數由計算核心即時組裝注入（LLM 不自行計算）；「單一標的」範圍變數僅在 per_symbol 洞察類型可用。外部資料（FinMind 籌碼基本面、市場情緒）抓取後以快照存入資料庫，供回測重現當時輸入。'));
    panel.appendChild(wrap);
    mount(panel);
  })();

  /* 任務組合／校正版本管理已整合至洞察管線中心（pipeline-hub）——本頁只維護提示詞資產（2026-07-05 收斂，原 mock 區塊移除）。 */

  /* ================= 自我進化設定 (GET/PUT /api/evolution-config) ================= */
  await (async function () {
    const panel = el('section', 'panel');
    const head = el('div', 'panel-head');
    head.appendChild(el('h2', 'panel-title', '自我進化設定'));
    head.appendChild(el('span', 'panel-sub', '安全邊界與成本上限 — 儲存後套用於下次校正產生批次'));
    panel.appendChild(head);

    /* Read the backend config so the 5 visible fields reflect the stored knobs AND the
       non-panel knobs (horizon_basis / defer_limit_days / shadow_on_alert) are kept for a
       lossless round-trip on save. gap_alert_pp is a Decimal STRING on the wire; coerce to
       a number only for the numeric input's display value (never recompute money here). */
    const cfg = { auto_promote: false, shadow_batches: 5, min_samples: 8, max_shadows: 2, gap_alert_pp: '15' };
    let serverCfg = {};
    try {
      const got = await api.get('/api/evolution-config');
      if (got && typeof got === 'object') {
        serverCfg = got;
        Object.assign(cfg, got);
      }
    } catch (err) {
      _toast('進化設定載入失敗', 'fail', (err && err.message) || undefined);
    }

    const FIELDS = [
      { id: 'auto_promote', name: '影子評估勝出後自動切換生效版', kind: 'toggle',
        desc: '關閉時需人工按「設為生效」；建議觀察兩輪後再開啟。' },
      { id: 'shadow_batches', name: '影子評估批次數', kind: 'num', min: 3, max: 20, step: 1, unit: '次',
        desc: '最新版需並行評估 N 次且成績不劣於生效版，才視為勝出。' },
      { id: 'min_samples', name: '校正產生最低樣本數', kind: 'num', min: 3, max: 50, step: 1, unit: '筆',
        desc: '組合的到期評估未達此數，AI 大師模型不產生新版本（避免小樣本過擬合）。' },
      { id: 'max_shadows', name: '同時影子評估上限', kind: 'num', min: 1, max: 5, step: 1, unit: '個',
        desc: '影子期 LLM 呼叫 ×2 — 控制 AI 大師模型額外成本，超過時排隊。' },
      { id: 'gap_alert_pp', name: '校準誤差預警門檻', kind: 'num', min: 5, max: 50, step: 1, unit: 'pp',
        desc: '與「設定›預警規則」的 AI 校準誤差規則同步（F4）。' }
    ];

    const list = el('div', 'evo-cfg-list');
    const inputs = {};
    FIELDS.forEach((fd) => {
      const row = el('div', 'evo-cfg-row');
      const main = el('div', 'evo-cfg-main');
      main.appendChild(el('div', 'evo-cfg-name', fd.name));
      main.appendChild(el('div', 'evo-cfg-desc', fd.desc));
      row.appendChild(main);
      const ctrl = el('div', 'evo-cfg-ctrl');
      if (fd.kind === 'toggle') {
        const tg = el('button', 'toggle' + (cfg[fd.id] ? ' on' : ''));
        tg.type = 'button';
        tg.setAttribute('role', 'switch');
        tg.setAttribute('data-evo-field', fd.id);  // stable e2e hook
        tg.addEventListener('click', () => tg.classList.toggle('on'));
        inputs[fd.id] = () => tg.classList.contains('on');
        ctrl.appendChild(tg);
      } else {
        const inp = el('input', 'input evo-cfg-input');
        inp.type = 'number'; inp.min = fd.min; inp.max = fd.max; inp.step = fd.step;
        inp.setAttribute('data-evo-field', fd.id);  // stable e2e hook
        inp.value = cfg[fd.id];
        inputs[fd.id] = () => Number(inp.value);
        ctrl.appendChild(inp);
        ctrl.appendChild(el('span', 'evo-cfg-unit', fd.unit));
      }
      row.appendChild(ctrl);
      list.appendChild(row);
    });
    panel.appendChild(list);

    const acts = el('div', 'cal-actions');
    acts.style.padding = '4px var(--pad) 14px';
    const save = el('button', 'btn btn-primary', '儲存進化設定');
    save.type = 'button';
    save.setAttribute('data-evo-save', '1');  // stable e2e hook
    save.addEventListener('click', async () => {
      /* Build the PUT body from the FULL fetched config, then OVERRIDE only the 5 visible
         fields — this preserves the non-panel knobs (horizon_basis / defer_limit_days /
         shadow_on_alert) across the round-trip. gap_alert_pp goes back as a Decimal STRING. */
      const body = Object.assign({}, serverCfg, {
        auto_promote: inputs.auto_promote(),
        shadow_batches: inputs.shadow_batches(),
        min_samples: inputs.min_samples(),
        max_shadows: inputs.max_shadows(),
        gap_alert_pp: String(inputs.gap_alert_pp()),
      });
      save.disabled = true;
      try {
        const got = await api.put('/api/evolution-config', body);
        if (got && typeof got === 'object') serverCfg = got;  // keep the canonical view
        _toast('進化設定已儲存', 'ok', '下次校正產生批次生效');
      } catch (err) {
        _toast((err && err.message) || '儲存失敗', 'fail', err && err.code);
      } finally {
        save.disabled = false;
      }
    });
    acts.appendChild(save);
    panel.appendChild(acts);
    mount(panel);
  })();

  /* Signal that all dynamic panels (組合器 / 校正庫 / 進化設定) are now in the DOM, so the
     page's trailing legacy-view collector runs AFTER they exist (panels mount async now). */
  document.dispatchEvent(new CustomEvent('pd-prompts-mounted'));
  } /* end boot() */

  boot();
})();
