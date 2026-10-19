/* portfolio-dash — the settlement status chip, its detail panel and the re-settle prompt.

   PERF spec 2026-10-18 §14 (UX), §15 (API); plan §6.3 / P10. Loaded by shell.js on every
   page that has a topbar (no HTML tag anywhere — the file stamps itself with shell.js's own
   ?v=). Everything it shows comes from the settlement API:

     GET  /api/settlement/status                 the chip, the prompt, the banners
     GET  /api/settlement/runs                   the history (paged 20)
     GET  /api/settlement/runs/{id}              one run's full report
     GET  /api/settlement/runs/{id}/diff         a report's per-day history changes (paged 50)
     GET  /api/settlement/changes                the change trail, pending or a run's (paged 50)
     POST /api/settlement/run   {scope}          立即重新結算 / 重試 / 重新結算
     POST /api/settlement/cancel                 取消（保留目前版本）

   Rules this file keeps (architecture.md › web/):
   - Every number is the server's — money as Decimal STRINGS, formatted through window.fmt,
     never computed. Durations, percents and counts are the server's numbers too.
   - Every sentence the server owns is the server's: status words, trigger and failure
     sentences, `on_cancel`, findings, `eta_basis`, reasons. Account ids arrive as
     `{account:<id>}` tokens and api.js has already resolved them. What this file words
     itself is the panel's own furniture (section titles, the prompt of spec §14.3, the
     chip's state words of §14.1) plus three small vocabularies MIRRORED from
     portfolio/settlement_models.py (tables, operations, actors, run reasons, phases) —
     tests/contract/test_settlement_web_static.py pins every mirrored word to Python's.
   - A state colour is a class with a stylesheet rule (DEF-077); no native dialog (the prompt
     and the cancel question are confirmDialog); every table sits in a .table-wrap.
   - Every status read is `{background: true}`: polling never moves the top progress bar.
   - Under PD_SETTLE=off the status reads `state: "off"`: no chip, no prompt, no banner, and
     the menu's 重新結算 falls back to the strict ledger replay it replaced (plan P5: off
     restores today's behaviour). */
