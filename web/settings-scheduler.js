/* portfolio-dash — 設定 · 排程 (mock + rendering) */
window.SCHED_DATA = {
  "jobs": [
    { "id": "quotes_tw", "desc": "台股收盤報價＋匯率", "cron": "0 14 * * mon-fri", "human": "週一至五 14:00",
      "tz": "Asia/Taipei", "enabled": true, "last": { "status": "ok", "rel": "2 小時前", "detail": "2026-06-11 14:00:04・成功 3 檔・耗時 3.8s" }, "next": "2026-06-12 14:00" },
    { "id": "quotes_us", "desc": "美股收盤報價", "cron": "30 16 * * mon-fri", "human": "週一至五 16:30",
      "tz": "America/New_York", "enabled": true, "last": { "status": "ok", "rel": "10 小時前", "detail": "2026-06-11 04:30:02（台北）・成功 3 檔・耗時 2.2s" }, "next": "2026-06-12 04:30（台北）" },
    { "id": "quotes_my", "desc": "馬股收盤報價", "cron": "30 17 * * mon-fri", "human": "週一至五 17:30",
      "tz": "Asia/Kuala_Lumpur", "enabled": true, "last": { "status": "error", "rel": "21 小時前", "detail": "2026-06-10 17:30:06・HTTP 502 from provider" }, "next": "2026-06-11 17:30" },
    { "id": "history_daily", "desc": "歷史回補", "cron": "0 2 * * *", "human": "每日 02:00",
      "tz": "Asia/Taipei", "enabled": true, "last": { "status": "ok", "rel": "12 小時前", "detail": "2026-06-11 02:00:11・回補 7 檔×30 日・耗時 41s" }, "next": "2026-06-12 02:00" },
    { "id": "dividends_daily", "desc": "除息資料", "cron": "0 3 * * *", "human": "每日 03:00",
      "tz": "Asia/Taipei", "enabled": true, "last": { "status": "ok", "rel": "11 小時前", "detail": "2026-06-11 03:00:05・更新 3 筆除息事件・耗時 6s" }, "next": "2026-06-12 03:00" },
    { "id": "insights", "desc": "AI 洞察批次", "cron": "0 8 * * *", "human": "每日 08:00",
      "tz": "Asia/Taipei", "enabled": true, "last": { "status": "ok", "rel": "6 小時前", "detail": "2026-06-11 08:00:12・2 則洞察・耗時 13.9s・費用 $0.081" }, "next": "2026-06-12 08:00" }
  ],
  "history": [
    { "at": "2026-06-11 14:00:04", "job": "quotes_tw", "status": "ok", "detail": "成功 3 檔（2330・0056・00919）", "dur": "3.8s", "cost_usd": null },
    { "at": "2026-06-11 04:30:02", "job": "quotes_us", "status": "ok", "detail": "成功 3 檔（AAPL・MSFT・NVDA）", "dur": "2.2s", "cost_usd": null },
    { "at": "2026-06-11 03:00:05", "job": "dividends_daily", "status": "ok", "detail": "更新 3 筆除息事件", "dur": "6.0s", "cost_usd": null },
    { "at": "2026-06-11 02:00:11", "job": "history_daily", "status": "ok", "detail": "回補 7 檔 × 30 日", "dur": "41.2s", "cost_usd": null },
    { "at": "2026-06-10 17:30:06", "job": "quotes_my", "status": "error", "detail": "HTTP 502 from provider — 1155.KL 未更新", "dur": "30.0s", "cost_usd": null },
    { "at": "2026-06-10 14:00:03", "job": "quotes_tw", "status": "ok", "detail": "成功 2 檔・失敗 1（00919: 來源無資料）", "dur": "4.1s", "cost_usd": null },
    { "at": "2026-06-10 04:30:01", "job": "quotes_us", "status": "ok", "detail": "成功 3 檔", "dur": "2.4s", "cost_usd": null },
    { "at": "2026-06-10 08:00:09", "job": "insights", "status": "ok", "detail": "2 策略模板產生 2 則洞察", "dur": "13.9s", "cost_usd": 0.081 }
  ]
};

