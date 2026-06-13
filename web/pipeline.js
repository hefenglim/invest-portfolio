/* portfolio-dash — 洞察管線中心（重構提案 v1）：健康列＋管線卡＋分層抽屜.
   依賴：pipeline-data.js（PIPE）、shell.js（toast/confirmDialog）、settings.css（pv-modal）。 */
(function () {
  'use strict';
  const P = window.PIPE;
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  window.ppEl = el;

  /* 共用 modal（沿用 settings.css pv- 樣式） */
  window.ppModal = function (title, buildBody, wide) {
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
  };

  const SCOPE_META = {
    per_symbol: { cls: 's-sym', label: '單一標的・每檔一張' },
    portfolio: { cls: 's-pf', label: '全組合' },
    on_alert: { cls: 's-alert', label: '預警觸發' }
  };
  const NODE_DEFS = [
    ['trigger', '① 觸發'], ['input', '② 輸入'], ['assemble', '③ 組裝'],
    ['exec', '④ 執行'], ['output', '⑤ 產出']
  ];

  /* ================= 健康列 ================= */
  (function () {
    const wrap = $('#pp-health');
    const card = (label, value, sub, lv) => {
      const c = el('div', 'pp-h-card' + (lv === 'warn' ? ' warn' : ''));
      c.appendChild(el('div', 'pp-h-label', label));
      const v = el('div', 'pp-h-value');
      v.appendChild(el('span', 'pp-dot ' + (lv || 'ok')));
      v.appendChild(el('span', null, value));
      c.appendChild(v);
      c.appendChild(el('div', 'pp-h-sub', sub));
      return c;
    };
    wrap.appendChild(card('AI 大師模型', P.health.master.model, P.health.master.note, P.health.master.ok ? 'ok' : 'fail'));
    wrap.appendChild(card('LLM 額度', '$' + P.health.quota.remaining, P.health.quota.note, P.health.quota.warn ? 'warn' : 'ok'));
    wrap.appendChild(card('最近批次', P.health.batch.at + '・' + P.health.batch.cards + ' 卡', P.health.batch.note + '・$' + P.health.batch.cost, 'ok'));
    const attn = P.tasks.filter((t) => ['fail', 'warn'].includes(P.taskLevel(t))).length;
    const idle = P.tasks.filter((t) => P.taskLevel(t) === 'idle').length;
    wrap.appendChild(card('任務狀態', attn + ' 個需注意・' + idle + ' 個閒置',
      P.tasks.length + ' 個洞察任務（點下方卡片展開）', attn ? 'warn' : 'ok'));
  })();

  /* ================= 篩選列 ================= */
  let filter = 'all';
  (function () {
    const bar = $('#pp-filter');
    [['all', '全部任務'], ['attention', '需注意'], ['scheduled', '已排程'], ['on_alert', '預警觸發'], ['idle', '閒置']].forEach(([val, label], i) => {
      const c = el('button', 'chip' + (i === 0 ? ' active' : ''), label);
      c.type = 'button';
      c.addEventListener('click', () => {
        filter = val;
        bar.querySelectorAll('.chip').forEach((x) => x.classList.remove('active'));
        c.classList.add('active');
        renderCards();
      });
      bar.appendChild(c);
    });
  })();

  function matchFilter(t) {
    const lv = P.taskLevel(t);
    if (filter === 'all') return true;
    if (filter === 'attention') return lv === 'fail' || lv === 'warn';
    if (filter === 'scheduled') return t.trigger.kind === 'schedule' && t.enabled;
    if (filter === 'on_alert') return t.scope === 'on_alert';
    if (filter === 'idle') return lv === 'idle';
    return true;
  }

  /* ================= 任務管線卡 ================= */
  const listWrap = $('#pp-list');

  function buildRail(t, clickable) {
    const rail = el('div', 'pp-rail');
    const nodes = P.nodeStates(t);
    NODE_DEFS.forEach(([key, label], i) => {
      if (i) rail.appendChild(el('span', 'pp-link', '→'));
      const ns = nodes[key];
      const node = el('div', 'pp-node n-' + ns.lv);
      const lb = el('div', 'pp-node-label');
      lb.appendChild(el('span', 'pp-dot ' + ns.lv));
      lb.appendChild(el('span', null, label));
      node.appendChild(lb);
      node.appendChild(el('div', 'pp-node-text', ns.text));
      node.appendChild(el('div', 'pp-node-sub', ns.sub));
      if (clickable) {
        node.title = '開啟「' + t.name + '」設定抽屜 — ' + label.slice(2) + ' 節';
        node.addEventListener('click', (e) => { e.stopPropagation(); openDrawer(t, key); });
      }
      rail.appendChild(node);
    });
    return rail;
  }

  function statusPill(status) {
    const map = {
      ok: ['pill-ok', '成功'], partial: ['pill-warn', '部分'], warn: ['pill-warn', '注意'],
      skipped: ['pill-fail', '跳過'], error: ['pill-fail', '失敗'], idle: ['pill-off', '閒置']
    };
    const [cls, label] = map[status] || ['pill-off', status];
    const p = el('span', 'pill ' + cls);
    p.appendChild(el('span', 'dot'));
    p.appendChild(document.createTextNode(label));
    return p;
  }

  function renderCards() {
    listWrap.replaceChildren();
    P.tasks.filter(matchFilter).forEach((t) => {
      const lv = P.taskLevel(t);
      const card = el('div', 'pp-card lv-' + lv);
      card.dataset.screenLabel = '任務卡-' + t.name;
      const head = el('div', 'pp-card-head');
      head.appendChild(el('span', 'pp-card-name', t.name));
      const sm = SCOPE_META[t.scope];
      head.appendChild(el('span', 'pp-scope ' + sm.cls, sm.label));
      const st = el('span', 'pp-statusline');
      st.appendChild(statusPill(t.enabled ? t.lastRun.status : 'idle'));
      st.appendChild(el('span', null, t.lastRun.at));
      head.appendChild(st);
      head.appendChild(el('span', 'spacer'));

      const needWhy = lv === 'idle' || t.lastRun.status === 'skipped';
      if (needWhy) {
        const why = el('button', 'btn btn-sm', '為什麼沒跑？');
        why.type = 'button';
        why.title = '逐道閘門診斷此任務為何未產出洞察（零成本）';
        why.addEventListener('click', (e) => { e.stopPropagation(); window.ppDiagnose(t); });
        head.appendChild(why);
      }
      const pf = el('button', 'btn btn-sm', '乾跑預檢');
      pf.type = 'button';
      pf.title = '不呼叫 LLM：跑完整守門檢查（R1–R8）＋組裝提示詞預覽＋成本估算';
      pf.addEventListener('click', (e) => { e.stopPropagation(); window.ppPreflight(t); });
      head.appendChild(pf);

      const tg = el('button', 'toggle' + (t.enabled ? ' on' : ''));
      tg.type = 'button';
      tg.setAttribute('role', 'switch');
      tg.title = '啟用/暫停此任務：暫停後排程保留但不執行';
      tg.addEventListener('click', (e) => {
        e.stopPropagation();
        t.enabled = !t.enabled;
        renderCards();
        window.toast(t.enabled ? '任務已啟用' : '任務已暫停', 'ok', t.name + (t.enabled ? '：恢復依觸發條件執行' : '：排程保留、不再執行') + '（設計稿）');
      });
      head.appendChild(tg);
      card.appendChild(head);
      card.appendChild(buildRail(t, true));
      card.addEventListener('click', () => openDrawer(t, null));
      listWrap.appendChild(card);
    });
    if (!listWrap.children.length) {
      listWrap.appendChild(el('div', 'wz-note', '沒有符合篩選的任務。'));
    }
  }
  renderCards();
  window.ppRenderCards = renderCards;

  /* ================= 分層抽屜 ================= */
  function openDrawer(t, focusKey) {
    document.querySelectorAll('.pp-drawer-backdrop, .pp-drawer').forEach((n) => n.remove());
    const back = el('div', 'pp-drawer-backdrop');
    back.addEventListener('click', () => { back.remove(); dr.remove(); });
    const dr = el('aside', 'pp-drawer');
    dr.dataset.screenLabel = '任務抽屜-' + t.name;

    const head = el('div', 'pp-d-head');
    head.appendChild(el('span', 'pp-d-title', t.name));
    const sm = SCOPE_META[t.scope];
    head.appendChild(el('span', 'pp-scope ' + sm.cls, sm.label));
    head.appendChild(el('span', 'spacer'));
    const pf = el('button', 'btn btn-sm', '乾跑預檢');
    pf.type = 'button';
    pf.addEventListener('click', () => window.ppPreflight(t));
    head.appendChild(pf);
    const del = el('button', 'btn btn-sm btn-danger', '刪除任務');
    del.type = 'button';
    del.addEventListener('click', () => window.confirmDialog({
      title: '刪除洞察任務 — ' + t.name,
      body: '將同步移除排程、整條校正鏈封存；歷史洞察與運行記錄保留可反查（軟刪除）。',
      confirmLabel: '確認刪除', danger: true,
      onConfirm: () => {
        const ix = P.tasks.indexOf(t);
        if (ix >= 0) P.tasks.splice(ix, 1);
        back.remove(); dr.remove(); renderCards();
        window.toast('已刪除', 'ok', t.name + '：排程已同步移除、校正鏈封存（設計稿）');
      }
    }));
    head.appendChild(del);
    const x = el('button', 'sd-close', '✕');
    x.type = 'button';
    x.addEventListener('click', () => { back.remove(); dr.remove(); });
    head.appendChild(x);
    dr.appendChild(head);

    /* 區段導覽 */
    const nav = el('div', 'pp-d-nav');
    const body = el('div', 'pp-d-body');
    const secs = {};
    const navTo = (id) => {
      const sec = secs[id];
      if (sec) body.scrollTo({ top: sec.offsetTop - 46, behavior: 'smooth' });
    };
    [['pipe', '管線'], ['trigger', '觸發'], ['input', '輸入'], ['assemble', '組裝'], ['calib', '校正版本'], ['runs', '運行記錄']].forEach(([id, label]) => {
      const c = el('button', 'chip', label);
      c.type = 'button';
      c.addEventListener('click', () => navTo(id));
      nav.appendChild(c);
    });
    dr.appendChild(nav);

    const sec = (id, title, orig) => {
      const s = el('div', 'pp-d-sec');
      const h = el('h3', null, title);
      if (orig) h.appendChild(el('span', 'orig', '原：' + orig));
      s.appendChild(h);
      secs[id] = s;
      body.appendChild(s);
      return s;
    };

    /* -- 管線總覽 -- */
    const sPipe = sec('pipe', '管線 — 一次執行的完整路徑');
    sPipe.appendChild(buildRail(t, false));
    if (t.lastRun.notes.length) {
      const c = el('div', 'pp-d-card');
      t.lastRun.notes.forEach((nt) => c.appendChild(el('div', null, '• ' + nt)));
      c.style.marginTop = '8px';
      sPipe.appendChild(c);
    }

    /* -- 觸發 -- */
    const sTrig = sec('trigger', '① 觸發', '排程頁／預警規則頁');
    const cTrig = el('div', 'pp-d-card');
    const kv = (k, v, bold) => {
      const r = el('div', 'pp-kv');
      r.appendChild(el('span', 'k', k));
      const vv = el('span', 'v');
      if (bold) vv.appendChild(el('b', null, v)); else vv.textContent = v;
      r.appendChild(vv);
      return r;
    };
    if (t.scope === 'on_alert') {
      cTrig.appendChild(kv('觸發方式', '預警事件（不可排程）', true));
      const names = (t.trigger.rules || []).map(P.ruleName).join('、') || '全部規則';
      cTrig.appendChild(kv('監聽規則', names));
      cTrig.appendChild(kv('防抖', '同一（規則×標的）24h 內不重複觸發（R7）'));
      const acts = el('div', 'pp-d-actions');
      const b = el('button', 'btn btn-sm', '編輯監聽規則');
      b.type = 'button';
      b.addEventListener('click', () => window.toast('編輯監聽規則', 'ok', '沿用既有規則多選表（設計稿 — 與預警規則頁互通）'));
      acts.appendChild(b);
      cTrig.appendChild(acts);
    } else {
      cTrig.appendChild(kv('排程', t.trigger.kind === 'manual' ? '未排程（僅能手動執行）' : t.trigger.human + '（' + t.trigger.cron + '）', true));
      if (t.trigger.next) cTrig.appendChild(kv('下次執行', t.enabled ? t.trigger.next : '— 任務已停用'));
      const acts = el('div', 'pp-d-actions');
      const b1 = el('button', 'btn btn-sm btn-primary', t.trigger.kind === 'manual' ? '啟動排程' : '調整週期');
      b1.type = 'button';
      b1.addEventListener('click', () => window.ppScheduleModal(t, () => { openDrawer(t, 'trigger'); renderCards(); }));
      acts.appendChild(b1);
      const b2 = el('button', 'btn btn-sm', '立即執行一次');
      b2.type = 'button';
      b2.addEventListener('click', () => window.toast('已送出執行', 'ok', t.name + '：本次執行完成後會出現在運行記錄（設計稿）'));
      acts.appendChild(b2);
      cTrig.appendChild(acts);
    }
    sTrig.appendChild(cTrig);

    /* -- 輸入 -- */
    const sIn = sec('input', '② 輸入 — 範圍與標的', '組合器範圍＋標的 chip');
    const cIn = el('div', 'pp-d-card');
    if (t.scope === 'portfolio') {
      cIn.appendChild(kv('範圍', '全組合 — 以單一資料快照執行，產 1 張卡', true));
    } else if (t.scope === 'on_alert') {
      cIn.appendChild(kv('範圍', '事件標的 — 由命中的預警規則決定', true));
    } else {
      const all = !t.universe || t.universe.mode === 'all';
      cIn.appendChild(kv('標的宇宙', all ? '全部持倉（' + P.HELD.length + ' 檔・自動跟隨持倉變動）' : '自選 ' + t.universe.symbols.length + ' 檔：' + t.universe.symbols.join('、'), true));
      cIn.appendChild(kv('生命週期', '出清/移出觀察清單自動移除；清單空 → 自動停用＋預警（R2）'));
      if (t.universeEvent) cIn.appendChild(kv('最近異動', t.universeEvent));
      const acts = el('div', 'pp-d-actions');
      const b = el('button', 'btn btn-sm', '編輯標的');
      b.type = 'button';
      b.addEventListener('click', () => window.toast('編輯標的', 'ok', '沿用既有標的選擇器（持倉＋觀察清單）（設計稿）'));
      acts.appendChild(b);
      cIn.appendChild(acts);
    }
    sIn.appendChild(cIn);

    /* -- 組裝 -- */
    const sAsm = sec('assemble', '③ 組裝 — 實際送出的提示詞層', '系統＋策略＋校正');
    const stack = el('div', 'pp-stack');
    const mkLayer = (tag, name, note, cls) => {
      const l = el('div', 'pp-layer' + (cls ? ' ' + cls : ''));
      l.appendChild(el('span', 'tag', tag));
      l.appendChild(el('span', 'name', name));
      if (note) l.appendChild(el('span', 'note' + (cls && cls.includes('l-off') ? ' warn' : ''), note));
      return l;
    };
    stack.appendChild(mkLayer('守則', P.SYSTEM_RULES, '全域・所有任務共用'));
    t.templates.forEach((tid, i) => {
      const tpl = P.tplOf(tid);
      if (!tpl) return;
      stack.appendChild(mkLayer('模板' + (i + 1), tpl.name + ' — ' + tpl.body,
        tpl.enabled ? null : '已停用 → 本段跳過（R3）', tpl.enabled ? null : 'l-off'));
    });
    const cl = P.calibLabel(t);
    if (t.self_correct) stack.appendChild(mkLayer('校正', cl.text, '只能附加、不得改寫上層', 'l-calib'));
    sAsm.appendChild(stack);
    const asmActs = el('div', 'pp-d-actions');
    asmActs.style.marginTop = '8px';
    const bEdit = el('button', 'btn btn-sm', '增減模板');
    bEdit.type = 'button';
    bEdit.addEventListener('click', () => window.toast('增減模板', 'ok', '沿用既有勾選表（含範圍相容性檢查 R1）（設計稿）'));
    asmActs.appendChild(bEdit);
    const bLib = el('a', 'btn btn-sm', '前往分析模板庫 →');
    bLib.href = 'settings-prompts.html';
    asmActs.appendChild(bLib);
    sAsm.appendChild(asmActs);

    /* -- 校正版本 -- */
    const sCal = sec('calib', '④ 校正版本鏈（1:1）', '自我校正提示詞庫');
    buildCalibSection(t, sCal, () => { openDrawer(t, 'calib'); renderCards(); });

    /* -- 運行記錄 -- */
    const sRun = sec('runs', '⑤ 運行記錄 — 本任務', '排程工作表＋執行歷史');
    const tbl = el('table', 'pp-runs');
    tbl.innerHTML = '<thead><tr><th class="num">時間</th><th>結果</th><th>內容</th><th class="num">卡</th><th class="num">費用</th></tr></thead>';
    const tb = el('tbody');
    P.runs.filter((r) => r.task === t.id).forEach((r) => {
      const tr = el('tr', r.status === 'skipped' ? 'r-skipped' : null);
      tr.appendChild(el('td', 'num', r.at));
      const td = el('td');
      td.appendChild(statusPill(r.status));
      tr.appendChild(td);
      tr.appendChild(el('td', null, r.detail));
      tr.appendChild(el('td', 'num', String(r.cards)));
      tr.appendChild(el('td', 'num', r.cost ? '$' + r.cost : '—'));
      tb.appendChild(tr);
    });
    if (!tb.children.length) {
      const tr = el('tr');
      const td = el('td', null, '尚無運行記錄');
      td.colSpan = 5;
      tr.appendChild(td);
      tb.appendChild(tr);
    }
    tbl.appendChild(tb);
    sRun.appendChild(tbl);
    const allRuns = el('a', 'btn btn-sm', '完整運行中心（全部任務）→');
    allRuns.href = 'settings-scheduler.html';
    allRuns.style.marginTop = '9px';
    allRuns.style.display = 'inline-flex';
    sRun.appendChild(allRuns);

    dr.appendChild(body);
    document.body.appendChild(back);
    document.body.appendChild(dr);
    if (focusKey) {
      const map = { trigger: 'trigger', input: 'input', assemble: 'assemble', exec: 'runs', output: 'runs', calib: 'calib' };
      requestAnimationFrame(() => navTo(map[focusKey] || 'pipe'));
    }
  }
  window.ppOpenDrawer = openDrawer;

  /* 校正版本區段（含影子進度、設為生效） */
  function buildCalibSection(t, sCal, rerender) {
    if (!t.self_correct) {
      const c = el('div', 'pp-d-card');
      c.appendChild(el('div', null, '自我校正未啟動 — 開啟後 AI 大師模型將累積回測樣本並產生 1:1 專屬校正版本。'));
      const acts = el('div', 'pp-d-actions');
      const b = el('button', 'btn btn-sm btn-primary', '啟動自我校正');
      b.type = 'button';
      b.addEventListener('click', () => {
        t.self_correct = true;
        window.toast('自我校正已啟動', 'ok', t.name + '：開始累積回測樣本（設計稿）');
        rerender();
      });
      acts.appendChild(b);
      c.appendChild(acts);
      sCal.appendChild(c);
      return;
    }
    const ch = P.chainOf(t.id);
    if (!ch || !ch.versions.length) {
      const c = el('div', 'pp-d-card');
      c.appendChild(el('div', null, '累積樣本中 — 到期評估達「最低樣本數」後，AI 大師模型才會生成初版（避免小樣本過擬合）。'));
      sCal.appendChild(c);
      return;
    }
    const latest = ch.versions[ch.versions.length - 1];
    if (ch.activeVer !== null && ch.activeVer !== latest.ver && ch.shadowProgress) {
      const sp = ch.shadowProgress;
      const bar = el('div', 'pp-shadow-bar');
      const meter = el('span', 'meter');
      const fill = el('i');
      fill.style.width = Math.round((sp.done / sp.need) * 100) + '%';
      meter.appendChild(fill);
      bar.appendChild(meter);
      bar.appendChild(el('span', null, 'v' + latest.ver + ' 影子評估 ' + sp.done + '/' + sp.need + ' 批次・影子均分 ' + sp.shadowAvg + ' vs 生效 ' + sp.activeAvg + '（領先）'));
      const promote = el('button', 'btn btn-sm btn-primary', '提前設為生效');
      promote.type = 'button';
      promote.addEventListener('click', () => {
        ch.activeVer = latest.ver;
        window.toast('已切換版本', 'ok', t.name + ' 生效版 → v' + latest.ver + '；最新版生效，無影子成本（設計稿）');
        rerender();
      });
      bar.appendChild(promote);
      bar.style.marginBottom = '8px';
      sCal.appendChild(bar);
    }
    const vers = el('div', 'pp-vers');
    ch.versions.slice().reverse().forEach((v) => {
      const isActive = ch.activeVer === v.ver;
      const isShadow = !v.archived && !isActive && v.ver === latest.ver && ch.activeVer !== null;
      const row = el('div', 'pp-ver' + (v.archived ? ' archived' : '') + (isActive ? ' is-active' : '') + (isShadow ? ' is-shadow' : ''));
      const top = el('div', 'pp-ver-top');
      top.appendChild(el('span', 'vid', 'v' + v.ver));
      top.appendChild(el('span', 'date', v.date));
      if (isActive) top.appendChild(el('span', 'pill pill-ok', '生效中'));
      else if (isShadow) top.appendChild(el('span', 'pill pill-shadow', '影子評估中'));
      else if (v.archived) top.appendChild(el('span', 'pill pill-off', '已封存'));
      else if (ch.activeVer === null && v.ver === latest.ver) top.appendChild(el('span', 'pill pill-warn', '尚未套用'));
      top.appendChild(el('span', 'pp-ver-stats', '評估 ' + v.stats.evals + '・均分 ' + v.stats.avg + '・失誤 ' + v.stats.miss + '%'));
      top.appendChild(el('span', 'spacer'));
      if (!v.archived && !isActive) {
        const b = el('button', 'btn btn-sm btn-primary', '設為生效');
        b.type = 'button';
        b.addEventListener('click', () => {
          ch.activeVer = v.ver;
          window.toast('已切換版本', 'ok', t.name + ' 生效版 → v' + v.ver + (v.ver !== latest.ver ? '；v' + latest.ver + ' 自動進入影子評估' : '') + '（設計稿）');
          rerender();
        });
        top.appendChild(b);
      }
      if (!v.archived) {
        const b = el('button', 'btn btn-sm', '封存');
        b.type = 'button';
        b.addEventListener('click', () => window.confirmDialog({
          title: '封存 v' + v.ver + ' — ' + t.name,
          body: '封存後從選擇器移除、不可再套用；歸因記錄保留可反查（軟刪除）。',
          confirmLabel: '確認封存', danger: true,
          onConfirm: () => {
            v.archived = true;
            if (ch.activeVer === v.ver) ch.activeVer = null;
            window.toast('已封存', 'ok', 'v' + v.ver + '（設計稿）');
            rerender();
          }
        }));
        top.appendChild(b);
      }
      row.appendChild(top);
      row.appendChild(el('div', 'pp-ver-cause', '產生原因：' + v.cause));
      const det = document.createElement('details');
      const sm2 = el('summary', null, '查看校正內文');
      det.appendChild(sm2);
      const pre = el('pre', null, v.body);
      det.appendChild(pre);
      row.appendChild(det);
      vers.appendChild(row);
    });
    sCal.appendChild(vers);
  }

  /* 排程設定 modal（與運行中心共用語意） */
  window.ppScheduleModal = function (t, onDone) {
    window.ppModal('排程 — ' + t.name, (body, close) => {
      body.appendChild(el('div', 'pv-note', '寫入運行中心的同一筆 job；之後在這裡或運行中心調整皆可（同一記錄）。'));
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
      time.type = 'time'; time.value = '08:00';
      const row = el('div', 'pv-fields');
      row.appendChild(fld('週期', period));
      row.appendChild(fld('時間', time));
      body.appendChild(row);
      const acts = el('div', 'cal-actions');
      const ok = el('button', 'btn btn-primary', '儲存排程');
      ok.type = 'button';
      ok.addEventListener('click', () => {
        t.trigger = { kind: 'schedule', human: period.value + ' ' + time.value, cron: '（由後端轉換）', next: '依新週期計算' };
        t.enabled = true;
        close();
        window.toast('已儲存排程', 'ok', t.name + '：' + t.trigger.human + '（設計稿）');
        if (onDone) onDone();
      });
      acts.appendChild(ok);
      body.appendChild(acts);
    });
  };
})();