(function () {
  'use strict';
  if (window.pdSettlement) return;
  const api = window.pdApi;
  if (!api) return;

  const BG = { background: true };
  const POLL_PANEL_MS = 1000;     // panel open, run active (§14.2 Polling)
  const POLL_CHIP_MS = 3000;      // only the chip shows a run
  const MUTATED_WAIT_MS = 800;    // §14.3: debounce on pd-mutated
  const CHANGES_PAGE = 50;
  const DIFF_PAGE = 50;
  const RUNS_PAGE = 20;
  const ASKED_KEY = 'pd_settle_asked';        // sessionStorage: prompt keys already asked
  const SECTIONS_KEY = 'pd_settle_sections';  // localStorage: collapsed panel sections
  const ACTIVE = { preparing: true, running: true };

  /* ---- vocabularies mirrored from portfolio/settlement_models.py (pinned by a test) ---- */
  const CHANGE_TABLE_ZH = {
    transactions: '交易', dividends: '股利', opening_inventory: '期初庫存',
    cash_movements: '現金收支', fx_conversions: '換匯', corporate_actions: '公司行動',
    prices: '價格', fx_rates: '匯率', instruments: '標的', accounts: '帳戶',
    account_market_rules: '帳戶市場規則',
  };
  const CHANGE_OP_ZH = { insert: '新增', update: '修改', delete: '刪除' };
  const RUN_REASON_ZH = {
    prompt: '確認提示後啟動', button: '按下「重新結算」', boot: '系統啟動時自動執行',
    nightly: '每晚例行結算', chained: '接續結算期間新增的變更', resume: '續跑中斷的結算',
    selfcheck: '每週自檢',
  };
  const JOB_REASON_ZH = '排程工作後自動執行';
  const PHASE_ZH = {
    load: '讀取帳本與行情', check: '檢查帳本', replay: '重播帳本', valuate: '逐日估值',
    close: '月結與年結', bench: '基準比較', swap: '比對與切換', prewarm: '預熱',
  };
  const ACTOR_EXTERNAL_ZH = '外部程式／腳本';
  const ACTOR_GUEST_ZH = '訪客';
  const ACTOR_BOOT_ZH = '系統（啟動）';
  const ACTOR_JOB_ZH = '排程';
  /* The panel's own words for the per-day history fields (§9.6: 證券市值 for the trend
     value, never 總市值, which is the KPI's word) and the comparison kinds. */
  const DIFF_FIELD_ZH = {
    total_value: '證券市值', net_invested: '累計淨投入', net_worth: '總淨值（含現金）',
    incomplete: '缺價標記',
  };
  const DIFF_KIND_ZH = {
    changed: '數值變動', value_to_none: '有值 → 無值', none_to_value: '無值 → 有值',
    flag_flip: '標記改變', new_day: '新結算', dropped_day: '移除',
  };
  const PHASE_MARK = { done: '✓', running: '▸', pending: '·', failed: '✕', skipped: '–' };

  /* ---- small helpers ---- */
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  };
  const NIL = '—';
  const fm = () => window.fmt || null;
  const nil = (v) => v === null || v === undefined || v === '';
  const money = (v, ccy) => (nil(v) ? NIL : (fm() ? fm().money(v, ccy) : String(v)));
  const count = (v) => (nil(v) ? NIL : (fm() ? fm().num(v, 0) : String(v)));
  const day = (v) => (nil(v) ? NIL : String(v).slice(0, 10));
  const stamp = (v) => (nil(v) ? NIL : String(v).slice(0, 10) + ' ' + String(v).slice(11, 16));
  const clock = (v) => (nil(v) ? NIL : String(v).slice(11, 19));
  /* Seconds as the server measured them (a float, never money): 「8 秒」, 「1 分 05 秒」. */
  function secs(v) {
    if (nil(v)) return NIL;
    const s = Number(v);
    if (!Number.isFinite(s)) return NIL;
    if (s < 60) return (fm() ? fm().num(s, s < 10 ? 1 : 0) : String(Math.round(s))) + ' 秒';
    const m = Math.floor(s / 60);
    const r = Math.round(s - m * 60);
    return m + ' 分 ' + (r < 10 ? '0' : '') + r + ' 秒';
  }
  const range2 = (a, b) => (nil(a) ? NIL : (nil(b) || a === b ? day(a) : day(a) + ' 至 ' + day(b)));
  const acct = (id) => (nil(id) ? '' : (window.pdNames ? window.pdNames.account(id) : id));
  const toast = (msg, kind, sub) => { if (window.toast) window.toast(msg, kind, sub); };

  function viewer() {
    const s = window.pdAuth && window.pdAuth.getSession ? window.pdAuth.getSession() : null;
    return s && s.username ? s.username : null;
  }
  /* settlement_models.actor_text: 「你」 only for the viewer's own login. */
  function actorText(actor) {
    if (nil(actor)) return ACTOR_EXTERNAL_ZH;
    if (actor === 'user:guest') return ACTOR_GUEST_ZH;
    if (actor === 'system:boot') return ACTOR_BOOT_ZH;
    if (actor.indexOf('job:') === 0) return ACTOR_JOB_ZH;
    if (actor.indexOf('user:') === 0) {
      const login = actor.slice(5);
      return viewer() === login ? '你' : login;
    }
    return actor;
  }
  /* A trigger sentence carries `{started_by}` (§9.5): the viewer reads 「你」 for their own run. */
  function trigger(text, startedBy) {
    if (nil(text)) return '';
    return String(text).split('{started_by}').join(actorText(startedBy));
  }
  function reasonText(code) {
    if (nil(code)) return '';
    if (RUN_REASON_ZH[code]) return RUN_REASON_ZH[code];
    if (String(code).indexOf('job:') === 0) return JOB_REASON_ZH;
    return String(code);
  }
  function isActive(s) {
    return !!(s && (ACTIVE[s.state] || (s.run && ACTIVE[s.run.status])));
  }
  /* 「交易 2 筆」 / 「價格 3 檔 14 筆」, with the affected days when `withDays`. */
  function tableLine(t, withDays) {
    const label = t.label || CHANGE_TABLE_ZH[t.table] || t.table;
    let s = label + ' ';
    if (!nil(t.keys)) s += count(t.keys) + (t.table === 'fx_rates' ? ' 組 ' : ' 檔 ');
    s += count(t.changes) + ' 筆';
    if (withDays && !nil(t.earliest)) s += '，' + range2(t.earliest, t.latest);
    return s;
  }
  function tablesSummary(tables) {
    return (tables || []).map((t) => tableLine(t, false)).join('、');
  }

  /* ---- storage (every access guarded: private mode / blocked storage just forgets) ---- */
  function askedKeys() {
    try { return JSON.parse(sessionStorage.getItem(ASKED_KEY) || '[]') || []; } catch (e) { return []; }
  }
  function markAsked(key) {
    try {
      const keys = askedKeys().filter((k) => k !== key);
      keys.push(key);
      sessionStorage.setItem(ASKED_KEY, JSON.stringify(keys.slice(-30)));
    } catch (e) { /* noop */ }
  }
  function sectionOpen(key) {
    try {
      const m = JSON.parse(localStorage.getItem(SECTIONS_KEY) || '{}') || {};
      return m[key] !== false;   // every section starts expanded (§14.2)
    } catch (e) { return true; }
  }
  function rememberSection(key, open) {
    try {
      const m = JSON.parse(localStorage.getItem(SECTIONS_KEY) || '{}') || {};
      if (open) delete m[key]; else m[key] = false;
      localStorage.setItem(SECTIONS_KEY, JSON.stringify(m));
    } catch (e) { /* noop */ }
  }

  /* ==================================================================== state + polling */
  let status = null;            // the last GET /api/settlement/status
  let lastGeneration = null;
  let activeRunId = null;       // the run the last status showed as active
  let pollTimer = null;
  let mutatedTimer = null;
  let promptWanted = true;      // boot asks once; pd-mutated re-arms it
  const startedHere = {};       // run_id -> true: runs this page started (toasts at the end)
  const toldFindings = {};      // run_id -> true: the findings toast was shown

  function readStatus() {
    return api.get('/api/settlement/status', null, BG)
      .then((s) => { apply(s && typeof s === 'object' ? s : null); return status; })
      .catch(() => { apply(status, true); return status; });
  }

  function schedulePoll() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    if (!isActive(status)) return;                // idle: no polling at all
    pollTimer = setTimeout(() => { pollTimer = null; readStatus(); },
      panel ? POLL_PANEL_MS : POLL_CHIP_MS);
  }

  function apply(s, failed) {
    const before = status;
    if (!failed) status = s;
    const off = !status || status.state === 'off';
    renderChip();
    renderBanners();
    if (!failed && status) {
      try {
        document.dispatchEvent(new CustomEvent('pd-settlement-status', { detail: status }));
      } catch (e) { /* noop */ }
      /* A swap published a new generation: every history surface rebuilds (§14.4). */
      if (lastGeneration !== null && typeof status.generation === 'number'
          && status.generation > lastGeneration) {
        try { document.dispatchEvent(new CustomEvent('pd-settlement-swap', { detail: status })); }
        catch (e) { /* noop */ }
      }
      if (typeof status.generation === 'number') lastGeneration = status.generation;
      const runNow = isActive(status) && status.run ? status.run.run_id : null;
      if (activeRunId !== null && activeRunId !== runNow) onRunEnded(activeRunId);
      activeRunId = runNow;
      if (status.run) maybeToastFindings(status.run);
    }
    if (panel) renderPanel(before);
    schedulePoll();
    if (!off && !failed && promptWanted) {
      promptWanted = false;
      maybePrompt();
    }
  }

  /* ==================================================================== the chip (§14.1) */
  let chip = null;
  function ensureChip() {
    if (chip) return chip;
    const tb = document.getElementById('topbar');
    const spacer = tb && tb.querySelector('.header-spacer');
    if (!tb || !spacer) return null;
    chip = el('button', 'badge st-chip');
    chip.type = 'button';
    chip.id = 'settle-chip';
    chip.hidden = true;
    chip.addEventListener('click', () => openPanel());
    spacer.insertAdjacentElement('afterend', chip);
    return chip;
  }
  function chipModel(s) {
    if (!s || s.state === 'off') return null;
    const last = s.last_run || null;
    switch (s.state) {
      case 'clean':
        return { cls: 'st-chip-clean', text: '✓ 已結算',
          title: '歷史已結算至 ' + day(s.settled_through) + '（第 ' + count(s.generation) + ' 版）' };
      case 'pending': {
        const from = s.stale_from || s.dirty_from;
        return { cls: 'st-chip-pending', text: '待結算' + (from ? ' · ' + day(from) + ' 起' : ''),
          title: '待結算：' + (tablesSummary(s.pending && s.pending.tables) || '有變更尚未結算')
            + '。點擊查看並立即重新結算' };
      }
      case 'preparing':
        return { cls: 'st-chip-run', text: '準備中', title: runTitle(s.run) };
      case 'running': {
        const pct = s.run && !nil(s.run.percent) ? ' ' + s.run.percent + '%' : '';
        return { cls: 'st-chip-run st-chip-pulse' + (s.slow ? ' st-chip-slow' : ''),
          text: '結算中' + pct + (s.slow ? ' · 進行緩慢' : ''), title: runTitle(s.run) };
      }
      case 'interrupted':
        return { cls: 'st-chip-warn',
          text: '結算中斷 · ' + (last && last.resumed ? '已續跑' : '未續跑'),
          title: (last && last.status_text) || '結算中斷' };
      case 'failed':
        return { cls: 'st-chip-fail', text: '結算失敗',
          title: ((last && last.status_text) || '失敗') + '。點擊查看原因並重試' };
      case 'selfcheck_diff':
        return { cls: 'st-chip-fail', text: '自檢發現差異',
          title: '每週自檢的全量重算與目前的結算版本不一致。點擊查看差異' };
      default:
        return { cls: 'st-chip-clean', text: String(s.state), title: '' };
    }
  }
  function runTitle(run) {
    if (!run) return '結算進行中';
    const r = run.range || {};
    return '重新結算 ' + range2(r.from, r.to) + ' · ' + trigger(run.trigger_text, run.started_by);
  }
  function renderChip() {
    const model = chipModel(status);
    if (!model) {
      if (chip) chip.hidden = true;
      return;
    }
    if (!ensureChip()) return;
    chip.className = 'badge st-chip ' + model.cls;
    chip.replaceChildren(el('span', 'dot'), el('span', 'st-chip-text', model.text));
    chip.title = model.title;
    chip.hidden = false;
  }

  /* ==================================================================== banners (§14.5) */
  function renderBanners() {
    const s = status;
    const stale = s && s.state !== 'off' && !nil(s.stale_from) ? s : null;
    document.querySelectorAll('[data-settle-banner]').forEach((host) => {
      if (!stale) { host.hidden = true; host.replaceChildren(); return; }
      const text = '更新前版本 · 尚未包含 ' + day(stale.stale_from) + ' 起的變更';
      host.replaceChildren();
      host.appendChild(el('span', 'st-banner-text', text));
      /* 績效比較 only (the trend panel's mode attribute shows it): a stale join can book a
         back-dated position's whole gain as one day's return. */
      if (host.dataset.settleBanner === 'trend') {
        host.appendChild(el('span', 'st-banner-twr', ' · 期間報酬可能失真'));
      }
      const b = el('button', 'st-banner-link', '查看結算');
      b.type = 'button';
      b.addEventListener('click', () => openPanel());
      host.appendChild(el('span', 'st-banner-sep', ' · '));
      host.appendChild(b);
      host.hidden = false;
    });
  }

  /* ==================================================================== the prompt (§14.3) */
  function promptKey(s) {
    if (!nil(s.seq)) return 'seq:' + s.seq;
    const tables = (s.pending && s.pending.tables) || [];
    return 'p:' + [s.dirty_from, s.pending && s.pending.records_total]
      .concat(tables.map((t) => t.table + '=' + t.changes)).join('|');
  }
  let promptRetries = 0;
  function maybePrompt() {
    const s = status;
    if (!s || s.state === 'off' || !s.needs_prompt || isActive(s)) return;
    const key = promptKey(s);
    if (askedKeys().indexOf(key) >= 0) return;
    /* Never on top of another dialog (an edit form that just saved may still be closing):
       ask once it has gone — re-reading the latest status then, for at most two minutes. */
    if (document.querySelector('.modal-backdrop')) {
      if (promptRetries < 120) {
        promptRetries += 1;
        setTimeout(maybePrompt, 1000);
      }
      return;
    }
    promptRetries = 0;
    if (!window.confirmDialog) return;
    markAsked(key);
    const p = s.prompt || {};
    const from = p.from || s.dirty_from;
    const days = nil(p.days) ? '' : '共 ' + count(p.days) + ' 天；';
    const pending = tablesSummary(s.pending && s.pending.tables);
    const est = nil(p.est_s) ? '' : '約需 ' + secs(p.est_s) + '，';
    window.confirmDialog({
      title: '要重新結算歷史資料嗎？',
      body: '這筆變更影響 ' + day(from) + ' 起的歷史結算（' + days + '待結算：'
        + (pending || '有變更') + '）。重新結算' + est + '以背景低優先序執行，期間可以繼續操作。'
        + '選「稍後」會先顯示更新前的歷史走勢與月度成績，並標示「待結算」；'
        + '帳戶數字（總市值、報酬、持倉）已是最新。',
      confirmLabel: '立即重新結算',
      cancelLabel: '稍後',
      cls: 'st-prompt',
      onConfirm: () => startRun('dirty'),
      onCancel: () => { /* 稍後: the chip keeps 待結算; the nightly run settles it (Q4) */ },
    });
  }

  /* ==================================================================== starting / cancelling */
  function startRun(scope, quietOk) {
    return api.post('/api/settlement/run', { scope: scope })
      .then((r) => {
        if (r && !nil(r.run_id)) startedHere[r.run_id] = true;
        if (!quietOk) {
          toast(scope === 'full' ? '重新結算已開始（驗證並重建歷史）' : '重新結算已開始', 'ok',
            '背景低優先序執行，可繼續操作；完成後圖表自動更新');
        }
        return readStatus();
      })
      .catch((err) => {
        if (err && err.status === 409) {
          toast((err && err.message) || '已有一次結算正在進行', 'warn');
          return readStatus();
        }
        toast((err && err.message) || '重新結算無法啟動', 'fail', err && err.code);
        return null;
      });
  }
  function cancelRun() {
    const run = status && status.run;
    if (!window.confirmDialog) return;
    window.confirmDialog({
      title: '取消重新結算？',
      body: (run && run.on_cancel) || '取消後保留目前的結算版本。',
      confirmLabel: '取消重新結算',
      cancelLabel: '繼續結算',
      danger: true,
      onConfirm: () => {
        api.post('/api/settlement/cancel', {})
          .then(() => readStatus())
          .catch((err) => {
            toast((err && err.message) || '無法取消', err && err.status === 409 ? 'warn' : 'fail',
              err && err.code);
            readStatus();
          });
      },
    });
  }
  /* The menu's 重新結算（驗證並重建歷史）(§14.4): a FULL run. Under PD_SETTLE=off there is no
     history to rebuild, so it is the strict ledger replay the item used to run. */
  function rebuild() {
    const go = () => {
      if (status && status.state === 'off') { legacyRecompute(); return; }
      startRun('full');
    };
    if (status) go(); else readStatus().then(go);
  }
  function legacyRecompute() {
    const prog = window.toastProgress
      ? window.toastProgress('重算…', '重放帳本只需一瞬間；驗證通過後重新整理頁面約需 2 秒') : null;
    api.post('/api/actions/recompute')
      .then(() => {
        if (prog) prog.done('重算完成', '帳本重放驗證通過（帳本本身未修改；換匯與資金收支不經此路徑驗證），正在重新整理…');
        setTimeout(() => {
          if (window.pdBeforeReload) window.pdBeforeReload();   // L25: keep the view state
          window.location.reload();
        }, 900);
      })
      .catch((e) => {
        if (prog) prog.fail('重算失敗', (e && e.message) || '請稍後再試');
      });
  }

  /* Findings are a warning toast, once per run, for a run this page started (§14.4). */
  function maybeToastFindings(run) {
    const id = run && run.run_id;
    if (nil(id) || !startedHere[id] || toldFindings[id]) return;
    const v = run.validation;
    if (!v || v.status !== 'done' || !(v.findings || []).length) return;
    toldFindings[id] = true;
    toast('重新結算：發現 ' + count(v.findings.length) + ' 項待釐清', 'warn',
      v.findings.slice(0, 2).map((x) => x.text).join('；')
        + (v.findings.length > 2 ? '；…（詳見結算面板）' : ''));
  }
  function onRunEnded(runId) {
    if (!startedHere[runId]) return;
    api.get('/api/settlement/runs/' + encodeURIComponent(runId), null, BG)
      .then((rep) => {
        if (!rep || !rep.summary) return;
        const sm = rep.summary;
        if (rep.validation) maybeToastFindings({ run_id: runId, validation: rep.validation });
        if (sm.status === 'failed' || sm.status === 'interrupted') {
          toast(sm.status === 'failed' ? '重新結算失敗' : (sm.status_text || '結算中斷'), 'fail',
            (rep.failure && (rep.failure.reason_text || rep.failure.consequence)) || undefined);
        } else if (sm.status === 'cancelled') {
          toast('已取消重新結算', 'ok', (rep.failure && rep.failure.consequence) || undefined);
        } else {
          toast('重新結算' + (sm.status_text || '完成'), 'ok', '歷史走勢與月度成績已更新');
        }
      })
      .catch(() => { /* the chip and the panel still say how it ended */ });
  }

  /* ==================================================================== the panel (§14.2) */
  let panel = null;   // { backdrop, body, view, runId, live, hist, cur }

  function openPanel(opts) {
    const done = () => {
      if (!panel) buildPanel();
      if (opts && !nil(opts.runId)) showRun(opts.runId);
      else renderPanel(null, true);
      schedulePoll();
      readStatus();
    };
    const css = window.pdLoadCss ? window.pdLoadCss('detail.css') : Promise.resolve();
    css.then(done, done);
  }
  function closePanel() {
    if (!panel) return;
    panel.backdrop.remove();
    document.removeEventListener('keydown', onKey);
    panel = null;
    schedulePoll();
  }
  function onKey(e) {
    if (e.key === 'Escape' && !document.querySelector('.modal-backdrop')) closePanel();
  }
  function buildPanel() {
    const backdrop = el('div', 'sd-backdrop st-backdrop');
    const drawer = el('div', 'sd-drawer st-drawer');
    drawer.setAttribute('role', 'dialog');
    drawer.setAttribute('aria-label', '歷史結算');
    backdrop.appendChild(drawer);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) closePanel(); });
    const head = el('div', 'sd-head');
    head.appendChild(el('span', 'sym-code st-title', '歷史結算'));
    head.appendChild(el('span', 'header-spacer'));
    const x = el('button', 'sd-close', '✕');
    x.type = 'button';
    x.title = '關閉';
    x.addEventListener('click', closePanel);
    head.appendChild(x);
    drawer.appendChild(head);
    const body = el('div', 'sd-body st-body');
    drawer.appendChild(body);
    document.body.appendChild(backdrop);
    document.removeEventListener('keydown', onKey);   // one listener per open panel
    document.addEventListener('keydown', onKey);
    panel = { backdrop: backdrop, body: body, view: null, runId: null, live: null,
              hist: { offset: 0, total: 0, rows: [], onlyChanges: false }, cur: null };
  }

  /* Pick the view from the status: a live run → the live sections; otherwise the idle view
     (current state, the last report, the last change-settling report, the history). A
     report opened from the history stays until the owner leaves it. */
  function renderPanel(before, force) {
    if (!panel) return;
    const s = status;
    if (panel.view === 'run' && !force) return;
    if (s && isActive(s) && s.run) {
      if (force || panel.view !== 'live' || !panel.live || panel.live.runId !== s.run.run_id) {
        buildLive(s);
      }
      updateLive(s);
      return;
    }
    const wasLive = panel.view === 'live';
    if (force || panel.view !== 'idle' || wasLive || idleKey(s) !== panel.idleKey) buildIdle(s);
  }
  function idleKey(s) {
    if (!s) return 'none';
    return [s.state, s.generation, s.dirty_from, s.stale_from,
      s.pending && s.pending.records_total, s.last_run && s.last_run.run_id,
      s.last_run && s.last_run.status].join('|');
  }

  /* A collapsible section, its open state remembered per browser. */
  function section(key, title, extraCls) {
    const d = el('details', 'st-sec' + (extraCls ? ' ' + extraCls : ''));
    d.dataset.sec = key;
    d.open = sectionOpen(key);
    const sum = el('summary', 'st-sec-head');
    sum.appendChild(el('span', 'caret', '▶'));
    sum.appendChild(el('span', 'st-sec-title', title));
    d.appendChild(sum);
    const body = el('div', 'st-sec-body');
    d.appendChild(body);
    d.addEventListener('toggle', () => rememberSection(key, d.open));
    return { node: d, body: body };
  }
  function kv(host, k, v, cls) {
    const row = el('div', 'st-kv' + (cls ? ' ' + cls : ''));
    row.appendChild(el('span', 'k', k));
    const val = el('span', 'v');
    if (v instanceof Node) val.appendChild(v); else val.textContent = nil(v) ? NIL : String(v);
    row.appendChild(val);
    host.appendChild(row);
    return row;
  }
  function line(host, text, cls) {
    const n = el('div', 'st-line' + (cls ? ' ' + cls : ''), text);
    host.appendChild(n);
    return n;
  }
  function button(label, cls, fn) {
    const b = el('button', 'btn ' + (cls || ''), label);
    b.type = 'button';
    b.addEventListener('click', fn);
    return b;
  }

  /* ---- a paged table (change trail 50, diff 50, history 20) ---- */
  function pagedTable(host, spec) {
    const box = el('div', 'st-paged');
    const wrap = el('div', 'table-wrap');
    const tbl = el('table', 'data st-table');
    const thead = el('thead');
    const hr = el('tr');
    spec.columns.forEach((c) => hr.appendChild(el('th', c.cls || null, c.label)));
    thead.appendChild(hr);
    tbl.appendChild(thead);
    const tbody = el('tbody');
    tbl.appendChild(tbody);
    wrap.appendChild(tbl);
    box.appendChild(wrap);
    const pager = el('div', 'st-pager');
    const prev = el('button', 'btn btn-sm', '‹ 上一頁');
    const info = el('span', 'st-pager-info');
    const next = el('button', 'btn btn-sm', '下一頁 ›');
    prev.type = 'button';
    next.type = 'button';
    pager.appendChild(prev);
    pager.appendChild(info);
    pager.appendChild(next);
    box.appendChild(pager);
    host.appendChild(box);
    const st = { offset: spec.offset || 0, total: 0, rows: [], seq: 0 };
    function load(offset) {
      const seq = ++st.seq;
      st.offset = Math.max(0, offset);
      info.textContent = '載入中…';
      return spec.read(spec.pageSize, st.offset).then((resp) => {
        if (seq !== st.seq) return;
        const rows = Array.isArray(resp) ? resp : ((resp && resp.rows) || []);
        st.rows = rows;
        st.total = resp && !nil(resp.total_count) ? resp.total_count : rows.length;
        tbody.replaceChildren();
        if (!rows.length) {
          const tr = el('tr');
          const td = el('td', 'sign-nil', spec.empty);
          td.colSpan = spec.columns.length;
          tr.appendChild(td);
          tbody.appendChild(tr);
        }
        rows.forEach((r, i) => {
          const tr = el('tr', spec.rowCls ? spec.rowCls(r) : null);
          spec.row(r).forEach((cell) => {
            const td = el('td', cell.cls || null);
            if (cell.node) td.appendChild(cell.node); else td.textContent = nil(cell.text) ? NIL : cell.text;
            if (cell.title) td.title = cell.title;
            tr.appendChild(td);
          });
          if (spec.onRow) {
            tr.classList.add('st-row-link');
            tr.tabIndex = 0;
            tr.addEventListener('click', () => spec.onRow(r, st.offset + i, st));
            tr.addEventListener('keydown', (e) => {
              if (e.key === 'Enter') spec.onRow(r, st.offset + i, st);
            });
          }
          tbody.appendChild(tr);
        });
        const to = st.offset + rows.length;
        info.textContent = st.total
          ? count(st.offset + (rows.length ? 1 : 0)) + '–' + count(to) + '／共 ' + count(st.total) + ' 筆'
          : '共 0 筆';
        prev.disabled = st.offset <= 0;
        next.disabled = to >= st.total;
        pager.hidden = st.total <= spec.pageSize && st.offset === 0;
        if (spec.onLoad) spec.onLoad(st);
      }).catch((err) => {
        if (seq !== st.seq) return;
        tbody.replaceChildren();
        const tr = el('tr');
        const td = el('td', 'sign-nil', (err && err.message) || '載入失敗，請稍後再試');
        td.colSpan = spec.columns.length;
        tr.appendChild(td);
        tbody.appendChild(tr);
        info.textContent = '';
      });
    }
    prev.addEventListener('click', () => load(st.offset - spec.pageSize));
    next.addEventListener('click', () => load(st.offset + spec.pageSize));
    load(st.offset);
    return { reload: () => load(st.offset), state: st, load: load };
  }

  /* ---- the change trail: every record, paged 50, never truncated (§9.6) ---- */
  const CHANGE_COLUMNS = [
    { label: '帳本', cls: 'col-text' }, { label: '動作', cls: 'col-text' },
    { label: '紀錄', cls: 'col-text' }, { label: '帳戶', cls: 'col-text' },
    { label: '代號', cls: 'col-text' }, { label: '日期', cls: 'col-text' },
    { label: '匯入批次', cls: 'col-text' }, { label: '寫入者', cls: 'col-text' },
    { label: '寫入時間', cls: 'col-text' }, { label: '備註', cls: 'col-text' },
  ];
  const oldNew = (a, b, fn) => {
    const fa = nil(a) ? '' : fn(a);
    const fb = nil(b) ? '' : fn(b);
    if (!fa && !fb) return NIL;
    if (!fa || !fb || fa === fb) return fa || fb;
    return fa + ' → ' + fb;
  };
  function attempted(ids) {
    return (ids || []).map((id) => '曾於 #' + id + ' 嘗試').join('、');
  }
  function changeRow(r) {
    if (nil(r.row_key) && !nil(r.key)) {   // a ChangeCounter: prices per symbol, FX per pair
      return [
        { text: CHANGE_TABLE_ZH[r.table] || r.table, cls: 'col-text' },
        { text: '寫入', cls: 'col-text' },
        { text: r.key, cls: 'col-text num' },
        { text: NIL, cls: 'col-text' }, { text: NIL, cls: 'col-text' },
        { text: range2(r.earliest, r.latest), cls: 'col-text num' },
        { text: NIL, cls: 'col-text' }, { text: NIL, cls: 'col-text' },
        { text: NIL, cls: 'col-text' },
        { text: [count(r.changes) + ' 筆', attempted(r.attempted_run_ids)]
          .filter(Boolean).join('；'), cls: 'col-text' },
      ];
    }
    return [
      { text: CHANGE_TABLE_ZH[r.table] || r.table, cls: 'col-text' },
      { text: CHANGE_OP_ZH[r.op] || r.op, cls: 'col-text' },
      { text: r.row_key, cls: 'col-text num' },
      { text: oldNew(r.old_account_id, r.new_account_id, acct), cls: 'col-text' },
      { text: oldNew(r.old_symbol, r.new_symbol, String), cls: 'col-text num' },
      { text: oldNew(r.old_day, r.new_day, day), cls: 'col-text num' },
      { text: nil(r.import_batch_id) ? NIL : String(r.import_batch_id), cls: 'col-text num' },
      { text: actorText(r.actor), cls: 'col-text' },
      { text: stamp(r.written_at), cls: 'col-text num' },
      { text: attempted(r.attempted_run_ids) || NIL, cls: 'col-text' },
    ];
  }
  function changeTrail(host, state, empty) {
    return pagedTable(host, {
      columns: CHANGE_COLUMNS,
      pageSize: CHANGES_PAGE,
      empty: empty || '沒有變更紀錄',
      read: (limit, offset) => api.get('/api/settlement/changes',
        { state: state, limit: limit, offset: offset }, BG),
      row: changeRow,
    });
  }
  function changeTables(host, tables, emptyText) {
    if (!(tables || []).length) { line(host, emptyText || '沒有變更', 'st-muted'); return; }
    const ul = el('ul', 'st-list');
    tables.forEach((t) => ul.appendChild(el('li', null, tableLine(t, true))));
    host.appendChild(ul);
  }

  /* ---- steps: ✓ / ▸ / · with start, elapsed, count and the phase's own detail line ---- */
  function stepsList(host, phases) {
    const ul = el('ul', 'st-steps');
    (phases || []).forEach((p) => {
      const li = el('li', 'st-step st-step-' + (p.status || 'pending'));
      li.appendChild(el('span', 'st-step-mark', PHASE_MARK[p.status] || '·'));
      const main = el('div', 'st-step-main');
      const top = el('div', 'st-step-top');
      top.appendChild(el('span', 'st-step-label', p.label || PHASE_ZH[p.key] || p.key));
      const bits = [];
      if (!nil(p.started_at)) bits.push(clock(p.started_at));
      if (!nil(p.elapsed_s)) bits.push(secs(p.elapsed_s));
      if (!nil(p.total)) {
        bits.push((nil(p.done) ? '' : count(p.done) + ' / ') + count(p.total) + (p.unit ? ' ' + p.unit : ''));
      }
      if (!nil(p.rate)) bits.push(p.rate);
      if (bits.length) top.appendChild(el('span', 'st-step-meta num', bits.join(' · ')));
      main.appendChild(top);
      if (!nil(p.detail)) main.appendChild(el('div', 'st-step-detail', p.detail));
      li.appendChild(main);
      ul.appendChild(li);
    });
    host.appendChild(ul);
  }
  function findingsList(host, validation) {
    const v = validation || {};
    const fs = v.findings || [];
    if (!fs.length) {
      line(host, v.status === 'done' ? '未發現待釐清項目'
        : (v.status === 'running' ? '檢查中…' : '尚未檢查'), 'st-muted');
      return;
    }
    const ul = el('ul', 'st-list st-findings');
    fs.forEach((x) => ul.appendChild(el('li', null, (nil(x.day) ? '' : day(x.day) + '　') + x.text)));
    host.appendChild(ul);
  }
  function versionsBlock(host, v, live) {
    v = v || {};
    const genAfter = !nil(v.generation_after) ? v.generation_after
      : (live && !nil(v.generation_before) ? v.generation_before + 1 : null);
    kv(host, '版本', '第 ' + count(v.generation_before) + ' 版'
      + (nil(genAfter) ? '' : ' → 第 ' + count(genAfter) + ' 版' + (live ? '（切換後生效）' : '')));
    kv(host, '結算至', day(v.settled_through_before)
      + (nil(v.settled_through_after) ? '' : ' → ' + day(v.settled_through_after)));
    const calcDiff = !nil(v.calc_version) && v.calc_version !== v.calc_version_before;
    kv(host, '計算規則', (v.calc_version_before || NIL)
      + (nil(v.calc_version) ? '' : ' → ' + v.calc_version), calcDiff ? 'st-changed' : null);
    const ccyDiff = !nil(v.reporting_ccy) && v.reporting_ccy !== v.reporting_ccy_before;
    kv(host, '報告幣別', (v.reporting_ccy_before || NIL)
      + (nil(v.reporting_ccy) ? '' : ' → ' + v.reporting_ccy), ccyDiff ? 'st-changed' : null);
  }
  function rangeText(r) {
    r = r || {};
    if (nil(r.from)) return NIL;
    return range2(r.from, r.to) + (nil(r.days) ? '' : '，共 ' + count(r.days) + ' 天');
  }
  function rangeBlock(host, r) {
    r = r || {};
    kv(host, '範圍', rangeText(r));
    if (!nil(r.kept_before)) kv(host, '沿用前一版', count(r.kept_before) + ' 天');
    if (!nil(r.kind)) kv(host, '種類', r.kind === 'full' ? '全部重新結算' : '待結算範圍');
    if (!nil(r.published_to) || !nil(r.handed_from)) {
      kv(host, '部分發布', (nil(r.published_to) ? '' : '已發布至 ' + day(r.published_to))
        + (nil(r.handed_from) ? '' : '；' + day(r.handed_from) + ' 起交由接續的結算'));
    }
  }

  /* ---- the LIVE view: every field of the progress file (§14.2 sections 1–9) ---- */
  function buildLive(s) {
    const run = s.run;
    panel.view = 'live';
    panel.idleKey = null;
    const b = panel.body;
    b.replaceChildren();
    const live = { runId: run.run_id };
    panel.live = live;

    const head = el('div', 'st-live-head');
    live.title = el('div', 'st-live-title');
    live.trigger = el('div', 'st-live-sub');
    live.time = el('div', 'st-live-sub num');
    const bar = el('div', 'st-bar');
    live.fill = el('div', 'st-bar-fill');
    bar.appendChild(live.fill);
    live.pct = el('span', 'st-bar-pct num');
    const barRow = el('div', 'st-bar-row');
    barRow.appendChild(bar);
    barRow.appendChild(live.pct);
    live.priority = el('div', 'st-live-sub');
    [live.title, live.trigger, live.time, barRow, live.priority].forEach((n) => head.appendChild(n));
    b.appendChild(head);

    const why = section('live-changes', '為何結算：這次結算的變更');
    live.whyTables = el('div');
    why.body.appendChild(live.whyTables);
    live.queuedBack = el('div');
    why.body.appendChild(live.queuedBack);
    changeTrail(why.body, 'run:' + run.run_id, '這次結算沒有領取任何變更（例行延伸結算）');
    b.appendChild(why.node);

    const steps = section('live-steps', '步驟');
    live.steps = steps.body;
    b.appendChild(steps.node);

    const cur = section('live-current', '目前項目');
    live.current = cur.body;
    b.appendChild(cur.node);

    const todo = section('live-todo', '尚待處理');
    live.todo = todo.body;
    b.appendChild(todo.node);

    const val = section('live-validation', '帳本檢查');
    live.validation = val.body;
    b.appendChild(val.node);

    const est = section('live-estimate', '預估');
    live.estimate = est.body;
    b.appendChild(est.node);

    const ver = section('live-versions', '版本');
    live.versions = ver.body;
    b.appendChild(ver.node);

    const act = el('div', 'st-actions');
    live.cancel = button('取消（保留目前版本）', 'btn-danger', cancelRun);
    act.appendChild(live.cancel);
    live.onCancel = el('div', 'st-muted st-consequence');
    act.appendChild(live.onCancel);
    b.appendChild(act);
  }
  function etaText(run) {
    if (nil(run.eta_s)) return '估算中';
    if (Number(run.eta_s) < 5) return '即將完成';
    return '預估還要 ' + secs(run.eta_s);
  }
  function updateLive(s) {
    const run = s.run;
    const live = panel.live;
    const r = run.range || {};
    live.title.textContent = '重新結算：' + rangeText(r);
    live.trigger.textContent = trigger(run.trigger_text, run.started_by);
    live.time.textContent = '已用 ' + secs(run.elapsed_s) + ' · ' + etaText(run)
      + (s.slow ? ' · 進行緩慢' : '');
    live.time.classList.toggle('st-slow', !!s.slow);
    const pct = nil(run.percent) ? 0 : Math.max(0, Math.min(100, Number(run.percent) || 0));
    live.fill.style.width = pct + '%';
    live.pct.textContent = pct + '%';
    live.priority.textContent = run.priority || '';
    live.priority.hidden = !run.priority;

    live.whyTables.replaceChildren();
    changeTables(live.whyTables, run.changes && run.changes.tables, '這次結算沒有領取任何變更');

    live.steps.replaceChildren();
    stepsList(live.steps, run.phases);

    live.current.replaceChildren();
    if (run.current) {
      line(live.current, '正在估值：' + day(run.current.day) + '（' + count(run.current.positions)
        + ' 個部位）', 'num');
    } else {
      line(live.current, run.status === 'preparing' ? '準備中' : '—', 'st-muted');
    }

    live.todo.replaceChildren();
    const pend = (run.phases || []).filter((p) => p.status === 'pending');
    if (pend.length) {
      const ul = el('ul', 'st-list');
      pend.forEach((p) => ul.appendChild(el('li', null, (p.label || PHASE_ZH[p.key] || p.key)
        + (nil(p.total) ? '' : '（' + count(p.total) + (p.unit ? ' ' + p.unit : '') + '）'))));
      live.todo.appendChild(ul);
    }
    (run.queued || []).forEach((q) => {
      line(live.todo, '完成後接著處理：' + day(q.from) + ' 起的變更（' + count(q.changes) + ' 筆）'
        + (q.reason ? '・' + q.reason : ''));
    });
    if (!pend.length && !(run.queued || []).length) line(live.todo, '沒有其他待處理項目', 'st-muted');

    live.validation.replaceChildren();
    findingsList(live.validation, run.validation);

    live.estimate.replaceChildren();
    kv(live.estimate, '首次預估', secs(run.eta_initial_s));
    kv(live.estimate, '目前預估', etaText(run));
    if (run.eta_basis) kv(live.estimate, '依據', run.eta_basis);

    live.versions.replaceChildren();
    versionsBlock(live.versions, run.versions, true);

    live.onCancel.textContent = run.on_cancel ? '取消的結果：' + run.on_cancel : '';
    live.cancel.disabled = run.status !== 'running' && run.status !== 'preparing';
  }

  /* ---- the IDLE view: state, last report, last change-settling report, history ---- */
  function buildIdle(s) {
    panel.view = 'idle';
    panel.live = null;
    panel.idleKey = idleKey(s);
    const b = panel.body;
    b.replaceChildren();
    const cur = el('div', 'st-state');
    b.appendChild(cur);
    if (!s) {
      line(cur, '無法讀取結算狀態，請稍後再試。', 'st-muted');
      return;
    }
    if (s.state === 'off') {
      line(cur, '歷史結算已停用：歷史走勢與月度成績都即時計算。', 'st-muted');
      return;
    }
    const acts = el('div', 'st-actions');
    if (s.state === 'pending') {
      line(cur, '待結算：歷史走勢與月度成績尚未包含 ' + day(s.stale_from || s.dirty_from)
        + ' 起的變更', 'st-state-pending');
      changeTables(cur, s.pending && s.pending.tables, '有變更尚未結算');
      changeTrail(cur, 'pending', '沒有待結算的變更');
      acts.appendChild(button('立即重新結算', 'btn-primary', () => startRun('dirty')));
    } else if (s.state === 'failed' || s.state === 'interrupted') {
      line(cur, (s.last_run && s.last_run.status_text)
        || (s.state === 'failed' ? '結算失敗' : '結算中斷'), 'st-state-fail');
      acts.appendChild(button('重試', 'btn-primary', retry));
    } else if (s.state === 'selfcheck_diff') {
      line(cur, '每週自檢發現差異：全量重算與目前的結算版本不一致，差異列在下方報告。',
        'st-state-fail');
    } else {
      line(cur, '已結算至 ' + day(s.settled_through) + ' · 第 ' + count(s.generation) + ' 版',
        'st-state-clean');
    }
    if (!nil(s.stale_from) && s.state !== 'pending') {
      line(cur, '歷史走勢與月度成績尚未包含 ' + day(s.stale_from) + ' 起的變更', 'st-state-pending');
    }
    acts.appendChild(button('重新結算（驗證並重建歷史）', '', () => startRun('full')));
    cur.appendChild(acts);

    const last = s.last_run;
    if (last) {
      const card = el('div', 'st-card');
      card.appendChild(el('h3', 'st-card-title', '最近一次結算 #' + last.run_id));
      b.appendChild(card);
      loadReport(card, last.run_id);
      const lc = s.last_change_run;
      if (!last.had_changes && lc && lc.run_id !== last.run_id) {
        const card2 = el('div', 'st-card');
        card2.appendChild(el('h3', 'st-card-title', '最近一次結算變更 #' + lc.run_id));
        b.appendChild(card2);
        loadReport(card2, lc.run_id);
      }
    } else {
      line(b, '尚無結算紀錄。', 'st-muted');
    }
    historyBlock(b);
  }
  function retry() {
    const last = status && status.last_run;
    if (!last) { startRun('dirty'); return; }
    api.get('/api/settlement/runs/' + encodeURIComponent(last.run_id), null, BG)
      .then((rep) => startRun(rep && rep.inputs && rep.inputs.scope === 'full' ? 'full' : 'dirty'))
      .catch(() => startRun('dirty'));
  }

  /* ---- the history (§14.2): newest first, paged 20, the nightly filter, click → report ---- */
  function historyBlock(host) {
    const sec = section('history', '結算紀錄');
    host.appendChild(sec.node);
    const filt = el('label', 'st-filter');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = !!panel.hist.onlyChanges;
    filt.appendChild(cb);
    filt.appendChild(el('span', null, '隱藏沒有結算變更的每晚例行結算'));
    sec.body.appendChild(filt);
    const holder = el('div');
    sec.body.appendChild(holder);
    const mount = () => {
      holder.replaceChildren();
      pagedTable(holder, {
        columns: [
          { label: '#', cls: 'col-text' }, { label: '開始', cls: 'col-text' },
          { label: '觸發', cls: 'col-text' }, { label: '範圍', cls: 'col-text' },
          { label: '耗時' }, { label: '結果', cls: 'col-text' },
        ],
        pageSize: RUNS_PAGE,
        offset: panel.hist.offset,
        empty: '尚無結算紀錄',
        read: (limit, offset) => api.get('/api/settlement/runs', {
          limit: limit, offset: offset, only_changes: panel.hist.onlyChanges ? 1 : undefined,
        }, BG),
        row: (r) => [
          { text: '#' + r.run_id, cls: 'col-text num' },
          { text: stamp(r.started_at), cls: 'col-text num' },
          { text: trigger(r.trigger_text, r.started_by) || reasonText(r.reason), cls: 'col-text' },
          { text: range2(r.range_from, r.range_to), cls: 'col-text num' },
          { text: secs(r.duration_s), cls: 'num' },
          { node: statusPill(r), cls: 'col-text' },
        ],
        onLoad: (st) => {
          panel.hist.offset = st.offset;
          panel.hist.total = st.total;
          panel.hist.rows = st.rows;
        },
        onRow: (r, index) => showRun(r.run_id, index),
      });
    };
    cb.addEventListener('change', () => {
      panel.hist.onlyChanges = cb.checked;
      panel.hist.offset = 0;
      mount();
    });
    mount();
  }
  function statusPill(sm) {
    const k = sm.status === 'published' ? 'st-pill-ok'
      : (sm.status === 'failed' || sm.status === 'interrupted' || sm.selfcheck_diff) ? 'st-pill-fail'
        : (sm.status === 'partial' || sm.status === 'superseded' || sm.status === 'cancelled')
          ? 'st-pill-warn' : 'st-pill-run';
    const p = el('span', 'st-pill ' + k);
    p.appendChild(el('span', 'dot'));
    p.appendChild(document.createTextNode(sm.status_text || sm.status));
    return p;
  }

  /* ---- one run's report, opened from the history, with 上一筆 / 下一筆 ---- */
  function showRun(runId, index) {
    if (!panel) return;
    panel.view = 'run';
    panel.runId = runId;
    panel.idleKey = null;
    const b = panel.body;
    b.replaceChildren();
    const nav = el('div', 'st-nav');
    nav.appendChild(button('← 回到目前狀態', 'btn-sm', () => { panel.view = null; renderPanel(null, true); }));
    const prev = button('上一筆', 'btn-sm', () => step(-1));
    prev.title = '較新的一筆';
    const next = button('下一筆', 'btn-sm', () => step(1));
    next.title = '較舊的一筆';
    const h = panel.hist;
    const idx = nil(index) ? h.rows.findIndex((r) => r.run_id === runId) + h.offset : index;
    panel.histIndex = idx >= h.offset ? idx : null;
    prev.disabled = panel.histIndex === null || panel.histIndex <= 0;
    next.disabled = panel.histIndex === null || panel.histIndex >= h.total - 1;
    nav.appendChild(prev);
    nav.appendChild(next);
    b.appendChild(nav);
    const card = el('div', 'st-card');
    card.appendChild(el('h3', 'st-card-title', '結算 #' + runId));
    b.appendChild(card);
    loadReport(card, runId);
  }
  /* Step through the history list in its own order (newest first), across pages. */
  function step(delta) {
    const h = panel.hist;
    if (panel.histIndex === null) return;
    const target = panel.histIndex + delta;
    if (target < 0 || target >= h.total) return;
    const local = target - h.offset;
    if (local >= 0 && local < h.rows.length) { showRun(h.rows[local].run_id, target); return; }
    const offset = Math.floor(target / RUNS_PAGE) * RUNS_PAGE;
    api.get('/api/settlement/runs', {
      limit: RUNS_PAGE, offset: offset, only_changes: h.onlyChanges ? 1 : undefined,
    }, BG).then((resp) => {
      const rows = Array.isArray(resp) ? resp : ((resp && resp.rows) || []);
      h.offset = offset;
      h.rows = rows;
      if (resp && !nil(resp.total_count)) h.total = resp.total_count;
      const r = rows[target - offset];
      if (r) showRun(r.run_id, target);
    }).catch((err) => toast((err && err.message) || '結算紀錄載入失敗', 'fail'));
  }

  /* ---- the full report (§9.6): every section, read from the database ---- */
  function loadReport(card, runId) {
    const holder = el('div', 'st-report');
    holder.appendChild(el('div', 'st-muted', '載入中…'));
    card.appendChild(holder);
    api.get('/api/settlement/runs/' + encodeURIComponent(runId), null, BG)
      .then((rep) => { holder.replaceChildren(); renderReport(holder, rep, runId); })
      .catch((err) => {
        holder.replaceChildren();
        line(holder, err && err.status === 404 ? '已超過保留期限'
          : ((err && err.message) || '報告載入失敗'), 'st-muted');
      });
  }
  function chainLink(label, runId) {
    const a = el('button', 'st-link', label + ' #' + runId);
    a.type = 'button';
    a.addEventListener('click', () => showRun(runId));
    return a;
  }
  function renderReport(host, rep, runId) {
    if (!rep || !rep.summary) { line(host, '報告內容不完整', 'st-muted'); return; }
    const sm = rep.summary;
    const key = (k) => 'rep-' + k;

    const sum = section(key('summary'), '摘要');
    const st = el('span');
    st.appendChild(statusPill(sm));
    kv(sum.body, '結果', st);
    kv(sum.body, '觸發', trigger(sm.trigger_text, sm.started_by) || reasonText(sm.reason));
    if (!nil(sm.cancelled_by)) kv(sum.body, '取消者', actorText(sm.cancelled_by));
    kv(sum.body, '開始', stamp(sm.started_at));
    kv(sum.body, '結束', stamp(sm.finished_at));
    kv(sum.body, '耗時', secs(sm.duration_s)
      + (nil(sm.eta_initial_s) ? '' : '（首次預估 ' + secs(sm.eta_initial_s) + '）'));
    if (!nil(sm.previous_run_id) || !nil(sm.next_run_id)) {
      const chain = el('span', 'st-chain');
      if (!nil(sm.previous_run_id)) chain.appendChild(chainLink('接續自', sm.previous_run_id));
      if (!nil(sm.next_run_id)) chain.appendChild(chainLink('接著處理', sm.next_run_id));
      kv(sum.body, '接續', chain);
    }
    host.appendChild(sum.node);

    const rg = section(key('range'), '範圍');
    rangeBlock(rg.body, rep.range);
    host.appendChild(rg.node);

    const ver = section(key('versions'), '版本');
    versionsBlock(ver.body, rep.versions, false);
    host.appendChild(ver.node);

    const ch = section(key('changes'), '結算的變更');
    if (rep.changes && rep.changes.handed_back) {
      line(ch.body, '本次未生效，已退回待結算', 'st-state-pending');
    }
    changeTables(ch.body, rep.changes && rep.changes.tables, '這次結算沒有變更（例行延伸結算）');
    if (rep.changes && rep.changes.records_total) {
      changeTrail(ch.body, 'run:' + runId, '沒有變更紀錄');
    }
    host.appendChild(ch.node);

    const val = section(key('validation'), '帳本檢查');
    findingsList(val.body, rep.validation);
    host.appendChild(val.node);

    const steps = section(key('steps'), '步驟');
    stepsList(steps.body, rep.steps);
    host.appendChild(steps.node);

    const hist = section(key('history'), '結果：歷史走勢');
    historyResult(hist.body, rep, runId);
    host.appendChild(hist.node);

    const closes = section(key('closes'), '結果：月結與年結');
    closesResult(closes.body, rep);
    host.appendChild(closes.node);

    if (rep.state) {
      const sr = section(key('state'), '結果：狀態');
      stateResult(sr.body, rep.state);
      host.appendChild(sr.node);
    }
    if (rep.failure) {
      const fl = section(key('failure'), '失敗／取消／中斷', 'st-sec-fail');
      const f = rep.failure;
      if (!nil(f.phase)) kv(fl.body, '停在', phaseLabel(rep, f.phase));
      kv(fl.body, '原因', f.reason_text);
      if (!nil(f.cancelled_by)) kv(fl.body, '取消者', actorText(f.cancelled_by));
      if (!nil(f.consequence)) kv(fl.body, '結果', f.consequence);
      host.appendChild(fl.node);
    }
  }
  function phaseLabel(rep, key) {
    const p = (rep.steps || []).find((x) => x.key === key);
    return (p && p.label) || PHASE_ZH[key] || key;
  }
  function historyResult(host, rep, runId) {
    line(host, '歷史走勢的變動（帳戶即時數字一直是最新的）', 'st-muted');
    const h = rep.history;
    if (!h) { line(host, '這次結算沒有比較歷史走勢', 'st-muted'); return; }
    const ccy = (rep.versions && rep.versions.reporting_ccy) || 'TWD';
    if (h.note) line(host, h.note);
    if (h.currency_changed) return;
    const counts = h.counts || {};
    const parts = Object.keys(counts).filter((k) => counts[k])
      .map((k) => (DIFF_FIELD_ZH[k] || k) + ' ' + count(counts[k]) + ' 天');
    if (h.value_to_none) parts.push('有值 → 無值 ' + count(h.value_to_none) + ' 天');
    if (h.none_to_value) parts.push('無值 → 有值 ' + count(h.none_to_value) + ' 天');
    if (h.flag_flips) parts.push('缺價標記改變 ' + count(h.flag_flips) + ' 天');
    if (h.new_days) parts.push('新結算 ' + count(h.new_days) + ' 天');
    if (h.dropped_days) parts.push('移除 ' + count(h.dropped_days) + ' 天');
    if (!parts.length) { line(host, '歷史走勢無變動'); return; }
    kv(host, '變動', parts.join('、'));
    if (!nil(h.first_changed_day)) kv(host, '變動期間', range2(h.first_changed_day, h.last_changed_day));
    const dv = (x) => (x ? day(x.day) + '：' + money(x.before, ccy) + ' → ' + money(x.after, ccy) : NIL);
    if (h.largest_net_worth_change) kv(host, '總淨值最大變動', dv(h.largest_net_worth_change));
    if (h.last_common_day) kv(host, '兩版共同的最後一天', dv(h.last_common_day));
    pagedTable(host, {
      columns: [
        { label: '日期', cls: 'col-text' }, { label: '欄位', cls: 'col-text' },
        { label: '類別', cls: 'col-text' }, { label: '更新前' }, { label: '更新後' },
      ],
      pageSize: DIFF_PAGE,
      empty: '沒有逐日變動',
      read: (limit, offset) => api.get('/api/settlement/runs/' + encodeURIComponent(runId) + '/diff',
        { limit: limit, offset: offset }, BG),
      row: (r) => {
        const flagged = (flags) => Object.keys(flags || {}).filter((k) => flags[k])
          .map((k) => (k === 'incomplete' ? '缺價' : k)).join('、') || '完整';
        const val = (v, flags) => (r.field === 'incomplete' ? flagged(flags) : money(v, ccy));
        return [
          { text: day(r.day), cls: 'col-text num' },
          { text: DIFF_FIELD_ZH[r.field] || r.field, cls: 'col-text' },
          { text: DIFF_KIND_ZH[r.kind] || r.kind, cls: 'col-text' },
          { text: r.kind === 'new_day' ? NIL : val(r.before, r.flags_before), cls: 'num' },
          { text: r.kind === 'dropped_day' ? NIL : val(r.after, r.flags_after), cls: 'num' },
        ];
      },
    });
  }
  function closesResult(host, rep) {
    const rows = Array.isArray(rep.restatements) ? rep.restatements : null;
    const ids = rep.restatement_ids || [];
    if (rows && rows.length) {
      const fmtUnit = (v, unit) => (unit === 'ratio'
        ? (fm() ? fm().signedPct(v) : String(v)) : money(v, unit));
      const ul = el('ul', 'st-list');
      rows.forEach((x) => {
        ul.appendChild(el('li', null, x.period + '　' + (x.label || x.field) + '：'
          + fmtUnit(x.old_value, x.unit) + ' → ' + fmtUnit(x.new_value, x.unit)
          + (x.cutover ? '（上線重述）' : '')));
      });
      host.appendChild(ul);
      return;
    }
    if (ids.length) {
      line(host, '月結與年結重述 ' + count(ids.length) + ' 筆（月度成績的列上標示「已重述」，滑過可看前後值）');
      return;
    }
    line(host, '月結與年結無變動');
  }
  function stateResult(host, s) {
    if (s.stale_cleared === true) kv(host, '待結算範圍', '已清除');
    else if (s.stale_cleared === false) kv(host, '待結算範圍', '仍未清除' + (s.stale_reason ? '：' + s.stale_reason : ''));
    if (!nil(s.chained_from)) kv(host, '接續結算', day(s.chained_from) + ' 起的變更');
    const pp = s.pending_positions || [];
    if (pp.length) {
      line(host, '最後結算日帳上的待釐清部位：');
      const ul = el('ul', 'st-list st-findings');
      pp.forEach((x) => ul.appendChild(el('li', null, x.text)));
      host.appendChild(ul);
    }
  }

  /* ==================================================================== boot */
  document.addEventListener('pd-mutated', () => {
    if (mutatedTimer) clearTimeout(mutatedTimer);
    mutatedTimer = setTimeout(() => {
      mutatedTimer = null;
      promptWanted = true;
      readStatus();
    }, MUTATED_WAIT_MS);
  });

  window.pdSettlement = {
    open: openPanel,
    refresh: readStatus,
    rebuild: rebuild,
    status: () => status,
    reasonText: reasonText,
  };
  readStatus();
})();
