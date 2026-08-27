/* portfolio-dash — 券商對帳單匯入 + 匯入批次復原 (v0.1.29, C6)

   Two things live here, and they belong together: the door that makes a big import EASY,
   and the door that makes it REVERSIBLE. Neither is safe to ship without the other — a
   one-click way to load five years of broker history into a ledger, with no way to take it
   back, is a button nobody should press.

   ── 券商對帳單 ──────────────────────────────────────────────────────────────
   Drop the broker's own export → POST /api/broker/convert → a report → 全部寫入.

   The endpoint returns CSV **text**, and this file feeds that text back through the ORDINARY
   /api/import/preview → /api/import/commit path, one kind at a time in the order the server
   names (`commit_order`). Nothing here writes to a ledger. That is the whole design: the
   converted rows meet every validation a hand-made CSV meets, get the same duplicate
   detection, and land in the same undoable batch.

   ⚠ The commit order is a DEPENDENCY. Trades come before corporate actions (an action's own
   guards need the position to exist), which means a sell that is only legal after a split
   would meet a pre-split share count — so every transactions request carries
   `pending_actions_csv`, the action file arriving in the same run. Without it the owner is
   asked to acknowledge 賣超, and acknowledging 賣超 permanently discards a cost basis.

   ⚠ **Stop on the first refusal.** If one kind comes back with rejected rows, the remaining
   kinds are NOT sent. Rows that depend on a position must not be written against a position
   that failed to arrive, and the batches already written are listed with 復原 beside them.

   ── 最近匯入 ────────────────────────────────────────────────────────────────
   GET/DELETE /api/import/batches. These have existed since the provenance work and had NO
   caller in web/ (#83) — an undo reachable only from a SQLite console is not an undo.

   Money is never computed here. Every figure rendered is a string the API produced. */
