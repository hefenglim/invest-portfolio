/* portfolio-dash — 資料中心 · 資料庫統計 (read-only, wired to GET /api/db-stats).

   Owner decision (2026-07-07): observe per-table row counts + oldest record dates
   across BOTH SQLite files (portfolio.db + news.db) to judge future retention
   windows. Display only — no pruning exists anywhere. Counts are JSON numbers;
   sizes are bytes (number) formatted to MB here (presentation, not money).

   FU-D15 (2026-07-16): moved off the settings tab to its OWN 資料中心 page. Adds a
   概況 summary strip (total tables / total rows / DB sizes) and per-category 小計
   subtotal rows. Every figure is an aggregation of INTEGER wire counts computed on
   read — display-only totals of counts, never money. The endpoint is unchanged;
   totals are summed client-side from the same payload the table renders.
   Follows the settings-JS convention: missing node -> skip. */
(function () {
  'use strict';
  const f = window.fmt;
  const body = document.getElementById('dbstats-body');
  if (!body || !window.pdApi) return; /* panel absent on this surface — skip */
  const files = document.getElementById('dbstats-files');
  const note = document.getElementById('dbstats-note');
  const refreshBtn = document.getElementById('dbstats-refresh');
  const updated = document.getElementById('dbstats-updated');
  const summary = document.getElementById('dc-summary');

  function stampUpdated() {
    if (!updated) return;
    const d = new Date();
    const p = (n) => (n < 10 ? '0' : '') + n;
    updated.textContent = '更新於 ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  function mb(bytes) {
    if (bytes == null) return f.NULL_GLYPH;
    return f.num(bytes / 1048576, 2) + ' MB';
  }

  function fileRow(label, sizeText) {
    const row = el('div', 'gen-row');
    row.appendChild(el('span', 'k', label));
    row.appendChild(el('span', 'v num', sizeText));
    return row;
  }

  function tableRow(category, t, firstOfGroup) {
    const tr = el('tr');
    const tdCat = el('td', 'col-text');
    if (firstOfGroup) tdCat.appendChild(el('span', null, category));
    tr.appendChild(tdCat);
    const tdName = el('td', 'col-text');
    tdName.appendChild(el('div', null, t.label));
    if (t.label !== t.name) tdName.appendChild(el('div', 'sym-name cron-code', t.name));
    tr.appendChild(tdName);
    tr.appendChild(el('td', 'num', f.num(t.count)));
    const tdOld = el('td', 'num');
    if (t.oldest == null) { tdOld.textContent = f.NULL_GLYPH; tdOld.classList.add('sign-nil'); }
    else tdOld.textContent = t.oldest.slice(0, 10);
    tr.appendChild(tdOld);
    return tr;
  }

  function subtotalRow(tableCount, rowCount) {
    const tr = el('tr', 'dc-subtotal');
    tr.appendChild(el('td', 'col-text'));
    tr.appendChild(el('td', 'col-text', '小計（' + f.num(tableCount) + ' 表）'));
    tr.appendChild(el('td', 'num', f.num(rowCount)));
    tr.appendChild(el('td', 'num'));
    return tr;
  }

  /* Render every category group (portfolio + news) into one table, appending a 小計
     subtotal after each group. Returns {tables, rows} totals for the summary strip. */
  function renderGroups(groups) {
    let tables = 0;
    let rows = 0;
    groups.forEach(function (g) {
      const list = g.tables || [];
      let groupRows = 0;
      list.forEach(function (t, i) {
        body.appendChild(tableRow(g.category, t, i === 0));
        groupRows += Number(t.count) || 0;
      });
      body.appendChild(subtotalRow(list.length, groupRows));
      tables += list.length;
      rows += groupRows;
    });
    return { tables: tables, rows: rows };
  }

  function statCard(label, value, sub) {
    const c = el('div', 'dc-stat');
    c.appendChild(el('span', 'dc-stat-label', label));
    c.appendChild(el('span', 'dc-stat-value num', value));
    if (sub) c.appendChild(el('span', 'dc-stat-sub', sub));
    return c;
  }

  function renderSummary(totalTables, totalRows, p, n) {
    if (!summary) return;
    summary.replaceChildren();
    summary.appendChild(statCard('資料表總數', f.num(totalTables), '個資料表'));
    summary.appendChild(statCard('總筆數', f.num(totalRows), '筆記錄'));
    summary.appendChild(statCard('主資料庫大小', mb(p.size_bytes), p.file || 'portfolio.db'));
    summary.appendChild(statCard(
      '新聞庫大小',
      n.present ? mb(n.size_bytes) : '尚未建立',
      n.file || 'news.db'));
  }

  function render(resp) {
    body.replaceChildren();
    files.replaceChildren();
    const p = (resp && resp.portfolio) || { file: 'portfolio.db', size_bytes: null, groups: [] };
    const n = (resp && resp.news) || { file: 'news.db', present: false, size_bytes: null, groups: [] };
    files.appendChild(fileRow('主資料庫 ' + p.file, mb(p.size_bytes)));
    files.appendChild(fileRow(
      '新聞庫 ' + n.file,
      n.present ? mb(n.size_bytes) : '尚未建立'));
    const pTot = renderGroups(p.groups || []);
    const nTot = n.present ? renderGroups(n.groups || []) : { tables: 0, rows: 0 };
    renderSummary(pTot.tables + nTot.tables, pTot.rows + nTot.rows, p, n);
    if (note) note.textContent = '唯讀統計 — 供保留期限評估；目前不做任何自動清理。';
    stampUpdated();
  }

  function load() {
    if (refreshBtn) refreshBtn.disabled = true;
    if (note) note.textContent = '載入中…';
    window.pdApi.get('/api/db-stats').then(function (resp) {
      render(resp);
    }).catch(function (err) {
      if (note) note.textContent = '資料庫統計載入失敗' + ((err && err.message) ? '：' + err.message : '');
    }).then(function () {
      if (refreshBtn) refreshBtn.disabled = false;
    });
  }

  if (refreshBtn) refreshBtn.addEventListener('click', load);
  load();

  // --- AI extraction failure log (AI-D64) --------------------------------------
  // A bounded ring the LLM seam writes to on failure. The panel exists so the ring is
  // visible: rows roll off silently at capacity, so `oldest` is the cue to download
  // before the tail is lost.
  var flBody = document.getElementById('fl-body');
  var flNote = document.getElementById('fl-note');
  var flCap = document.getElementById('fl-cap');
  var flRefresh = document.getElementById('fl-refresh');
  var flDownload = document.getElementById('fl-download');
  var flClear = document.getElementById('fl-clear');

  function flRow(r) {
    var tr = document.createElement('tr');
    var a = document.createElement('td');
    a.className = 'col-text';
    a.textContent = r.agent;
    var n = document.createElement('td');
    n.className = 'num';
    n.textContent = String(r.n);
    var o = document.createElement('td');
    o.className = 'num';
    // An empty table shows an em-dash, never a 0 that reads as "measured zero".
    o.textContent = r.oldest ? String(r.oldest).slice(0, 19).replace('T', ' ') : '—';
    tr.appendChild(a); tr.appendChild(n); tr.appendChild(o);
    return tr;
  }

  function flRender(resp) {
    if (!flBody) return;
    flBody.textContent = '';
    var rows = (resp && resp.by_agent) || [];
    if (!rows.length) {
      var tr = document.createElement('tr');
      var td = document.createElement('td');
      td.colSpan = 3;
      td.className = 'panel-sub';
      td.textContent = '目前沒有失敗記錄。';
      tr.appendChild(td); flBody.appendChild(tr);
    } else {
      rows.forEach(function (r) { flBody.appendChild(flRow(r)); });
    }
    var total = (resp && resp.total) || 0;
    var cap = (resp && resp.capacity) || 0;
    if (flCap) flCap.textContent = total + ' / ' + cap + ' 筆';
    if (flNote) {
      flNote.textContent = total >= cap
        ? '已達上限：最舊的記錄正在被覆寫，需要保留請先下載 .jsonl。'
        : '抽取或解析失敗時即時留存完整提示詞與模型原始回覆，供分析、重測或微調語料使用。';
    }
    if (flDownload) flDownload.disabled = !total;
    if (flClear) flClear.disabled = !total;
  }

  function flLoad() {
    if (!flBody || !window.pdApi) return;
    if (flRefresh) flRefresh.disabled = true;
    window.pdApi.get('/api/llm-fail-log').then(flRender).catch(function (err) {
      if (flNote) flNote.textContent = '失敗記錄載入失敗' + ((err && err.message) ? '：' + err.message : '');
    }).then(function () {
      if (flRefresh) flRefresh.disabled = false;
    });
  }

  if (flRefresh) flRefresh.addEventListener('click', flLoad);

  if (flDownload) {
    flDownload.addEventListener('click', function () {
      var restore = window.pdBusy ? window.pdBusy(flDownload, '打包中…') : function () {};
      window.pdApi.download('/api/llm-fail-log/export', {}).catch(function (err) {
        if (window.toast) window.toast(err.message, 'fail', err.code);
      }).then(function () { restore(); });
    });
  }

  if (flClear) {
    flClear.addEventListener('click', function () {
      // Native dialogs are banned (tests/contract/test_web_native_dialogs.py); and the
      // copy names what SURVIVES, because the neighbouring table is the billing record
      // and deleting that would hand back spent budget.
      var run = function () {
        var restore = window.pdBusy ? window.pdBusy(flClear, '清除中…') : function () {};
        window.pdApi.del('/api/llm-fail-log').then(function (resp) {
          if (window.toast) window.toast('已清除 ' + ((resp && resp.deleted) || 0) + ' 筆失敗記錄', 'ok');
          flLoad();
        }).catch(function (err) {
          if (window.toast) window.toast(err.message, 'fail', err.code);
        }).then(function () { restore(); });
      };
      if (!window.confirmDialog) { run(); return; }
      window.confirmDialog({
        title: '清除 AI 抽取失敗記錄',
        body: '將永久刪除所有已留存的失敗情境（提示詞、模型原始回覆、錯誤原因）。'
            + '若還需要作為分析或微調語料，請先下載 .jsonl。'
            + 'AI 請求明細與額度不受影響，一筆都不會刪。',
        confirmLabel: '永久清除',
        danger: true,
        onConfirm: run
      });
    });
  }

  flLoad();
})();
