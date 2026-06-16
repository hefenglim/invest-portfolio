/* portfolio-dash — 設定 · LLM 與額度 (wired to /api/llm/*, spec 16/19).

   Boot: GET /api/llm/config -> { models, roles, quota, usage }. The inline
   window.LLM_DATA mock is RETIRED; all structural data + money come from the API.

   MONEY DISCIPLINE (data-and-pricing.md): every money value (price_in/price_out,
   quota.remaining_usd / alert_threshold_usd, usage cost_usd) arrives as a Decimal
   STRING and is rendered ONLY through window.fmt (which Number()-coerces internally) —
   never `bareString.toFixed()`. Counts (calls / tokens / context_window) ride along as
   JSON numbers. The frontend NEVER computes money of record. (C2 fix: the former bare
   `remaining.toFixed(2)` quota chip + `m.price_in.toFixed(2)` model price cells now go
   through f.* / coercion.)

   Write paths (all via pdApi; success -> toast + re-fetch; PdApiError -> toast(message,
   'fail', code); try/catch graceful so a failure never throws an unhandled rejection):
   - PUT /api/llm/roles                 (角色預設 — including "關閉全部角色")
   - PUT /api/llm/quota                 (alert threshold)
   - POST /api/llm/quota/topup          (添加額度)
   - POST /api/llm/models/{alias}/test  (ping)
   - PUT /api/llm/models/{alias}        (edit drawer save / enable toggle)
   - DELETE /api/llm/models/{alias}     (刪除) */
