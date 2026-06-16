/* portfolio-dash — 洞察管線中心：健康列＋管線卡＋分層抽屜（spec 07 / 19 wiring）.
   依賴：api.js（window.pdApi 單一 fetch 層）、format.js（window.fmt）、shell.js
   （toast / confirmDialog）、settings.css（pv-modal）、pipeline.css。

   資料來源（全部經 window.pdApi，從不直接 fetch）：
   - 任務列＋健康列 ← GET /api/insight-tasks/status
       { as_of, health{master_ok, quota_remaining, last_batch{at,cards,cost_usd}},
         tasks[{ id, name, scope, enabled, level,
                 nodes{trigger,input,assemble,exec,output: {lv,text,sub}},
                 last_run{at,status,summary,notes[]} | null }] }
   - 校正版本鏈 ← GET /api/calibrations?insight_type={id}
   - 運行記錄   ← GET /api/insight-tasks/{id}/runs
   - 乾跑預檢   ← POST /api/insight-tasks/{id}/preflight（pipeline-preflight.js）
   - 為什麼沒跑 ← GET  /api/insight-tasks/{id}/diagnose（pipeline-preflight.js）

   錢規則：quota_remaining / last_batch.cost_usd / run.cost_usd 為後端 Decimal STRING，
   一律經 window.fmt 呈現（fmt 內部才 Number()-coerce），前端不自行算錢。
   I2：後端 node level 詞彙為 idle（非 off）；本檔將任何 'off' 正規化為 'idle'。 */
