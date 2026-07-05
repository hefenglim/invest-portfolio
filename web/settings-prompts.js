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

  /* ================= 組合器資料（多策略＋自我校正開關） ================= */
  const COMPOSER_DATA = [
    { id: 'it-health', name: '持倉健診', scope: '單一標的（每檔一張）',
      strategies: ['st-concentration', 'st-momentum'], self_correct: true,
      universe: { mode: 'all' },
      schedule: '每日 08:00', job_id: 'insight_health_daily', enabled: true },
    { id: 'it-portfolio', name: '組合洞察', scope: '全組合',
      strategies: ['st-fx', 'st-concentration'], self_correct: true,
      schedule: '每日 08:00', job_id: 'insight_portfolio_daily', enabled: true },
    { id: 'it-dividend', name: '股利展望', scope: '全組合',
      strategies: ['st-dividend'], self_correct: true,
      schedule: null, job_id: null, enabled: false },
    { id: 'it-alert', name: '預警解讀', scope: '預警事件觸發時',
      strategies: ['st-concentration'], self_correct: false,
      alert_rules: 'all',
      schedule: '事件觸發', job_id: 'insight_on_alert', enabled: true }
  ];
  window.PD_COMPOSERS = COMPOSER_DATA;

  /* 持倉代號（設計稿示意；正式執行由組合範圍逐檔代入）。在外層宣告，供 universeLabel 與
     boot() 內的選擇器共用（boot 內外皆需引用）。 */
  const HELD_SYMBOLS = ['2330', '0056', '00919', 'AAPL', 'MSFT', 'NVDA', '1155.KL'];
  /* 標的宇宙（per_symbol 組合的可選清單）與預警規則名冊 */
  const WATCHLIST_SYMBOLS = [['6488', '環球晶'], ['8069', '元太']];
  const ALERT_RULE_NAMES = [
    ['single_weight', '單一標的集中度'], ['sector_weight', '產業集中度'],
    ['fx_drift', '匯率漂移'], ['exdiv_days', '即將除息'],
    ['stale_price', '價格過期/缺價'], ['calib_gap', 'AI 校準誤差'], ['quota_low', 'AI 額度偏低']
  ];
  const universeLabel = (c) => !c.universe || c.universe.mode === 'all'
    ? '全部持倉（' + HELD_SYMBOLS.length + ' 檔・自動跟隨）'
    : '自選 ' + c.universe.symbols.length + ' 檔';
  const rulesLabel = (c) => c.alert_rules === 'all' || !c.alert_rules
    ? '全部預警規則'
    : '自選 ' + c.alert_rules.length + ' 條規則';

  /* ================= 校正鏈（1:1 掛組合・版本管理） =================
     active = 使用者手動選定的生效版；active ≠ 最新版 → 最新版自動成為影子並行評估。
     每版累計：評估次數、平均分（AI 大師回測評分 0–100）、失誤率。封存 = 軟刪除。 */
  const CALIB_CHAINS = [
    { comboId: 'it-health', activeVer: 3,
      versions: [
        { ver: 1, date: '2026-04-20', archived: false,
          cause: '初版 — 累積 8 筆樣本後由 AI 大師模型生成',
          stats: { evals: 10, avg_score: 61, miss_rate: 0.40 },
          body: '1.（範圍）僅描述風險與現象，不得給出買賣時點建議。' },
        { ver: 2, date: '2026-05-12', archived: false,
          cause: '連續 3 次動能高估（2330、NVDA）→ 大師模型加入幅度下修條款',
          stats: { evals: 12, avg_score: 70, miss_rate: 0.33 },
          body: '1.（個股）2330 的 5 日漲幅預測歷史高估 4.2pp — 動能類幅度下修 30–40%。\n2.（範圍）僅描述風險與現象，不得給出買賣時點建議。' },
        { ver: 3, date: '2026-06-05', archived: false,
          cause: '高信心區間（≥0.8）校準誤差 +20pp → 加入信心錨定條款',
          stats: { evals: 14, avg_score: 82, miss_rate: 0.29 },
          body: '1.（個股）2330 的 5 日漲幅預測歷史高估 4.2pp — 動能類幅度下修 30–40%。\n2.（信心）信心值不得超過 {{backtest_json}} 中對應信心區間的實際命中率 +5pp；樣本 <8 時信心上限 0.7。\n3.（範圍）僅描述風險與現象，不得給出買賣時點建議。' }
      ] },
    { comboId: 'it-portfolio', activeVer: 2,
      versions: [
        { ver: 1, date: '2026-05-02', archived: true,
          cause: '初版', stats: { evals: 8, avg_score: 58, miss_rate: 0.38 },
          body: '所有比率必須引用輸入 JSON 欄位值。' },
        { ver: 2, date: '2026-06-01', archived: false,
          cause: '匯率敏感度兩次與後端計算不符（LLM 自行心算）',
          stats: { evals: 18, avg_score: 84, miss_rate: 0.11 },
          body: '所有敏感度與比率必須逐字引用輸入 JSON 的欄位值，禁止任何推導計算；找不到對應欄位時寫「資料未提供」。' },
        { ver: 3, date: '2026-06-10', archived: false,
          cause: 'USD 匯損益歸因描述含糊（股/現金未拆分）→ 大師模型加入拆分條款',
          stats: { evals: 2, avg_score: 91, miss_rate: 0.00 },
          body: '同 v2，另：描述匯損益時必須拆分「股票」與「現金」兩部分，逐項標注（引用 fx_json 對應欄位）。' }
      ] },
    { comboId: 'it-dividend', activeVer: null,
      versions: [
        { ver: 1, date: '2026-06-10', archived: false,
          cause: '一次洞察將 TWD 與 USD 股利合計呈現（違反幣別規則）',
          stats: { evals: 2, avg_score: 88, miss_rate: 0.00 },
          body: '輸出任何股利金額時，逐一標注幣別且分行呈現；嚴禁任何形式的跨幣別加總、平均或比較。' }
      ] }
  ];

  const stratName = (id) => {
    const s = D.strategies.find((x) => x.id === id);
    return s ? s.name : id;
  };
  const chainOf = (comboId) => CALIB_CHAINS.find((c) => c.comboId === comboId) || null;

  function calibSummary(combo) {
    if (!combo.self_correct) return { text: '自我校正未啟動', dim: true };
    const ch = chainOf(combo.id);
    if (!ch || !ch.versions.length) return { text: '尚未產生（累積樣本中）', dim: true };
    const latest = ch.versions[ch.versions.length - 1];
    if (ch.activeVer === null) return { text: 'v' + latest.ver + ' 影子評估中・尚未套用', dim: true };
    const shadow = ch.activeVer !== latest.ver ? '・v' + latest.ver + ' 影子中' : '';
    return { text: '專屬 v' + ch.activeVer + ' 生效' + shadow, dim: false };
  }

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

  /* mount point */
  const promptsView = document.getElementById('view-prompts') || document.querySelector('.page');
  const saveBar = promptsView ? promptsView.querySelector('.save-bar') : null;
  if (!promptsView) return;
  const mount = (node) => {
    if (saveBar) promptsView.insertBefore(node, saveBar);
    else promptsView.appendChild(node);
  };

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

  /* ================= 洞察類型組合器 ================= */
  /* 標的選擇器（共用：組合列「標的」chip 與 新增洞察類型） */
  function buildUniversePicker(initial) {
    const wrap = el('div');
    const mode = initial && initial.mode === 'custom' ? 'custom' : 'all';
    const picked = (initial && initial.symbols) || [];
    const rAll = el('label', 'pv-check');
    const rbAll = el('input'); rbAll.type = 'radio'; rbAll.name = 'uni-' + Date.now(); rbAll.checked = mode === 'all';
    rAll.appendChild(rbAll);
    rAll.appendChild(el('span', null, '全部持倉（自動跟隨持倉變動，新買入自動納入）'));
    const rCus = el('label', 'pv-check');
    const rbCus = el('input'); rbCus.type = 'radio'; rbCus.name = rbAll.name; rbCus.checked = mode === 'custom';
    rCus.appendChild(rbCus);
    rCus.appendChild(el('span', null, '自選標的（持倉＋觀察清單）'));
    wrap.appendChild(rAll);
    wrap.appendChild(rCus);
    const grid = el('div', 'pv-symgrid');
    const checks = [];
    HELD_SYMBOLS.forEach((s) => {
      const lb = el('label', 'pv-check');
      const cb = el('input'); cb.type = 'checkbox'; cb.value = s;
      cb.checked = picked.includes(s);
      lb.appendChild(cb);
      lb.appendChild(el('span', null, s));
      lb.appendChild(el('span', 'pv-symtag', '持倉'));
      checks.push(cb);
      grid.appendChild(lb);
    });
    WATCHLIST_SYMBOLS.forEach(([s, nm]) => {
      const lb = el('label', 'pv-check');
      const cb = el('input'); cb.type = 'checkbox'; cb.value = s;
      cb.checked = picked.includes(s);
      lb.appendChild(cb);
      lb.appendChild(el('span', null, s + ' ' + nm));
      lb.appendChild(el('span', 'pv-symtag watch', '觀察'));
      checks.push(cb);
      grid.appendChild(lb);
    });
    const syncDim = () => grid.classList.toggle('dim', rbAll.checked);
    rbAll.addEventListener('change', syncDim);
    rbCus.addEventListener('change', syncDim);
    syncDim();
    wrap.appendChild(grid);
    return {
      wrap,
      get() {
        if (rbAll.checked) return { mode: 'all' };
        const syms = checks.filter((cb) => cb.checked).map((cb) => cb.value);
        if (!syms.length) return null;
        return { mode: 'custom', symbols: syms };
      }
    };
  }

  /* 標的範圍設定（per_symbol 組合）：全部持倉 / 自選（持倉＋觀察清單） */
  function universeModal(c, chip) {
    openModal('標的範圍 — ' + c.name, (body, close) => {
      body.appendChild(el('div', 'pv-note',
        '選擇本洞察要逐檔執行的標的。生命週期規則：標的出清或移出觀察清單時自動從自選清單移除；清單清空時自動關閉本組合並發出預警。'));
      const picker = buildUniversePicker(c.universe);
      body.appendChild(picker.wrap);
      const acts = el('div', 'cal-actions');
      const ok = el('button', 'btn btn-primary', '儲存標的範圍');
      ok.type = 'button';
      ok.addEventListener('click', () => {
        const u = picker.get();
        if (!u) {
          window.toast('至少選一檔', 'fail', '自選模式需勾選至少一個標的');
          return;
        }
        c.universe = u;
        chip.querySelector('.pv').textContent = universeLabel(c);
        close();
        window.toast('已更新標的範圍', 'ok', c.name + '：' + universeLabel(c) + '（設計稿）');
      });
      acts.appendChild(ok);
      body.appendChild(acts);
    });
  }

  /* 觸發規則設定（on_alert 組合） */
  function rulesModal(c, chip) {
    openModal('觸發規則 — ' + c.name, (body, close) => {
      body.appendChild(el('div', 'pv-note',
        '選擇哪些預警規則發出警示時會觸發本解讀。規則門檻與啟停在「設定›預警規則」調整；被停用的規則不會觸發。'));
      const isAll = c.alert_rules === 'all' || !c.alert_rules;
      const rAll = el('label', 'pv-check');
      const rbAll = el('input'); rbAll.type = 'radio'; rbAll.name = 'rls'; rbAll.checked = isAll;
      rAll.appendChild(rbAll);
      rAll.appendChild(el('span', null, '全部規則（含未來新增的規則）'));
      const rCus = el('label', 'pv-check');
      const rbCus = el('input'); rbCus.type = 'radio'; rbCus.name = 'rls'; rbCus.checked = !isAll;
      rCus.appendChild(rbCus);
      rCus.appendChild(el('span', null, '自選規則'));
      body.appendChild(rAll);
      body.appendChild(rCus);
      const grid = el('div', 'pv-symgrid');
      const checks = [];
      ALERT_RULE_NAMES.forEach(([id, nm]) => {
        const lb = el('label', 'pv-check');
        const cb = el('input'); cb.type = 'checkbox'; cb.value = id;
        cb.checked = !isAll && c.alert_rules.includes(id);
        lb.appendChild(cb);
        lb.appendChild(el('span', null, nm));
        checks.push(cb);
        grid.appendChild(lb);
      });
      const syncDim = () => grid.classList.toggle('dim', rbAll.checked);
      rbAll.addEventListener('change', syncDim);
      rbCus.addEventListener('change', syncDim);
      syncDim();
      body.appendChild(grid);
      const acts = el('div', 'cal-actions');
      const ok = el('button', 'btn btn-primary', '儲存觸發規則');
      ok.type = 'button';
      ok.addEventListener('click', () => {
        if (rbAll.checked) {
          c.alert_rules = 'all';
        } else {
          const ids = checks.filter((cb) => cb.checked).map((cb) => cb.value);
          if (!ids.length) {
            window.toast('至少選一條', 'fail', '自選模式需勾選至少一條規則');
            return;
          }
          c.alert_rules = ids;
        }
        chip.querySelector('.pv').textContent = rulesLabel(c);
        /* 預警規則頁的指示即時同步 */
        if (window.PD_RENDER_ALERT_RULES) window.PD_RENDER_ALERT_RULES();
        close();
        window.toast('已更新觸發規則', 'ok', c.name + '：' + rulesLabel(c) + '（設計稿）');
      });
      acts.appendChild(ok);
      body.appendChild(acts);
    });
  }

  function scheduleModal(combo, schedCell) {
    openModal('排程設定 — ' + combo.name, (body, close) => {
      body.appendChild(el('div', 'pv-note', '與「設定 › 排程」工作表共用：確定後寫入排程工作表，之後的週期變更請至排程工作表調整。'));
      const fld = (label, node) => {
        const w = el('div', 'pv-field');
        w.appendChild(el('label', null, label));
        w.appendChild(node);
        return w;
      };
      const period = el('select', 'select');
      ['每日', '每週一', '每週五', '每月 1 日', '自訂 cron'].forEach((p) => {
        const o = el('option', null, p); o.value = p; period.appendChild(o);
      });
      const time = el('input', 'input');
      time.type = 'time';
      time.value = '08:00';
      const cron = el('input', 'input');
      cron.placeholder = '0 8 * * *';
      cron.style.display = 'none';
      period.addEventListener('change', () => {
        cron.style.display = period.value === '自訂 cron' ? '' : 'none';
        time.style.display = period.value === '自訂 cron' ? 'none' : '';
      });
      const row = el('div', 'pv-fields');
      row.appendChild(fld('週期', period));
      row.appendChild(fld('時間', time));
      row.appendChild(fld('cron 表達式', cron));
      body.appendChild(row);
      const acts = el('div', 'cal-actions');
      const ok = el('button', 'btn btn-primary', '確定並加入排程工作表');
      ok.type = 'button';
      ok.addEventListener('click', () => {
        const sched = period.value === '自訂 cron' ? cron.value || '0 8 * * *' : period.value + ' ' + time.value;
        combo.schedule = sched;
        combo.job_id = 'insight_' + combo.id.replace('it-', '');
        combo.enabled = true;
        schedCell.textContent = sched;
        close();
        window.toast('已加入排程工作表', 'ok', combo.job_id + '：' + sched + '（設計稿 — 至「設定›排程」可調整）');
      });
      acts.appendChild(ok);
      body.appendChild(acts);
    });
  }

  (function () {
    const panel = el('section', 'panel');
    const head = el('div', 'panel-head');
    head.appendChild(el('h2', 'panel-title', '洞察類型組合器'));
    head.appendChild(el('span', 'panel-sub', '系統提示詞(可選) ＋ 一或多個策略 ＋ 自我校正開關 ＝ 一種定期洞察'));
    head.appendChild(el('span', 'spacer'));
    const addBtn = el('button', 'btn btn-primary', '＋ 新增洞察類型');
    addBtn.type = 'button';
    addBtn.addEventListener('click', () => {
      openModal('新增洞察類型', (mbody, close) => {
        const nameFld = el('div', 'pv-field');
        nameFld.appendChild(el('label', null, '名稱'));
        const nameInp = el('input', 'input');
        nameInp.placeholder = '例：高息部位體檢';
        nameFld.appendChild(nameInp);
        const scopeFld = el('div', 'pv-field');
        scopeFld.appendChild(el('label', null, '範圍'));
        const scopeSel = el('select', 'select');
        ['全組合', '單一標的（每檔一張）', '預警事件觸發時'].forEach((s) => {
          const o = el('option', null, s); o.value = s; scopeSel.appendChild(o);
        });
        scopeFld.appendChild(scopeSel);
        const row1 = el('div', 'pv-fields');
        row1.appendChild(nameFld);
        row1.appendChild(scopeFld);
        mbody.appendChild(row1);

        const stFld = el('div', 'pv-section');
        stFld.appendChild(el('div', 'pv-sec-label', '勾選策略（一或多個，依勾選順序執行）'));
        const checks = [];
        const rowsBySid = {};
        D.strategies.filter((s) => !s.archived).forEach((s) => {
          const lb = el('label', 'pv-check');
          const cb = el('input');
          cb.type = 'checkbox';
          cb.value = s.id;
          lb.appendChild(cb);
          lb.appendChild(el('span', null, s.name));
          const perSym = hasPerSymbolVars(s.body);
          lb.appendChild(el('span', 'pv-symtag' + (perSym ? ' watch' : ''), perSym ? '單一標的' : '全組合'));
          checks.push(cb);
          rowsBySid[s.id] = { lb, cb, perSym };
          stFld.appendChild(lb);
        });
        /* 範圍與策略變數範圍的相容性：非單一標的範圍 → 含單一標的變數的策略不可選 */
        const syncCompat = () => {
          const isPerSym = scopeSel.value === '單一標的（每檔一張）';
          Object.values(rowsBySid).forEach((r) => {
            const blocked = r.perSym && !isPerSym;
            r.cb.disabled = blocked;
            if (blocked) r.cb.checked = false;
            r.lb.classList.toggle('blocked', blocked);
            r.lb.title = blocked ? '此策略含「單一標的」變數，只能用於範圍為「單一標的」的洞察類型' : '';
          });
        };
        scopeSel.addEventListener('change', syncCompat);
        syncCompat();
        mbody.appendChild(stFld);

        /* 標的範圍（範圍選「單一標的」時出現，與組合列「標的」chip 共用同一選擇器） */
        const uniWrap = el('div', 'pv-section');
        uniWrap.appendChild(el('div', 'pv-sec-label', '標的範圍（持倉＋觀察清單）'));
        const uniPicker = buildUniversePicker({ mode: 'all' });
        uniWrap.appendChild(uniPicker.wrap);
        mbody.appendChild(uniWrap);
        const syncUni = () => {
          uniWrap.style.display = scopeSel.value === '單一標的（每檔一張）' ? '' : 'none';
        };
        scopeSel.addEventListener('change', syncUni);
        syncUni();

        const scRow = el('div', 'pv-field');
        const scLb = el('label', 'pv-check');
        const scCb = el('input');
        scCb.type = 'checkbox';
        scCb.checked = true;
        scLb.appendChild(scCb);
        scLb.appendChild(el('span', null, '啟動自我校正（AI 大師模型產生 1:1 專屬校正提示詞）'));
        scRow.appendChild(scLb);
        mbody.appendChild(scRow);

        const acts = el('div', 'cal-actions');
        const ok = el('button', 'btn btn-primary', '建立洞察類型');
        ok.type = 'button';
        ok.addEventListener('click', () => {
          const nm = nameInp.value.trim();
          const picked = checks.filter((c2) => c2.checked).map((c2) => c2.value);
          if (!nm || !picked.length) {
            window.toast('請填寫完整', 'fail', '名稱必填，且至少勾選一個策略');
            return;
          }
          const c = { id: 'it-custom-' + Date.now(), name: nm, scope: scopeSel.value,
            strategies: picked, self_correct: scCb.checked,
            schedule: scopeSel.value === '預警事件觸發時' ? '事件觸發' : null,
            job_id: null, enabled: false };
          if (c.scope === '單一標的（每檔一張）') {
            const u = uniPicker.get();
            if (!u) {
              window.toast('至少選一檔', 'fail', '標的範圍為自選時需勾選至少一個標的');
              return;
            }
            c.universe = u;
          }
          if (c.scope === '預警事件觸發時') c.alert_rules = 'all';
          COMPOSER_DATA.push(c);
          addComposerRow(c);
          if (c.scope === '預警事件觸發時' && window.PD_RENDER_ALERT_RULES) window.PD_RENDER_ALERT_RULES();
          close();
          window.toast('已建立', 'ok', nm + (c.schedule === null ? '：接著按「啟動排程」設定執行週期' : '：將由預警事件觸發') + '（設計稿）');
        });
        acts.appendChild(ok);
        mbody.appendChild(acts);
      }, true);
    });
    head.appendChild(addBtn);
    panel.appendChild(head);

    const list = el('div', 'cmp-list');
    function addComposerRow(c) {
      const row = el('div', 'cmp-row');
      const main = el('div', 'cmp-main');
      const nameRow = el('div', 'cmp-name-row');
      nameRow.appendChild(el('span', 'cmp-name', c.name));
      nameRow.appendChild(el('span', 'cmp-scope', c.scope));
      main.appendChild(nameRow);
      const combo = el('div', 'cmp-combo');
      const part = (label, value, dim) => {
        const p = el('span', 'cmp-part' + (dim ? ' dim' : ''));
        p.appendChild(el('span', 'pl', label));
        p.appendChild(el('span', 'pv', value));
        return p;
      };
      combo.appendChild(part('系統', '全域系統提示詞'));
      c.strategies.forEach((sid) => {
        combo.appendChild(el('span', 'cmp-plus', '＋'));
        const s = D.strategies.find((x) => x.id === sid);
        const p = part('策略', stratName(sid));
        if (s && hasPerSymbolVars(s.body)) {
          if (c.scope !== '單一標的（每檔一張）') {
            p.classList.add('mismatch');
            p.title = '⚠ 此策略含「單一標的」變數，但本組合範圍非單一標的 — 執行時會被擋下，請更換策略或調整範圍';
          } else {
            p.title = '含「單一標的」變數，逐檔代入';
          }
        }
        combo.appendChild(p);
      });
      combo.appendChild(el('span', 'cmp-plus', '＋'));
      const cs = calibSummary(c);
      combo.appendChild(part('校正', cs.text, cs.dim));
      /* per_symbol：標的範圍 chip（可點擊設定） */
      if (c.scope === '單一標的（每檔一張）') {
        combo.appendChild(el('span', 'cmp-plus', '▸'));
        const up = part('標的', universeLabel(c));
        up.classList.add('clickable');
        up.title = '點擊選擇要跡哪些標的（持倉或觀察清單）';
        up.addEventListener('click', () => universeModal(c, up));
        combo.appendChild(up);
      }
      /* on_alert：觸發規則 chip（可點擊設定） */
      if (c.scope === '預警事件觸發時') {
        combo.appendChild(el('span', 'cmp-plus', '▸'));
        const rp = part('觸發', rulesLabel(c));
        rp.classList.add('clickable');
        rp.title = '點擊選擇哪些預警規則會觸發本解讀（與設定›預警規則互通）';
        rp.addEventListener('click', () => rulesModal(c, rp));
        combo.appendChild(rp);
      }
      main.appendChild(combo);
      row.appendChild(main);

      const right = el('div', 'cmp-right');
      /* 自我校正開關 */
      const scWrap = el('span', 'cmp-sc');
      scWrap.title = '啟動後由 AI 大師模型回測評分並產生 1:1 專屬校正提示詞';
      scWrap.appendChild(el('span', 'cmp-sc-label', '自我校正'));
      const scTg = el('button', 'toggle mini' + (c.self_correct ? ' on' : ''));
      scTg.type = 'button';
      scTg.setAttribute('role', 'switch');
      scTg.addEventListener('click', () => {
        scTg.classList.toggle('on');
        c.self_correct = scTg.classList.contains('on');
        const ncs = calibSummary(c);
        const pv = combo.querySelector('.cmp-part:last-of-type');
        pv.classList.toggle('dim', ncs.dim);
        pv.querySelector('.pv').textContent = ncs.text;
        window.toast(c.self_correct ? '自我校正已啟動' : '自我校正已關閉', 'ok',
          c.self_correct ? c.name + '：AI 大師模型將開始累積回測樣本（設計稿）' : c.name + '：校正鏈保留，不再評估與套用（設計稿）');
      });
      scWrap.appendChild(scTg);
      right.appendChild(scWrap);

      /* 排程 */
      const schedCell = el('span', 'cmp-sched', c.schedule || '未排程');
      right.appendChild(schedCell);
      if (c.scope !== '預警事件觸發時') {
        const schedBtn = el('button', 'btn btn-sm', c.schedule ? '改排程' : '啟動排程');
        schedBtn.type = 'button';
        schedBtn.title = '開啟週期設定表（與排程工作表共用）';
        schedBtn.addEventListener('click', () => scheduleModal(c, schedCell));
        right.appendChild(schedBtn);
      }
      const tg = el('button', 'toggle' + (c.enabled ? ' on' : ''));
      tg.type = 'button';
      tg.setAttribute('role', 'switch');
      tg.title = '啟用/暫停此洞察類型：暫停後排程保留但不執行，恢復開啟即繼續';
      tg.addEventListener('click', () => {
        tg.classList.toggle('on');
        c.enabled = tg.classList.contains('on');
        /* on_alert 組合啟停影響預警規則頁的「觸發 AI 解讀」指示 — 即時同步 */
        if (c.scope === '預警事件觸發時' && window.PD_RENDER_ALERT_RULES) window.PD_RENDER_ALERT_RULES();
      });
      right.appendChild(tg);

      /* 刪除組合 → 同步刪排程 */
      const delBtn = el('button', 'btn btn-sm btn-danger', '刪除');
      delBtn.type = 'button';
      delBtn.addEventListener('click', () => window.confirmDialog({
        title: '刪除洞察類型 — ' + c.name,
        body: '將同步移除排程工作表中的 ' + (c.job_id || '（無排程）') +
          '；其專屬校正鏈與歷史洞察/戰績記錄封存保留可反查。',
        confirmLabel: '確認刪除', danger: true,
        onConfirm: () => {
          const ix = COMPOSER_DATA.indexOf(c);
          if (ix >= 0) COMPOSER_DATA.splice(ix, 1);
          row.remove();
          /* on_alert 組合被刪 → 預警規則頁的「觸發 AI 解讀」指示同步更新 */
          if (window.PD_RENDER_ALERT_RULES) window.PD_RENDER_ALERT_RULES();
          window.toast('已刪除', 'ok', c.name + '＋排程 ' + (c.job_id || '—') + ' 已同步移除；校正鏈已封存（設計稿）');
        }
      }));
      right.appendChild(delBtn);
      row.appendChild(right);
      list.appendChild(row);
    }
    COMPOSER_DATA.forEach(addComposerRow);
    panel.appendChild(list);
    panel.appendChild(el('div', 'cmp-note',
      '策略提示詞本身不可排程 — 排程只掛在洞察類型上。啟動「自我校正」的組合由 AI 大師模型（設定›AI 與額度）負責回測評分與校正提示詞生成。'));
    mount(panel);
  })();

  /* ================= AI 自我校正提示詞庫（版本管理器） ================= */
  (function () {
    const panel = el('section', 'panel');
    const head = el('div', 'panel-head');
    head.appendChild(el('h2', 'panel-title', 'AI 自我校正提示詞庫'));
    head.appendChild(el('span', 'panel-sub', '1:1 掛組合・AI 大師模型回測評分驅動版本演進・手動版本選擇器'));
    panel.appendChild(head);

    const pipe = el('div', 'evo-pipe');
    ['洞察產生', '戰績追蹤', 'AI 大師回測評分', '未命中聚類分析', '大師產生校正新版', '影子測試', '套用'].forEach((s, i, arr) => {
      pipe.appendChild(el('span', 'evo-step', s));
      if (i < arr.length - 1) pipe.appendChild(el('span', 'evo-arrow', '→'));
    });
    panel.appendChild(pipe);

    const listEl = el('div', 'cal-list');

    COMPOSER_DATA.filter((c) => c.self_correct || chainOf(c.id)).forEach((combo) => {
      const ch = chainOf(combo.id);
      const card = el('div', 'cal-card');
      const h = el('div', 'cal-head');
      h.appendChild(el('span', 'cal-type', '1:1 專屬'));
      h.appendChild(el('span', 'cal-target', combo.name));
      if (!ch) {
        h.appendChild(el('span', 'cal-ver num', '尚未產生'));
        h.appendChild(el('span', 'cal-status pill-candidate', '累積樣本中'));
        card.appendChild(h);
        listEl.appendChild(card);
        return;
      }
      const latest = ch.versions[ch.versions.length - 1];
      const liveVers = ch.versions.filter((v) => !v.archived).length;
      h.appendChild(el('span', 'cal-ver num', '共 ' + ch.versions.length + ' 版・現行 ' +
        (ch.activeVer ? 'v' + ch.activeVer : '未套用')));
      if (ch.activeVer !== null && ch.activeVer !== latest.ver) {
        h.appendChild(el('span', 'cal-status pill-shadow', 'v' + latest.ver + ' 影子評估中'));
      } else if (ch.activeVer === latest.ver) {
        h.appendChild(el('span', 'cal-status pill-active', '最新版生效・無影子'));
      } else {
        h.appendChild(el('span', 'cal-status pill-shadow', 'v' + latest.ver + ' 影子評估中'));
      }
      const caret = el('span', 'cal-caret', '▶');
      h.appendChild(caret);
      card.appendChild(h);

      const body = el('div', 'cal-body');
      /* 顯示已封存 toggle */
      let showArchived = false;
      const verList = el('div', 'cal-versions');
      const renderVers = () => {
        verList.replaceChildren();
        ch.versions.slice().reverse().forEach((v) => {
          if (v.archived && !showArchived) return;
          const isActive = ch.activeVer === v.ver;
          const isShadow = !v.archived && !isActive && v.ver === latest.ver;
          const vRow = el('div', 'cal-ver-row' + (v.archived ? ' archived' : '') + (isActive ? ' is-active' : ''));
          const top = el('div', 'cal-ver-top');
          top.appendChild(el('span', 'cal-ver-id num', 'v' + v.ver));
          top.appendChild(el('span', 'cal-ver-date num', v.date));
          if (isActive) top.appendChild(el('span', 'cal-status pill-active', '生效中'));
          else if (isShadow) top.appendChild(el('span', 'cal-status pill-shadow', '影子評估中'));
          else if (v.archived) top.appendChild(el('span', 'cal-status pill-archived', '已封存'));
          const stats = el('span', 'cal-ver-stats num',
            '評估 ' + v.stats.evals + ' 次・均分 ' + v.stats.avg_score + '・失誤率 ' + Math.round(v.stats.miss_rate * 100) + '%');
          stats.title = 'AI 大師模型定期回測洞察內容 vs 實際結果的累計成績';
          top.appendChild(stats);
          top.appendChild(el('span', 'spacer'));
          /* actions */
          const mkA = (label, primary, fn) => {
            const b = el('button', 'btn btn-sm' + (primary ? ' btn-primary' : ''), label);
            b.type = 'button';
            b.addEventListener('click', (e) => { e.stopPropagation(); fn(); });
            return b;
          };
          if (!v.archived && !isActive) {
            top.appendChild(mkA('設為生效', true, () => {
              ch.activeVer = v.ver;
              window.toast('已切換版本', 'ok', combo.name + ' 生效版 → v' + v.ver +
                (v.ver !== latest.ver ? '；v' + latest.ver + ' 自動進入影子評估' : '；最新版生效，無影子測試') + '（設計稿）');
              renderVers();
            }));
          }
          if (!v.archived) {
            top.appendChild(mkA('封存', false, () => {
              window.confirmDialog({
                title: '封存 v' + v.ver + ' — ' + combo.name,
                body: '封存後從版本選擇器移除、不可再套用；歷史戰績的歸因記錄保留可反查（軟刪除）。',
                confirmLabel: '確認封存', danger: true,
                onConfirm: () => {
                  v.archived = true;
                  if (ch.activeVer === v.ver) ch.activeVer = null;
                  window.toast('已封存', 'ok', 'v' + v.ver + '（設計稿）');
                  renderVers();
                }
              });
            }));
          }
          vRow.appendChild(top);
          const cause = el('div', 'cal-kv');
          cause.appendChild(el('span', 'k', '產生原因'));
          cause.appendChild(el('span', 'v', v.cause));
          vRow.appendChild(cause);
          const det = document.createElement('details');
          det.className = 'cal-ver-body';
          const ds = el('summary', null, '查看校正內文');
          det.appendChild(ds);
          det.appendChild(el('pre', 'cal-prompt', v.body));
          vRow.appendChild(det);
          verList.appendChild(vRow);
        });
      };
      renderVers();

      const tools = el('div', 'cal-actions');
      const arcTg = el('button', 'btn btn-sm', '顯示已封存');
      arcTg.type = 'button';
      arcTg.addEventListener('click', () => {
        showArchived = !showArchived;
        arcTg.textContent = showArchived ? '隱藏已封存' : '顯示已封存';
        renderVers();
      });
      tools.appendChild(arcTg);
      const samples = el('button', 'btn btn-sm', '查看失誤樣本');
      samples.type = 'button';
      samples.addEventListener('click', () =>
        window.toast('失誤樣本', 'ok', '顯示驅動各版本產生的原始預測 vs 實際結果（設計稿）'));
      tools.appendChild(samples);
      body.appendChild(tools);
      body.appendChild(verList);
      card.appendChild(body);
      h.addEventListener('click', () => card.classList.toggle('open'));
      listEl.appendChild(card);
    });
    panel.appendChild(listEl);
    panel.appendChild(el('div', 'cmp-note',
      '規則：生效版 = 手動選定；只要生效版 ≠ 最新版，最新版自動成為影子並行評估（不展示、成績照計）。' +
      '選擇最新版生效則無影子測試。每版累計「評估次數・均分・失誤率」由 AI 大師模型回測產生：' +
      '量化預測由程式比對價格（客觀），敘事準確度由大師模型評分 0–100。' +
      '未命中達條件（連續 3 次／失誤率超標／規則違規）→ 大師模型分析原因並產生新版本。封存 = 軟刪除，歸因鏈永不斷。'));
    mount(panel);
  })();

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
