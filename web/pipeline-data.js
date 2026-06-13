/* portfolio-dash — 洞察管線中心（重構提案 v1）mock 資料層.
   新命名（對照原名）：
   - 洞察任務 Insight Task  ← 洞察類型（組合器）— 唯一可排程/校正的「執行單位」
   - 分析模板 Template      ← 策略提示詞 — 純設計物件（資產庫）
   - 全域守則 System Rules  ← 系統提示詞
   - 校正版本 Calibration   ← 自我校正提示詞（1:1 鏈）
   - 運行中心 Runs          ← 排程工作表＋執行歷史（任務視角整合）
   內建 3 個故障情境：
   A) it-dividend：未啟用＋未排程 → 「為什麼沒跑」診斷
   B) it-momentum：唯一模板被停用（R3）→ 整次 skip
   C) it-theme：自選標的 00919 出清 → 自動移除（R2）
   另：st-momentum 停用 → it-health 的組裝節點呈 1/2 模板（段落跳過、其餘照常） */
window.PIPE = (function () {
  'use strict';

  const health = {
    master: { ok: true, model: 'claude-opus（master）', note: '回測評分・校正生成正常' },
    quota: { remaining: '0.83', warn: true, note: '低於 $1.00 預警門檻' },
    batch: { at: '今日 08:00', cards: 8, cost: '0.094', note: '6 LLM 卡＋1 資料異常卡＋1 組合卡' },
    attention: 3 /* 需要注意的任務數（由 tasks 推導，初始值） */
  };

  const SYSTEM_RULES = '你是資深投資組合分析師…（繁中、幣別不混算、紅漲綠跌、引用具體數字、不給買賣建議）';

  const templates = [
    { id: 'st-concentration', name: '集中度風險', scope: 'portfolio', enabled: true,
      vars: ['holdings_json', 'allocation_json'],
      body: '根據 {{holdings_json}} 與 {{allocation_json}}，找出產業與個股集中度風險，輸出洞察並附具體權重數字。' },
    { id: 'st-fx', name: '匯率影響', scope: 'portfolio', enabled: true,
      vars: ['fx_json', 'fx_rates_json'],
      body: '根據 {{fx_json}} 分析各帳戶外幣部位匯損益，並估算 {{fx_rates_json}} 變動 1% 的敏感度。' },
    { id: 'st-dividend', name: '股利展望', scope: 'portfolio', enabled: true,
      vars: ['dividends_json', 'ex_dividend_calendar_json', 'dividend_projection_json'],
      body: '根據 {{dividends_json}} 與 {{ex_dividend_calendar_json}}，摘要未來 60 天股利現金流，標注幣別。' },
    { id: 'st-momentum', name: '動能追蹤', scope: 'per_symbol', enabled: false,
      vars: ['price_history_json', 'ma_signals_json', 'symbol_detail_json', 'institutional_json', 'backtest_json'],
      body: '根據 {{price_history_json}} 與 {{ma_signals_json}}，描述 {{symbol_detail_json}} 標的趨勢與動能。' }
  ];

  const HELD = ['2330', '0056', '00919', 'AAPL', 'MSFT', 'NVDA', '1155.KL'];
  const WATCH = [['6488', '環球晶'], ['8069', '元太']];
  const ALERT_RULES = [
    ['single_weight', '單一標的集中度'], ['sector_weight', '產業集中度'],
    ['fx_drift', '匯率漂移'], ['exdiv_days', '即將除息'],
    ['stale_price', '價格過期/缺價'], ['calib_gap', 'AI 校準誤差'], ['quota_low', 'AI 額度偏低']
  ];

  /* trigger.kind: 'schedule' | 'on_alert' | 'manual'（manual = 尚未排程） */
  const tasks = [
    { id: 'it-health', name: '持倉健診', scope: 'per_symbol',
      trigger: { kind: 'schedule', human: '每日 08:00', cron: '0 8 * * *', next: '明日 08:00' },
      universe: { mode: 'all' },
      templates: ['st-concentration', 'st-momentum'],
      self_correct: true, enabled: true,
      lastRun: { at: '今日 08:00', status: 'warn', summary: '7 卡（6 LLM＋1 資料異常）・$0.066',
        notes: ['1155.KL 缺價 → 產確定性「資料異常」卡（R4・零成本）', '「動能追蹤」模板停用 → 該段跳過，其餘照常（R3）'] },
      inputNote: { level: 'warn', text: '1155.KL 缺價（馬股報價 502）' } },

    { id: 'it-portfolio', name: '組合洞察', scope: 'portfolio',
      trigger: { kind: 'schedule', human: '每日 08:00', cron: '0 8 * * *', next: '明日 08:00' },
      templates: ['st-fx', 'st-concentration'],
      self_correct: true, enabled: true,
      lastRun: { at: '今日 08:00', status: 'ok', summary: '1 卡・$0.011', notes: [] } },

    { id: 'it-dividend', name: '股利展望', scope: 'portfolio',
      trigger: { kind: 'manual', human: '未排程' },
      templates: ['st-dividend'],
      self_correct: true, enabled: false,
      lastRun: { at: '14 天前', status: 'idle', summary: '最後一次產卡 05-29', notes: [] } },

    { id: 'it-theme', name: '高息標的體檢', scope: 'per_symbol',
      trigger: { kind: 'schedule', human: '每週一 09:00', cron: '0 9 * * 1', next: '06-15（一）09:00' },
      universe: { mode: 'custom', symbols: ['0056'] },
      universeEvent: '06-10 自動移除 00919（已出清・R2）— 原 2 檔 → 現 1 檔',
      templates: ['st-concentration', 'st-dividend'],
      self_correct: false, enabled: true,
      lastRun: { at: '06-08（一）09:00', status: 'ok', summary: '2 卡・$0.018', notes: [] },
      inputNote: { level: 'info', text: '06-10 自動移除 00919（出清）' } },

    { id: 'it-momentum', name: '動能週報', scope: 'per_symbol',
      trigger: { kind: 'schedule', human: '每週五 17:00', cron: '0 17 * * 5', next: '今日 17:00' },
      universe: { mode: 'all' },
      templates: ['st-momentum'],
      self_correct: false, enabled: true,
      lastRun: { at: '06-05（五）17:00', status: 'skipped', summary: '已跳過 — 模板全部停用（R3）',
        notes: ['唯一模板「動能追蹤」於 06-04 被停用 → 組裝段全空，該次執行跳過並發 warn 預警', '模板恢復啟用即自動繼續，無需重設排程'] } },

    { id: 'it-alert', name: '預警解讀', scope: 'on_alert',
      trigger: { kind: 'on_alert', human: '預警觸發', rules: ['single_weight', 'fx_drift', 'quota_low'] },
      templates: ['st-concentration'],
      self_correct: false, enabled: true,
      lastRun: { at: '06-09 14:02', status: 'ok', summary: '匯率漂移 → 1 卡・$0.009', notes: [] } }
  ];

  /* 校正版本鏈（1:1 掛任務） */
  const chains = {
    'it-health': { activeVer: 3, versions: [
      { ver: 1, date: '04-20', archived: false, cause: '初版 — 累積 8 筆樣本後由 AI 大師生成',
        stats: { evals: 10, avg: 61, miss: 40 }, body: '1.（範圍）僅描述風險與現象，不得給出買賣時點建議。' },
      { ver: 2, date: '05-12', archived: false, cause: '連續 3 次動能高估（2330、NVDA）→ 加入幅度下修條款',
        stats: { evals: 12, avg: 70, miss: 33 }, body: '1.（個股）2330 動能類幅度下修 30–40%。\n2.（範圍）僅描述風險與現象。' },
      { ver: 3, date: '06-05', archived: false, cause: '高信心區間校準誤差 +20pp → 加入信心錨定條款',
        stats: { evals: 14, avg: 82, miss: 29 }, body: '1.（個股）2330 動能類幅度下修 30–40%。\n2.（信心）信心值不得超過 {{backtest_json}} 對應區間實際命中率 +5pp。\n3.（範圍）僅描述風險與現象。' }
    ] },
    'it-portfolio': { activeVer: 2, shadowProgress: { done: 3, need: 5, shadowAvg: 91, activeAvg: 84 }, versions: [
      { ver: 1, date: '05-02', archived: true, cause: '初版', stats: { evals: 8, avg: 58, miss: 38 },
        body: '所有比率必須引用輸入 JSON 欄位值。' },
      { ver: 2, date: '06-01', archived: false, cause: '匯率敏感度兩次與後端計算不符（LLM 自行心算）',
        stats: { evals: 18, avg: 84, miss: 11 }, body: '所有敏感度與比率必須逐字引用輸入 JSON 欄位值，禁止推導計算。' },
      { ver: 3, date: '06-10', archived: false, cause: 'USD 匯損益歸因含糊（股/現金未拆分）→ 加入拆分條款',
        stats: { evals: 3, avg: 91, miss: 0 }, body: '同 v2，另：匯損益必須拆分「股票」與「現金」逐項標注。' }
    ] },
    'it-dividend': { activeVer: null, versions: [
      { ver: 1, date: '06-10', archived: false, cause: '一次洞察跨幣別合計股利（違反幣別規則）',
        stats: { evals: 2, avg: 88, miss: 0 }, body: '股利金額逐一標注幣別且分行呈現；嚴禁跨幣別加總。' }
    ] }
  };

  /* 運行記錄（任務視角整合 — 含 skipped 與原因） */
  const runs = [
    { at: '06-12 08:00', task: 'it-health', status: 'partial', detail: '6 LLM 卡＋1 資料異常卡（1155.KL 缺價・R4）；動能段跳過（模板停用・R3）', cards: 7, dur: '11.2s', cost: '0.066' },
    { at: '06-12 08:00', task: 'it-portfolio', status: 'ok', detail: '1 卡（校正 v2 生效・v3 影子並行 3/5）', cards: 1, dur: '4.1s', cost: '0.022' },
    { at: '06-11 08:00', task: 'it-health', status: 'ok', detail: '7 卡全數產出', cards: 7, dur: '13.9s', cost: '0.072' },
    { at: '06-11 08:00', task: 'it-portfolio', status: 'ok', detail: '1 卡', cards: 1, dur: '3.8s', cost: '0.021' },
    { at: '06-09 14:02', task: 'it-alert', status: 'ok', detail: '觸發規則：匯率漂移（USD +3.2%）→ 1 卡', cards: 1, dur: '3.0s', cost: '0.009' },
    { at: '06-08 09:00', task: 'it-theme', status: 'ok', detail: '2 卡（0056、00919）', cards: 2, dur: '6.1s', cost: '0.018' },
    { at: '06-05 17:00', task: 'it-momentum', status: 'skipped', detail: '模板全部停用（R3）— 已發 warn 預警；恢復模板即繼續', cards: 0, dur: '—', cost: null },
    { at: '06-05 08:00', task: 'it-health', status: 'ok', detail: '7 卡全數產出', cards: 7, dur: '12.6s', cost: '0.070' },
    { at: '05-29 10:12', task: 'it-dividend', status: 'ok', detail: '手動執行 → 1 卡', cards: 1, dur: '4.4s', cost: '0.012' }
  ];

  const tplOf = (id) => templates.find((t) => t.id === id) || null;
  const taskOf = (id) => tasks.find((t) => t.id === id) || null;
  const chainOf = (id) => chains[id] || null;
  const ruleName = (id) => { const r = ALERT_RULES.find((x) => x[0] === id); return r ? r[1] : id; };

  /* ---- 節點狀態推導（單一事實來源：之後由後端 status API 提供，spec 07） ---- */
  function nodeStates(t) {
    const n = {};
    /* 觸發 */
    if (!t.enabled) n.trigger = { lv: 'off', text: t.trigger.kind === 'manual' ? '未排程' : t.trigger.human, sub: '任務已停用' };
    else if (t.trigger.kind === 'manual') n.trigger = { lv: 'warn', text: '未排程', sub: '僅能手動執行' };
    else if (t.trigger.kind === 'on_alert') n.trigger = { lv: 'ok', text: '預警觸發', sub: t.trigger.rules ? '自選 ' + t.trigger.rules.length + ' 條規則' : '全部規則' };
    else n.trigger = { lv: 'ok', text: t.trigger.human, sub: '下次 ' + t.trigger.next };
    /* 輸入 */
    if (t.scope === 'portfolio') n.input = { lv: 'ok', text: '全組合', sub: '單一快照' };
    else if (t.scope === 'on_alert') n.input = { lv: 'ok', text: '事件標的', sub: '由命中規則決定' };
    else {
      const all = !t.universe || t.universe.mode === 'all';
      const cnt = all ? HELD.length : t.universe.symbols.length;
      const lv = t.inputNote ? t.inputNote.level : 'ok';
      n.input = { lv: lv === 'info' ? 'info' : lv, text: (all ? '全部持倉 ' : '自選 ') + cnt + ' 檔',
        sub: t.inputNote ? t.inputNote.text : (all ? '自動跟隨持倉' : '持倉＋觀察清單') };
    }
    /* 組裝 */
    const tpls = t.templates.map(tplOf).filter(Boolean);
    const live = tpls.filter((x) => x.enabled);
    const calib = calibLabel(t);
    if (!live.length) n.assemble = { lv: 'fail', text: '模板全部停用', sub: '執行將跳過（R3）' };
    else if (live.length < tpls.length) n.assemble = { lv: 'warn', text: live.length + '/' + tpls.length + ' 模板啟用', sub: '停用段跳過、其餘照常' };
    else n.assemble = { lv: 'ok', text: '守則＋' + tpls.length + ' 模板' + (calib.on ? '＋校正' : ''), sub: calib.text };
    /* 執行（額度偏低顯示在全局健康列，不逐卡標警告；歸零才擋） */
    const quotaZero = Number(health.quota.remaining) <= 0;
    n.exec = { lv: quotaZero ? 'fail' : 'ok', text: 'sonnet via LiteLLM', sub: '額度餘 $' + health.quota.remaining };
    /* 產出 */
    const lr = t.lastRun;
    const lvMap = { ok: 'ok', warn: 'warn', partial: 'warn', skipped: 'fail', idle: 'off' };
    n.output = { lv: lvMap[lr.status] || 'ok', text: lr.summary, sub: lr.at };
    return n;
  }

  function calibLabel(t) {
    if (!t.self_correct) return { on: false, text: '自我校正未啟動' };
    const ch = chainOf(t.id);
    if (!ch || !ch.versions.length) return { on: true, text: '校正：累積樣本中' };
    if (ch.activeVer === null) return { on: true, text: '校正：v' + ch.versions[ch.versions.length - 1].ver + ' 尚未套用' };
    const latest = ch.versions[ch.versions.length - 1].ver;
    return { on: true, text: '校正 v' + ch.activeVer + ' 生效' + (ch.activeVer !== latest ? '・v' + latest + ' 影子' : '') };
  }

  /* 任務整體層級：fail > warn > info > idle > ok */
  function taskLevel(t) {
    const n = nodeStates(t);
    const lvs = [n.trigger, n.input, n.assemble, n.exec, n.output].map((x) => x.lv);
    if (!t.enabled) return 'idle';
    if (lvs.includes('fail')) return 'fail';
    if (lvs.includes('warn')) return 'warn';
    if (lvs.includes('info')) return 'info';
    return 'ok';
  }

  return { health, SYSTEM_RULES, templates, tasks, chains, runs,
    HELD, WATCH, ALERT_RULES, tplOf, taskOf, chainOf, ruleName, nodeStates, calibLabel, taskLevel };
})();
