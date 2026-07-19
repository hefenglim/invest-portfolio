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
})();
