/* portfolio-dash — E1 預警規則設定 + E7 匯出中心 (spec 19/03, Task 2.7c).
   Rule thresholds are SERVER state now: boot GETs /api/alert-rules and renders the
   editor; 儲存 PUTs the edited rules. The topbar bell + dashboard pick up changes on
   their NEXT load (each reads the backend rules). The old localStorage 'pd_alert_rules'
   read/write is retired. */
(function () {
  'use strict';
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  /* ============ E1: 預警規則 ============ */
  /* Display metadata keyed on the BACKEND rule id (strategy/rules_config.py RULE_META).
     The wire supplies enabled/value/unit/min/max; name/desc/sev are presentation-only.
     `step` is a UI hint per unit (ratio rules are edited in % for UX continuity). */
  const META = {
    single_weight: { name: '單一標的集中度', sev: 'risk', step: 1,
      desc: '任一持倉權重（報告幣別市值）超過此比例時警示。' },
    sector_weight: { name: '產業集中度', sev: 'risk', step: 5,
      desc: '任一產業合計權重超過此比例時警示。' },
    stale_price: { name: '價格過期', sev: 'warn',
      desc: '任一標的報價過期即警示（不可調門檻，僅可停用）。' },
    missing_price: { name: '缺價', sev: 'warn',
      desc: '任一標的無任何儲存價格即警示（不可調門檻，僅可停用）。' },
    fx_drift: { name: '匯率漂移', sev: 'info', step: 0.5,
      desc: '外幣池取得均價與現匯偏離超過此幅度時提示（順風/逆風）。' },
    exdiv_upcoming: { name: '即將除息提醒', sev: 'info', step: 1,
      desc: '持倉標的除息日落在未來 N 天內時提示。' },
    quota_low: { name: 'AI 額度偏低', sev: 'warn',
      desc: '剩餘額度低於閾值時警示（閾值在 AI 額度設定調整，此處僅可停用）。' },
    calib_gap: { name: 'AI 校準誤差', sev: 'warn', step: 1,
      desc: 'AI 預測信心與實際命中率偏差超過此值時警示（資料來源：AI 戰績自我回測）。' },
    drawdown_from_peak: { name: '高點回撤', sev: 'risk', step: 1,
      desc: '持股／觀察股現價自 52 週高點回撤達此幅度時警示（risk）；達一半幅度先給 warn。' },
    vol_spike: { name: '波動突升', sev: 'warn', step: 0.1,
      desc: '持股 30 日年化波動達 90 日基準的此倍數時警示（「最近不對勁」的早期訊號）。' },
    rebalance_drift: { name: '配置漂移', sev: 'risk', step: 1,
      desc: '有設目標的持股，現權重偏離目標超過此絕對帶寬或目標的 25%（Swedroe 5/25）時警示。' },
    consensus_change: { name: '分析師共識轉弱', sev: 'info', step: 0.1,
      desc: '評級分數惡化達此值（1→5 制）或均值目標價下修逾 10%（對比 7 日前）時提示。' },
    target_cross: { name: '目標價穿越', sev: 'warn',
      desc: '個股現價跌破目標下限或突破目標上限時警示。目標價在「觀察清單」逐檔設定，此處僅可停用。' }
  };

  /* Unit conversion between the WIRE (backend native units) and the EDITOR input.
     Ratio rules (unit "ratio") are stored as a Decimal STRING ratio ("0.30") and edited
     as a percent number (30). pp / days are edited 1:1. We never compute MONEY here —
     these are config thresholds, not money of record. */
  function wireToInput(value, unit) {
    if (value === null || value === undefined) return null;
    if (unit === 'ratio') {
      const pct = Number(value) * 100;
      return Math.round(pct * 1e6) / 1e6;  // strip binary-float dust (30.0000001 -> 30)
    }
    return Number(value);
  }
  function inputToWire(input, unit) {
    if (input === null || input === undefined || input === '') return null;
    if (unit === 'ratio') {
      return (Number(input) / 100).toFixed(4);  // % -> ratio string (e.g. 30 -> "0.3000")
    }
    return String(Number(input));
  }
  function displayUnit(unit) {
    if (unit === 'ratio') return '%';
    if (unit === 'pp') return 'pp';
    if (unit === 'days') return '天';
    if (unit === 'x') return '倍';    /* vol_spike multiple */
    if (unit === 'score') return '分'; /* consensus rating-score points (1..5) */
    return '';
  }

  /* The last GET response — the per-rule unit/min/max we need to build the PUT body
     (the backend bounds-check is unit-native, so we convert the % input back to ratio). */
  let WIRE = [];

  function renderRulesFrom(rules) {
    const wrap = $('#alert-rules-wrap');
    if (!wrap) return;
    WIRE = rules || [];
    wrap.replaceChildren();
    const list = el('div', 'ar-list');
    WIRE.forEach((w) => {
      const m = META[w.id] || { name: w.id, sev: 'info', desc: '' };
      const fixed = w.value === null || w.value === undefined;  // toggle-only rules
      const row = el('div', 'ar-row');
      const sev = el('span', 'ar-sev sev-' + m.sev);
      sev.title = m.sev === 'risk' ? '紅色警示' : m.sev === 'warn' ? '琥珀警示' : '資訊提示';
      row.appendChild(sev);
      const main = el('div', 'ar-main');
      main.appendChild(el('div', 'ar-name', m.name));
      main.appendChild(el('div', 'ar-desc', m.desc));
      /* 對應的 AI 解讀組合（與洞察類型組合器的「觸發」設定互通） */
      const combos = (window.PD_COMPOSERS || [{ name: '預警解讀', scope: '預警事件觸發時', alert_rules: 'all', enabled: true }])
        .filter((c) => c.scope === '預警事件觸發時' && c.enabled !== false)
        .filter((c) => c.alert_rules === 'all' || !c.alert_rules || (Array.isArray(c.alert_rules) && c.alert_rules.includes(w.id)));
      const ai = el('div', 'ar-ai');
      if (combos.length) {
        ai.textContent = '⚡ 觸發 AI 解讀：' + combos.map((c) => '「' + c.name + '」').join('、') + ' ›';
        ai.title = '觸發哪些規則、用哪個策略組合，在「AI 提示詞›洞察類型組合器」的「觸發」chip 設定 — 點擊前往';
        ai.classList.add('linkish');
        ai.addEventListener('click', () => {
          const tab = document.querySelector('.set-tab[data-tab="prompts"]') || document.querySelector('[data-view="prompts"]');
          if (tab) tab.click();
          else window.location.href = 'settings.html#prompts';
        });
      } else {
        ai.textContent = '不觸發 AI 解讀（僅鈴鐺警示）— 可在洞察類型組合器新增「預警事件觸發」組合';
        ai.classList.add('none');
      }
      main.appendChild(ai);
      row.appendChild(main);
      const ctrl = el('div', 'ar-ctrl');
      if (!fixed) {
        const inp = el('input', 'input ar-input');
        inp.type = 'number';
        if (w.min !== null && w.min !== undefined) inp.min = wireToInput(w.min, w.unit);
        if (w.max !== null && w.max !== undefined) inp.max = wireToInput(w.max, w.unit);
        if (m.step !== undefined) inp.step = m.step;
        const iv = wireToInput(w.value, w.unit);
        inp.value = iv === null ? '' : iv;
        inp.dataset.rule = w.id;
        ctrl.appendChild(inp);
        ctrl.appendChild(el('span', 'ar-unit', displayUnit(w.unit)));
      } else {
        ctrl.appendChild(el('span', 'ar-unit', '自動'));
      }
      const tg = el('button', 'toggle' + (w.enabled === false ? '' : ' on'));
      tg.type = 'button';
      tg.setAttribute('role', 'switch');
      tg.dataset.rule = w.id;
      tg.title = '啟用 / 停用此規則';
      tg.addEventListener('click', () => tg.classList.toggle('on'));
      ctrl.appendChild(tg);
      row.appendChild(ctrl);
      list.appendChild(row);
    });
    wrap.appendChild(list);
  }

  async function loadRules() {
    if (!window.pdApi) return;
    try {
      const res = await window.pdApi.get('/api/alert-rules');
      renderRulesFrom((res && res.rules) || []);
    } catch (err) {
      if (window.toast) window.toast(err.message, 'fail', err.code);
    }
  }

  function collectRuleBody() {
    /* Build the PUT body from the editor, converting % inputs back to ratio strings
       per the rule's wire unit. Toggle-only rules carry value:null. */
    const byId = {};
    WIRE.forEach((w) => { byId[w.id] = w; });
    const inputs = {};
    document.querySelectorAll('#alert-rules-wrap .ar-input').forEach((inp) => {
      inputs[inp.dataset.rule] = inp.value;
    });
    const out = [];
    document.querySelectorAll('#alert-rules-wrap .toggle').forEach((tg) => {
      const id = tg.dataset.rule;
      const w = byId[id];
      const unit = w ? w.unit : null;
      const hasInput = Object.prototype.hasOwnProperty.call(inputs, id);
      out.push({
        id: id,
        enabled: tg.classList.contains('on'),
        value: hasInput ? inputToWire(inputs[id], unit) : null
      });
    });
    return out;
  }

  async function saveRules() {
    if (!window.pdApi) return;
    try {
      const res = await window.pdApi.put('/api/alert-rules', { rules: collectRuleBody() });
      if (res && res.rules) renderRulesFrom(res.rules);
      if (window.toast) window.toast('預警規則已儲存', 'ok', '頂欄鈴鐺與儀表板於下次載入時生效');
    } catch (err) {
      if (window.toast) window.toast(err.message, 'fail', err.code);
    }
  }

  /* 還原預設值 — PUT the backend default thresholds (rules_config.py RULE_META):
     all rules enabled; ratio defaults as ratio strings; pp/days as native; toggle-only
     rules value:null. No dedicated reset endpoint exists, so we PUT the known contract. */
  const DEFAULTS_WIRE = [
    { id: 'single_weight', enabled: true, value: '0.30' },
    { id: 'sector_weight', enabled: true, value: '0.60' },
    { id: 'stale_price', enabled: true, value: null },
    { id: 'missing_price', enabled: true, value: null },
    { id: 'fx_drift', enabled: true, value: '0.03' },
    { id: 'exdiv_upcoming', enabled: true, value: '14' },
    { id: 'quota_low', enabled: true, value: null },
    { id: 'calib_gap', enabled: true, value: '15' },
    { id: 'drawdown_from_peak', enabled: true, value: '0.20' },
    { id: 'vol_spike', enabled: true, value: '1.8' },
    { id: 'rebalance_drift', enabled: true, value: '0.05' },
    { id: 'consensus_change', enabled: true, value: '0.5' },
    { id: 'target_cross', enabled: true, value: null }
  ];

  async function resetRules() {
    if (!window.pdApi) return;
    try {
      const res = await window.pdApi.put('/api/alert-rules', { rules: DEFAULTS_WIRE });
      if (res && res.rules) renderRulesFrom(res.rules);
      if (window.toast) window.toast('已還原預設值', 'ok');
    } catch (err) {
      if (window.toast) window.toast(err.message, 'fail', err.code);
    }
  }

  const saveBtn = $('#alert-rules-save');
  window.PD_RENDER_ALERT_RULES = loadRules;
  if (saveBtn) saveBtn.addEventListener('click', saveRules);
  const resetBtn = $('#alert-rules-reset');
  if (resetBtn) resetBtn.addEventListener('click', resetRules);
  loadRules();

  /* ============ 目標配置 (D8) — per-symbol target weights ============ */
  /* Server state: GET /api/target-weights lists every REGISTERED symbol (name + held/watch
     badge + stored target). Weights are RATIOS on the wire (Decimal strings, 4dp) and edited
     as a percent number here (display-only % <-> ratio; the frontend never computes money).
     Σ ≤ 100% enforced server-side; the live sum indicator is a UI hint only. */
  const TW_SYMBOLS = [];  /* the last GET rows, for the PUT body */

  function fmtPct(ratioStr) {
    if (ratioStr === null || ratioStr === undefined) return '';
    const pct = Number(ratioStr) * 100;
    return Math.round(pct * 1e6) / 1e6;  /* strip float dust (0.25 -> 25) */
  }

  function updateTwSum() {
    const sumEl = $('#tw-sum');
    if (!sumEl) return;
    let sum = 0;
    document.querySelectorAll('#target-weights-wrap .tw-input').forEach((inp) => {
      const v = Number(inp.value);
      if (inp.value !== '' && !Number.isNaN(v)) sum += v;
    });
    sumEl.textContent = (Math.round(sum * 100) / 100) + '%';
    sumEl.classList.toggle('sign-up', sum > 100.0001);
  }

  function renderTargetsFrom(view) {
    const wrap = $('#target-weights-wrap');
    if (!wrap) return;
    TW_SYMBOLS.length = 0;
    (view && view.symbols || []).forEach((s) => TW_SYMBOLS.push(s));
    wrap.replaceChildren();
    if (!TW_SYMBOLS.length) {
      wrap.appendChild(el('div', 'tw-empty', '尚無已註冊標的 — 先於「輸入中心」新增標的後即可設定目標配置。'));
      updateTwSum();
      return;
    }
    const list = el('div', 'tw-list');
    TW_SYMBOLS.forEach((s) => {
      const row = el('div', 'tw-row');
      const main = el('div', 'tw-main');
      const head = el('div', 'tw-head');
      head.appendChild(el('span', 'tw-code', s.symbol));
      head.appendChild(el('span', 'tw-badge ' + (s.held ? 'tw-held' : 'tw-watch'),
        s.held ? '持有' : '觀察'));
      main.appendChild(head);
      main.appendChild(el('div', 'tw-name', s.name || ''));
      row.appendChild(main);
      const ctrl = el('div', 'tw-ctrl');
      const inp = el('input', 'input tw-input');
      inp.type = 'number'; inp.min = '0'; inp.max = '100'; inp.step = '0.5';
      inp.placeholder = '不設';
      const iv = fmtPct(s.weight);
      inp.value = iv === '' ? '' : iv;
      inp.dataset.sym = s.symbol;
      inp.addEventListener('input', updateTwSum);
      ctrl.appendChild(inp);
      ctrl.appendChild(el('span', 'tw-unit', '%'));
      row.appendChild(ctrl);
      list.appendChild(row);
    });
    wrap.appendChild(list);
    updateTwSum();
  }

  async function loadTargets() {
    if (!window.pdApi) return;
    try {
      const res = await window.pdApi.get('/api/target-weights');
      renderTargetsFrom(res);
    } catch (err) {
      if (window.toast) window.toast(err.message, 'fail', err.code);
    }
  }

  function collectTargets() {
    /* Build the PUT body: only non-empty inputs become targets (empty = unset). % -> ratio. */
    const weights = {};
    document.querySelectorAll('#target-weights-wrap .tw-input').forEach((inp) => {
      if (inp.value === '' || inp.value === null) return;
      const v = Number(inp.value);
      if (Number.isNaN(v) || v <= 0) return;
      weights[inp.dataset.sym] = (v / 100).toFixed(4);  /* ratio string, 4dp */
    });
    return weights;
  }

  async function saveTargets() {
    if (!window.pdApi) return;
    try {
      const res = await window.pdApi.put('/api/target-weights', { weights: collectTargets() });
      renderTargetsFrom(res);
      if (window.toast) window.toast('目標配置已儲存', 'ok', '再平衡帶漂移警示與再平衡試算即以此為準');
    } catch (err) {
      if (window.toast) window.toast(err.message, 'fail', err.code);
    }
  }

  const twSaveBtn = $('#target-weights-save');
  if (twSaveBtn) twSaveBtn.addEventListener('click', saveTargets);
  loadTargets();

  /* ============ E7: 匯出中心 ============ */
  const EXPORTS = [
    { group: '對帳與核對', items: [
      { name: '持倉快照 CSV', desc: '目前所有持倉（原始/調整成本、現價、損益、權重），raw Decimal 精度。', backend: true },
      { name: '全帳本匯出（zip）', desc: '四帳本（期初/交易/股利/換匯）各一份 CSV ＋ 費率規則快照，打包下載。', backend: true },
      { name: 'AI 用量明細 CSV', desc: 'llm_usage 全表：每次呼叫的模型、tokens、成本。', backend: true },
      { name: '排程執行記錄 CSV', desc: 'job_runs 全表：時間、狀態、摘要、耗時。', backend: true }
    ]},
    { group: '報稅', items: [
      { name: '年度報稅包', desc: '指定年度的已實現損益＋股利收入（含預扣稅）＋匯損益實現明細，各幣別分列。', backend: true, year: true }
    ]}
  ];

  function renderExports() {
    const wrap = $('#export-center-wrap');
    if (!wrap) return;
    wrap.replaceChildren();
    EXPORTS.forEach((g) => {
      const sec = el('div', 'ec-group');
      sec.appendChild(el('h3', 'ec-group-title', g.group));
      const grid = el('div', 'ec-grid');
      g.items.forEach((item) => {
        const card = el('div', 'ec-card');
        const head = el('div', 'ec-head');
        head.appendChild(el('span', 'ec-name', item.name));
        head.appendChild(el('span', 'ec-glyph', '⬇'));
        card.appendChild(head);
        card.appendChild(el('p', 'ec-desc', item.desc));
        const foot = el('div', 'ec-foot');
        if (item.year) {
          const sel = el('select', 'select ec-year');
          ['2026', '2025', '2024'].forEach((y) => {
            const o = el('option', null, y + ' 年度');
            o.value = y;
            sel.appendChild(o);
          });
          foot.appendChild(sel);
        }
        const btn = el('button', 'btn btn-primary', '產生並下載');
        btn.type = 'button';
        btn.addEventListener('click', () => {
          if (window.toast) window.toast('已排入產生佇列', 'ok', item.name + ' — 由後端以 raw Decimal 產生（設計預覽，等待 export endpoint）');
        });
        foot.appendChild(btn);
        if (item.backend) {
          const tag = el('span', 'ec-tag', '後端產生');
          tag.title = '此匯出需要後端 export endpoint（見 specs/）；頁面上各表格的「匯出 CSV」則立即可用（顯示值精度）。';
          foot.appendChild(tag);
        }
        card.appendChild(foot);
        grid.appendChild(card);
      });
      sec.appendChild(grid);
      wrap.appendChild(sec);
    });
    wrap.appendChild(el('div', 'ec-note',
      '提示：各頁面表格右上角的「⬇ 匯出 CSV」可立即匯出目前篩選結果（顯示值精度）。本頁項目為對帳級匯出，數字直接出自後端 Decimal 計算核心，不經前端格式化。'));
  }
  renderExports();
})();
