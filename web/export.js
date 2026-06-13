/* portfolio-dash — CSV export framework.
   One utility used by every table: export = current filter/sort result.
   Backend later swaps the data getters for real endpoints; the UI stays. */
window.pdExport = (function () {
  'use strict';

  /** Escape one CSV cell. */
  function cell(v) {
    if (v === null || v === undefined) return '';
    const s = String(v);
    if (/[",\n]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
    return s;
  }

  /** Build CSV text from headers + array-of-arrays rows (UTF-8 BOM for Excel). */
  function toCsv(headers, rows) {
    const lines = [headers.map(cell).join(',')];
    rows.forEach((r) => lines.push(r.map(cell).join(',')));
    return '\uFEFF' + lines.join('\r\n');
  }

  /** Trigger a client-side download. */
  function download(filename, headers, rows) {
    const blob = new Blob([toCsv(headers, rows)], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    if (window.toast) window.toast('已匯出 ' + filename, 'ok', rows.length + ' 列・依目前篩選與排序');
  }

  /** Factory: a small export button for a panel head.
      getData() must return { filename, headers, rows }. */
  function button(getData, label) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn-export';
    b.title = '匯出目前篩選/排序結果為 CSV';
    b.appendChild(Object.assign(document.createElement('span'), { textContent: '⬇' }));
    b.appendChild(Object.assign(document.createElement('span'), { textContent: label || '匯出 CSV' }));
    b.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const d = getData();
      if (!d || !d.rows || d.rows.length === 0) {
        if (window.toast) window.toast('沒有可匯出的資料', 'fail', '目前篩選結果為空');
        return;
      }
      download(d.filename, d.headers, d.rows);
    });
    return b;
  }

  /** Read a rendered <table class="data"> into rows (skips badge-only cells' extra text). */
  function rowsFromTable(table) {
    const rows = [];
    table.querySelectorAll('tbody tr').forEach((tr) => {
      if (tr.classList.contains('detail-row')) return;
      const r = [];
      tr.querySelectorAll('td').forEach((td) => r.push(td.textContent.trim().replace(/\s+/g, ' ')));
      rows.push(r);
    });
    return rows;
  }

  function headersFromTable(table) {
    const h = [];
    table.querySelectorAll('thead th').forEach((th) => h.push(th.textContent.trim()));
    return h;
  }

  /** Convenience: wire a button that exports a live table element as-is. */
  function tableButton(table, filename, label) {
    return button(() => ({
      filename,
      headers: headersFromTable(table),
      rows: rowsFromTable(table)
    }), label);
  }

  return { toCsv, download, button, tableButton, rowsFromTable, headersFromTable };
})();
