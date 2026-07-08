/* portfolio-dash — 設定 · 資料庫統計 (read-only, wired to GET /api/db-stats).

   Owner decision (2026-07-07): observe per-table row counts + oldest record dates
   across BOTH SQLite files (portfolio.db + news.db) to judge future retention
   windows. Display only — no pruning exists anywhere. Counts are JSON numbers;
   sizes are bytes (number) formatted to MB here (presentation, not money).
   Follows the settings-JS convention: missing node -> skip (the panel lives ONLY
   on the canonical tabbed settings.html). */
(function () {
  'use strict';
  const f = window.fmt;
  const body = document.getElementById('dbstats-body');
  if (!body || !window.pdApi) return; /* panel absent on this surface — skip */
  const files = document.getElementById('dbstats-files');
  const note = document.getElementById('dbstats-note');
  const refreshBtn = document.getElementById('dbstats-refresh');

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

  function renderGroups(groups) {
    groups.forEach(function (g) {
      (g.tables || []).forEach(function (t, i) {
        body.appendChild(tableRow(g.category, t, i === 0));
      });
    });
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
    renderGroups(p.groups || []);
    if (n.present) renderGroups(n.groups || []);
    if (note) note.textContent = '唯讀統計 — 供保留期限評估；目前不做任何自動清理。';
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
