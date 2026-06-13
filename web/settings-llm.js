/* portfolio-dash — 設定 · LLM 與額度 (mock + rendering) */
window.LLM_DATA = {
  "ai_enabled": true,
  "models": [
    { "alias": "claude-sonnet", "provider": "anthropic", "model_name": "claude-sonnet-4-5",
      "api_base": "https://api.anthropic.com", "api_key_masked": "sk-•••••••3f2",
      "vision": true, "price_in": 3.00, "price_out": 15.00, "context_window": 200000,
      "max_output_tokens": 8192, "timeout_seconds": 60, "max_retries": 2, "enabled": true, "notes": "",
      "health": "ok", "last_called": "2026-06-11T08:00:12+08:00" },
    { "alias": "claude-opus", "provider": "anthropic", "model_name": "claude-opus-4-5",
      "api_base": "https://api.anthropic.com", "api_key_masked": "sk-•••••••3f2",
      "vision": true, "price_in": 15.00, "price_out": 75.00, "context_window": 200000,
      "max_output_tokens": 8192, "timeout_seconds": 120, "max_retries": 2, "enabled": true, "notes": "AI 大師（校正）專用",
      "health": "ok", "last_called": "2026-06-10T22:00:05+08:00" },
    { "alias": "gpt-4o-mini", "provider": "openai-compatible", "model_name": "gpt-4o-mini",
      "api_base": "https://api.openai.com/v1", "api_key_masked": "sk-•••••••9d1",
      "vision": false, "price_in": 0.15, "price_out": 0.60, "context_window": 128000,
      "max_output_tokens": 4096, "timeout_seconds": 45, "max_retries": 2, "enabled": true, "notes": "後備用",
      "health": "ok", "last_called": "2026-06-11T08:00:12+08:00" },
    { "alias": "qwen-vl", "provider": "openrouter", "model_name": "qwen/qwen2.5-vl-72b",
      "api_base": "https://openrouter.ai/api/v1", "api_key_masked": "sk-•••••••k88",
      "vision": true, "price_in": 0.40, "price_out": 0.40, "context_window": 32000,
      "max_output_tokens": 2048, "timeout_seconds": 90, "max_retries": 1, "enabled": false, "notes": "測試中",
      "health": "error", "last_called": "2026-06-08T14:22:00+08:00" }
  ],
  "roles": {
    "default_model": "claude-sonnet",
    "default_fallback": "gpt-4o-mini",
    "vision_model": "claude-sonnet",
    "vision_fallback": null,
    "master_model": "claude-opus",
    "master_fallback": null
  },
  "quota": {
    "remaining_usd": 3.84,
    "note": "由 liteLLM endpoint 回傳累計成本後遞減；歸零即停止呼叫",
    "alert_threshold_usd": 1.00,
    "topups": [
      { "at": "2026-06-01T09:00:00+08:00", "amount_usd": 10.00, "note": "六月加值" },
      { "at": "2026-04-15T10:30:00+08:00", "amount_usd": 10.00, "note": "四月加值" },
      { "at": "2026-03-02T08:12:00+08:00", "amount_usd": 5.00, "note": "首次加值" }
    ]
  },
  "usage": {
    "by_model": [
      { "alias": "claude-sonnet", "calls": 42, "tokens_in": 512400, "tokens_out": 96100, "cost_usd": 4.83 },
      { "alias": "gpt-4o-mini", "calls": 18, "tokens_in": 96300, "tokens_out": 31200, "cost_usd": 1.33 },
      { "alias": "qwen-vl", "calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0 }
    ],
    "by_agent": [
      { "agent": "ai_agents_input", "cost_usd": 1.92 },
      { "agent": "insight", "cost_usd": 0.00 }
    ],
    "daily": [
      ["05-13", 0.18, 0.05], ["05-14", 0.21, 0.04], ["05-15", 0.16, 0.06], ["05-16", 0.00, 0.00],
      ["05-17", 0.00, 0.00], ["05-18", 0.22, 0.05], ["05-19", 0.19, 0.03], ["05-20", 0.24, 0.06],
      ["05-21", 0.17, 0.04], ["05-22", 0.20, 0.05], ["05-23", 0.00, 0.00], ["05-24", 0.00, 0.00],
      ["05-25", 0.23, 0.04], ["05-26", 0.18, 0.05], ["05-27", 0.21, 0.06], ["05-28", 0.26, 0.04],
      ["05-29", 0.19, 0.03], ["05-30", 0.00, 0.00], ["05-31", 0.00, 0.00], ["06-01", 0.22, 0.05],
      ["06-02", 0.21, 0.04], ["06-03", 0.24, 0.05], ["06-04", 0.20, 0.03], ["06-05", 0.23, 0.06],
      ["06-06", 0.18, 0.04], ["06-07", 0.00, 0.00], ["06-08", 0.31, 0.05], ["06-09", 0.27, 0.04],
      ["06-10", 0.29, 0.06], ["06-11", 0.26, 0.05]
    ]
  }
};

(function () {
  'use strict';
  const D = window.LLM_DATA;
  const f = window.fmt;
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const usd = (v) => '$' + Number(v).toFixed(2);

  /* ---- AI 狀態 chip ---- */
  function renderStatus() {
    const chip = $('#ai-status');
    const anyRole = Object.values(D.roles).some((v) => v !== null);
    const remaining = D.quota.remaining_usd;
    const threshold = D.quota.alert_threshold_usd || 1.00;
    if (!anyRole) {
      chip.className = 'pill pill-off';
      chip.replaceChildren(el('span', 'dot'), document.createTextNode('AI：已關閉'));
    } else if (remaining !== null && remaining <= 0) {
      chip.className = 'pill pill-fail';
      chip.replaceChildren(el('span', 'dot'), document.createTextNode('AI：額度歸零'));
      chip.title = '剩餘額度已歸零，請添加額度';
    } else if (remaining !== null && remaining < threshold) {
      chip.className = 'pill';
      chip.style.color = 'var(--amber)';
      chip.style.borderColor = 'rgba(217,161,63,0.4)';
      chip.style.background = 'var(--amber-soft)';
      chip.replaceChildren(el('span', 'dot'), document.createTextNode('AI：額度偏低 $' + remaining.toFixed(2)));
      chip.title = '剩餘額度 $' + remaining.toFixed(2) + '，低於警示閾值 $' + threshold.toFixed(2);
    } else {
      chip.className = 'pill pill-ok';
      chip.style.color = '';
      chip.style.borderColor = '';
      chip.style.background = '';
      chip.replaceChildren(el('span', 'dot'), document.createTextNode('AI：啟用中'));
      chip.title = '';
    }
  }

  /* ---- Section A 模型註冊表 ---- */
  function renderModels() {
    const tbody = $('#model-body');
    tbody.replaceChildren();
    D.models.forEach((m) => {
      const tr = el('tr');
      const HEALTH_LABEL = { ok: 'API 可連', error: 'API 無法連線', unknown: '未測試' };
      const HEALTH_CLS   = { ok: 'pill-ok', error: 'pill-fail', unknown: '' };
      /* health dot + alias combined cell */
      const tdAlias = el('td', 'col-text');
      const healthRow = el('div', 'model-health-row');
      const dot = el('span', 'run-dot ' + (m.health === 'ok' ? 'dot-ok' : m.health === 'error' ? 'dot-err' : 'dot-gray'));
      dot.style.width = '9px';
      dot.style.height = '9px';
      dot.style.flex = 'none';
      healthRow.appendChild(dot);
      healthRow.appendChild(el('span', 'cron-code', m.alias));
      /* last latency badge */
      if (m.latency_ms) {
        const lat = el('span', 'ping-lat', m.latency_ms + ' ms');
        lat.title = '上次 ping 延遲';
        healthRow.appendChild(lat);
      }
      /* ping button */
      const pingBtn = el('button', 'btn ping-btn', '⚡');
      pingBtn.type = 'button';
      pingBtn.title = '立即 ping — 測試 API 可用性';
      pingBtn.addEventListener('click', () => {
        pingBtn.disabled = true; pingBtn.textContent = '…';
        setTimeout(() => {
          pingBtn.disabled = false; pingBtn.textContent = '⚡';
          const ok = m.health !== 'error';
          const latMs = Math.round(200 + Math.random() * 600);
          dot.className = 'run-dot ' + (ok ? 'dot-ok' : 'dot-err');
          if (ok && !healthRow.querySelector('.ping-lat')) {
            const l = el('span', 'ping-lat', latMs + ' ms');
            healthRow.insertBefore(l, pingBtn);
          } else if (ok) {
            healthRow.querySelector('.ping-lat').textContent = latMs + ' ms';
          }
          window.toast(ok ? 'Ping 成功' : 'Ping 失敗',
            ok ? 'ok' : 'fail',
            m.alias + (ok ? ' · ' + latMs + ' ms' : ' · API 無法連線'));
        }, 700);
      });
      healthRow.appendChild(pingBtn);
      tdAlias.appendChild(healthRow);
      tr.appendChild(tdAlias);
      tr.appendChild(el('td', 'col-text', m.provider));
      tr.appendChild(el('td', 'col-text num', m.model_name));
      tr.appendChild(el('td', 'col-text', m.vision ? '✓' : '—'));
      tr.appendChild(el('td', 'num', '$' + m.price_in.toFixed(2)));
      tr.appendChild(el('td', 'num', '$' + m.price_out.toFixed(2)));
      tr.appendChild(el('td', 'num', f.num(m.context_window / 1000) + 'k'));
      tr.appendChild(el('td', 'num', m.timeout_seconds + 's'));
      /* last called */
      const tdLast = el('td', 'num');
      if (!m.last_called) { tdLast.textContent = f.NULL_GLYPH; tdLast.classList.add('sign-nil'); }
      else tdLast.textContent = f.datetime(m.last_called);
      tr.appendChild(tdLast);
      const tdTog = el('td');
      const t = el('button', 'toggle' + (m.enabled ? ' on' : ''));
      t.type = 'button';
      t.setAttribute('role', 'switch');
      t.addEventListener('click', () => t.classList.toggle('on'));
      tdTog.appendChild(t);
      tr.appendChild(tdTog);
      const tdAct = el('td');
      const acts = el('div', 'wl-actions');
      const edit = el('button', 'btn', '編輯');
      edit.type = 'button';
      edit.addEventListener('click', () => openDrawer(m));
      const del = el('button', 'btn btn-danger', '刪除');
      del.type = 'button';
      del.addEventListener('click', () => window.confirmDialog({
        title: '刪除模型 — ' + m.alias,
        body: '刪除後，引用此模型的角色預設將被清空；歷史用量紀錄保留。此動作無法復原。',
        confirmLabel: '確認刪除', danger: true,
        onConfirm: () => window.toast('已刪除', 'ok', m.alias + '（設計稿 — 未實際刪除）')
      }));
      acts.appendChild(edit);
      acts.appendChild(del);
      tdAct.appendChild(acts);
      tr.appendChild(tdAct);
      tbody.appendChild(tr);
    });
  }

  /* ---- edit drawer ---- */
  function openDrawer(m) {
    $('#drawer-backdrop').hidden = false;
    $('#drawer-title').textContent = m ? '編輯模型 — ' + m.alias : '新增模型';
    const set = (id, v) => { $('#' + id).value = v; };
    const x = m || { alias: '', provider: 'anthropic', model_name: '', api_base: '',
      api_key_masked: '', vision: false, price_in: 0, price_out: 0, context_window: 128000,
      max_output_tokens: 4096, timeout_seconds: 60, max_retries: 2, notes: '' };
    set('dr-alias', x.alias); set('dr-provider', x.provider); set('dr-model', x.model_name);
    set('dr-base', x.api_base); set('dr-key', x.api_key_masked || '（尚未設定）');
    set('dr-pin', x.price_in); set('dr-pout', x.price_out);
    set('dr-ctx', x.context_window); set('dr-maxout', x.max_output_tokens);
    set('dr-timeout', x.timeout_seconds); set('dr-retries', x.max_retries);
    set('dr-notes', x.notes || '');
    const vt = $('#dr-vision');
    vt.classList.toggle('on', !!x.vision);
  }
  function closeDrawer() { $('#drawer-backdrop').hidden = true; }
  $('#drawer-close').addEventListener('click', closeDrawer);
  $('#drawer-cancel').addEventListener('click', closeDrawer);
  $('#drawer-backdrop').addEventListener('click', (e) => {
    if (e.target === $('#drawer-backdrop')) closeDrawer();
  });
  $('#drawer-save').addEventListener('click', () => {
    closeDrawer();
    window.toast('已儲存', 'ok', '模型設定已更新（設計稿）');
  });
  $('#dr-vision').addEventListener('click', () => $('#dr-vision').classList.toggle('on'));
  $('#dr-key-reset').addEventListener('click', () => {
    $('#dr-key').value = '';
    $('#dr-key').readOnly = false;
    $('#dr-key').placeholder = '輸入新的 API Key…';
    $('#dr-key').focus();
  });
  $('#model-add').addEventListener('click', () => openDrawer(null));

  /* ---- Section B 角色預設 ---- */
  function renderRoles() {
    const ROLES = [
      ['role-default', 'default_model'], ['role-default-fb', 'default_fallback'],
      ['role-vision', 'vision_model'], ['role-vision-fb', 'vision_fallback'],
      ['role-master', 'master_model'], ['role-master-fb', 'master_fallback']
    ];
    ROLES.forEach(([id, key]) => {
      const sel = $('#' + id);
      if (!sel) return; /* 頁面可能缺少部分角色欄位 — 略過 */
      sel.replaceChildren();
      const empty = el('option', null, '（空 = 關閉）');
      empty.value = '';
      sel.appendChild(empty);
      D.models.filter((m) => m.enabled).forEach((m) => {
        const o = el('option', null, m.alias);
        o.value = m.alias;
        if (D.roles[key] === m.alias) o.selected = true;
        sel.appendChild(o);
      });
      if (D.roles[key] === null) empty.selected = true;
    });
  }

  /* ---- Section C 額度治理 ---- */
  function renderQuota() {
    const Q = D.quota;
    const remaining = Q.remaining_usd;
    const threshold = Q.alert_threshold_usd || 1.00;
    const qv = $('#quota-value');
    qv.textContent = remaining === null ? '無上限' : usd(remaining);
    qv.style.color = remaining !== null && remaining <= 0
      ? 'var(--up)'
      : remaining !== null && remaining < threshold
        ? 'var(--amber)' : '';
    $('#quota-threshold').value = threshold;

    /* topup history */
    const tbody = $('#topup-body');
    tbody.replaceChildren();
    Q.topups.forEach((r) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'num', f.datetime(r.at)));
      const tdAmt = el('td', 'num sign-down', '+' + usd(r.amount_usd));
      tr.appendChild(tdAmt);
      tr.appendChild(el('td', 'col-text', r.note || ''));
      tbody.appendChild(tr);
    });

    /* 添加額度 modal */
    $('#quota-topup').addEventListener('click', () => {
      /* build custom modal with amount input */
      const backdrop = el('div', 'modal-backdrop');
      const modal = el('div', 'modal');
      const head = el('div', 'modal-head');
      head.appendChild(el('h3', 'modal-title', '添加額度'));
      const close = el('button', 'modal-close', '✕'); close.type = 'button';
      head.appendChild(close);
      modal.appendChild(head);
      const body = el('div', 'modal-body');
      const desc = el('p');
      desc.innerHTML = '輸入欲添加的金額（USD）。系統將寫入一筆加值事件，' +
        '餘額立即更新；歸零後 AI 呼叫將自動暫停。<br>' +
        '<span style="color:var(--text-3);font-size:11px">目前餘額：' + usd(remaining) + ' USD</span>';
      body.appendChild(desc);
      const f1 = el('div', 'field');
      f1.appendChild(el('label', null, '加值金額（USD）'));
      const inp = el('input', 'input');
      inp.type = 'number'; inp.min = '0.01'; inp.step = '0.01'; inp.placeholder = '10.00';
      inp.style.fontFamily = 'var(--font-num)';
      f1.appendChild(inp);
      body.appendChild(f1);
      const f2 = el('div', 'field');
      f2.appendChild(el('label', null, '備註（選填）'));
      const noteIn = el('input', 'input'); noteIn.type = 'text';
      f2.appendChild(noteIn);
      body.appendChild(f2);
      modal.appendChild(body);
      const foot = el('div', 'modal-foot');
      const cancel = el('button', 'btn', '取消'); cancel.type = 'button';
      const ok = el('button', 'btn btn-primary', '確認加值'); ok.type = 'button';
      foot.appendChild(cancel); foot.appendChild(ok);
      modal.appendChild(foot);
      backdrop.appendChild(modal);
      const dismiss = () => backdrop.remove();
      close.addEventListener('click', dismiss);
      cancel.addEventListener('click', dismiss);
      backdrop.addEventListener('click', (e) => { if (e.target === backdrop) dismiss(); });
      ok.addEventListener('click', () => {
        const amt = parseFloat(inp.value);
        if (!amt || amt <= 0) { inp.classList.add('field-error'); return; }
        dismiss();
        window.toast('加值成功', 'ok',
          '+' + usd(amt) + ' USD 已加入額度（設計稿）');
      });
      document.body.appendChild(backdrop);
      setTimeout(() => inp.focus(), 50);
    });
  }

  /* ---- Section D 用量與趨勢 ---- */
  function renderUsage() {
    const tbody = $('#usage-body');
    D.usage.by_model.forEach((u) => {
      const tr = el('tr');
      const td = el('td', 'col-text');
      td.appendChild(el('span', 'cron-code', u.alias));
      tr.appendChild(td);
      tr.appendChild(el('td', 'num', f.num(u.calls)));
      tr.appendChild(el('td', 'num', f.num(u.tokens_in)));
      tr.appendChild(el('td', 'num', f.num(u.tokens_out)));
      tr.appendChild(el('td', 'num', u.cost_usd ? usd(u.cost_usd) : f.NULL_GLYPH));
      tbody.appendChild(tr);
    });
    const chips = $('#agent-chips');
    D.usage.by_agent.forEach((a) => {
      const chip = el('span', 'ccy-chip');
      chip.appendChild(el('span', null, a.agent + ' '));
      chip.appendChild(el('b', null, usd(a.cost_usd)));
      chips.appendChild(chip);
    });

    const chart = echarts.init($('#llm-daily-chart'));
    const axisStyle = { color: '#5e6b7c', fontSize: 9, fontFamily: "'IBM Plex Mono', monospace" };
    chart.setOption({
      grid: { left: 40, right: 8, top: 26, bottom: 22 },
      legend: {
        top: 0, left: 0, icon: 'rect', itemWidth: 10, itemHeight: 3,
        textStyle: { color: '#9aa6b5', fontSize: 10, fontFamily: "'IBM Plex Mono', monospace" }
      },
      tooltip: {
        trigger: 'axis',
        backgroundColor: '#18202b', borderColor: '#232e3c',
        textStyle: { color: '#e6ebf2', fontSize: 11, fontFamily: "'IBM Plex Mono', monospace" },
        valueFormatter: (v) => '$' + Number(v).toFixed(2)
      },
      xAxis: {
        type: 'category', data: D.usage.daily.map((d) => d[0]),
        axisLine: { lineStyle: { color: '#232e3c' } }, axisTick: { show: false },
        axisLabel: { ...axisStyle, interval: 4 }
      },
      yAxis: {
        type: 'value',
        splitLine: { lineStyle: { color: '#232e3c', type: 'dashed' } },
        axisLabel: { ...axisStyle, formatter: '${value}' }
      },
      series: [
        { name: 'claude-sonnet', type: 'line', showSymbol: false,
          lineStyle: { color: '#58a6dd', width: 1.6 }, itemStyle: { color: '#58a6dd' },
          data: D.usage.daily.map((d) => d[1]) },
        { name: 'gpt-4o-mini', type: 'line', showSymbol: false,
          lineStyle: { color: '#9b86d8', width: 1.6 }, itemStyle: { color: '#9b86d8' },
          data: D.usage.daily.map((d) => d[2]) }
      ]
    });
    window.addEventListener('resize', () => chart.resize());
  }

  $('#llm-restore').addEventListener('click', () => window.confirmDialog({
    title: '關閉全部角色預設',
    body: '將四個角色選單全部設為空，AI 功能將立即停用。已註冊的模型不受影響，重新選定角色後即可恢復。',
    confirmLabel: '確認關閉',
    onConfirm: () => window.toast('角色已關閉', 'ok', '四個角色預設已清空，AI 已停用（設計稿）')
  }));

  renderStatus();
  renderModels();
  renderRoles();
  renderQuota();
  /* defer chart init until LLM tab is actually visible */
  let llmChartInited = false;
  function maybeInitLlmChart() {
    if (llmChartInited) return;
    const host = document.getElementById('llm-daily-chart');
    if (!host || host.offsetHeight === 0) return;
    llmChartInited = true;
    renderUsage();
  }
  /* try immediately (standalone page), then watch for tab activation */
  maybeInitLlmChart();
  window.addEventListener('pd-settings-tab', function(e) {
    if (e.detail === 'llm') maybeInitLlmChart();
  });
  /* fallback: MutationObserver for class change on view-llm */
  const llmView = document.getElementById('view-llm');
  if (llmView) {
    new MutationObserver(maybeInitLlmChart).observe(llmView, { attributes: true, attributeFilter: ['class'] });
  }
})();
