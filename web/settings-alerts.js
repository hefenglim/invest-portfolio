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
      desc: 'AI 預測信心與實際命中率偏差超過此值時警示（資料來源：AI 戰績自我回測）。' }
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
    { id: 'calib_gap', enabled: true, value: '15' }
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