(function () {
  'use strict';
  var pdApi = window.pdApi;
  var f = window.fmt;

  /* fetched state (filled by boot()) */
  var STATE = { tasks: [], health: null };

  var $ = function (s) { return document.querySelector(s); };
  var el = function (tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  };
  window.ppEl = el;

  /* I2 — level vocab: the backend emits idle; normalize any legacy 'off' to 'idle'. */
  function normLv(lv) { return lv === 'off' ? 'idle' : (lv || 'ok'); }
  /* node CSS class: pipeline.css styles `.pp-node.n-off` (dim) but not `.pp-node.n-idle`,
     so map idle/off -> 'off' for the node rail dimming; other levels pass through. */
  function nodeCss(lv) { var n = normLv(lv); return n === 'idle' ? 'off' : n; }

  /* I2 — fix.kind enum (spec 07 §7.2 / pipeline_status) -> human label + one-click action.
     The action receives the task object; it performs the existing PUT/POST flow. */
  var FIX_KINDS = {
    enable_task: { label: '啟用任務', run: function (t) { setEnabled(t, true); } },
    create_schedule: { label: '啟動排程', run: function (t) { window.ppScheduleModal(t); } },
    enable_schedule: { label: '啟動排程', run: function (t) { window.ppScheduleModal(t); } },
    edit_universe: { label: '編輯標的', run: function (t) { openDrawer(t, 'input'); } },
    enable_template: { label: '啟用模板', run: function () { go('settings-prompts.html'); } },
    edit_templates: { label: '增減模板', run: function (t) { openDrawer(t, 'assemble'); } },
    set_active_calibration: { label: '前往校正版本鏈', run: function (t) { openDrawer(t, 'calib'); } },
    fund_quota: { label: '前往額度設定', run: function () { go('settings-llm.html'); } },
    activate_role: { label: '前往 AI 大師設定', run: function () { go('settings-llm.html'); } }
  };
  window.ppFixKinds = FIX_KINDS;

  /* I2 — recent_skips reason enum (04b gating) -> human zh label. */
  var SKIP_REASONS = {
    R1_scope_mismatch: '範圍不相容（模板含 per_symbol 變數，任務非單一標的）',
    R2_universe_empty: '標的宇宙為空（清單已出清）',
    R2_symbols_removed: '部分標的已自動移除',
    R3_no_live_templates: '模板全部停用 — 組裝段為空',
    R4_missing_price: '缺價 — 該檔產確定性「資料異常」卡',
    R5_var_unavailable: '外部變數暫時無法取得',
    R6_quota: 'LLM 額度耗盡',
    R7_rule_not_matched: '預警規則未命中 / 防抖期內',
    master_missing: '未設定 AI 大師模型 — 校正暫停',
    unknown_insight_type: '未知的洞察任務'
  };
  window.ppSkipReason = function (code) { return SKIP_REASONS[code] || code || '未知原因'; };

  function go(href) { window.location.href = href; }

  /* Toggle a task's enabled flag through the composer update endpoint (PUT mirrors
     InsightTypeIn), then refresh. The status payload (build_status) does NOT carry the
     task's strategies/universe/self_correct, so we MUST first read the FULL insight-type
     row (GET /api/insight-tasks returns the wire with strategies + universe + alert_rules +
     self_correct) and resend it verbatim with only `enabled` changed — otherwise a toggle
     would silently RESET those fields. */
  function setEnabled(t, enabled) {
    if (!pdApi) return;
    pdApi.get('/api/insight-tasks').then(function (list) {
      var full = (Array.isArray(list) ? list : []).find(function (x) { return x.id === t.id; });
      if (!full) throw new Error('未找到洞察任務 ' + t.id);
      return pdApi.put('/api/insight-tasks/' + t.id, {
        name: full.name,
        scope: full.scope,
        strategy_ids: (full.strategies || []).map(function (s) { return s.id; }),
        use_system_prompt: full.use_system_prompt !== false,
        self_correct: !!full.self_correct,
        universe: full.universe != null ? full.universe : null,
        alert_rules: full.alert_rules != null ? full.alert_rules : null,
        enabled: enabled,
        horizon_days: full.horizon_days != null ? full.horizon_days : 5,
        eval_prompt: full.eval_prompt != null ? full.eval_prompt : null
      });
    }).then(function () {
      window.toast(enabled ? '任務已啟用' : '任務已暫停', 'ok',
        t.name + (enabled ? '：恢復依觸發條件執行' : '：排程保留、不再執行'));
      refresh();
    }).catch(function (err) {
      window.toast((err && err.message) || '更新失敗', 'fail', err && err.code);
    });
  }
  window.ppSetEnabled = setEnabled;

  /* 共用 modal（沿用 settings.css pv- 樣式） */
  window.ppModal = function (title, buildBody, wide) {
    var back = el('div', 'pv-backdrop');
    var box = el('div', 'pv-box' + (wide ? ' wide' : ''));
    var head = el('div', 'pv-head');
    head.appendChild(el('span', 'pv-title', title));
    var x = el('button', 'sd-close', '✕');
    x.type = 'button';
    x.addEventListener('click', function () { back.remove(); });
    head.appendChild(x);
    box.appendChild(head);
    var body = el('div', 'pv-body');
    box.appendChild(body);
    buildBody(body, function () { back.remove(); });
    back.appendChild(box);
    back.addEventListener('click', function (e) { if (e.target === back) back.remove(); });
    document.body.appendChild(back);
    return back;
  };

  var SCOPE_META = {
    per_symbol: { cls: 's-sym', label: '單一標的・每檔一張' },
    portfolio: { cls: 's-pf', label: '全組合' },
    on_alert: { cls: 's-alert', label: '預警觸發' }
  };
  function scopeMeta(scope) { return SCOPE_META[scope] || { cls: 's-pf', label: scope }; }

  var NODE_DEFS = [
    ['trigger', '① 觸發'], ['input', '② 輸入'], ['assemble', '③ 組裝'],
    ['exec', '④ 執行'], ['output', '⑤ 產出']
  ];

  /* ================= 健康列 ================= */
  function renderHealth() {
    var wrap = $('#pp-health');
    wrap.replaceChildren();
    var h = STATE.health || {};
    var card = function (label, value, sub, lv) {
      var c = el('div', 'pp-h-card' + (lv === 'warn' ? ' warn' : ''));
      c.appendChild(el('div', 'pp-h-label', label));
      var v = el('div', 'pp-h-value');
      v.appendChild(el('span', 'pp-dot ' + normLv(lv)));
      v.appendChild(el('span', null, value));
      c.appendChild(v);
      c.appendChild(el('div', 'pp-h-sub', sub));
      return c;
    };
    /* master role status */
    wrap.appendChild(card('AI 大師模型', h.master_ok ? '已設定' : '未設定',
      h.master_ok ? '回測評分・校正生成正常' : '回測評分與校正生成暫停', h.master_ok ? 'ok' : 'warn'));
    /* quota — Decimal STRING via f.num (never bare .toFixed). */
    var quota = h.quota_remaining;
    var quotaLow = quota != null && Number(quota) < 1;  /* presentation flag only */
    wrap.appendChild(card('LLM 額度', '$' + f.num(quota, 2),
      quotaLow ? '低於 $1.00 預警門檻' : '額度充足', quotaLow ? 'warn' : 'ok'));
    /* last batch */
    var lb = h.last_batch;
    if (lb) {
      wrap.appendChild(card('最近批次', f.datetime(lb.at) + '・' + f.num(lb.cards) + ' 卡',
        '本批成本 $' + f.num(lb.cost_usd, 3), 'ok'));
    } else {
      wrap.appendChild(card('最近批次', '尚無批次', '尚未產生任何洞察卡', 'idle'));
    }
    /* task attention summary */
    var attn = STATE.tasks.filter(function (t) {
      var lv = normLv(t.level); return lv === 'fail' || lv === 'warn';
    }).length;
    var idle = STATE.tasks.filter(function (t) { return normLv(t.level) === 'idle'; }).length;
    wrap.appendChild(card('任務狀態', attn + ' 個需注意・' + idle + ' 個閒置',
      STATE.tasks.length + ' 個洞察任務（點下方卡片展開）', attn ? 'warn' : 'ok'));
  }

  /* ================= 篩選列 ================= */
  var filter = 'all';
  function renderFilterBar() {
    var bar = $('#pp-filter');
    bar.replaceChildren();
    [['all', '全部任務'], ['attention', '需注意'], ['scheduled', '已排程'],
     ['on_alert', '預警觸發'], ['idle', '閒置']].forEach(function (pair, i) {
      var c = el('button', 'chip' + (pair[0] === filter ? ' active' : ''), pair[1]);
      c.type = 'button';
      c.addEventListener('click', function () {
        filter = pair[0];
        bar.querySelectorAll('.chip').forEach(function (x) { x.classList.remove('active'); });
        c.classList.add('active');
        renderCards();
      });
      bar.appendChild(c);
    });
  }

  function matchFilter(t) {
    var lv = normLv(t.level);
    if (filter === 'all') return true;
    if (filter === 'attention') return lv === 'fail' || lv === 'warn';
    if (filter === 'scheduled') {
      /* scheduled = trigger node is ok (a schedule binding exists) and enabled. */
      var trig = t.nodes && t.nodes.trigger;
      return !!t.enabled && t.scope !== 'on_alert' && !!trig && normLv(trig.lv) === 'ok';
    }
    if (filter === 'on_alert') return t.scope === 'on_alert';
    if (filter === 'idle') return lv === 'idle';
    return true;
  }

  /* ================= 任務管線卡 ================= */
  var listWrap;

  function buildRail(t, clickable) {
    var rail = el('div', 'pp-rail');
    var nodes = t.nodes || {};
    NODE_DEFS.forEach(function (def, i) {
      if (i) rail.appendChild(el('span', 'pp-link', '→'));
      var ns = nodes[def[0]] || { lv: 'idle', text: '—', sub: '' };
      var node = el('div', 'pp-node n-' + nodeCss(ns.lv));
      var lb = el('div', 'pp-node-label');
      lb.appendChild(el('span', 'pp-dot ' + normLv(ns.lv)));
      lb.appendChild(el('span', null, def[1]));
      node.appendChild(lb);
      node.appendChild(el('div', 'pp-node-text', ns.text || ''));
      node.appendChild(el('div', 'pp-node-sub', ns.sub || ''));
      if (clickable) {
        node.title = '開啟「' + t.name + '」設定抽屜 — ' + def[1].slice(2) + ' 節';
        node.addEventListener('click', function (e) { e.stopPropagation(); openDrawer(t, def[0]); });
      }
      rail.appendChild(node);
    });
    return rail;
  }

  function statusPill(status) {
    var s = normLv(status);  /* I2: 'off' -> 'idle' */
    var map = {
      ok: ['pill-ok', '成功'], partial: ['pill-warn', '部分'], warn: ['pill-warn', '注意'],
      skipped: ['pill-fail', '跳過'], error: ['pill-fail', '失敗'], idle: ['pill-off', '閒置']
    };
    var pair = map[s] || ['pill-off', s];
    var p = el('span', 'pill ' + pair[0]);
    p.appendChild(el('span', 'dot'));
    p.appendChild(document.createTextNode(pair[1]));
    return p;
  }
  window.ppStatusPill = statusPill;

  function renderCards() {
    listWrap.replaceChildren();
    STATE.tasks.filter(matchFilter).forEach(function (t) {
      var lv = normLv(t.level);
      var card = el('div', 'pp-card lv-' + lv);
      card.dataset.screenLabel = '任務卡-' + t.name;
      var head = el('div', 'pp-card-head');
      head.appendChild(el('span', 'pp-card-name', t.name));
      var sm = scopeMeta(t.scope);
      head.appendChild(el('span', 'pp-scope ' + sm.cls, sm.label));
      var st = el('span', 'pp-statusline');
      var lastRun = t.last_run;
      st.appendChild(statusPill(t.enabled ? (lastRun ? lastRun.status : 'idle') : 'idle'));
      st.appendChild(el('span', null, lastRun ? f.datetime(lastRun.at) : '尚未執行'));
      head.appendChild(st);
      head.appendChild(el('span', 'spacer'));

      var needWhy = lv === 'idle' || (lastRun && lastRun.status === 'skipped') || lv === 'fail';
      if (needWhy) {
        var why = el('button', 'btn btn-sm', '為什麼沒跑？');
        why.type = 'button';
        why.title = '逐道閘門診斷此任務為何未產出洞察（零成本）';
        why.addEventListener('click', function (e) { e.stopPropagation(); window.ppDiagnose(t); });
        head.appendChild(why);
      }
      var pf = el('button', 'btn btn-sm', '乾跑預檢');
      pf.type = 'button';
      pf.title = '不呼叫 LLM：跑完整守門檢查（R1–R8）＋組裝提示詞預覽＋成本估算';
      pf.addEventListener('click', function (e) { e.stopPropagation(); window.ppPreflight(t); });
      head.appendChild(pf);

      var tg = el('button', 'toggle' + (t.enabled ? ' on' : ''));
      tg.type = 'button';
      tg.setAttribute('role', 'switch');
      tg.title = '啟用/暫停此任務：暫停後排程保留但不執行';
      tg.addEventListener('click', function (e) { e.stopPropagation(); setEnabled(t, !t.enabled); });
      head.appendChild(tg);
      card.appendChild(head);
      card.appendChild(buildRail(t, true));
      card.addEventListener('click', function () { openDrawer(t, null); });
      listWrap.appendChild(card);
    });
    if (!listWrap.children.length) {
      listWrap.appendChild(el('div', 'wz-note',
        STATE.tasks.length ? '沒有符合篩選的任務。' : '尚無洞察任務 — 點右上「＋ 新增洞察任務」建立第一條管線。'));
    }
  }
  window.ppRenderCards = renderCards;

  /* ================= 分層抽屜 ================= */
  function kvRow(k, v, bold) {
    var r = el('div', 'pp-kv');
    r.appendChild(el('span', 'k', k));
    var vv = el('span', 'v');
    if (bold) vv.appendChild(el('b', null, v)); else vv.textContent = v;
    r.appendChild(vv);
    return r;
  }

  function openDrawer(t, focusKey) {
    document.querySelectorAll('.pp-drawer-backdrop, .pp-drawer').forEach(function (n) { n.remove(); });
    var back = el('div', 'pp-drawer-backdrop');
    var dr = el('aside', 'pp-drawer');
    dr.dataset.screenLabel = '任務抽屜-' + t.name;
    var closeAll = function () { back.remove(); dr.remove(); };
    back.addEventListener('click', closeAll);

    var head = el('div', 'pp-d-head');
    head.appendChild(el('span', 'pp-d-title', t.name));
    var sm = scopeMeta(t.scope);
    head.appendChild(el('span', 'pp-scope ' + sm.cls, sm.label));
    head.appendChild(el('span', 'spacer'));
    var pf = el('button', 'btn btn-sm', '乾跑預檢');
    pf.type = 'button';
    pf.addEventListener('click', function () { window.ppPreflight(t); });
    head.appendChild(pf);
    var del = el('button', 'btn btn-sm btn-danger', '刪除任務');
    del.type = 'button';
    del.addEventListener('click', function () { window.confirmDialog({
      title: '刪除洞察任務 — ' + t.name,
      body: '將同步移除排程、整條校正鏈封存；歷史洞察與運行記錄保留可反查（軟刪除）。',
      confirmLabel: '確認刪除', danger: true,
      onConfirm: function () {
        if (!pdApi) { closeAll(); return; }
        pdApi.del('/api/insight-tasks/' + t.id).then(function () {
          closeAll();
          window.toast('已刪除', 'ok', t.name + '：排程已同步移除、校正鏈封存');
          refresh();
        }).catch(function (err) {
          window.toast((err && err.message) || '刪除失敗', 'fail', err && err.code);
        });
      }
    }); });
    head.appendChild(del);
    var x = el('button', 'sd-close', '✕');
    x.type = 'button';
    x.addEventListener('click', closeAll);
    head.appendChild(x);
    dr.appendChild(head);

    var nav = el('div', 'pp-d-nav');
    var body = el('div', 'pp-d-body');
    var secs = {};
    var navTo = function (id) {
      var sec = secs[id];
      if (sec) body.scrollTo({ top: sec.offsetTop - 46, behavior: 'smooth' });
    };
    [['pipe', '管線'], ['trigger', '觸發'], ['input', '輸入'], ['assemble', '組裝'],
     ['calib', '校正版本'], ['runs', '運行記錄']].forEach(function (pair) {
      var c = el('button', 'chip', pair[1]);
      c.type = 'button';
      c.addEventListener('click', function () { navTo(pair[0]); });
      nav.appendChild(c);
    });
    dr.appendChild(nav);

    var sec = function (id, title) {
      var s = el('div', 'pp-d-sec');
      s.appendChild(el('h3', null, title));
      secs[id] = s;
      body.appendChild(s);
      return s;
    };

    /* -- 管線總覽 -- */
    var sPipe = sec('pipe', '管線 — 一次執行的完整路徑');
    sPipe.appendChild(buildRail(t, false));
    var lastRun = t.last_run;
    if (lastRun && lastRun.notes && lastRun.notes.length) {
      var c = el('div', 'pp-d-card');
      lastRun.notes.forEach(function (nt) { c.appendChild(el('div', null, '• ' + window.ppSkipReason(nt))); });
      c.style.marginTop = '8px';
      sPipe.appendChild(c);
    }

    /* -- 觸發 -- */
    var sTrig = sec('trigger', '① 觸發');
    var cTrig = el('div', 'pp-d-card');
    var trigNode = (t.nodes && t.nodes.trigger) || {};
    if (t.scope === 'on_alert') {
      cTrig.appendChild(kvRow('觸發方式', '預警事件（不可排程）', true));
      cTrig.appendChild(kvRow('狀態', trigNode.text || '預警觸發'));
      cTrig.appendChild(kvRow('防抖', '同一（規則×標的）24h 內不重複觸發（R7）'));
    } else {
      cTrig.appendChild(kvRow('排程', trigNode.text || '—', true));
      if (trigNode.sub) cTrig.appendChild(kvRow('說明', trigNode.sub));
      var acts = el('div', 'pp-d-actions');
      var b1 = el('button', 'btn btn-sm btn-primary',
        normLv(trigNode.lv) === 'ok' ? '調整週期' : '啟動排程');
      b1.type = 'button';
      b1.addEventListener('click', function () { window.ppScheduleModal(t); });
      acts.appendChild(b1);
      cTrig.appendChild(acts);
    }
    sTrig.appendChild(cTrig);

    /* -- 輸入 -- */
    var sIn = sec('input', '② 輸入 — 範圍與標的');
    var cIn = el('div', 'pp-d-card');
    var inNode = (t.nodes && t.nodes.input) || {};
    cIn.appendChild(kvRow('範圍', inNode.text || sm.label, true));
    if (inNode.sub) cIn.appendChild(kvRow('說明', inNode.sub));
    if (t.scope === 'per_symbol') {
      var ed = el('div', 'pp-d-actions');
      var be = el('button', 'btn btn-sm', '編輯標的');
      be.type = 'button';
      be.addEventListener('click', function () { window.toast('編輯標的', 'ok', '沿用既有標的選擇器（持倉＋觀察清單）'); });
      ed.appendChild(be);
      cIn.appendChild(ed);
    }
    sIn.appendChild(cIn);

    /* -- 組裝 -- */
    var sAsm = sec('assemble', '③ 組裝 — 實際送出的提示詞層');
    var asmNode = (t.nodes && t.nodes.assemble) || {};
    var cAsm = el('div', 'pp-d-card');
    cAsm.appendChild(kvRow('組裝狀態', asmNode.text || '—', true));
    if (asmNode.sub) cAsm.appendChild(kvRow('說明', asmNode.sub));
    sAsm.appendChild(cAsm);
    var asmActs = el('div', 'pp-d-actions');
    asmActs.style.marginTop = '8px';
    var bLib = el('a', 'btn btn-sm', '前往分析模板庫 →');
    bLib.href = 'settings-prompts.html';
    asmActs.appendChild(bLib);
    sAsm.appendChild(asmActs);

    /* -- 校正版本（async: GET /api/calibrations?insight_type={id}）-- */
    var sCal = sec('calib', '④ 校正版本鏈（1:1）');
    var calBox = el('div', 'pp-d-card');
    calBox.appendChild(el('div', null, '載入校正版本…'));
    sCal.appendChild(calBox);
    loadCalibrations(t, sCal, calBox);

    /* -- 運行記錄（async: GET /api/insight-tasks/{id}/runs）-- */
    var sRun = sec('runs', '⑤ 運行記錄 — 本任務');
    var runBox = el('div');
    runBox.appendChild(el('div', 'wz-note', '載入運行記錄…'));
    sRun.appendChild(runBox);
    loadRuns(t, runBox);
    var allRuns = el('a', 'btn btn-sm', '完整運行中心（全部任務）→');
    allRuns.href = 'settings-scheduler.html';
    allRuns.style.marginTop = '9px';
    allRuns.style.display = 'inline-flex';
    sRun.appendChild(allRuns);

    dr.appendChild(body);
    document.body.appendChild(back);
    document.body.appendChild(dr);
    if (focusKey) {
      var map = { trigger: 'trigger', input: 'input', assemble: 'assemble',
        exec: 'runs', output: 'runs', calib: 'calib' };
      requestAnimationFrame(function () { navTo(map[focusKey] || 'pipe'); });
    }
  }
  window.ppOpenDrawer = openDrawer;

  /* 校正版本鏈 — rendered from GET /api/calibrations?insight_type={id}. The status payload
     (build_status) does NOT carry active_calibration_version / self_correct, so we read the
     FULL insight-type row (GET /api/insight-tasks) in parallel to flag the active version. */
  function loadCalibrations(t, sCal, placeholder) {
    if (!pdApi) { placeholder.replaceChildren(el('div', null, '校正版本不可用。')); return; }
    Promise.all([
      pdApi.get('/api/calibrations', { insight_type: t.id }),
      pdApi.get('/api/insight-tasks').catch(function () { return []; })
    ]).then(function (res) {
      var vers = res[0];
      var full = (Array.isArray(res[1]) ? res[1] : []).find(function (x) { return x.id === t.id; });
      var activeVer = full ? full.active_calibration_version : null;
      var selfCorrect = full ? full.self_correct : !!t.self_correct;
      sCal.querySelectorAll('.pp-d-card, .pp-vers, .wz-note').forEach(function (n) { n.remove(); });
      var list = Array.isArray(vers) ? vers : [];
      if (!list.length) {
        var c = el('div', 'pp-d-card');
        c.appendChild(el('div', null, selfCorrect
          ? '累積樣本中 — 達「最低樣本數」後，AI 大師模型才會生成初版。'
          : '自我校正未啟動 — 不附加校正層。'));
        sCal.appendChild(c);
        return;
      }
      var wrap = el('div', 'pp-vers');
      var latest = list[list.length - 1];
      list.slice().reverse().forEach(function (v) {
        var isActive = activeVer === v.version;
        var row = el('div', 'pp-ver' + (v.archived ? ' archived' : '') + (isActive ? ' is-active' : ''));
        var top = el('div', 'pp-ver-top');
        top.appendChild(el('span', 'vid', 'v' + v.version));
        top.appendChild(el('span', 'date', f.date(v.created_at)));
        if (isActive) top.appendChild(el('span', 'pill pill-ok', '生效中'));
        else if (v.archived) top.appendChild(el('span', 'pill pill-off', '已封存'));
        else if (v.version === latest.version) top.appendChild(el('span', 'pill pill-warn', '最新版'));
        top.appendChild(el('span', 'spacer'));
        if (!v.archived) {
          var ba = el('button', 'btn btn-sm', '封存');
          ba.type = 'button';
          ba.addEventListener('click', function () { window.confirmDialog({
            title: '封存 v' + v.version + ' — ' + t.name,
            body: '封存後從選擇器移除、不可再套用；歸因記錄保留可反查（軟刪除）。',
            confirmLabel: '確認封存', danger: true,
            onConfirm: function () {
              pdApi.post('/api/calibrations/' + v.id + '/archive').then(function () {
                window.toast('已封存', 'ok', 'v' + v.version);
                openDrawer(t, 'calib');
              }).catch(function (err) {
                window.toast((err && err.message) || '封存失敗', 'fail', err && err.code);
              });
            }
          }); });
          top.appendChild(ba);
        }
        row.appendChild(top);
        if (v.cause) row.appendChild(el('div', 'pp-ver-cause', '產生原因：' + v.cause));
        var det = document.createElement('details');
        det.appendChild(el('summary', null, '查看校正內文'));
        det.appendChild(el('pre', null, v.body || ''));
        row.appendChild(det);
        wrap.appendChild(row);
      });
      sCal.appendChild(wrap);
    }).catch(function () {
      placeholder.replaceChildren(el('div', null, '校正版本載入失敗。'));
    });
  }

  /* 運行記錄 — rendered from GET /api/insight-tasks/{id}/runs. */
  function loadRuns(t, runBox) {
    if (!pdApi) { runBox.replaceChildren(el('div', 'wz-note', '運行記錄不可用。')); return; }
    pdApi.get('/api/insight-tasks/' + t.id + '/runs', { limit: 20 }).then(function (resp) {
      var rows = (resp && resp.rows) || [];
      var tbl = el('table', 'pp-runs');
      tbl.innerHTML = '<thead><tr><th class="num">時間</th><th>結果</th><th>原因/說明</th><th class="num">費用</th></tr></thead>';
      var tb = el('tbody');
      rows.forEach(function (r) {
        var tr = el('tr', r.status === 'skipped' ? 'r-skipped' : null);
        tr.appendChild(el('td', 'num', f.datetime(r.finished_at || r.started_at)));
        var td = el('td');
        td.appendChild(statusPill(r.status));
        tr.appendChild(td);
        tr.appendChild(el('td', null, r.detail || (r.reason ? window.ppSkipReason(r.reason) : '—')));
        /* cost_usd Decimal STRING -> f.num (no bare .toFixed); null -> em-dash. */
        tr.appendChild(el('td', 'num', r.cost_usd == null ? f.NULL_GLYPH : '$' + f.num(r.cost_usd, 3)));
        tb.appendChild(tr);
      });
      if (!tb.children.length) {
        var tr2 = el('tr');
        var td2 = el('td', null, '尚無運行記錄');
        td2.colSpan = 4;
        tr2.appendChild(td2);
        tb.appendChild(tr2);
      }
      tbl.appendChild(tb);
      runBox.replaceChildren(tbl);
    }).catch(function () {
      runBox.replaceChildren(el('div', 'wz-note', '運行記錄載入失敗。'));
    });
  }

  /* 排程設定 modal（POST /api/insight-tasks/{id}/schedule with a cron）. */
  window.ppScheduleModal = function (t, onDone) {
    window.ppModal('排程 — ' + t.name, function (body, close) {
      body.appendChild(el('div', 'pv-note', '寫入運行中心的同一筆 job；之後在這裡或運行中心調整皆可（同一記錄）。'));
      var fld = function (label, node) {
        var w = el('div', 'pv-field');
        w.appendChild(el('label', null, label));
        w.appendChild(node);
        return w;
      };
      var CRONS = [['每日 08:00', '0 8 * * *'], ['每週一 09:00', '0 9 * * 1'],
        ['每週五 17:00', '0 17 * * 5'], ['每月 1 日 08:00', '0 8 1 * *']];
      var period = el('select', 'select');
      CRONS.forEach(function (pair) {
        var o = el('option', null, pair[0]); o.value = pair[1]; period.appendChild(o);
      });
      var row = el('div', 'pv-fields');
      row.appendChild(fld('週期', period));
      body.appendChild(row);
      var acts = el('div', 'cal-actions');
      var ok = el('button', 'btn btn-primary', '儲存排程');
      ok.type = 'button';
      ok.addEventListener('click', function () {
        if (!pdApi) { close(); return; }
        pdApi.post('/api/insight-tasks/' + t.id + '/schedule', { cron: period.value }).then(function () {
          close();
          window.toast('已儲存排程', 'ok', t.name);
          refresh();
          if (onDone) onDone();
        }).catch(function (err) {
          window.toast((err && err.message) || '排程儲存失敗', 'fail', err && err.code);
        });
      });
      acts.appendChild(ok);
      body.appendChild(acts);
    });
  };

  /* ================= boot / refresh ================= */
  function refresh() {
    if (!pdApi) {
      STATE = { tasks: [], health: null };
      renderHealth(); renderFilterBar(); renderCards();
      return;
    }
    return pdApi.get('/api/insight-tasks/status').then(function (resp) {
      STATE.tasks = (resp && resp.tasks) || [];
      STATE.health = (resp && resp.health) || null;
      renderHealth();
      renderFilterBar();
      renderCards();
    }).catch(function () {
      STATE = { tasks: [], health: null };
      renderHealth(); renderFilterBar(); renderCards();
      if (window.toast) window.toast('任務狀態載入失敗', 'fail', '已顯示空狀態');
    });
  }
  window.ppRefresh = refresh;

  function init() {
    listWrap = $('#pp-list');
    if (!listWrap) return;       /* mount point absent — nothing to render */
    renderHealth();              /* initial empty shell while the fetch is in flight */
    renderFilterBar();
    refresh();
  }
  /* The pipeline scripts load at the END of <body>, so the DOM is already parsed by the
     time this runs (readyState 'interactive' or 'complete'); init synchronously. Guard the
     'loading' case (defensive) with a single listener so init runs EXACTLY once. */
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
