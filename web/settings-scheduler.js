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
   - POST /api/scheduler/jobs/{id}/run   (manual run; 202 + run_id, 409 already-running) */
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

  /* Structural data from the two GETs. Starts empty so a pre-fetch render is blank. */
  let jobs = [];
  let runs = [];
  let jobFilter = 'all';

  /* ---- Section A jobs ---- */
  function renderJobs() {
    const tbody = $('#jobs-body');
    if (!tbody) return;
    tbody.replaceChildren();
    jobs.forEach((j) => {
      const tr = el('tr');
      const tdJob = el('td', 'col-text');
      tdJob.appendChild(el('div', 'cron-code', j.id));
      tdJob.appendChild(el('div', 'sym-name', j.desc || ''));
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
          await boot();
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
          await boot();
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

      /* manual run -> POST /api/scheduler/jobs/{id}/run */
      const tdRun = el('td');
      const runBtn = el('button', 'btn', '立即執行');
      runBtn.type = 'button';
      const resultSlot = el('div', 'run-result');
      runBtn.addEventListener('click', async () => {
        runBtn.disabled = true;
        runBtn.textContent = '執行中…';
        resultSlot.replaceChildren();
        try {
          const resp = await api.post('/api/scheduler/jobs/' + encodeURIComponent(j.id) + '/run');
          const chip = el('span', 'pill pill-ok');
          chip.appendChild(el('span', 'dot'));
          chip.appendChild(document.createTextNode('已排入執行 #' + ((resp && resp.run_id) || '?')));
          resultSlot.appendChild(chip);
          _toast('已開始執行', 'ok', j.id);
          /* refresh the run log so the new run appears once it lands. */
          await refreshRuns();
        } catch (err) {
          const chip = el('span', 'pill pill-fail');
          chip.appendChild(el('span', 'dot'));
          chip.appendChild(document.createTextNode((err && err.message) || '執行失敗'));
          resultSlot.appendChild(chip);
          _toast((err && err.message) || '執行失敗', 'fail', err && err.code);
        } finally {
          runBtn.disabled = false;
          runBtn.textContent = '立即執行';
        }
      });
      tdRun.appendChild(runBtn);
      tdRun.appendChild(resultSlot);
      tr.appendChild(tdRun);
      tbody.appendChild(tr);
    });
  }

  /* ---- Section B run history ---- */
  function renderHistory() {
    const tbody = $('#hist-body');
    if (!tbody) return;
    tbody.replaceChildren();
    runs
      .filter((h) => jobFilter === 'all' || h.job_id === jobFilter)
      .forEach((h) => {
        const tr = el('tr');
        tr.appendChild(el('td', 'num', f.datetime(h.started_at)));
        const tdJob = el('td', 'col-text');
        tdJob.appendChild(el('span', 'cron-code', h.job_id));
        tr.appendChild(tdJob);
        const tdSt = el('td');
        const pill = el('span', 'pill ' + (h.status === 'ok' ? 'pill-ok' : 'pill-fail'));
        pill.appendChild(el('span', 'dot'));
        pill.appendChild(document.createTextNode(h.status === 'ok' ? '成功' : '失敗'));
        tdSt.appendChild(pill);
        tr.appendChild(tdSt);
        tr.appendChild(el('td', 'log-msg', h.detail || ''));
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
      const c = el('button', 'chip' + (val === jobFilter ? ' active' : ''), label);
      c.type = 'button';
      c.addEventListener('click', () => {
        jobFilter = val;
        bar.querySelectorAll('.chip').forEach((x) => x.classList.remove('active'));
        c.classList.add('active');
        renderHistory();
      });
      return c;
    };
    bar.appendChild(el('span', 'group-label', '工作'));
    bar.appendChild(mk('all', '全部'));
    jobs.forEach((j) => bar.appendChild(mk(j.id, j.id)));
  }

  async function refreshRuns() {
    try {
      const resp = await api.get('/api/scheduler/runs', { limit: 200 });
      runs = (resp && resp.rows) || [];
    } catch (err) {
      _toast('執行紀錄載入失敗', 'fail', (err && err.message) || undefined);
      runs = [];
    }
    renderHistory();
  }

  /* ===== boot: GET jobs + runs in PARALLEL, then render. Graceful: on failure leave the
     page empty + surface ONE toast (never an unhandled rejection — the e2e smoke asserts
     ZERO console errors). 401 is handled inside api.js. ===== */
  async function boot() {
    const [jobsResp, runsResp] = await Promise.all([
      api.get('/api/scheduler/jobs').catch((err) => {
        _toast('排程載入失敗', 'fail', (err && err.message) || undefined);
        return null;
      }),
      api.get('/api/scheduler/runs', { limit: 200 }).catch((err) => {
        _toast('執行紀錄載入失敗', 'fail', (err && err.message) || undefined);
        return null;
      }),
    ]);
    jobs = (jobsResp && jobsResp.jobs) || [];
    runs = (runsResp && runsResp.rows) || [];
    renderJobs();
    initHistFilter();
    renderHistory();
  }

  boot();
})();