(function () {
  'use strict';

  const api = window.pdApi;
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  };

  /* zh labels for the import kinds, so the report and the batch list agree with the chips
     the owner already knows from the 標準範本 mode. */
  const KIND_ZH = {
    transactions: '交易', dividends: '股利', fx: '換匯', openings: '期初庫存',
    corporate_actions: '公司行動', cash: '資金',
  };

  /* One line per blocking issue code: what it means and what to do. The server's `detail`
     is precise but written for the person who wrote the reconciler; these are written for
     the person holding the statement. Both are shown — the code, the sentence, the detail. */
  const BLOCK_ZH = {
    cusip_unresolved: '有幾列只用 CUSIP 標示標的。照原樣匯入會變成第二檔標的，之後的公司行動就會套不上去。請在下方填入對應的代號。',
    priced_row_no_cash: '有一列標了價格卻沒有金額流動。請看報告指出的兩列，通常其中一列應該從匯出檔刪掉。',
    priced_row_mismatch: '股數 × 價格 ± 費用 和該列自己的金額對不起來。可能是券商的特例，也可能是這裡的解析錯誤 — 請把這段訊息回報。',
    cash_not_conserved: '轉換過程本身遺失或多算了現金。這是程式的問題，不是你檔案的問題 — 請回報。',
    shares_not_conserved: '轉換過程本身遺失或多算了股數。這是程式的問題，不是你檔案的問題 — 請回報。',
    suppressed_not_zero: '被判定為互相抵銷而丟棄的群組並沒有真的歸零。這是程式的問題 — 請回報。',
    rows_lost: '有列在轉換途中消失了。這是程式的問題 — 請回報。',
    over_reinvested: '有一組配息再投入的金額超過它收到的金額。請回報。',
    withholding_exceeds_gross: '有一筆預扣稅大於股利總額。請回報。',
  };

  const ADVISORY_ZH = {
    option_row_unsupported: '選擇權腿',
    prehistory_position: '匯出區間之前就持有的部位',
    overlap_duplicate: '兩份匯出檔區間重疊、可能重複的列',
    unrouted_row: '沒有對應到任何帳本的列',
    vetoed_group: '本來要丟棄、但算術不合而保留的群組',
    reinvest_without_payout: '只有再投入、沒有配息本體的列',
  };

  let conversion = null;      // the last /api/broker/convert response
  let files = [];             // [{name, text}]
  let accounts = [];

  // ---------------------------------------------------------------- source switch

  function initSourceChips() {
    const bar = $('#csv-source');
    if (!bar) return;
    [['標準範本', 'standard'], ['券商對帳單', 'broker']].forEach(([label, id], i) => {
      const c = el('button', 'chip' + (i === 0 ? ' active' : ''), label);
      c.type = 'button';
      c.addEventListener('click', () => {
        bar.querySelectorAll('.chip').forEach((x) => x.classList.remove('active'));
        c.classList.add('active');
        const broker = id === 'broker';
        $('#csv-standard').hidden = broker;
        $('#csv-broker').hidden = !broker;
        const note = $('#csv-source-note');
        if (note) {
          note.textContent = broker
            ? '直接放入券商網站下載的原始交易明細，系統會轉成帳本格式並先對帳。'
            : '使用本系統的六種匯入範本。';
        }
      });
      bar.appendChild(c);
    });
    const note = $('#csv-source-note');
    if (note) note.textContent = '使用本系統的六種匯入範本。';
  }

  // ---------------------------------------------------------------- batches (#83)

  async function loadBatches() {
    const tbody = $('#bk-batches');
    if (!tbody) return;
    let rows = [];
    try {
      const resp = await api.get('/api/import/batches?limit=20');
      rows = (resp && resp.batches) || [];
    } catch (err) {
      tbody.replaceChildren();
      const tr = el('tr');
      const td = el('td', 'hint', '匯入紀錄讀取失敗');
      td.colSpan = 5;
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    tbody.replaceChildren();
    if (!rows.length) {
      const tr = el('tr');
      const td = el('td', 'hint', '尚無匯入紀錄');
      td.colSpan = 5;
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    rows.forEach((b) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'col-text', String(b.imported_at || '').replace('T', ' ').slice(0, 19)));
      tr.appendChild(el('td', 'col-text', KIND_ZH[b.kind] || b.kind));
      tr.appendChild(el('td', 'col-text', b.source_name || '—'));
      tr.appendChild(el('td', 'num', String(b.row_count)));
      const act = el('td');
      const undo = el('button', 'btn btn-sm', '復原');
      undo.type = 'button';
      undo.addEventListener('click', () => undoBatch(b));
      act.appendChild(undo);
      tr.appendChild(act);
      tbody.appendChild(tr);
    });
  }

  function undoBatch(b) {
    window.confirmDialog({
      title: '復原這批匯入',
      /* Names the count and the kind. "Undo the import" is not enough to decide on — the
         whole reason this control exists is a batch that turned out wrong, and the owner
         needs to see WHICH one they are about to remove. */
      body: '將刪除這批匯入寫進帳本的 ' + b.row_count + ' 筆'
        + (KIND_ZH[b.kind] || b.kind) + '紀錄（來源：' + (b.source_name || '未命名')
        + '）。手動輸入的紀錄與其他批次不受影響，統計會由其餘帳本重建。',
      confirmLabel: '復原', danger: true,
      onConfirm: async () => {
        try {
          const r = await api.del('/api/import/batches/' + b.id);
          if (window.toast) window.toast('已復原', 'ok', '刪除 ' + r.deleted + ' 筆');
          await loadBatches();
          /* F-04: `pdAfterLedgerChange` was never defined ANYWHERE — the
             `if (window.…)` guard turned a wrong name into a silent no-op, so an undo
             toasted 「刪除 3 筆」 over a table still listing all three. The seam ledger.js
             actually exposes is `pdLedgerRefresh` (ledger.js:788), which input.js:1996 and
             corp-action-form.js:712 both use correctly. */
          if (window.pdLedgerRefresh) {
            try { await window.pdLedgerRefresh(); } catch (e) { /* degrade silently */ }
          }
        } catch (err) {
          if (window.toast) window.toast((err && err.message) || '復原失敗', 'fail', err && err.code);
        }
      },
    });
  }

  // ---------------------------------------------------------------- convert

  function setFiles() {
    const box = $('#bk-files');
    if (!box) return;
    box.textContent = files.length
      ? files.map((f) => f.name).join('・')
      : '尚未選擇';
  }

  function readFiles(fileList) {
    const picked = Array.prototype.slice.call(fileList || []);
    if (!picked.length) return;
    let pending = picked.length;
    picked.forEach((f) => {
      const r = new FileReader();
      r.onload = () => {
        files.push({ name: f.name, text: String(r.result || '') });
        if (--pending === 0) { setFiles(); runConvert(); }
      };
      r.onerror = () => {
        if (window.toast) window.toast('檔案讀取失敗', 'fail', f.name);
        if (--pending === 0) { setFiles(); runConvert(); }
      };
      r.readAsText(f, 'utf-8');
    });
  }

  async function runConvert() {
    if (!files.length) return;
    const box = $('#bk-report');
    box.replaceChildren(el('div', 'hint', '轉換中…'));
    $('#bk-commit').disabled = true;
    try {
      conversion = await api.post('/api/broker/convert', {
        account: $('#bk-account').value,
        broker: $('#bk-broker').value,
        currency: 'USD',
        exports: files,
        aliases: {},
      });
    } catch (err) {
      conversion = null;
      box.replaceChildren();
      const card = el('div', 'result-banner');
      card.appendChild(el('div', null, '✗ 無法轉換：' + ((err && err.message) || '未知錯誤')));
      if (err && err.code === 'broker_row_unmapped') {
        card.appendChild(el('div', 'panel-sub',
          '這個檔案裡有一種本系統還不認得的交易類型。系統不會用猜的把它歸到某一類 — '
          + '猜錯會讓一筆錢安靜地跑進錯的地方。請把上面那行訊息回報。'));
      }
      box.appendChild(card);
      return;
    }
    renderReport();
  }

  function renderReport() {
    const box = $('#bk-report');
    box.replaceChildren();
    const c = conversion;
    if (!c) return;

    const card = el('div', 'result-banner');
    if (!c.ok) {
      card.appendChild(el('div', null, '✗ 對帳未通過，沒有產生任何可匯入的資料（' + c.blocking.length + ' 項）'));
      card.appendChild(el('div', 'panel-sub',
        '這是全有全無的檢查：只要有一項對不起來，就不會寫入任何一列。'
        + '部分匯入會留下一本沒有人重建得回來的帳。'));
      c.blocking.forEach((i) => {
        card.appendChild(el('div', 'panel-sub', '・' + (BLOCK_ZH[i.code] || i.code)));
        card.appendChild(el('div', 'hint', '　' + i.detail
          + (i.refs.length ? '（' + i.refs.slice(0, 6).join('、') + '）' : '')));
      });
      box.appendChild(card);
      $('#bk-commit').disabled = true;
      return;
    }

    card.appendChild(el('div', null, '✓ 對帳通過 —— 現金與各代號股數都守恆'));
    const counts = (c.commit_order || [])
      .map((k) => (KIND_ZH[k] || k) + ' ' + c.counts[k]).join(' · ');
    card.appendChild(el('div', 'panel-sub', '讀入 ' + c.rows_in + ' 列，產生：' + (counts || '（無）')));
    box.appendChild(card);

    renderNeedsInput(box, c);
    renderAdvisories(box, c);
    $('#bk-commit').disabled = !(c.files && Object.keys(c.files).length);
  }

  /* The two worksheets, as FORM FIELDS. The CLI writes them as *_TO_COMPLETE.csv and asks
     the owner to open a spreadsheet; what the file cannot determine is exactly what a form
     should be asking for, so here it asks. Left blank, the row is simply not imported —
     and the result says which ones were left out. */
  function renderNeedsInput(box, c) {
    const pending = c.actions_needing_input || [];
    const openings = c.openings_needing_cost || [];
    if (!pending.length && !openings.length) return;
    box.appendChild(el('div', 'panel-sub', '⚠ 需要你補的資料（' + (pending.length + openings.length) + '）'));
    box.appendChild(el('div', 'hint',
      '這些欄位匯出檔本身沒有寫，系統不會用猜的填 —— 猜出來的比例或成本會安靜地算錯，'
      + '而且畫面上看起來完全正常。留白就是不匯入那一列。'));

    pending.forEach((p, idx) => {
      const row = el('div', 'field');
      row.appendChild(el('label', null,
        p.date + '　' + p.kind + '　' + (p.from_symbol || '?') + ' → ' + p.to_symbol));
      const line = el('div', 'fee-line');
      line.appendChild(el('span', 'hint', '每持有'));
      const from = el('input', 'input');
      from.type = 'number'; from.min = '1'; from.step = '1'; from.style.width = '90px';
      from.id = 'bk-ratio-from-' + idx;
      if (p.ratio_from !== null) from.value = String(p.ratio_from);
      line.appendChild(from);
      line.appendChild(el('span', 'hint', '股 → 變成'));
      const to = el('input', 'input');
      to.type = 'number'; to.min = '1'; to.step = '1'; to.style.width = '90px';
      to.id = 'bk-ratio-to-' + idx;
      if (p.ratio_to !== null) to.value = String(p.ratio_to);
      line.appendChild(to);
      line.appendChild(el('span', 'hint', '股'));
      row.appendChild(line);
      row.appendChild(el('span', 'hint', p.needs));
      box.appendChild(row);
    });

    openings.forEach((o, idx) => {
      const row = el('div', 'field');
      row.appendChild(el('label', null,
        '期初庫存　' + o.symbol + (o.shares ? '　' + o.shares + ' 股' : '　（股數未知）')));
      const cost = el('input', 'input');
      cost.type = 'number'; cost.min = '0'; cost.step = '0.01';
      cost.id = 'bk-opening-cost-' + idx;
      cost.placeholder = '當初買進的總金額（含手續費與稅）';
      row.appendChild(cost);
      row.appendChild(el('span', 'hint',
        '這是匯出檔開始之前就持有的部位。成本填 0 會讓這個部位的成本基礎永久歸零，'
        + '而且不會出現任何「待釐清」標記 —— 查不到就先不要匯入這一列。'));
      box.appendChild(row);
    });
  }

  function renderAdvisories(box, c) {
    const adv = c.advisory || [];
    const unconv = c.unconvertible || [];
    if (!adv.length && !unconv.length) return;
    const det = el('details');
    const sum = el('summary', 'panel-sub',
      '不會匯入的項目（' + (adv.length + unconv.length) + '）—— 這些不會擋住匯入');
    det.appendChild(sum);
    adv.forEach((i) => {
      det.appendChild(el('div', 'hint',
        '・' + (ADVISORY_ZH[i.code] || i.code) + '：' + i.detail
        + (i.refs.length ? '（' + i.refs.slice(0, 8).join('、') + '）' : '')));
    });
    unconv.forEach((u) => {
      det.appendChild(el('div', 'hint',
        '・' + u.date + ' ' + u.kind + '（' + u.ref + '）：' + u.why));
    });
    box.appendChild(det);
  }

  // ---------------------------------------------------------------- commit

  /* Re-render the worksheet rows the owner filled in back into their CSV kind. Blank stays
     blank: an unfilled ratio row is dropped rather than guessed, and the result says so. */
  function completedActionsCsv() {
    const c = conversion;
    const ready = (c.files && c.files.corporate_actions) || '';
    const pending = c.actions_needing_input || [];
    const filled = [];
    pending.forEach((p, idx) => {
      const from = ($('#bk-ratio-from-' + idx) || {}).value;
      const to = ($('#bk-ratio-to-' + idx) || {}).value;
      if (!from || !to || !p.from_symbol) return;
      filled.push([$('#bk-account').value, p.date, p.kind, p.from_symbol, p.to_symbol,
        String(parseInt(to, 10)), String(parseInt(from, 10)), '', p.refs.join(' · ')]);
    });
    if (!filled.length) return ready;
    /* The header comes from the SERVER (`worksheet_headers`), never from a copy kept here:
       a second column list in JS drifts from the parser's, and the symptom is an import
       that rejects every row for a reason nobody can see. */
    const header = c.worksheet_headers.corporate_actions;
    const body = ready ? ready.slice(header.length + 2) : '';
    return header + '\r\n' + body
      + filled.map((r) => r.map(csvCell).join(',') + '\r\n').join('');
  }

  function completedOpeningsCsv() {
    const c = conversion;
    const rows = [];
    (c.openings_needing_cost || []).forEach((o, idx) => {
      const cost = ($('#bk-opening-cost-' + idx) || {}).value;
      if (!cost || !o.shares) return;
      rows.push([$('#bk-account').value, o.symbol, o.shares, cost, buildDate(), '']);
    });
    if (!rows.length) return '';
    return c.worksheet_headers.openings + '\r\n'
      + rows.map((r) => r.map(csvCell).join(',') + '\r\n').join('');
  }

  /* The day BEFORE the earliest converted row. An opening exists before the ledger starts,
     and dating it onto a day that also carries trades makes the replay's same-date ordering
     decide whether it covers a sell. Derived from the transactions the server just built,
     so it needs no second source of truth. */
  function buildDate() {
    const tx = (conversion.files && conversion.files.transactions) || '';
    const dates = tx.split(/\r?\n/).slice(1)
      .map((l) => (l.split(',')[3] || '').trim()).filter(Boolean).sort();
    if (!dates.length) return '';
    const d = new Date(dates[0] + 'T00:00:00Z');
    d.setUTCDate(d.getUTCDate() - 1);
    return d.toISOString().slice(0, 10);
  }

  function csvCell(v) {
    const s = String(v === null || v === undefined ? '' : v);
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  async function commitAll() {
    const c = conversion;
    if (!c || !c.ok) return;
    const btn = $('#bk-commit');
    const restore = window.pdBusy ? window.pdBusy(btn, '寫入中…') : () => {};
    const actionsCsv = completedActionsCsv();
    const openingsCsv = completedOpeningsCsv();
    const plan = [];
    if (openingsCsv) plan.push(['openings', openingsCsv]);
    (c.commit_order || []).forEach((kind) => {
      if (kind === 'openings') return;            // supplied above, from the form
      const text = kind === 'corporate_actions' ? actionsCsv : c.files[kind];
      if (text && text.split(/\r?\n/).filter((l) => l.trim()).length > 1) plan.push([kind, text]);
    });

    const results = [];
    let stopped = null;
    for (const [kind, text] of plan) {
      const body = {
        kind, csv_text: text, ack_warnings: true,
        source_name: files.map((f) => f.name).join('+'),
        broker: $('#bk-broker').value,
      };
      /* Trades are validated against the actions arriving in the SAME run — see the module
         header. Without this the guard measures a post-split sell against a pre-split count
         and demands the one acknowledgement that discards a cost basis. */
      if (kind === 'transactions' && actionsCsv) body.pending_actions_csv = actionsCsv;
      try {
        const r = await api.post('/api/import/commit', body);
        results.push([kind, r]);
        if (r.rejected) { stopped = kind; break; }
      } catch (err) {
        results.push([kind, { error: (err && err.message) || '寫入失敗' }]);
        stopped = kind;
        break;
      }
    }
    restore();
    renderCommitResult(results, stopped, plan.length);
    await loadBatches();
    if (window.pdLedgerRefresh) {                       // see the note on the undo path
      try { await window.pdLedgerRefresh(); } catch (e) { /* degrade silently */ }
    }
  }

  function renderCommitResult(results, stopped, planned) {
    const box = $('#bk-report');
    const card = el('div', 'result-banner');
    card.appendChild(el('div', null, stopped ? '⚠ 寫入中止' : '✓ 全部寫入完成'));
    results.forEach(([kind, r]) => {
      if (r.error) {
        card.appendChild(el('div', 'panel-sub', (KIND_ZH[kind] || kind) + '：' + r.error));
        return;
      }
      const bits = [(KIND_ZH[kind] || kind) + ' 寫入 ' + r.written + ' 筆'];
      if (r.duplicates) bits.push('已匯入過 ' + r.duplicates + ' 筆');
      if (r.skipped) bits.push('跳過 ' + r.skipped + ' 筆');
      if (r.rejected) bits.push('擋下 ' + r.rejected + ' 筆');
      card.appendChild(el('div', 'panel-sub', bits.join('・')));
      (r.rejected_rows || []).slice(0, 10).forEach((rr) => {
        card.appendChild(el('div', 'hint', '　第 ' + rr.row + ' 列：' + rr.message));
      });
    });
    if (stopped) {
      card.appendChild(el('div', 'panel-sub',
        '在「' + (KIND_ZH[stopped] || stopped) + '」這一步停下來了，後面 '
        + (planned - results.length) + ' 個類型沒有送出。'
        + '後面的資料要對得上前面建立的部位，所以不會在缺一段的情況下硬寫進去。'));
      card.appendChild(el('div', 'hint',
        '已經寫進去的批次列在下方，可以逐批「復原」回到匯入前的狀態。'
        + '修正問題後重新上傳整份匯出檔即可 —— 已寫入的列會自動略過，不會變成兩筆。'));
    }
    box.replaceChildren(card);
    $('#bk-commit').disabled = true;
  }

  // ---------------------------------------------------------------- init

  async function initBroker() {
    const bsel = $('#bk-broker');
    const asel = $('#bk-account');
    if (!bsel || !asel) return;
    try {
      const [b, ctx] = await Promise.all([
        api.get('/api/broker/adapters'),
        api.get('/api/input/context'),
      ]);
      (b.brokers || []).forEach((id) => {
        const o = el('option', null, id === 'schwab' ? 'Charles Schwab' : id);
        o.value = id;
        bsel.appendChild(o);
      });
      accounts = (ctx && ctx.accounts) || [];
      accounts.forEach((a) => {
        const o = el('option', null, a.name + '（' + a.id + '）');
        o.value = a.id;
        asel.appendChild(o);
      });
      const schwab = accounts.find((a) => a.id === 'schwab');
      if (schwab) asel.value = 'schwab';
    } catch (err) { /* the pane still renders; the selects are simply empty */ }

    const note = $('#bk-note');
    if (note) note.textContent = '轉換後仍會逐列檢核，寫入的每一批都可以復原。';

    const dz = $('#bk-dropzone');
    const fin = $('#bk-file-input');
    if (dz && fin) {
      dz.style.cursor = 'pointer';
      dz.addEventListener('click', () => fin.click());
      fin.addEventListener('change', () => { readFiles(fin.files); fin.value = ''; });
      dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('dz-over'); });
      dz.addEventListener('dragleave', () => dz.classList.remove('dz-over'));
      dz.addEventListener('drop', (e) => {
        e.preventDefault();
        dz.classList.remove('dz-over');
        readFiles(e.dataTransfer && e.dataTransfer.files);
      });
    }
    asel.addEventListener('change', () => { if (files.length) runConvert(); });
    bsel.addEventListener('change', () => { if (files.length) runConvert(); });
    $('#bk-commit').addEventListener('click', commitAll);
    $('#bk-clear').addEventListener('click', () => {
      files = []; conversion = null;
      setFiles();
      $('#bk-report').replaceChildren();
      $('#bk-commit').disabled = true;
    });
  }

  function boot() {
    if (!$('#csv-source')) return;      // not the trades page
    initSourceChips();
    initBroker();
    loadBatches();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  /* Exposed so input.js can refresh the list after an ORDINARY CSV import — the batch card
     serves both modes and would otherwise go stale exactly when it matters. */
  window.pdReloadImportBatches = loadBatches;
})();
