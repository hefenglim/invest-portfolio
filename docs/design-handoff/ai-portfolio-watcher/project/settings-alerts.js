/* portfolio-dash — E1 預警規則設定 + E7 匯出中心.
   Rule thresholds persist to localStorage('pd_alert_rules'); alerts.js reads them,
   so changes take effect on the topbar bell at next page load. */
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
  const DEFAULTS = [
    { id: 'single_weight', name: '單一標的集中度', sev: 'risk', unit: '%', value: 30, min: 5, max: 100, step: 1,
      desc: '任一持倉權重（報告幣別市值）超過此比例時警示。' },
    { id: 'sector_weight', name: '產業集中度（半導體＋科技）', sev: 'risk', unit: '%', value: 60, min: 10, max: 100, step: 5,
      desc: '相關產業合計權重超過此比例時警示。' },
    { id: 'fx_drift', name: '匯率漂移', sev: 'info', unit: '%', value: 3, min: 0.5, max: 20, step: 0.5,
      desc: '外幣池取得均價與現匯偏離超過此幅度時提示（順風/逆風）。' },
    { id: 'exdiv_days', name: '即將除息提醒', sev: 'info', unit: '天', value: 14, min: 1, max: 60, step: 1,
      desc: '持倉標的除息日落在未來 N 天內時提示。' },
    { id: 'stale_price', name: '價格過期 / 缺價', sev: 'warn', unit: '', value: null, fixed: true,
      desc: '任一標的無報價或報價過期即警示（不可關閉門檻，僅可停用）。' },
    { id: 'calib_gap', name: 'AI 校準誤差', sev: 'warn', unit: 'pp', value: 15, min: 5, max: 50, step: 1,
      desc: 'AI 預測信心與實際命中率偏差超過此值時警示（資料來源：AI 戰績自我回測）。' },
    { id: 'quota_low', name: 'AI 額度偏低', sev: 'warn', unit: 'USD', value: 1.0, min: 0.1, max: 50, step: 0.1,
      desc: '剩餘額度低於此金額時警示；歸零時升級為紅色並暫停 AI 服務。' }
  ];

  function loadRules() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem('pd_alert_rules') || '{}'); } catch (e) { saved = {}; }
    return DEFAULTS.map((d) => Object.assign({}, d, saved[d.id] || {}));
  }

  function renderRules() {
    const wrap = $('#alert-rules-wrap');
    if (!wrap) return;
    wrap.replaceChildren();
    const rules = loadRules();
    const list = el('div', 'ar-list');
    rules.forEach((r) => {
      const row = el('div', 'ar-row');
      const sev = el('span', 'ar-sev sev-' + r.sev);
      sev.title = r.sev === 'risk' ? '紅色警示' : r.sev === 'warn' ? '琥珀警示' : '資訊提示';
      row.appendChild(sev);
      const main = el('div', 'ar-main');
      main.appendChild(el('div', 'ar-name', r.name));
      main.appendChild(el('div', 'ar-desc', r.desc));
      /* 對應的 AI 解讀組合（與洞察類型組合器的「觸發」設定互通） */
      const combos = (window.PD_COMPOSERS || [{ name: '預警解讀', scope: '預警事件觸發時', alert_rules: 'all', enabled: true }])
        .filter((c) => c.scope === '預警事件觸發時' && c.enabled !== false)
        .filter((c) => c.alert_rules === 'all' || !c.alert_rules || (Array.isArray(c.alert_rules) && c.alert_rules.includes(r.id)));
      const ai = el('div', 'ar-ai');
      if (combos.length) {
        ai.textContent = '⚡ 觸發 AI 解讀：' + combos.map((c) => '「' + c.name + '」').join('、') + ' ›';
        ai.title = '觸發哪些規則、用哪個策略組合，在「AI 提示詞›洞察類型組合器」的「觸發」chip 設定 — 點擊前往';
        ai.classList.add('linkish');
        ai.addEventListener('click', () => {
          const tab = document.querySelector('.set-tab[data-tab="prompts"]') || document.querySelector('[data-view="prompts"]');
          if (tab) tab.click();
          else window.location.href = 'settings-prompts.html';
        });
      } else {
        ai.textContent = '不觸發 AI 解讀（僅鈴鐺警示）— 可在洞察類型組合器新增「預警事件觸發」組合';
        ai.classList.add('none');
      }
      main.appendChild(ai);
      row.appendChild(main);
      const ctrl = el('div', 'ar-ctrl');
      if (!r.fixed) {
        const inp = el('input', 'input ar-input');
        inp.type = 'number'; inp.min = r.min; inp.max = r.max; inp.step = r.step;
        inp.value = r.value;
        inp.dataset.rule = r.id;
        ctrl.appendChild(inp);
        ctrl.appendChild(el('span', 'ar-unit', r.unit));
      } else {
        ctrl.appendChild(el('span', 'ar-unit', '自動'));
      }
      const tg = el('button', 'toggle' + (r.enabled === false ? '' : ' on'));
      tg.type = 'button';
      tg.setAttribute('role', 'switch');
      tg.dataset.rule = r.id;
      tg.title = '啟用 / 停用此規則';
      tg.addEventListener('click', () => tg.classList.toggle('on'));
      ctrl.appendChild(tg);
      row.appendChild(ctrl);
      list.appendChild(row);
    });
    wrap.appendChild(list);
  }

  function saveRules() {
    const out = {};
    document.querySelectorAll('#alert-rules-wrap .ar-input').forEach((inp) => {
      out[inp.dataset.rule] = out[inp.dataset.rule] || {};
      out[inp.dataset.rule].value = Number(inp.value);
    });
    document.querySelectorAll('#alert-rules-wrap .toggle').forEach((tg) => {
      out[tg.dataset.rule] = out[tg.dataset.rule] || {};
      out[tg.dataset.rule].enabled = tg.classList.contains('on');
    });
    try { localStorage.setItem('pd_alert_rules', JSON.stringify(out)); } catch (e) { /* noop */ }
    if (window.toast) window.toast('預警規則已儲存', 'ok', '下次資料更新（或重新整理頁面）時生效');
  }

  const saveBtn = $('#alert-rules-save');
  window.PD_RENDER_ALERT_RULES = renderRules;
  if (saveBtn) saveBtn.addEventListener('click', saveRules);
  const resetBtn = $('#alert-rules-reset');
  if (resetBtn) resetBtn.addEventListener('click', () => {
    try { localStorage.removeItem('pd_alert_rules'); } catch (e) { /* noop */ }
    renderRules();
    if (window.toast) window.toast('已還原預設值', 'ok');
  });
  renderRules();

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
