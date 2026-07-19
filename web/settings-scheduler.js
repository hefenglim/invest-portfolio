/* portfolio-dash — 設定 · 排程 (wired to /api/scheduler/*, spec 15/19).

   Boot: GET /api/scheduler/jobs -> { jobs:[{id,desc,cron,tz,enabled,last,next}] } and
   GET /api/scheduler/runs -> { rows:[{id,job_id,started_at,finished_at,status,detail,
   duration_s,cost_usd}], total_count }, fetched in PARALLEL. The inline window.SCHED_DATA
   mock is RETIRED.

   MONEY DISCIPLINE (data-and-pricing.md / war-game Finding 8): a run's `cost_usd` arrives
   as a Decimal STRING and is OFTEN null (non-insight jobs). The string "0"/"0.00" is
   TRUTHY, so the nil-check is `cost_usd == null` (NOT `!cost_usd`) and the value is shown
   via f.num(cost_usd, 3) — never `bareString.toFixed`. `duration_s` is a count (seconds).

   Write paths (all via pdApi; success -> toast + re-fetch; PdApiError -> toast(message,
   'fail', code); try/catch graceful so a failure never throws an unhandled rejection):
   - PUT /api/scheduler/jobs/{id}        (cron / tz / enabled)
   - POST /api/scheduler/jobs/{id}/run   (manual run; 202 + run_id, 409 already-running)

   FU-D36 (需求七) — run-now live status: each row carries a 狀態 chip fed by
   GET /api/scheduler/status -> { active, jobs:{ <id>:{running, queued, progress, last_run} } }.
   Clicking 立即執行 flips the row to 已排入, then polling (every ~3s, ONLY while something
   is queued/running + a short grace after a trigger) advances it 執行中 -> 成功/失敗 with the
   last-run message; polling STOPS when nothing is active, so an idle page polls zero times.

   FU-D46 — progress sub-text + completion detail modal: while 執行中 the chip shows the
   job's live `progress` message (re-painted each poll tick). A COMPLETED chip (成功/失敗/
   略過) is clickable -> a detail modal (shared .modal-backdrop pattern from styles.css):
   zh job name, status, started/finished + duration_seconds, the full detail text
   (scrollable pre-wrap), an LLM Token/費用 line when the status served a `cost` block
   (cost_usd is a Decimal STRING -> f.num, same Finding-8 discipline), and a 前往 button
   for jobs with a verified landing page (JOB_HREF map below). One modal at a time; closes
   on ✕ / backdrop / Esc, and the Esc listener is removed on dismiss (no listener leak). */
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

  function _toast(msg, kind, code) {
    if (window.toast) window.toast(msg, kind, code);
  }

  /* Desync fix (FU-D3): after ANY schedule-row write, broadcast so the digest card
     (settings-digest.js, now on this same tab) and this jobs table both re-fetch from
     fresh data. The listeners GET + re-render only (never re-PUT), so there is no loop. */
  function dispatchJobsChanged() {
    document.dispatchEvent(new CustomEvent('pd-jobs-changed'));
  }

  /* Structural data from the GETs. Starts empty so a pre-fetch render is blank. */
  let jobs = [];
  let runs = [];
  let sysRows = [];

  /* WPD (2026-07-07): the 200-dump + client-side job filter is replaced by SERVER
     job_id filter + limit/offset with the shared pdPager. Page size = the user's
     每頁筆數 clamped to the endpoint max (500). */
  const PAGE = Math.min((window.pdPrefs && window.pdPrefs.page_size) || 50, 500);
  const runState = { job: 'all', offset: 0, limit: PAGE };
  const sysState = { offset: 0, limit: PAGE };
  let runsPager = null;
  let sysPager = null;

  /* zh labels for the registered jobs (item 8: the run history must read like
     "what the system did", not internal ids). Unmapped ids fall back to desc/id. */
  const JOB_ZH = {
    quotes_tw: '台股報價＋匯率（收盤後）',
    quotes_us: '美股報價＋匯率（收盤後）',
    quotes_my: '馬股報價＋匯率（收盤後）',
    history_daily: '日線歷史回補（近 7 天滾動）',
    dividends_daily: '股利／除息事件掃描',
    dividend_inbox_scan: '配息偵測（餵入待確認匯入）',
    snapshot_monthly: '月度 KPI 快照（每晚覆寫當月）',
    finmind_chips_daily: '台股籌碼（法人＋融資券）',
    finmind_valuation_daily: '台股估值（PER／PBR／殖利率）',
    finmind_fundamentals_monthly: '台股月營收＋財報（每月）',
    sentiment_daily: '市場情緒（VIX＋恐懼貪婪）',
    index_quotes_daily: '大盤指數收盤（台／美／馬）',
    consensus_daily: '分析師共識',
    signal_scan: '技術訊號掃描',
    news_daily: '新聞摘要',
    alert_scan: '風險警示掃描＋AI 派發',
    evaluate_insights: 'AI 洞察每日評分（Loop 2）',
    generate_calibrations: 'AI 校準版本週產生（Loop 3）',
    backup_daily: '資料庫備份＋完整性檢查',
    digest_daily: '每日收盤摘要',
    digest_weekly: '每週行動清單',
  };
  const jobLabel = (id, desc) => JOB_ZH[id] || desc || id;

  /* ===== FU-D36 (需求七): per-row live run status ==============================
     renderJobs stores each row's 狀態 slot + run button by job_id; GET /api/scheduler/
     status feeds a chip 已排入 → 執行中 → 成功/失敗. Polling runs ONLY while something is
     queued/running (plus a short grace after a trigger) and STOPS when idle, so an idle
     page makes zero status requests. Single timer handle → the loop provably terminates. */
  const rowRefs = new Map();   // job_id -> { statusSlot, runBtn, lastSeed }
  let statusByJob = {};        // last /status map { <id>:{running,queued,last_run} }
  let pollActive = false;      // true between start/stop — guards against overlapping ticks
  let pollTimer = null;        // setTimeout handle (cleared + nulled on stop)
  let graceUntil = 0;          // keep polling until >= this even if nothing looks active yet
  const POLL_MS = 3000;
  const GRACE_MS = 6000;

  /* Run-history status -> [pill class, zh label]. Shared by the history table so a
     'running'/'skipped' row is not miscoloured as 失敗 (unmapped -> 失敗). */
  const HIST_STATUS = {
    ok: ['pill-ok', '成功'],
    running: ['pill-run', '執行中'],
    skipped: ['pill-off', '略過'],
  };

  /* ===== FU-D46: per-job landing pages + completion detail modal ============== */

  /* VERIFIED landing targets only (each page/anchor exists in web/): quotes -> the
     dashboard (latest quotes surface); history/backup -> 資料中心; dividend sweeps ->
     待確認匯入; the external-snapshot ingests -> 資料來源 health tab; alert scan -> 預警
     規則 tab; AI loops + insight tasks -> insights.html; news -> news.html; digests ->
     their index cards (anchored panel ids). snapshot_monthly / signal_scan have no
     natural user-facing page today -> deliberately absent (no 前往 button). */
  const JOB_HREF = {
    quotes_tw: 'index.html',
    quotes_us: 'index.html',
    quotes_my: 'index.html',
    history_daily: 'data-center.html',
    backup_daily: 'data-center.html',
    dividends_daily: 'dividend-inbox.html',
    dividend_inbox_scan: 'dividend-inbox.html',
    finmind_chips_daily: 'settings.html#datasources',
    finmind_valuation_daily: 'settings.html#datasources',
    finmind_fundamentals_monthly: 'settings.html#datasources',
    sentiment_daily: 'settings.html#datasources',
    index_quotes_daily: 'settings.html#datasources',
    consensus_daily: 'settings.html#datasources',
    alert_scan: 'settings.html#alerts',
    evaluate_insights: 'insights.html',
    generate_calibrations: 'insights.html',
    news_daily: 'news.html',
    digest_daily: 'index.html#digest-daily-panel',
    digest_weekly: 'index.html#digest-weekly-panel',
  };
  function jobHref(id) {
    if (JOB_HREF[id]) return JOB_HREF[id];
    if (id.indexOf('insight:') === 0) return 'insights.html'; // AI 洞察任務卡片頁
    return null;
  }
  /* Same-page settings targets switch the tab directly (a bare hash assignment would not
     re-fire on an identical hash); everything else is a normal navigation. */
  function navigateTo(href) {
    if (href.indexOf('settings.html#') === 0 && window.showSettingsTab) {
      window.showSettingsTab(href.split('#')[1]);
      return;
    }
    window.location.href = href;
  }

  /* One modal at a time; closes on ✕ / backdrop / Esc. The Esc keydown listener is
     bound on open and removed inside dismiss, so repeated open/close cycles never
     accumulate document-level listeners (senior-review invariant). */
  let activeModal = null; // () => void — the current modal's dismiss, else null

  function openRunModal(jobId, lr) {
    if (!lr) return;
    if (activeModal) activeModal(); // never stack
    const backdrop = el('div', 'modal-backdrop');
    const modal = el('div', 'modal');
    const head = el('div', 'modal-head');
    head.appendChild(el('h3', 'modal-title', jobLabel(jobId, '') + ' — 執行結果'));
    const close = el('button', 'modal-close', '✕');
    close.type = 'button';
    head.appendChild(close);
    modal.appendChild(head);

    const body = el('div', 'modal-body');
    /* normalize BOTH last-run shapes: /status last_run {started_at,finished_at,status,
       message,duration_seconds,cost} and the jobs-GET seed {status,at,detail,duration_s}. */
    const status = lr.status;
    const ok = lr.ok === true || status === 'ok';
    let cls = 'pill-fail', label = '失敗';
    if (status === 'skipped') { cls = 'pill-off'; label = '略過'; }
    else if (ok) { cls = 'pill-ok'; label = '成功'; }
    const metaRow = el('div');
    metaRow.style.display = 'flex';
    metaRow.style.flexWrap = 'wrap';
    metaRow.style.gap = '8px 16px';
    metaRow.style.alignItems = 'center';
    metaRow.appendChild(_pill(cls, label));
    const started = lr.started_at || lr.at || null;
    const finished = lr.finished_at || null;
    const dur = lr.duration_seconds != null ? lr.duration_seconds
      : (lr.duration_s != null ? lr.duration_s : null);
    const meta = (labelTxt, value) => {
      const wrapEl = el('span', 'hint');
      wrapEl.appendChild(el('span', null, labelTxt + '：'));
      const v = el('span', 'num', value == null ? f.NULL_GLYPH : value);
      if (value == null) v.classList.add('sign-nil');
      wrapEl.appendChild(v);
      return wrapEl;
    };
    metaRow.appendChild(meta('開始', started ? f.datetime(started) : null));
    metaRow.appendChild(meta('結束', finished ? f.datetime(finished) : null));
    /* duration is a count (seconds), not money — f.num at 1 dp. */
    metaRow.appendChild(meta('耗時', dur == null ? null : f.num(dur, 1) + 's'));
    body.appendChild(metaRow);

    /* full run detail — scrollable pre-wrap so a long source/target breakdown stays
       readable inside the modal (never widens the page). */
    const detail = el('div', null, (lr.message || lr.detail || '') || '（無詳細資訊）');
    detail.style.whiteSpace = 'pre-wrap';
    detail.style.wordBreak = 'break-word';
    detail.style.maxHeight = '40vh';
    detail.style.overflowY = 'auto';
    detail.style.fontSize = '12px';
    detail.style.lineHeight = '1.6';
    detail.style.border = '1px solid var(--line, rgba(128,128,128,0.25))';
    detail.style.borderRadius = '6px';
    detail.style.padding = '8px 10px';
    if (!(lr.message || lr.detail)) detail.classList.add('sign-nil');
    body.appendChild(detail);

    /* LLM Token/費用 line — only when the status served a cost block. cost_usd is a
       Decimal STRING ("0.012"): nil-check == null, render via f.num (Finding 8). */
    const cost = lr.cost || null;
    if (cost && cost.cost_usd != null) {
      let txt;
      if (cost.tokens_in != null) {
        txt = 'Token／費用：' + f.num(cost.tokens_in) + ' in ／ ' + f.num(cost.tokens_out)
          + ' out ・ $' + f.num(cost.cost_usd, 3)
          + (cost.calls != null ? '（' + f.num(cost.calls) + ' 次呼叫）' : '');
      } else {
        txt = '費用：$' + f.num(cost.cost_usd, 3);
      }
      body.appendChild(el('div', 'hint', txt));
    }
    modal.appendChild(body);

    const foot = el('div', 'modal-foot');
    const href = jobHref(jobId);
    function dismiss() {
      document.removeEventListener('keydown', onKey);
      backdrop.remove();
      activeModal = null;
    }
    function onKey(e) { if (e.key === 'Escape') dismiss(); }
    if (href) {
      const go = el('button', 'btn btn-primary', '前往查看');
      go.type = 'button';
      go.title = href;
      go.addEventListener('click', () => { dismiss(); navigateTo(href); });
      foot.appendChild(go);
    }
    const closeBtn = el('button', 'btn', '關閉');
    closeBtn.type = 'button';
    closeBtn.addEventListener('click', dismiss);
    foot.appendChild(closeBtn);
    modal.appendChild(foot);

    backdrop.appendChild(modal);
    close.addEventListener('click', dismiss);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) dismiss(); });
    document.addEventListener('keydown', onKey);
    activeModal = dismiss;
    document.body.appendChild(backdrop);
  }

  /* Build the 狀態 chip (+ short last-result message) for a job. `st` = the /status entry
     ({running,queued,progress,last_run}) when known, else null; `fallbackLast` = the
     jobs-GET `last` used before the first status poll. Both last-run shapes are
     normalized. FU-D46: 執行中 rows append the live progress sub-text; terminal chips
     become clickable -> the detail modal. */
  function statusChip(jobId, st, fallbackLast) {
    const wrap = el('div', 'run-status');
    if (st && st.running) {
      wrap.appendChild(_pill('pill-run', '執行中'));
      if (st.progress) {
        const p = el('div', 'run-msg run-progress', st.progress);
        p.title = st.progress;
        wrap.appendChild(p);
      }
      return wrap;
    }
    if (st && st.queued) {
      wrap.appendChild(_pill('pill-queued', '已排入'));
      return wrap;
    }
    const lr = (st && st.last_run) || fallbackLast || null;
    if (!lr) { wrap.appendChild(el('span', 'sign-nil', f.NULL_GLYPH)); return wrap; }
    // /status last_run: {ok,status,message,...}; jobs-GET last: {status:'ok'|'error',detail}.
    const status = lr.status;
    // A seed from jobs-GET `last` can carry an in-flight 'running' status (a run was live
    // at page load, before the first poll overlays live state) — render it honestly.
    if (status === 'running') { wrap.appendChild(_pill('pill-run', '執行中')); return wrap; }
    const ok = lr.ok === true || status === 'ok';
    const msg = lr.message || lr.detail || '';
    let cls = 'pill-fail', label = '失敗';
    if (status === 'skipped') { cls = 'pill-off'; label = '略過'; }
    else if (ok) { cls = 'pill-ok'; label = '成功'; }
    const pill = _pill(cls, label);
    pill.title = msg ? msg + '（點擊查看詳情）' : '點擊查看詳情';
    wrap.appendChild(pill);
    if (msg) { const m = el('div', 'run-msg', msg); m.title = msg; wrap.appendChild(m); }
    /* FU-D46: the whole terminal chip (pill + message) opens the detail modal. The
       handler lives on the wrap, which is replaced wholesale on every repaint —
       no accumulating listeners. Click or Enter (keyboard reachable). */
    wrap.style.cursor = 'pointer';
    wrap.setAttribute('role', 'button');
    wrap.setAttribute('tabindex', '0');
    wrap.addEventListener('click', () => openRunModal(jobId, lr));
    wrap.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openRunModal(jobId, lr); }
    });
    return wrap;
  }
  function _pill(cls, label) {
    const pill = el('span', 'pill ' + cls);
    pill.appendChild(el('span', 'dot'));
    pill.appendChild(document.createTextNode(label));
    return pill;
  }

  /* Repaint one row's status slot + toggle its run button (disabled while queued/running,
     so a re-click can't race a 409). Safe if the row is not currently mounted. */
  function paintStatus(jobId) {
    const ref = rowRefs.get(jobId);
    if (!ref) return;
    const st = statusByJob[jobId] || null;
    ref.statusSlot.replaceChildren(statusChip(jobId, st, ref.lastSeed));
    if (ref.runBtn) ref.runBtn.disabled = !!(st && (st.running || st.queued));
  }

  function applyStatus(map) {
    statusByJob = map || {};
    rowRefs.forEach((_ref, jobId) => paintStatus(jobId));
  }

  /* One status fetch. Returns whether ANY job is active; degrades quietly on error (the
     grace timeout winds polling down rather than looping on a dead endpoint). */
  async function refreshStatus() {
    try {
      const resp = await api.get('/api/scheduler/status');
      applyStatus((resp && resp.jobs) || {});
      return !!(resp && resp.active);
    } catch (_e) {
      return false;
    }
  }

  function startPolling() {
    graceUntil = Date.now() + GRACE_MS;
    if (!pollActive) { pollActive = true; pollTick(); }  // idempotent — a 2nd call just bumps grace
  }

  function stopPolling() {
    pollActive = false;
    if (pollTimer != null) { clearTimeout(pollTimer); pollTimer = null; }
    // Idle by definition when we stop → never leave a run button stuck disabled.
    rowRefs.forEach((ref) => { if (ref.runBtn) ref.runBtn.disabled = false; });
  }

  async function pollTick() {
    if (!pollActive) return;
    const active = await refreshStatus();
    if (!pollActive) return;                 // stopped while awaiting (e.g. teardown)
    if (active || Date.now() < graceUntil) {
      pollTimer = setTimeout(pollTick, POLL_MS);
    } else {
      stopPolling();
      // Run(s) finished — refresh the jobs table (上次執行 time / next-fire) + run history.
      refreshJobs();
      refreshRuns();
    }
  }

  /* ---- Section A jobs ---- */
  function renderJobs() {
    const tbody = $('#jobs-body');
    if (!tbody) return;
    tbody.replaceChildren();
    rowRefs.clear();  // rows are rebuilt below; stale slot/button refs must not linger
    jobs.forEach((j) => {
      const tr = el('tr');
      const tdJob = el('td', 'col-text');
      tdJob.appendChild(el('div', null, jobLabel(j.id, j.desc)));
      tdJob.appendChild(el('div', 'sym-name cron-code', j.id));
      tr.appendChild(tdJob);

      /* enable toggle -> PUT /api/scheduler/jobs/{id} {enabled} */
      const tdTog = el('td');
      const t = el('button', 'toggle' + (j.enabled ? ' on' : ''));
      t.type = 'button';
      t.setAttribute('role', 'switch');
      t.addEventListener('click', async () => {
        const next = !t.classList.contains('on');
        t.disabled = true;
        try {
          await api.put('/api/scheduler/jobs/' + encodeURIComponent(j.id), { enabled: next });
          _toast('已更新', 'ok', j.id + (next ? ' 已啟用' : ' 已停用'));
          dispatchJobsChanged();
        } catch (err) {
          _toast((err && err.message) || '更新失敗', 'fail', err && err.code);
          t.disabled = false;
        }
      });
      tdTog.appendChild(t);
      tr.appendChild(tdTog);

      /* cron editor: an input bound to the raw cron expression; persist on change. */
      const tdCron = el('td', 'col-text');
      const cronWrap = el('div', 'cron-friendly');
      const cronInput = el('input', 'input');
      cronInput.value = j.cron || '';
      cronInput.style.width = '160px';
      cronInput.style.fontFamily = 'var(--font-num)';
      cronInput.title = 'cron 語法（分 時 日 月 週）';
      cronInput.addEventListener('change', async () => {
        const cron = cronInput.value.trim();
        if (!cron) { cronInput.classList.add('field-error'); return; }
        cronInput.classList.remove('field-error');
        try {
          await api.put('/api/scheduler/jobs/' + encodeURIComponent(j.id), { cron: cron });
          _toast('排程已更新', 'ok', j.id + ' · ' + cron);
          dispatchJobsChanged();
        } catch (err) {
          cronInput.classList.add('field-error');
          _toast((err && err.message) || 'cron 更新失敗', 'fail', err && err.code);
        }
      });
      cronWrap.appendChild(cronInput);
      tdCron.appendChild(cronWrap);
      tr.appendChild(tdCron);

      /* tz */
      tr.appendChild(el('td', 'col-text num', j.tz || ''));

      /* 狀態 (FU-D36): live run-status chip; seeded from j.last, advanced by polling. */
      const tdStatus = el('td', 'col-text');
      const statusSlot = el('div');
      tdStatus.appendChild(statusSlot);
      tr.appendChild(tdStatus);

      /* last run: null when never run -> em-dash. */
      const tdLast = el('td', 'col-text');
      if (!j.last) {
        const sp = el('span', 'sign-nil', f.NULL_GLYPH);
        tdLast.appendChild(sp);
      } else {
        const lastWrap = el('span', 'last-run');
        const dot = el('span', 'run-dot ' + (j.last.status === 'ok' ? 'dot-ok' : 'dot-err'));
        lastWrap.appendChild(dot);
        lastWrap.appendChild(el('span', 'num', f.datetime(j.last.at)));
        if (j.last.detail) lastWrap.title = j.last.detail;
        tdLast.appendChild(lastWrap);
        if (j.last.status === 'error' && j.last.detail) {
          tdLast.appendChild(el('div', 'err-inline', j.last.detail));
        }
      }
      tr.appendChild(tdLast);

      /* next fire: null when scheduler off / disabled -> em-dash. */
      const tdNext = el('td', 'num');
      tdNext.textContent = j.next ? f.datetime(j.next) : f.NULL_GLYPH;
      if (!j.next) tdNext.classList.add('sign-nil');
      tr.appendChild(tdNext);

      /* manual run -> POST /api/scheduler/jobs/{id}/run (202 + run_id, 409 already-running).
         The row's 狀態 chip carries the outcome; the button just enqueues + starts polling. */
      const tdRun = el('td');
      const runBtn = el('button', 'btn', '立即執行');
      runBtn.type = 'button';
      runBtn.addEventListener('click', async () => {
        runBtn.disabled = true;
        statusByJob[j.id] = { running: false, queued: true, last_run: null };  // optimistic 已排入
        paintStatus(j.id);
        try {
          const resp = await api.post('/api/scheduler/jobs/' + encodeURIComponent(j.id) + '/run');
          _toast('已排入執行', 'ok', j.id + ' #' + ((resp && resp.run_id) || '?'));
          startPolling();  // advance 執行中 -> 成功/失敗 live, then stop when idle
        } catch (err) {
          _toast((err && err.message) || '執行失敗', 'fail', err && err.code);
          if (err && err.code === 'already_running') {
            startPolling();  // a run is genuinely in flight (prior trigger / cron) — track it
          } else {
            delete statusByJob[j.id];  // enqueue failed; revert to last known status
            paintStatus(j.id);
            runBtn.disabled = false;
          }
        }
      });
      tdRun.appendChild(runBtn);
      tr.appendChild(tdRun);

      /* register the row so polling can repaint it in place; seed the chip from j.last. */
      rowRefs.set(j.id, { statusSlot: statusSlot, runBtn: runBtn, lastSeed: j.last || null });
      paintStatus(j.id);
      tbody.appendChild(tr);
    });
  }

  /* ---- Section B run history (server-filtered + paged, WPD) ---- */
  function renderHistory() {
    const tbody = $('#hist-body');
    if (!tbody) return;
    tbody.replaceChildren();
    runs
      .forEach((h) => {
        const tr = el('tr');
        tr.appendChild(el('td', 'num', f.datetime(h.started_at)));
        const tdJob = el('td', 'col-text');
        tdJob.appendChild(el('div', null, jobLabel(h.job_id, '')));
        tdJob.appendChild(el('div', 'sym-name cron-code', h.job_id));
        /* WPB cross-link: an LLM-kind run (insight:* / news_daily) deep-links the
           Request 明細 pre-filtered to this run's started_at→finished_at window. */
        if ((h.job_id.indexOf('insight:') === 0 || h.job_id === 'news_daily') && h.started_at) {
          const qs = ['req_since=' + encodeURIComponent(h.started_at)];
          if (h.finished_at) qs.push('req_until=' + encodeURIComponent(h.finished_at));
          const link = el('a', 'runs-ai-link', '查看 AI 請求 ↗');
          link.href = 'settings.html?' + qs.join('&') + '#llm';
          link.title = '在 Request 明細以此執行的時間窗篩選 AI 請求';
          const wrap = el('div');
          wrap.appendChild(link);
          tdJob.appendChild(wrap);
        }
        tr.appendChild(tdJob);
        const tdSt = el('td');
        /* Map every terminal/in-flight status honestly (a 'running'/'skipped' row must not
           read as 失敗): ok→成功, running→執行中, skipped→略過, else (error/…)→失敗. */
        const st = HIST_STATUS[h.status] || ['pill-fail', '失敗'];
        const pill = el('span', 'pill ' + st[0]);
        pill.appendChild(el('span', 'dot'));
        pill.appendChild(document.createTextNode(st[1]));
        tdSt.appendChild(pill);
        tr.appendChild(tdSt);
        const tdDetail = el('td', 'log-msg', h.detail || '');
        tdDetail.title = h.detail || '';  // full source/target breakdown on hover
        tr.appendChild(tdDetail);
        /* duration_s is a count (seconds) -> num, not money. */
        const tdDur = el('td', 'num');
        tdDur.textContent = h.duration_s == null ? f.NULL_GLYPH : f.num(h.duration_s, 1) + 's';
        tr.appendChild(tdDur);
        /* Finding 8: cost_usd is a Decimal STRING and often null; "0"/"0.00" are TRUTHY.
           Nil-check with == null, then display via f.num(..,3) (NEVER bareString.toFixed). */
        const tdCost = el('td', 'num');
        if (h.cost_usd == null) { tdCost.textContent = f.NULL_GLYPH; tdCost.classList.add('sign-nil'); }
        else tdCost.textContent = '$' + f.num(h.cost_usd, 3);
        tr.appendChild(tdCost);
        tbody.appendChild(tr);
      });
  }
  function initHistFilter() {
    const bar = $('#hist-filter');
    if (!bar) return;
    bar.replaceChildren();
    const mk = (val, label) => {
      const c = el('button', 'chip' + (val === runState.job ? ' active' : ''), label);
      c.type = 'button';
      c.addEventListener('click', () => {
        runState.job = val;
        runState.offset = 0;
        bar.querySelectorAll('.chip').forEach((x) => x.classList.remove('active'));
        c.classList.add('active');
        refreshRuns(); // server-side filter (WPD) — not a client slice
      });
      return c;
    };
    bar.appendChild(el('span', 'group-label', '工作'));
    bar.appendChild(mk('all', '全部'));
    jobs.forEach((j) => bar.appendChild(mk(j.id, jobLabel(j.id, j.desc))));
  }

  async function refreshRuns() {
    const params = { limit: runState.limit, offset: runState.offset };
    if (runState.job !== 'all') params.job_id = runState.job;
    try {
      const resp = await api.get('/api/scheduler/runs', params);
      runs = (resp && resp.rows) || [];
      if (runsPager) {
        runsPager.update({
          offset: runState.offset,
          totalCount: (resp && resp.total_count) || 0,
        });
      }
    } catch (err) {
      _toast('執行紀錄載入失敗', 'fail', (err && err.message) || undefined);
      runs = [];
      if (runsPager) runsPager.update({});
    }
    renderHistory();
  }

  /* ---- Section C 系統操作記錄 (item 8) ---- */
  function renderSyslog() {
    const tbody = $('#syslog-body');
    if (!tbody) return;
    tbody.replaceChildren();
    sysRows.forEach((r) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'num', f.datetime(r.ts)));
      tr.appendChild(el('td', 'col-text', r.action));
      const tdUser = el('td', 'col-text');
      if (r.username) tdUser.textContent = r.username;
      else { tdUser.textContent = '訪客'; tdUser.classList.add('sign-nil'); }
      tr.appendChild(tdUser);
      const tdPath = el('td', 'col-text');
      tdPath.appendChild(el('span', 'cron-code', r.method + ' ' + r.path));
      tr.appendChild(tdPath);
      const tdSt = el('td');
      const ok = r.status < 400;
      const pill = el('span', 'pill ' + (ok ? 'pill-ok' : 'pill-fail'));
      pill.appendChild(el('span', 'dot'));
      pill.appendChild(document.createTextNode(String(r.status)));
      pill.title = ok ? '成功' : '被拒絕／失敗（未寫入或需修正）';
      tdSt.appendChild(pill);
      tr.appendChild(tdSt);
      const tdDur = el('td', 'num');
      tdDur.textContent = r.duration_ms == null ? f.NULL_GLYPH : f.num(r.duration_ms) + 'ms';
      tr.appendChild(tdDur);
      tbody.appendChild(tr);
    });
  }
  async function refreshSyslog() {
    if (!$('#syslog-body')) return; /* surface without the panel — skip */
    try {
      const resp = await api.get('/api/system-log',
        { limit: sysState.limit, offset: sysState.offset });
      sysRows = (resp && resp.rows) || [];
      if (sysPager) {
        sysPager.update({
          offset: sysState.offset,
          totalCount: (resp && resp.total_count) || 0,
        });
      }
    } catch (err) {
      sysRows = [];
      if (sysPager) sysPager.update({});
      _toast('系統操作記錄載入失敗', 'fail', (err && err.message) || undefined);
    }
    renderSyslog();
  }

  /* pagers (shared pdPager; hosts may be absent on a surface — guarded) */
  if (window.pdPager) {
    if (document.getElementById('hist-pager')) {
      runsPager = window.pdPager.create({
        host: document.getElementById('hist-pager'),
        limit: runState.limit, offset: 0, totalCount: 0,
        onPage: (offset) => { runState.offset = offset; refreshRuns(); },
      });
    }
    if (document.getElementById('syslog-pager')) {
      sysPager = window.pdPager.create({
        host: document.getElementById('syslog-pager'),
        limit: sysState.limit, offset: 0, totalCount: 0,
        onPage: (offset) => { sysState.offset = offset; refreshSyslog(); },
      });
    }
  }

  /* ===== boot: GET jobs + the first runs page in PARALLEL, then render. Graceful: on
     failure leave the page empty + surface ONE toast (never an unhandled rejection —
     the e2e smoke asserts ZERO console errors). 401 is handled inside api.js. ===== */
  async function boot() {
    const jobsResp = await api.get('/api/scheduler/jobs').catch((err) => {
      _toast('排程載入失敗', 'fail', (err && err.message) || undefined);
      return null;
    });
    jobs = (jobsResp && jobsResp.jobs) || [];
    renderJobs();
    initHistFilter();
    await refreshRuns();
    refreshSyslog();  // section C, independent fetch (graceful on failure)
    // FU-D36: seed live status once (catches a cron / prior-trigger run already in flight);
    // start polling ONLY if something is active, so an idle page polls zero times.
    if (await refreshStatus()) startPolling();
  }

  /* Lightweight refresh of JUST the jobs table (cron / enabled / next-fire) — run when a
     schedule row changes anywhere (pd-jobs-changed, incl. an edit on the digest card) or
     when the 排程中心 tab is (re)activated. Re-fetch + re-render only; never re-PUT, so a
     self-echo from this script's own dispatch is an idempotent no-op (no loop). */
  async function refreshJobs() {
    const jobsResp = await api.get('/api/scheduler/jobs').catch(() => null);
    if (jobsResp && jobsResp.jobs) {
      jobs = jobsResp.jobs;
      renderJobs();
      initHistFilter();  // job list unchanged in practice; preserves the active filter chip
      // Overlay live status onto the freshly-rendered rows; resume polling if a run is
      // active (e.g. switching to this tab mid-run). On the poll stop-path this returns
      // not-active, so it never re-arms the loop.
      if (await refreshStatus() && !pollActive) startPolling();
    }
  }

  document.addEventListener('pd-jobs-changed', refreshJobs);
  window.addEventListener('pd-settings-tab', (e) => {
    if (e && e.detail === 'scheduler') refreshJobs();
  });

  boot();
})();