(function () {
  'use strict';
  const D = window.SCHED_DATA;
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  /* ---- Section A jobs ---- */
  function renderJobs() {
    const tbody = $('#jobs-body');
    D.jobs.forEach((j) => {
      const tr = el('tr');
      const tdJob = el('td', 'col-text');
      tdJob.appendChild(el('div', 'cron-code', j.id));
      tdJob.appendChild(el('div', 'sym-name', j.desc));
      tr.appendChild(tdJob);

      const tdTog = el('td');
      const t = el('button', 'toggle' + (j.enabled ? ' on' : ''));
      t.type = 'button';
      t.setAttribute('role', 'switch');
      t.addEventListener('click', () => t.classList.toggle('on'));
      tdTog.appendChild(t);
      tr.appendChild(tdTog);

      const tdCron = el('td', 'col-text');
      /* friendly cron builder */
      const cronWrap = el('div', 'cron-friendly');
      const freqSel = el('select', 'select cron-sel');
      freqSel.title = '執行頻率';
      [['每日', 'daily'], ['每週', 'weekly'], ['工作日', 'weekdays'], ['每小時', 'hourly']].forEach(([lbl, val]) => {
        const o = el('option', null, lbl); o.value = val;
        if (j.human.includes('週一至五') || j.human.includes('mon-fri')) o.value = 'weekdays';
        freqSel.appendChild(o);
      });
      freqSel.value = j.human.includes('週一至五') || j.human.includes('mon-fri') ? 'weekdays' : 'daily';
      if (j.human.includes('小時')) freqSel.value = 'hourly';
      cronWrap.appendChild(freqSel);
      const timePart = j.cron.match(/^(\d+)\s+(\d+)/);
      let timeIn = null;
      if (timePart && freqSel.value !== 'hourly') {
        timeIn = el('input', 'input cron-time');
        timeIn.type = 'time';
        const hh = timePart[2].padStart(2, '0');
        const mm = timePart[1].padStart(2, '0');
        timeIn.value = hh + ':' + mm;
        timeIn.title = '執行時間';
        cronWrap.appendChild(timeIn);
      }
      const advLink = el('button', 'btn cron-adv', '進階…');
      advLink.type = 'button';
      advLink.title = '顯示 cron 語法：' + j.cron;
      let advMode = false;
      advLink.addEventListener('click', () => {
        advMode = !advMode;
        if (advMode) {
          const inp = el('input', 'input'); inp.value = j.cron;
          inp.style.width = '150px'; inp.style.fontFamily = 'var(--font-num)';
          inp.id = 'cron-inp-' + j.id;
          const backBtn = el('button', 'btn cron-adv', '← 簡易');
          backBtn.type = 'button';
          backBtn.title = '退回簡易模式';
          backBtn.addEventListener('click', () => { advMode = false; renderCronSimple(); });
          cronWrap.replaceChildren(inp, backBtn);
        } else {
          renderCronSimple();
        }
      });
      cronWrap.appendChild(advLink);
      function renderCronSimple() {
        cronWrap.replaceChildren();
        cronWrap.appendChild(freqSel);
        if (freqSel.value !== 'hourly') {
          if (!timeIn) { timeIn = el('input', 'input cron-time'); timeIn.type = 'time'; timeIn.value = '08:00'; }
          cronWrap.appendChild(timeIn);
        }
        cronWrap.appendChild(advLink);
        advLink.textContent = '進階…';
      }
      tdCron.appendChild(cronWrap);
      tdCron.appendChild(el('div', 'sym-name cron-human', j.human));
      tr.appendChild(tdCron);

      const tdTz = el('td', 'col-text num', j.tz);
      tr.appendChild(tdTz);

      const tdLast = el('td', 'col-text');
      const lastWrap = el('span', 'last-run');
      const dot = el('span', 'run-dot ' + (j.last.status === 'ok' ? 'dot-ok' : 'dot-err'));
      lastWrap.appendChild(dot);
      lastWrap.appendChild(el('span', 'num', j.last.rel));
      lastWrap.title = j.last.detail;
      tdLast.appendChild(lastWrap);
      if (j.last.status === 'error') {
        tdLast.appendChild(el('div', 'err-inline', j.last.detail.split('・').pop()));
      }
      tr.appendChild(tdLast);

      tr.appendChild(el('td', 'num', j.next));

      const tdRun = el('td');
      const runBtn = el('button', 'btn', '立即執行');
      runBtn.type = 'button';
      const resultSlot = el('div', 'run-result');
      runBtn.addEventListener('click', () => {
        runBtn.disabled = true;
        runBtn.textContent = '執行中…';
        resultSlot.replaceChildren();
        setTimeout(() => {
          runBtn.disabled = false;
          runBtn.textContent = '立即執行';
          const chip = el('span', 'pill ' + (j.id === 'quotes_tw' ? '' : 'pill-ok'));
          if (j.id === 'quotes_tw') {
            chip.classList.add('pill-warn');
            chip.appendChild(el('span', 'dot'));
            chip.appendChild(document.createTextNode('成功 7 檔・失敗 1（00919: 來源無資料）'));
          } else {
            chip.appendChild(el('span', 'dot'));
            chip.appendChild(document.createTextNode('成功・' + j.desc));
          }
          resultSlot.appendChild(chip);
        }, 900);
      });
      tdRun.appendChild(runBtn);
      tdRun.appendChild(resultSlot);
      tr.appendChild(tdRun);
      tbody.appendChild(tr);
    });
  }

  /* ---- Section B history ---- */
  let jobFilter = 'all';
  function renderHistory() {
    const tbody = $('#hist-body');
    tbody.replaceChildren();
    D.history
      .filter((h) => jobFilter === 'all' || h.job === jobFilter)
      .forEach((h) => {
        const tr = el('tr');
        tr.appendChild(el('td', 'num', h.at));
        const tdJob = el('td', 'col-text');
        tdJob.appendChild(el('span', 'cron-code', h.job));
        tr.appendChild(tdJob);
        const tdSt = el('td');
        const pill = el('span', 'pill ' + (h.status === 'ok' ? 'pill-ok' : 'pill-fail'));
        pill.appendChild(el('span', 'dot'));
        pill.appendChild(document.createTextNode(h.status === 'ok' ? '成功' : '失敗'));
        tdSt.appendChild(pill);
        tr.appendChild(tdSt);
        tr.appendChild(el('td', 'log-msg', h.detail));
        tr.appendChild(el('td', 'num', h.dur));
        const tdCost = el('td', 'num');
        if (!h.cost_usd) { tdCost.textContent = '—'; tdCost.classList.add('sign-nil'); }
        else { tdCost.textContent = '$' + h.cost_usd.toFixed(3); }
        tr.appendChild(tdCost);
        tbody.appendChild(tr);
      });
  }
  function initHistFilter() {
    const bar = $('#hist-filter');
    const mk = (val, label) => {
      const c = el('button', 'chip' + (val === 'all' ? ' active' : ''), label);
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
    D.jobs.forEach((j) => bar.appendChild(mk(j.id, j.id)));
  }

  renderJobs();
  initHistFilter();
  renderHistory();
})();