(function () {
  'use strict';
  const f = window.fmt;
  const api = window.pdApi;
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  /* USD with 2 dp via fmt.money (Number()-coerces a Decimal STRING internally;
     NEVER bareString.toFixed). f.money returns the em-dash glyph for null. */
  const usd = (v) => '$' + f.money(v, 'USD');

  /* Structural data from GET /api/llm/config. Starts empty so a pre-fetch render is
     blank; populated on boot. */
  let D = {
    models: [],
    roles: {
      default_model: null, default_fallback: null,
      vision_model: null, vision_fallback: null,
      master_model: null, master_fallback: null,
    },
    quota: { remaining_usd: null, alert_threshold_usd: null, topups: [] },
    usage: { by_model: [], by_agent: [], daily: { dates: [], series: [] } },
  };

  function _toast(msg, kind, code) {
    if (window.toast) window.toast(msg, kind, code);
  }

  /* ---- AI 狀態 chip ---- */
  function renderStatus() {
    const chip = $('#ai-status');
    if (!chip) return;
    const anyRole = Object.values(D.roles).some((v) => v !== null && v !== '');
    /* remaining/threshold are Decimal STRINGS — coerce ONLY for comparison + display. */
    const remaining = D.quota.remaining_usd;
    const remNum = remaining === null ? null : Number(remaining);
    const thrNum = D.quota.alert_threshold_usd === null ? 1.0 : Number(D.quota.alert_threshold_usd);
    if (!anyRole) {
      chip.className = 'pill pill-off';
      chip.replaceChildren(el('span', 'dot'), document.createTextNode('AI：已關閉'));
    } else if (remNum !== null && remNum <= 0) {
      chip.className = 'pill pill-fail';
      chip.replaceChildren(el('span', 'dot'), document.createTextNode('AI：額度歸零'));
      chip.title = '剩餘額度已歸零，請添加額度';
    } else if (remNum !== null && remNum < thrNum) {
      chip.className = 'pill';
      chip.style.color = 'var(--amber)';
      chip.style.borderColor = 'rgba(217,161,63,0.4)';
      chip.style.background = 'var(--amber-soft)';
      chip.replaceChildren(el('span', 'dot'),
        document.createTextNode('AI：額度偏低 ' + usd(remaining)));
      chip.title = '剩餘額度 ' + usd(remaining) + '，低於警示閾值 '
        + usd(D.quota.alert_threshold_usd);
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
    if (!tbody) return;
    tbody.replaceChildren();
    D.models.forEach((m) => {
      const tr = el('tr');
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
      /* ping button -> POST /api/llm/models/{alias}/test */
      const pingBtn = el('button', 'btn ping-btn', '⚡');
      pingBtn.type = 'button';
      pingBtn.title = '立即 ping — 測試 API 可用性';
      pingBtn.addEventListener('click', async () => {
        pingBtn.disabled = true; pingBtn.textContent = '…';
        try {
          const resp = await api.post('/api/llm/models/' + encodeURIComponent(m.alias) + '/test');
          const ok = !!(resp && resp.ok);
          const latMs = resp && resp.latency_ms;
          dot.className = 'run-dot ' + (ok ? 'dot-ok' : 'dot-err');
          if (ok && latMs != null) {
            let l = healthRow.querySelector('.ping-lat');
            if (!l) { l = el('span', 'ping-lat'); healthRow.insertBefore(l, pingBtn); }
            l.textContent = latMs + ' ms';
          }
          _toast(ok ? 'Ping 成功' : 'Ping 失敗', ok ? 'ok' : 'fail',
            m.alias + (ok && latMs != null ? ' · ' + latMs + ' ms'
              : ' · ' + ((resp && resp.error_detail) || 'API 無法連線')));
        } catch (err) {
          dot.className = 'run-dot dot-err';
          _toast((err && err.message) || 'Ping 失敗', 'fail', err && err.code);
        } finally {
          pingBtn.disabled = false; pingBtn.textContent = '⚡';
        }
      });
      healthRow.appendChild(pingBtn);
      tdAlias.appendChild(healthRow);
      tr.appendChild(tdAlias);
      tr.appendChild(el('td', 'col-text', m.provider));
      tr.appendChild(el('td', 'col-text num', m.model_name));
      tr.appendChild(el('td', 'col-text', m.vision ? '✓' : '—'));
      /* price_in/price_out are Decimal STRINGS -> via fmt (NEVER bareString.toFixed). */
      tr.appendChild(el('td', 'num', '$' + f.num(m.price_in, 2)));
      tr.appendChild(el('td', 'num', '$' + f.num(m.price_out, 2)));
      tr.appendChild(el('td', 'num',
        m.context_window != null ? f.num(m.context_window / 1000) + 'k' : f.NULL_GLYPH));
      tr.appendChild(el('td', 'num', m.timeout_seconds != null ? m.timeout_seconds + 's' : f.NULL_GLYPH));
      /* last called */
      const tdLast = el('td', 'num');
      if (!m.last_called) { tdLast.textContent = f.NULL_GLYPH; tdLast.classList.add('sign-nil'); }
      else tdLast.textContent = f.datetime(m.last_called);
      tr.appendChild(tdLast);
      const tdTog = el('td');
      const t = el('button', 'toggle' + (m.enabled ? ' on' : ''));
      t.type = 'button';
      t.setAttribute('role', 'switch');
      t.addEventListener('click', async () => {
        const next = !t.classList.contains('on');
        t.disabled = true;
        try {
          await api.put('/api/llm/models/' + encodeURIComponent(m.alias), { enabled: next });
          _toast('已更新', 'ok', m.alias + (next ? ' 已啟用' : ' 已停用'));
          await boot();
        } catch (err) {
          _toast((err && err.message) || '更新失敗', 'fail', err && err.code);
          t.disabled = false;
        }
      });
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
        onConfirm: async () => {
          try {
            await api.del('/api/llm/models/' + encodeURIComponent(m.alias));
            _toast('已刪除', 'ok', m.alias);
            await boot();
          } catch (err) {
            _toast((err && err.message) || '刪除失敗', 'fail', err && err.code);
          }
        },
      }));
      acts.appendChild(edit);
      acts.appendChild(del);
      tdAct.appendChild(acts);
      tr.appendChild(tdAct);
      tbody.appendChild(tr);
    });
  }

  /* ---- edit drawer ---- */
  let drawerAlias = null; /* null = create (POST), else PUT path */
  function openDrawer(m) {
    drawerAlias = m ? m.alias : null;
    $('#drawer-backdrop').hidden = false;
    $('#drawer-title').textContent = m ? '編輯模型 — ' + m.alias : '新增模型';
    const set = (id, v) => { $('#' + id).value = v; };
    const x = m || { alias: '', provider: 'anthropic', model_name: '', api_base: '',
      api_key_masked: '', vision: false, price_in: '0', price_out: '0', context_window: 128000,
      max_output_tokens: 4096, timeout_seconds: 60, max_retries: 2, notes: '' };
    set('dr-alias', x.alias); set('dr-provider', x.provider); set('dr-model', x.model_name);
    set('dr-base', x.api_base || ''); set('dr-key', x.api_key_masked || '（尚未設定）');
    set('dr-pin', x.price_in); set('dr-pout', x.price_out);
    set('dr-ctx', x.context_window); set('dr-maxout', x.max_output_tokens);
    set('dr-timeout', x.timeout_seconds); set('dr-retries', x.max_retries);
    set('dr-notes', x.notes || '');
    $('#dr-alias').readOnly = !!m; /* alias is the identity; immutable on edit */
    /* key field: readonly placeholder until 重設 unlocks it */
    $('#dr-key').readOnly = true;
    $('#dr-key').dataset.touched = '';
    const vt = $('#dr-vision');
    vt.classList.toggle('on', !!x.vision);
  }
  function closeDrawer() { $('#drawer-backdrop').hidden = true; }
  $('#drawer-close').addEventListener('click', closeDrawer);
  $('#drawer-cancel').addEventListener('click', closeDrawer);
  $('#drawer-backdrop').addEventListener('click', (e) => {
    if (e.target === $('#drawer-backdrop')) closeDrawer();
  });
  $('#drawer-save').addEventListener('click', async () => {
    /* Build a body of EDITABLE fields. Price fields are sent as the user's raw string;
       the backend parses to Decimal. The masked key is never sent unless 重設 unlocked it. */
    const v = (id) => $('#' + id).value;
    const body = {
      provider: v('dr-provider'),
      model_name: v('dr-model'),
      api_base: v('dr-base'),
      vision: $('#dr-vision').classList.contains('on'),
      price_in: v('dr-pin'),
      price_out: v('dr-pout'),
      context_window: Number(v('dr-ctx')) || null,
      max_output_tokens: Number(v('dr-maxout')) || null,
      timeout_seconds: Number(v('dr-timeout')) || null,
      max_retries: Number(v('dr-retries')) || null,
      notes: v('dr-notes'),
    };
    if ($('#dr-key').dataset.touched === '1') body.api_key = v('dr-key');
    try {
      if (drawerAlias === null) {
        body.alias = v('dr-alias');
        await api.post('/api/llm/models', body);
      } else {
        await api.put('/api/llm/models/' + encodeURIComponent(drawerAlias), body);
      }
      closeDrawer();
      _toast('已儲存', 'ok', '模型設定已更新');
      await boot();
    } catch (err) {
      _toast((err && err.message) || '儲存失敗', 'fail', err && err.code);
    }
  });
  $('#dr-vision').addEventListener('click', () => $('#dr-vision').classList.toggle('on'));
  $('#dr-key-reset').addEventListener('click', () => {
    $('#dr-key').value = '';
    $('#dr-key').readOnly = false;
    $('#dr-key').placeholder = '輸入新的 API Key…';
    $('#dr-key').dataset.touched = '1';
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
      /* persist on change -> PUT /api/llm/roles (full role map; unchanged keys preserved) */
      sel.onchange = async () => {
        const fields = {
          default_model: $('#role-default') ? $('#role-default').value || null : D.roles.default_model,
          default_fallback: $('#role-default-fb') ? $('#role-default-fb').value || null : D.roles.default_fallback,
          vision_model: $('#role-vision') ? $('#role-vision').value || null : D.roles.vision_model,
          vision_fallback: $('#role-vision-fb') ? $('#role-vision-fb').value || null : D.roles.vision_fallback,
          master_model: $('#role-master') ? $('#role-master').value || null : D.roles.master_model,
          master_fallback: $('#role-master-fb') ? $('#role-master-fb').value || null : D.roles.master_fallback,
        };
        try {
          await api.put('/api/llm/roles', fields);
          _toast('角色已更新', 'ok', '角色預設已儲存');
          await boot();
        } catch (err) {
          _toast((err && err.message) || '角色更新失敗', 'fail', err && err.code);
          await boot(); /* re-sync selects to the server's last-good state */
        }
      };
    });
  }

  /* ---- Section C 額度治理 ---- */
  function renderQuota() {
    const Q = D.quota;
    const remaining = Q.remaining_usd; /* Decimal STRING or null */
    const remNum = remaining === null ? null : Number(remaining);
    const thrNum = Q.alert_threshold_usd === null ? 1.0 : Number(Q.alert_threshold_usd);
    const qv = $('#quota-value');
    if (qv) {
      qv.textContent = remaining === null ? '無上限' : usd(remaining);
      qv.style.color = remNum !== null && remNum <= 0
        ? 'var(--up)'
        : remNum !== null && remNum < thrNum
          ? 'var(--amber)' : '';
    }
    const thrInput = $('#quota-threshold');
    if (thrInput) thrInput.value = Q.alert_threshold_usd === null ? '' : Q.alert_threshold_usd;

    /* topup history */
    const tbody = $('#topup-body');
    if (tbody) {
      tbody.replaceChildren();
      (Q.topups || []).forEach((r) => {
        const tr = el('tr');
        tr.appendChild(el('td', 'num', f.datetime(r.at)));
        const tdAmt = el('td', 'num sign-down', '+' + usd(r.amount_usd));
        tr.appendChild(tdAmt);
        tr.appendChild(el('td', 'col-text', r.note || ''));
        tbody.appendChild(tr);
      });
    }
  }

  /* threshold persist (blur/Enter) -> PUT /api/llm/quota */
  (function initThreshold() {
    const inp = $('#quota-threshold');
    if (!inp) return;
    const save = async () => {
      const raw = inp.value;
      if (raw === '' || Number(raw) < 0) { inp.classList.add('field-error'); return; }
      inp.classList.remove('field-error');
      try {
        await api.put('/api/llm/quota', { alert_threshold_usd: raw });
        _toast('已更新', 'ok', '警示閾值 ' + usd(raw));
        await boot();
      } catch (err) {
        _toast((err && err.message) || '更新失敗', 'fail', err && err.code);
      }
    };
    inp.addEventListener('change', save);
  })();

  /* 添加額度 modal -> POST /api/llm/quota/topup */
  (function initTopup() {
    const btn = $('#quota-topup');
    if (!btn) return;
    btn.addEventListener('click', () => {
      const remaining = D.quota.remaining_usd;
      const backdrop = el('div', 'modal-backdrop');
      const modal = el('div', 'modal');
      const head = el('div', 'modal-head');
      head.appendChild(el('h3', 'modal-title', '添加額度'));
      const close = el('button', 'modal-close', '✕'); close.type = 'button';
      head.appendChild(close);
      modal.appendChild(head);
      const body = el('div', 'modal-body');
      const desc = el('p');
      desc.appendChild(document.createTextNode(
        '輸入欲添加的金額（USD）。系統將寫入一筆加值事件，餘額立即更新；歸零後 AI 呼叫將自動暫停。'));
      desc.appendChild(el('br'));
      const bal = el('span');
      bal.style.color = 'var(--text-3)'; bal.style.fontSize = '11px';
      bal.textContent = '目前餘額：' + (remaining === null ? '無上限' : usd(remaining)) + ' USD';
      desc.appendChild(bal);
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
      ok.addEventListener('click', async () => {
        const raw = inp.value;
        if (!raw || Number(raw) <= 0) { inp.classList.add('field-error'); return; }
        ok.disabled = true;
        try {
          await api.post('/api/llm/quota/topup', { amount_usd: raw });
          dismiss();
          _toast('加值成功', 'ok', '+' + usd(raw) + ' USD 已加入額度');
          await boot();
        } catch (err) {
          ok.disabled = false;
          _toast((err && err.message) || '加值失敗', 'fail', err && err.code);
        }
      });
      document.body.appendChild(backdrop);
      setTimeout(() => inp.focus(), 50);
    });
  })();

  /* ---- Section D 用量與趨勢 ---- */
  let llmChart = null;
  function renderUsageTable() {
    const tbody = $('#usage-body');
    if (tbody) {
      tbody.replaceChildren();
      D.usage.by_model.forEach((u) => {
        const tr = el('tr');
        const td = el('td', 'col-text');
        td.appendChild(el('span', 'cron-code', u.alias));
        tr.appendChild(td);
        tr.appendChild(el('td', 'num', f.num(u.calls)));
        tr.appendChild(el('td', 'num', f.num(u.tokens_in)));
        tr.appendChild(el('td', 'num', f.num(u.tokens_out)));
        /* cost_usd is a Decimal STRING; "0"/"0.00" are truthy so nil-check ==null. */
        const tdCost = el('td', 'num');
        if (u.cost_usd == null) { tdCost.textContent = f.NULL_GLYPH; tdCost.classList.add('sign-nil'); }
        else tdCost.textContent = usd(u.cost_usd);
        tr.appendChild(tdCost);
        tbody.appendChild(tr);
      });
    }
    const chips = $('#agent-chips');
    if (chips) {
      chips.replaceChildren();
      D.usage.by_agent.forEach((a) => {
        const chip = el('span', 'ccy-chip');
        chip.appendChild(el('span', null, a.agent + ' '));
        chip.appendChild(el('b', null, usd(a.cost_usd)));
        chips.appendChild(chip);
      });
    }
  }
  function renderUsageChart() {
    const host = $('#llm-daily-chart');
    if (!host || typeof echarts === 'undefined') return;
    if (!llmChart) llmChart = echarts.init(host);
    const daily = D.usage.daily || { dates: [], series: [] };
    const axisStyle = { color: '#5e6b7c', fontSize: 9, fontFamily: "'IBM Plex Mono', monospace" };
    const palette = ['#58a6dd', '#9b86d8', '#d9a13f', '#5fb878', '#d86c6c', '#6cb8d8'];
    /* costs[] are Decimal STRINGS -> Number()-coerce for the chart (display-only). */
    const series = (daily.series || []).map((s, i) => ({
      name: s.alias, type: 'line', showSymbol: false,
      lineStyle: { color: palette[i % palette.length], width: 1.6 },
      itemStyle: { color: palette[i % palette.length] },
      data: (s.costs || []).map((c) => (c == null ? null : Number(c))),
    }));
    llmChart.setOption({
      grid: { left: 40, right: 8, top: 26, bottom: 22 },
      legend: {
        top: 0, left: 0, icon: 'rect', itemWidth: 10, itemHeight: 3,
        textStyle: { color: '#9aa6b5', fontSize: 10, fontFamily: "'IBM Plex Mono', monospace" }
      },
      tooltip: {
        trigger: 'axis',
        backgroundColor: '#18202b', borderColor: '#232e3c',
        textStyle: { color: '#e6ebf2', fontSize: 11, fontFamily: "'IBM Plex Mono', monospace" },
        valueFormatter: (v) => '$' + f.num(v, 2)
      },
      xAxis: {
        type: 'category', data: daily.dates || [],
        axisLine: { lineStyle: { color: '#232e3c' } }, axisTick: { show: false },
        axisLabel: { ...axisStyle, interval: 4 }
      },
      yAxis: {
        type: 'value',
        splitLine: { lineStyle: { color: '#232e3c', type: 'dashed' } },
        axisLabel: { ...axisStyle, formatter: '${value}' }
      },
      series: series
    });
    llmChart.resize();
  }
  window.addEventListener('resize', () => { if (llmChart) llmChart.resize(); });

  /* 關閉全部角色 -> PUT /api/llm/roles with every role null */
  (function initRestore() {
    const btn = $('#llm-restore');
    if (!btn) return;
    btn.addEventListener('click', () => window.confirmDialog({
      title: '關閉全部角色預設',
      body: '將所有角色選單全部設為空，AI 功能將立即停用。已註冊的模型不受影響，重新選定角色後即可恢復。',
      confirmLabel: '確認關閉',
      onConfirm: async () => {
        try {
          await api.put('/api/llm/roles', {
            default_model: null, default_fallback: null,
            vision_model: null, vision_fallback: null,
            master_model: null, master_fallback: null,
          });
          _toast('角色已關閉', 'ok', '所有角色預設已清空，AI 已停用');
          await boot();
        } catch (err) {
          _toast((err && err.message) || '關閉失敗', 'fail', err && err.code);
        }
      }
    }));
  })();

  /* defer chart render until the LLM tab is actually visible (combined settings.html);
     the usage TABLE + chips render immediately on boot regardless. */
  function maybeRenderLlmChart() {
    const host = document.getElementById('llm-daily-chart');
    if (!host || host.offsetHeight === 0) return;
    renderUsageChart();
  }

  /* ===== boot: GET /api/llm/config, then render. Graceful: on failure leave the page
     empty + surface ONE toast (never an unhandled rejection — the e2e smoke asserts ZERO
     console errors). 401 is handled inside api.js. ===== */
  async function boot() {
    try {
      const resp = await api.get('/api/llm/config');
      D = {
        models: (resp && resp.models) || [],
        roles: (resp && resp.roles) || D.roles,
        quota: (resp && resp.quota) || { remaining_usd: null, alert_threshold_usd: null, topups: [] },
        usage: (resp && resp.usage) || { by_model: [], by_agent: [], daily: { dates: [], series: [] } },
      };
    } catch (err) {
      _toast('LLM 設定載入失敗', 'fail', (err && err.message) || undefined);
      /* fall through with empty D so the page still renders an (empty) shell */
    }
    renderStatus();
    renderModels();
    renderRoles();
    renderQuota();
    renderUsageTable(); /* table + chips render now */
    maybeRenderLlmChart(); /* chart only if the tab is visible (standalone page = yes) */
  }

  boot();

  window.addEventListener('pd-settings-tab', function (e) {
    if (e.detail === 'llm') maybeRenderLlmChart();
  });
  const llmView = document.getElementById('view-llm');
  if (llmView) {
    new MutationObserver(maybeRenderLlmChart).observe(llmView, { attributes: true, attributeFilter: ['class'] });
  }
})();
