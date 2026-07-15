/* portfolio-dash — 設定 · 摘要與週報 (P3 batch 3 · Wave 1). Wired to /api/scheduler/* +
   /api/digest/*.

   SINGLE SOURCE OF TRUTH: the enable toggle + send-time controls read/write the SAME
   schedule_config rows through the EXISTING GET/PUT /api/scheduler/jobs/{id} that the
   「排程」 tab uses — NO duplicate state. Friendly pickers compose the cron:
     daily  -> "M H * * mon-fri"     weekly -> "M H * * <dow>"
   A cron the user hand-edited into a shape these pickers cannot represent renders a
   read-only 「自訂 cron」 hint instead (edit it on the 排程 tab).

   The AI one-liner switch persists via PUT /api/digest/config (default off). All controls
   AUTO-SAVE on interaction (switch-shaped controls persist on click — LESSONS rule). Guest
   demo: /api/digest/* writes 403 (honest toast); scheduler edits stay owner-only via the
   app session gate. Every handler is try/caught so a failure never throws unhandled. */
(function () {
  'use strict';
  var api = window.pdApi;
  var $ = function (id) { return document.getElementById(id); };
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function toast(msg, kind, code) { if (window.toast) window.toast(msg, kind, code); }

  var DOW_NAMES = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];
  var DOW_ZH = ['週日', '週一', '週二', '週三', '週四', '週五', '週六'];
  var JOB = { daily: 'digest_daily', weekly: 'digest_weekly' };

  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function splitHhmm(v) {
    var mm = /^(\d{1,2}):(\d{2})$/.exec((v || '').trim());
    if (!mm) return null;
    var h = Number(mm[1]), m = Number(mm[2]);
    if (h < 0 || h > 23 || m < 0 || m > 59) return null;
    return { h: String(h), m: String(m) };
  }
  function dowIndex(tok) {
    if (/^\d$/.test(tok)) { var n = Number(tok); return n === 7 ? 0 : (n >= 0 && n <= 6 ? n : -1); }
    return DOW_NAMES.indexOf((tok || '').toLowerCase());
  }
  /* Parse "M H * * mon-fri" -> {hhmm} or null (not a simple daily cron). */
  function parseDaily(cron) {
    var p = (cron || '').trim().split(/\s+/);
    if (p.length !== 5 || p[2] !== '*' || p[3] !== '*' || p[4] !== 'mon-fri') return null;
    if (!/^\d{1,2}$/.test(p[0]) || !/^\d{1,2}$/.test(p[1])) return null;
    var m = Number(p[0]), h = Number(p[1]);
    if (m > 59 || h > 23) return null;
    return { hhmm: pad(h) + ':' + pad(m) };
  }
  /* Parse "M H * * <dow>" -> {hhmm,dow} or null (not a simple weekly cron). */
  function parseWeekly(cron) {
    var p = (cron || '').trim().split(/\s+/);
    if (p.length !== 5 || p[2] !== '*' || p[3] !== '*') return null;
    if (!/^\d{1,2}$/.test(p[0]) || !/^\d{1,2}$/.test(p[1])) return null;
    var di = dowIndex(p[4]);
    if (di < 0) return null;
    var m = Number(p[0]), h = Number(p[1]);
    if (m > 59 || h > 23) return null;
    return { hhmm: pad(h) + ':' + pad(m), dow: di };
  }

  var jobsById = {};
  var llmEnabled = false;

  async function putJob(jobId, body) {
    return api.put('/api/scheduler/jobs/' + encodeURIComponent(jobId), body);
  }

  /* Build one edition row (daily/weekly). */
  function editionRow(kind) {
    var jobId = JOB[kind];
    var job = jobsById[jobId] || {};
    var row = el('div', 'digest-cfg-row');

    var title = el('div', 'digest-cfg-title', kind === 'daily' ? '每日收盤摘要' : '每週行動清單');
    row.appendChild(title);

    /* enable toggle -> PUT {enabled} */
    var tog = el('button', 'toggle' + (job.enabled ? ' on' : ''));
    tog.type = 'button';
    tog.setAttribute('role', 'switch');
    tog.title = '啟用 / 停用';
    tog.addEventListener('click', function () {
      var next = !tog.classList.contains('on');
      tog.classList.toggle('on', next);  // optimistic
      putJob(jobId, { enabled: next }).then(function () {
        toast(next ? '已啟用' : '已停用', 'ok', jobId);
      }).catch(function (err) {
        tog.classList.toggle('on', !next);  // revert
        toast((err && err.message) || '更新失敗', 'fail', err && err.code);
      });
    });
    row.appendChild(tog);

    /* time picker OR read-only custom-cron hint */
    var timeWrap = el('div', 'digest-cfg-time');
    var parsed = kind === 'daily' ? parseDaily(job.cron) : parseWeekly(job.cron);
    if (!parsed) {
      timeWrap.appendChild(el('span', 'digest-cfg-custom',
        '自訂 cron（' + (job.cron || '—') + '）— 於「排程」分頁編輯'));
    } else {
      var timeInput = el('input', 'input digest-cfg-hhmm');
      timeInput.type = 'time';
      timeInput.value = parsed.hhmm;

      var dowSel = null;
      if (kind === 'weekly') {
        dowSel = el('select', 'select');
        DOW_ZH.forEach(function (label, i) {
          var opt = el('option', null, label);
          opt.value = String(i);
          if (i === parsed.dow) opt.selected = true;
          dowSel.appendChild(opt);
        });
        timeWrap.appendChild(dowSel);
      }
      timeWrap.appendChild(timeInput);

      var persist = function () {
        var t = splitHhmm(timeInput.value);
        if (!t) { timeInput.classList.add('field-error'); return; }
        timeInput.classList.remove('field-error');
        var cron = kind === 'daily'
          ? (t.m + ' ' + t.h + ' * * mon-fri')
          : (t.m + ' ' + t.h + ' * * ' + DOW_NAMES[Number(dowSel.value)]);
        putJob(jobId, { cron: cron }).then(function () {
          toast('發送時間已更新', 'ok', jobId + ' · ' + cron);
        }).catch(function (err) {
          timeInput.classList.add('field-error');
          toast((err && err.message) || '時間更新失敗', 'fail', err && err.code);
        });
      };
      timeInput.addEventListener('change', persist);
      if (dowSel) dowSel.addEventListener('change', persist);
    }
    row.appendChild(timeWrap);

    /* 立即產生 -> POST /api/digest/run */
    var runBtn = el('button', 'btn', '立即產生');
    runBtn.type = 'button';
    runBtn.addEventListener('click', function () {
      runBtn.disabled = true;
      runBtn.textContent = '產生中…';
      api.post('/api/digest/run', { kind: kind }).then(function (resp) {
        toast('已開始產生摘要', 'ok', jobId + ' #' + ((resp && resp.run_id) || '?'));
      }).catch(function (err) {
        if (err && err.status === 403) toast('示範站不開放摘要設定，請於正式站操作', 'fail', 'forbidden');
        else toast((err && err.message) || '產生失敗', 'fail', err && err.code);
      }).finally(function () {
        runBtn.disabled = false;
        runBtn.textContent = '立即產生';
      });
    });
    row.appendChild(runBtn);
    return row;
  }

  function llmRow() {
    var row = el('div', 'digest-cfg-row');
    row.appendChild(el('div', 'digest-cfg-title', 'AI 一句話總結'));
    var tog = el('button', 'toggle' + (llmEnabled ? ' on' : ''));
    tog.type = 'button';
    tog.setAttribute('role', 'switch');
    tog.title = '啟用 / 停用 AI 一句話';
    tog.addEventListener('click', function () {
      var next = !tog.classList.contains('on');
      tog.classList.toggle('on', next);
      api.put('/api/digest/config', { llm_summary_enabled: next }).then(function () {
        llmEnabled = next;
        toast(next ? '已啟用 AI 一句話' : '已停用 AI 一句話', 'ok');
      }).catch(function (err) {
        tog.classList.toggle('on', !next);
        if (err && err.status === 403) toast('示範站不開放摘要設定，請於正式站操作', 'fail', 'forbidden');
        else toast((err && err.message) || '儲存失敗', 'fail', err && err.code);
      });
    });
    row.appendChild(tog);
    row.appendChild(el('div', 'digest-cfg-hint',
      '開啟後，今日摘要會附一句只引用已算好數字的 AI 說明（需已啟用 AI 服務）。'));
    return row;
  }

  function render() {
    var host = $('digest-config-wrap');
    if (!host) return;
    host.replaceChildren();
    host.appendChild(editionRow('daily'));
    host.appendChild(editionRow('weekly'));
    host.appendChild(llmRow());
  }

  async function boot() {
    if (!$('digest-config-wrap') || !api) return;
    try {
      var jobsResp = await api.get('/api/scheduler/jobs');
      (jobsResp && jobsResp.jobs || []).forEach(function (j) { jobsById[j.id] = j; });
    } catch (err) {
      toast('排程載入失敗', 'fail', (err && err.message) || undefined);
    }
    try {
      var cfg = await api.get('/api/digest/config');
      llmEnabled = !!(cfg && cfg.llm_summary_enabled);
    } catch (err) { /* config GET is open; ignore on failure (default off) */ }
    render();
  }

  boot();
})();
