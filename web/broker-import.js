/* portfolio-dash — 券商對帳單匯入 + 匯入批次復原 (v0.1.29, C6)

   Two things live here, and they belong together: the door that makes a big import EASY,
   and the door that makes it REVERSIBLE. Neither is safe to ship without the other — a
   one-click way to load five years of broker history into a ledger, with no way to take it
   back, is a button nobody should press.

   ── 券商對帳單 ──────────────────────────────────────────────────────────────
   Drop the broker's own export → POST /api/broker/convert → a report, ROW BY ROW → tick
   the rows → 寫入勾選列.

   The endpoint returns CSV **text** plus the same rows as structure (`rows[kind][]`,
   DEF-028), and this file feeds that text back through the ORDINARY
   /api/import/preview → /api/import/commit path, one kind at a time in the order the server
   names (`commit_order`), with the ticked rows as `select` indices — never as a re-rendered
   CSV (F-03: the server has already parsed the rows; the page must not parse them again).
   Nothing here writes to a ledger. That is the whole design: the converted rows meet every
   validation a hand-made CSV meets, get the same duplicate detection, and land in the same
   undoable batch.

   ⚠ The commit order is a DEPENDENCY. Trades come before corporate actions (an action's own
   guards need the position to exist), which means a sell that is only legal after a split
   would meet a pre-split share count — so every transactions request carries
   `pending_actions_csv`, the action file arriving in the same run, built from the TICKED
   action rows only (an action the owner unticked is not arriving, and must not legalise a
   sell). Without it the owner is asked to acknowledge 賣超, and acknowledging 賣超
   permanently discards a cost basis.

   ⚠ **No blanket acknowledgement — ever (DEF-027, 2026-09-23).** This file used to send
   `ack_warnings: true` on every commit, so a statement selling 1,000 AAPL into an account
   holding 85.04 wrote the sell with no dialog, discarded the cost basis and left the
   position at −914.96 shares. A commit is first sent UNACKNOWLEDGED; on the server's 422
   `warnings_unacknowledged` the warning rows are fetched through /api/import/preview and
   shown one by one — symbol, date, shares, the server's own message, and for a 賣超 the
   consequence in words — each with a checkbox that starts UNTICKED. Only the rows the owner
   ticks are re-sent (as `select`) under the acknowledgement; the rest are skipped. The
   server's own shrink-only rule (QA-01) then drops any row whose warning only appeared
   because of the narrowing, so the acknowledgement never covers a warning nobody saw.

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
  /* Both display names come from web/names.js (the single naming authority); the id is
     the no-names.js fallback. */
  const acctZh = (id) => (window.pdNames ? window.pdNames.account(id) : String(id));
  const brokerZh = (id) => (window.pdNames ? window.pdNames.broker(id) : String(id));

  /* zh labels for the import kinds, so the report and the batch list agree with the chips
     the owner already knows from the 標準範本 mode. */
  const KIND_ZH = {
    transactions: '交易', dividends: '股利', fx: '換匯', openings: '期初庫存',
    corporate_actions: '公司行動', cash: '資金',
  };

  /* The row TYPE column of the per-row tables — display only, never a computation. */
  const TYPE_ZH = {
    BUY: '買入', SELL: '賣出',
    DRIP: '股利再投入', CASH: '現金股利',
    DEPOSIT: '入金', WITHDRAW: '出金', INTEREST: '利息收入',
    INTEREST_EXPENSE: '利息支出', BROKER_FEE: '券商費用',
    SPLIT: '分割', REVERSE_SPLIT: '反向分割', EXCHANGE: '換股', NAME_CHANGE: '更名',
    SPINOFF: '分拆',
  };

  /* Why a source row became no row of its own (`dropped[].why`, DEF-028). */
  const DROPPED_ZH = {
    suppressed: '互相抵銷的群組（內部轉帳／取消／調整），已丟棄',
    merged_dividend: '配息分錄，已合併為一筆股利',
    folded_interest_tax: '利息預扣稅，已併入同日的利息收入',
    option_row_unsupported: '選擇權腿，本系統不支援',
    unrouted_row: '已分類但不屬於任何帳本',
    unconvertible: '無法轉換為帳本列',
    action_needs_input: '公司行動的比例待補',
  };

  /* One line per blocking issue code: what it means and what to do. The server's `detail`
     is precise but written for the person who wrote the reconciler; these are written for
     the person holding the statement. Both are shown — the code, the sentence, the detail.
     A FUNCTION entry builds its sentence from the issue's own fields. */
  const BLOCK_ZH = {
    cusip_unresolved: '有幾列只用 CUSIP 標示標的。照原樣匯入會變成第二檔標的，之後的公司行動就會套不上去。請在下方填入對應的代號。',
    priced_row_no_cash: '有一列標了價格卻沒有金額流動。請看報告指出的兩列，通常其中一列應該從匯出檔刪掉。',
    priced_row_mismatch: '股數 × 價格 ± 費用 和該列自己的金額對不起來。可能是券商的特例，也可能是這裡的解析錯誤 — 請把這段訊息回報。',
    cash_not_conserved: '轉換過程本身遺失或多算了現金。這是程式的問題，不是你檔案的問題 — 請回報。',
    shares_not_conserved: '轉換過程本身遺失或多算了股數。這是程式的問題，不是你檔案的問題 — 請回報。',
    suppressed_not_zero: '被判定為互相抵銷而丟棄的群組並沒有真的歸零。這是程式的問題 — 請回報。',
    /* Its sibling above was mapped and this one was not, so it rendered as the raw code
       `suppressed_ref_unknown` — a blocking issue explained to the owner in nothing but a
       Python identifier (QA-29b). Same failure mode as the printed statement's private
       label map: `|| i.code` turns a missing entry into plausible-looking output. */
    suppressed_ref_unknown: '被丟棄的群組指向了匯出檔裡找不到的列。這是程式的問題，不是你檔案的問題 — 請回報。',
    rows_lost: '有列在轉換途中消失了。這是程式的問題 — 請回報。',
    over_reinvested: '有一組配息再投入的金額超過它收到的金額。請回報。',
    withholding_exceeds_gross: '有一筆預扣稅大於股利總額。請回報。',
    /* DEF-029: the statement's broker and the target account's broker disagree. Named
       through pdNames on both sides, so the sentence reads in one language. */
    account_broker_mismatch: (i) => '所選券商「' + brokerZh(i.broker_id) + '」的對帳單不能匯入帳戶「'
      + acctZh(i.account_id) + '」—— 這個帳戶不屬於這家券商，一列都不會轉換。'
      + '請改選這家券商的帳戶，或改選對應的券商。',
  };
  const blockSentence = (i) => {
    const entry = BLOCK_ZH[i.code];
    return typeof entry === 'function' ? entry(i) : (entry || i.code);
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
  let accounts = [];          // the /api/input/context account list
  let accountsByBroker = {};  // adapter id -> [account ids] (server-owned, DEF-029)
  let ticks = {};             // kind -> Set of ticked row indices (DEF-028)

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
      tr.appendChild(el('td', 'num', batchCountText(b)));
      const act = el('td');
      /* I-8 (DEF-017's page half): a batch whose rows carry no batch id (期初庫存 upserts on
         account + symbol) cannot be undone by batch — the server refuses it (422
         batch_not_undoable). Offering 復原 there was a button that could only fail. */
      if (b.undoable === false) {
        const note = el('span', 'hint', '無法依批次復原');
        note.title = '期初庫存的匯入不帶批次標記，請到「期初庫存」分頁逐筆修正或刪除';
        act.appendChild(note);
      } else {
        const undo = el('button', 'btn btn-sm', '復原');
        undo.type = 'button';
        undo.addEventListener('click', () => undoBatch(b));
        act.appendChild(undo);
      }
      tr.appendChild(act);
      tbody.appendChild(tr);
    });
  }

  /* I-8: `row_count` is LIVE (what the batch still owns — DEF-017) and `written_count` is
     what the commit originally wrote. When rows were since deleted on their ledger tabs the
     cell says both, so 「2」 on a batch that imported 5 is not read as a short import. */
  function batchCountText(b) {
    const live = String(b.row_count);
    const wrote = b.written_count;
    if (wrote === undefined || wrote === null || String(wrote) === live) return live;
    return live + '（原寫入 ' + wrote + '）';
  }

  function undoBatch(b) {
    window.confirmDialog({
      title: '復原這批匯入',
      /* Names the count and the kind. "Undo the import" is not enough to decide on — the
         whole reason this control exists is a batch that turned out wrong, and the owner
         needs to see WHICH one they are about to remove. */
      /* I-8: the old sentence exempted 「手動輸入的紀錄」 from the undo — but the one-row
         股利／期初 forms write through this same import door and each becomes a batch of its
         own (source 「手動輸入」), which 復原 removes like any other. The sentence now says
         what the delete is keyed on: this batch's rows, nothing else. */
      body: '將刪除這批匯入寫進帳本的 ' + b.row_count + ' 筆'
        + (KIND_ZH[b.kind] || b.kind) + '紀錄（來源：' + (b.source_name || '未命名')
        + '）。只刪除這一批寫入的列，其他批次不受影響；單筆表單寫入的紀錄也各自是一批'
        + '（來源標示「手動輸入」），復原那一批同樣會刪除它寫入的列。統計會由其餘帳本重建。'
        + (b.kind === 'corporate_actions'
          ? '換股時搬到新代號的目標價與目標權重，若之後沒有再改動，會一併移回原代號。' : ''),
      confirmLabel: '復原', danger: true,
      onConfirm: async () => {
        try {
          const r = await api.del('/api/import/batches/' + b.id);
          if (window.toast) {
            /* DEF-017: a batch whose rows were all deleted on their ledger tabs answers
               {deleted: 0, message} — say that, not 「刪除 0 筆」 under 已復原. */
            if (r && r.deleted === 0) window.toast('沒有可復原的列', 'warn', r.message || '');
            else window.toast('已復原', 'ok', '刪除 ' + r.deleted + ' 筆');
            undoRestoreNotes(r).forEach((n) => window.toast(n.title, 'warn', n.detail));
          }
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
          /* 422 batch_not_undoable carries its own sentence (the server names the tab to
             use instead); the list is re-read so a stale 復原 does not stay offered. */
          if (window.toast) window.toast((err && err.message) || '復原失敗', 'fail', err && err.code);
          if (err && err.code === 'batch_not_undoable') await loadBatches();
        }
      },
    });
  }

  /* I-3/I-8: an undone corporate-action batch reports, per event, whether the band and the
     target weight its EXCHANGE carried across came back. Only the ones that did NOT are
     worth a toast — with the server's reason, so the owner knows where to fix it by hand. */
  function undoRestoreNotes(r) {
    const notes = [];
    ((r && r.band_restore) || []).forEach((v) => {
      if (v && !v.restored) notes.push({ title: '目標價未移回', detail: v.reason || '' });
    });
    ((r && r.weight_restore) || []).forEach((v) => {
      if (v && !v.restored) notes.push({ title: '目標權重未移回', detail: v.reason || '' });
    });
    return notes;
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

  /* The selected account's settlement currency — the statement's currency for the cash
     rows. It was the literal 'USD' for every account, the TW one included (DEF-029). */
  function selectedCurrency() {
    const id = $('#bk-account').value;
    const a = accounts.find((x) => x.id === id);
    return (a && (a.settlement_ccy || a.ccy)) || 'USD';
  }

  async function runConvert() {
    if (!files.length) return;
    const box = $('#bk-report');
    box.replaceChildren(el('div', 'hint', '轉換中…'));
    setCommitEnabled(false);
    try {
      conversion = await api.post('/api/broker/convert', {
        account: $('#bk-account').value,
        broker: $('#bk-broker').value,
        currency: selectedCurrency(),
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
        card.appendChild(el('div', 'panel-sub', '・' + blockSentence(i)));
        /* The mismatch sentence above already names both sides; its server `detail`
           carries the same fact as an account MARKER for the log, not for the screen. */
        if (i.code !== 'account_broker_mismatch') {
          card.appendChild(el('div', 'hint', '　' + i.detail
            + (i.refs.length ? '（' + i.refs.slice(0, 6).join('、') + '）' : '')));
        }
      });
      box.appendChild(card);
      ticks = {};
      setCommitEnabled(false);
      return;
    }

    card.appendChild(el('div', null, '✓ 對帳通過 —— 現金與各代號股數都守恆'));
    const counts = (c.commit_order || [])
      .map((k) => (KIND_ZH[k] || k) + ' ' + c.counts[k]).join(' · ');
    card.appendChild(el('div', 'panel-sub', '讀入 ' + c.rows_in + ' 列，產生：' + (counts || '（無）')));
    card.appendChild(el('div', 'hint',
      '每一列都列在下方，取消勾選的列不會寫入。寫入時仍會逐列檢核；有警告的列會再逐列確認。'));
    box.appendChild(card);

    ticks = {};
    (c.commit_order || []).forEach((k) => {
      ticks[k] = new Set(((c.rows && c.rows[k]) || []).map((r) => r.i));
    });
    renderRowTables(box, c);
    renderDropped(box, c);
    renderNeedsInput(box, c);
    renderAdvisories(box, c);
    updateCommitButton();
  }

  // ---------------------------------------------------------------- the rows (DEF-028)

  const COLUMNS = [
    ['來源', 'col-text'], ['日期', 'col-text'], ['類型', 'col-text'], ['代號', 'col-text'],
    ['股數', 'num'], ['價格', 'num'], ['金額', 'num'], ['幣別', 'col-text'], ['備註', 'col-text'],
  ];

  function rowCells(r) {
    return [
      r.refs.join('、'), r.date, TYPE_ZH[r.type] || r.type, r.symbol,
      r.shares, r.price, r.amount, r.currency, r.note,
    ];
  }

  /* One table per kind, every row with its own checkbox (ticked by default). The figures
     are the server's strings, column for column what the CSV will carry. */
  function renderRowTables(box, c) {
    (c.commit_order || []).forEach((kind) => {
      const rows = (c.rows && c.rows[kind]) || [];
      if (!rows.length) return;
      const det = el('details');
      det.open = true;
      det.className = 'bk-kind';
      det.dataset.kind = kind;
      det.appendChild(el('summary', 'panel-sub',
        (KIND_ZH[kind] || kind) + '（' + rows.length + ' 列）—— 逐列檢視，取消勾選即不寫入'));
      const wrap = el('div', 'table-wrap');
      const table = el('table', 'data');
      const thead = el('thead');
      const hr = el('tr');
      const allTh = el('th');
      const all = el('input');
      all.type = 'checkbox';
      all.checked = true;
      all.className = 'bk-tick-all';
      all.title = '全選／全不選';
      all.addEventListener('change', () => {
        rows.forEach((r) => setTick(kind, r.i, all.checked));
        table.querySelectorAll('input.bk-row-tick').forEach((cb) => { cb.checked = all.checked; });
      });
      allTh.appendChild(all);
      hr.appendChild(allTh);
      COLUMNS.forEach(([label, cls]) => hr.appendChild(el('th', cls, label)));
      thead.appendChild(hr);
      table.appendChild(thead);
      const tbody = el('tbody');
      rows.forEach((r) => {
        const tr = el('tr');
        const td = el('td');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = true;
        cb.className = 'bk-row-tick';
        cb.id = 'bk-row-' + kind + '-' + r.i;
        cb.dataset.kind = kind;
        cb.dataset.i = String(r.i);
        cb.addEventListener('change', () => {
          setTick(kind, r.i, cb.checked);
          all.checked = rows.every((x) => ticks[kind].has(x.i));
        });
        td.appendChild(cb);
        tr.appendChild(td);
        rowCells(r).forEach((v, idx) => tr.appendChild(el('td', COLUMNS[idx][1], v || '—')));
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      wrap.appendChild(table);
      det.appendChild(wrap);
      box.appendChild(det);
    });
  }

  function setTick(kind, i, on) {
    if (!ticks[kind]) ticks[kind] = new Set();
    if (on) ticks[kind].add(i); else ticks[kind].delete(i);
    updateCommitButton();
  }

  function tickedCount() {
    return Object.keys(ticks).reduce((n, k) => n + ticks[k].size, 0);
  }

  function setCommitEnabled(on) {
    const btn = $('#bk-commit');
    if (btn) btn.disabled = !on;
  }

  function updateCommitButton() {
    const btn = $('#bk-commit');
    if (!btn) return;
    const n = tickedCount();
    const c = conversion;
    const pendingOpenings = c && (c.openings_needing_cost || []).some((o) => !o.satisfied);
    btn.textContent = '寫入勾選列（' + n + '）';
    btn.disabled = !(c && c.ok && (n > 0 || pendingOpenings));
  }

  /* Where `into` points: 「股利 第 1 列」, 「需要你補的資料 第 1 項」, or 「—」 when the row
     left the conversion for good. */
  function intoZh(into) {
    if (!into) return '—';
    const m = /^([a-z_]+):(\d+)$/.exec(into);
    if (!m) return into;
    const n = parseInt(m[2], 10) + 1;
    if (m[1] === 'actions_needing_input') return '需要你補的資料 第 ' + n + ' 項';
    return (KIND_ZH[m[1]] || m[1]) + ' 第 ' + n + ' 列';
  }

  /* Fifteen rows in, nine out: the six that went elsewhere, each with its destination.
     Without this the count line was the only trace, and a difference the owner cannot
     account for reads as rows lost. */
  function renderDropped(box, c) {
    const dropped = c.dropped || [];
    if (!dropped.length) return;
    const det = el('details');
    det.className = 'bk-dropped';
    det.appendChild(el('summary', 'panel-sub',
      '沒有成為獨立帳本列的原始列（' + dropped.length + ' 組）—— 被抵銷、合併或略過的列與去向'));
    const wrap = el('div', 'table-wrap');
    const table = el('table', 'data');
    const thead = el('thead');
    const hr = el('tr');
    [['來源', 'col-text'], ['日期', 'col-text'], ['代號', 'col-text'], ['原因', 'col-text'], ['去向', 'col-text']]
      .forEach(([label, cls]) => hr.appendChild(el('th', cls, label)));
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el('tbody');
    dropped.forEach((d) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'col-text', d.refs.join('、')));
      tr.appendChild(el('td', 'col-text', d.date || '—'));
      tr.appendChild(el('td', 'col-text', d.symbol || '—'));
      tr.appendChild(el('td', 'col-text', (DROPPED_ZH[d.why] || d.why) + (d.detail ? '：' + d.detail : '')));
      tr.appendChild(el('td', 'col-text', intoZh(d.into)));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    det.appendChild(wrap);
    box.appendChild(det);
  }

  /* The two worksheets, as FORM FIELDS. The CLI writes them as *_TO_COMPLETE.csv and asks
     the owner to open a spreadsheet; what the file cannot determine is exactly what a form
     should be asking for, so here it asks. Left blank, the row is simply not imported —
     and the result says which ones were left out. */
  function renderNeedsInput(box, c) {
    const pending = c.actions_needing_input || [];
    const openings = c.openings_needing_cost || [];
    if (!pending.length && !openings.length) return;
    const covered = openings.filter((o) => o.satisfied);
    const open = openings.filter((o) => !o.satisfied);
    if (covered.length) {
      box.appendChild(el('div', 'panel-sub', '期初庫存：帳本已涵蓋（' + covered.length + '）'));
    }
    if (pending.length || open.length) {
      box.appendChild(el('div', 'panel-sub', '⚠ 需要你補的資料（' + (pending.length + open.length) + '）'));
      box.appendChild(el('div', 'hint',
        '這些欄位匯出檔本身沒有寫，系統不會用猜的填 —— 猜出來的比例或成本會安靜地算錯，'
        + '而且畫面上看起來完全正常。留白就是不匯入那一列。'));
    }

    pending.forEach((p, idx) => {
      const row = el('div', 'field');
      row.appendChild(el('label', null,
        p.date + '　' + (TYPE_ZH[p.kind] || p.kind) + '　' + (p.from_symbol || '?') + ' → ' + p.to_symbol));
      const line = el('div', 'fee-line');
      line.appendChild(el('span', 'hint', '每持有'));
      /* type=text with an integer inputmode, not type=number: a number input hands back
         '' for a value it cannot parse, which is a silent rewrite of what was typed. */
      const from = el('input', 'input');
      from.type = 'text'; from.inputMode = 'numeric'; from.style.width = '90px';
      from.id = 'bk-ratio-from-' + idx;
      if (p.ratio_from !== null) from.value = String(p.ratio_from);
      from.addEventListener('input', () => flagRatio(from, from.value.trim() !== '' && !ratioValid(from.value.trim())));
      line.appendChild(from);
      line.appendChild(el('span', 'hint', '股 → 變成'));
      const to = el('input', 'input');
      to.type = 'text'; to.inputMode = 'numeric'; to.style.width = '90px';
      to.id = 'bk-ratio-to-' + idx;
      if (p.ratio_to !== null) to.value = String(p.ratio_to);
      to.addEventListener('input', () => flagRatio(to, to.value.trim() !== '' && !ratioValid(to.value.trim())));
      line.appendChild(to);
      line.appendChild(el('span', 'hint', '股'));
      row.appendChild(line);
      row.appendChild(el('span', 'hint', p.needs));
      box.appendChild(row);
    });

    /* DEF-027: the gap is measured against the LEDGER — what the file needs minus what the
       account already held the day before the statement starts (`as_of`) — so an account
       holding 85.04 of a symbol the statement sells 1,000 of is asked for 914.96, not
       1,000 on top. A position the ledger already covers is stated, and no field is
       offered: an opening row there would count the same shares twice. Covered ones are
       rendered first, under their own heading. */
    covered.concat(open).forEach((o) => {
      const idx = openings.indexOf(o);
      const row = el('div', 'field');
      const asOf = o.as_of ? '在 ' + o.as_of + ' ' : '目前';
      if (o.satisfied) {
        row.appendChild(el('label', null, '期初庫存　' + o.symbol + '　不需補'));
        row.appendChild(el('span', 'hint',
          '帳本' + asOf + '已持有 ' + o.ledger_shares + ' 股'
          + (o.shares ? '，足以涵蓋這份對帳單需要的 ' + o.shares + ' 股' : '')
          + '，不會再新增期初庫存。'));
        box.appendChild(row);
        return;
      }
      const need = o.shares
        ? '對帳單需要 ' + o.shares + ' 股，帳本' + asOf + '已有 ' + o.ledger_shares + ' 股，仍缺 ' + o.gap + ' 股'
        : '（股數未知）帳本' + asOf + '持有 ' + o.ledger_shares + ' 股';
      row.appendChild(el('label', null, '期初庫存　' + o.symbol + '　' + need));
      const cost = el('input', 'input');
      cost.type = 'number'; cost.min = '0'; cost.step = '0.01';
      cost.id = 'bk-opening-cost-' + idx;
      cost.placeholder = o.gap ? '這 ' + o.gap + ' 股當初買進的總金額（含手續費與稅）' : '當初買進的總金額（含手續費與稅）';
      row.appendChild(cost);
      row.appendChild(el('span', 'hint',
        '這是匯出檔開始之前就持有、而帳本裡還沒有的部位。成本填 0 會讓這個部位的成本基礎永久歸零，'
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

  /* A ratio term as TYPED must be a positive integer; nothing else is accepted, and nothing
     is rewritten. `String(parseInt(to, 10))` used to send a typed 1.5 as 1 — the same silent
     rewrite as DEF-005, one door over — and D14's whole point is that a ratio the owner did
     not type must never reach the ledger. */
  const RATIO_ERR = '請輸入正整數（例如 1、3、10）';
  function ratioValid(raw) {
    return /^\d+$/.test(raw) && raw !== '0' && !/^0/.test(raw);
  }
  function flagRatio(input, bad) {
    if (!input) return;
    input.classList.toggle('input-error', bad);
    input.setAttribute('aria-invalid', bad ? 'true' : 'false');
    input.style.borderColor = bad ? 'var(--up)' : '';
    const msgId = input.id + '-msg';
    let msg = document.getElementById(msgId);
    if (bad && !msg) {
      msg = el('span', 'hint', RATIO_ERR);
      msg.id = msgId;
      msg.style.color = 'var(--up)';
      input.insertAdjacentElement('afterend', msg);
    } else if (!bad && msg) {
      msg.remove();
    }
  }

  /* The worksheet rows the owner filled in, exactly as typed. Blank stays blank: an
     unfilled ratio row is dropped rather than guessed, and the result says so. A non-integer
     term is flagged inline and listed in `invalid`, and the commit refuses to start. */
  function filledActionRows() {
    const c = conversion;
    const pending = c.actions_needing_input || [];
    const filled = [];
    const invalid = [];
    pending.forEach((p, idx) => {
      const fromEl = $('#bk-ratio-from-' + idx);
      const toEl = $('#bk-ratio-to-' + idx);
      const from = ((fromEl || {}).value || '').trim();
      const to = ((toEl || {}).value || '').trim();
      if (!from && !to) { flagRatio(fromEl, false); flagRatio(toEl, false); return; }
      const badFrom = !ratioValid(from);
      const badTo = !ratioValid(to);
      flagRatio(fromEl, badFrom);
      flagRatio(toEl, badTo);
      if (badFrom || badTo) { invalid.push(idx); return; }
      if (!p.from_symbol) return;
      filled.push([$('#bk-account').value, p.date, p.kind, p.from_symbol, p.to_symbol,
        to, from, '', p.refs.join(' · ')]);
    });
    filledActionRows.invalid = invalid;
    return filled;
  }

  function completedActionsCsv() {
    const c = conversion;
    const ready = (c.files && c.files.corporate_actions) || '';
    const filled = filledActionRows();
    if (!filled.length) return ready;
    /* The header comes from the SERVER (`worksheet_headers`), never from a copy kept here:
       a second column list in JS drifts from the parser's, and the symptom is an import
       that rejects every row for a reason nobody can see. */
    const header = c.worksheet_headers.corporate_actions;
    const body = ready ? ready.slice(header.length + 2) : '';
    return header + '\r\n' + body
      + filled.map((r) => r.map(csvCell).join(',') + '\r\n').join('');
  }

  /* The action file the TRADES are validated against: only the ticked ready rows (from the
     server's own `cells`, never parsed out of text) plus the filled worksheet rows. An
     action the owner unticked is not arriving, so it must not legalise a sell. */
  function pendingActionsCsv() {
    const c = conversion;
    const ready = ((c.rows && c.rows.corporate_actions) || [])
      .filter((r) => ticks.corporate_actions && ticks.corporate_actions.has(r.i))
      .map((r) => r.cells);
    const rows = ready.concat(filledActionRows());
    if (!rows.length) return '';
    return c.worksheet_headers.corporate_actions + '\r\n'
      + rows.map((r) => r.map(csvCell).join(',') + '\r\n').join('');
  }

  function completedOpeningsCsv() {
    const c = conversion;
    const rows = [];
    (c.openings_needing_cost || []).forEach((o, idx) => {
      if (o.satisfied) return;                       // the ledger already holds it
      const cost = ($('#bk-opening-cost-' + idx) || {}).value;
      /* The GAP, not the file's figure: the difference between what the statement needs
         and what the ledger already holds (DEF-027). */
      if (!cost || !o.gap) return;
      rows.push([$('#bk-account').value, o.symbol, o.gap, cost, c.openings_build_date || '', '']);
    });
    if (!rows.length) return '';
    return c.worksheet_headers.openings + '\r\n'
      + rows.map((r) => r.map(csvCell).join(',') + '\r\n').join('');
  }

  function csvCell(v) {
    const s = String(v === null || v === undefined ? '' : v);
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  /* The commit plan: one step per kind, in the server's order, each with the CSV text the
     server produced and the `select` indices the owner ticked. */
  function buildPlan() {
    const c = conversion;
    const actionsCsv = completedActionsCsv();
    const openingsCsv = completedOpeningsCsv();
    const plan = [];
    if (openingsCsv) plan.push({ kind: 'openings', text: openingsCsv, select: null, rows: [] });
    (c.commit_order || []).forEach((kind) => {
      if (kind === 'openings') return;            // supplied above, from the form
      const rows = (c.rows && c.rows[kind]) || [];
      const ticked = rows.filter((r) => ticks[kind] && ticks[kind].has(r.i)).map((r) => r.i);
      if (kind === 'corporate_actions') {
        /* The filled worksheet rows sit AFTER the ready rows in the file, and are always
           selected — the owner typed them in. */
        const filled = filledActionRows().map((_, k) => rows.length + k);
        const select = ticked.concat(filled);
        if (select.length && actionsCsv) plan.push({ kind, text: actionsCsv, select, rows });
        return;
      }
      const text = c.files[kind];
      if (ticked.length && text) plan.push({ kind, text, select: ticked, rows });
    });
    return plan;
  }

  /* The display fields of row `n` of a step — for the warning dialog. Row `n` of the
     committed file is the server's row `n` (same order, same index), or one of the filled
     worksheet rows past the ready ones. */
  function rowLabel(step, n) {
    const r = step.rows.find((x) => x.i === n);
    if (r) return { symbol: r.symbol, date: r.date, shares: r.shares, type: TYPE_ZH[r.type] || r.type };
    if (step.kind === 'corporate_actions') {
      const p = (conversion.actions_needing_input || [])[n - step.rows.length];
      if (p) return { symbol: p.to_symbol, date: p.date, shares: '', type: TYPE_ZH[p.kind] || p.kind };
    }
    if (step.kind === 'openings') {
      return { symbol: '期初庫存', date: conversion.openings_build_date || '', shares: '', type: '' };
    }
    return { symbol: '', date: '', shares: '', type: '' };
  }

  /* ★ DEF-027: the acknowledgement dialog. Every warning row, one per line, with the
     server's own message and — for a 賣超 — the consequence in words; each checkbox starts
     UNTICKED. Resolves to one of:
       { mode: 'ack',  keep: Set<n> }  write the ticked warning rows under the ack
       { mode: 'skip' }                write only the rows without a warning
       { mode: 'cancel' }              stop the run here
     Built on the app's own modal (the same classes shell.js's confirmDialog and input.js's
     賣超 dialog use), never on window.confirm — one line of text cannot list ten rows. */
  function warningsDialog(step, warns) {
    return new Promise((resolve) => {
      const backdrop = el('div', 'modal-backdrop');
      const modal = el('div', 'modal');
      modal.style.width = 'min(680px, calc(100vw - 40px))';
      const head = el('div', 'modal-head');
      head.appendChild(el('h3', 'modal-title', '匯入警告確認 —— ' + (KIND_ZH[step.kind] || step.kind)));
      const x = el('button', 'modal-close', '✕');
      x.type = 'button';
      head.appendChild(x);
      modal.appendChild(head);
      const body = el('div', 'modal-body');
      body.appendChild(el('div', null,
        '⚠ 以下 ' + warns.length + ' 列在寫入前檢核時有警告。預設不寫入；只有你勾選並確認的列才會帶著警告寫入，'
        + '其餘列照常寫入。'));
      body.appendChild(el('div', 'hint',
        '賣超列一旦確認：這個部位的成本基礎會被永久捨棄，並在儀表板標示為待釐清；之後再買回也不會還原。'
        + '如果是分割／換股／分拆造成的，請先取消、補登公司行動，再重新上傳整份對帳單。'));
      const wrap = el('div', 'table-wrap');
      const table = el('table', 'data');
      const thead = el('thead');
      const hr = el('tr');
      hr.appendChild(el('th', null, ''));
      [['代號', 'col-text'], ['日期', 'col-text'], ['類型', 'col-text'], ['股數', 'num'], ['警告', 'col-text']]
        .forEach(([label, cls]) => hr.appendChild(el('th', cls, label)));
      thead.appendChild(hr);
      table.appendChild(thead);
      const tbody = el('tbody');
      const keep = new Set();
      const ok = el('button', 'btn btn-danger', '寫入勾選的警告列');
      ok.type = 'button';
      ok.disabled = true;
      warns.forEach((w) => {
        const lab = rowLabel(step, w.n);
        const tr = el('tr');
        const td = el('td');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = false;                            // never pre-ticked
        cb.className = 'bk-warn-tick';
        cb.dataset.n = String(w.n);
        cb.addEventListener('change', () => {
          if (cb.checked) keep.add(w.n); else keep.delete(w.n);
          ok.disabled = keep.size === 0;
        });
        td.appendChild(cb);
        tr.appendChild(td);
        tr.appendChild(el('td', 'col-text', lab.symbol || '—'));
        tr.appendChild(el('td', 'col-text', lab.date || '—'));
        tr.appendChild(el('td', 'col-text', lab.type || '—'));
        tr.appendChild(el('td', 'num', lab.shares || '—'));
        const why = el('td', 'col-text');
        why.appendChild(el('div', null, w.reason || '有警告'));
        /* I-13: the preview row carries its findings' KINDS (`kinds`), so the 賣超 line is
           keyed on `sell_exceeds_holdings` — it used to pattern-match the server's
           sentence 「賣出 N 股，超過…」, which any rewording would have silently broken. */
        if ((w.kinds || []).indexOf('sell_exceeds_holdings') !== -1) {
          why.appendChild(el('div', 'hint', '→ 賣超：確認後成本基礎永久捨棄（待釐清），之後再買回也不會還原'));
        }
        tr.appendChild(why);
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      wrap.appendChild(table);
      body.appendChild(wrap);
      modal.appendChild(body);
      const foot = el('div', 'modal-foot');
      const cancel = el('button', 'btn', '取消，停在這一步');
      cancel.type = 'button';
      const skip = el('button', 'btn', '略過所有警告列，只寫入其他列');
      skip.type = 'button';
      foot.appendChild(cancel);
      foot.appendChild(skip);
      foot.appendChild(ok);
      modal.appendChild(foot);
      backdrop.appendChild(modal);
      const finish = (decision) => { backdrop.remove(); resolve(decision); };
      x.addEventListener('click', () => finish({ mode: 'cancel' }));
      cancel.addEventListener('click', () => finish({ mode: 'cancel' }));
      backdrop.addEventListener('click', (e) => { if (e.target === backdrop) finish({ mode: 'cancel' }); });
      skip.addEventListener('click', () => finish({ mode: 'skip' }));
      ok.addEventListener('click', () => finish({ mode: 'ack', keep: new Set(keep) }));
      document.body.appendChild(backdrop);
    });
  }

  /* One kind, committed. Unacknowledged first; on `warnings_unacknowledged` the warning
     rows are fetched, shown, and only the ticked ones are re-sent — narrowed through
     `select`, under the ack. Returns the commit response, or { cancelled: true }. */
  async function commitStep(step, actionsCsv) {
    let select = step.select;
    let ack = false;
    for (;;) {
      const body = {
        kind: step.kind, csv_text: step.text, ack_warnings: ack,
        source_name: files.map((f) => f.name).join('+'),
        broker: $('#bk-broker').value,
      };
      if (select !== null) body.select = select;
      /* Trades are validated against the actions arriving in the SAME run — see the module
         header. Without this the guard measures a post-split sell against a pre-split count
         and demands the one acknowledgement that discards a cost basis. */
      if (step.kind === 'transactions' && actionsCsv) body.pending_actions_csv = actionsCsv;
      try {
        return await api.post('/api/import/commit', body);
      } catch (err) {
        if (!(err && err.status === 422 && err.code === 'warnings_unacknowledged' && !ack)) throw err;
        const pvBody = { kind: step.kind, csv_text: step.text };
        if (step.kind === 'transactions' && actionsCsv) pvBody.pending_actions_csv = actionsCsv;
        const pv = await api.post('/api/import/preview', pvBody);
        const chosen = select === null ? null : new Set(select);
        const warns = ((pv && pv.rows) || [])
          .filter((r) => r.status === 'warn' && (chosen === null || chosen.has(r.n)))
          .map((r) => ({ n: r.n, reason: r.reason, kinds: r.kinds || [] }));
        if (!warns.length) {
          /* Every warning row is already deselected — nothing to confirm. The ack only
             releases the server's whole-file gate; the rows it would cover are not sent. */
          ack = true;
          continue;
        }
        const decision = await warningsDialog(step, warns);
        if (decision.mode === 'cancel') return { cancelled: true };
        const warnIdx = new Set(warns.map((w) => w.n));
        const base = chosen === null ? ((pv && pv.rows) || []).map((r) => r.n) : Array.from(chosen);
        select = base.filter((n) => !warnIdx.has(n) || (decision.mode === 'ack' && decision.keep.has(n)));
        ack = true;
        if (!select.length) return { written: 0, skipped: base.length };
      }
    }
  }

  async function commitAll() {
    const c = conversion;
    if (!c || !c.ok) return;
    const plan = buildPlan();
    if (filledActionRows.invalid && filledActionRows.invalid.length) {
      if (window.toast) window.toast('公司行動的比例必須是正整數', 'fail', '請修正標示的欄位後再寫入');
      return;
    }
    if (!plan.length) {
      if (window.toast) window.toast('沒有可寫入的列', 'fail', '請勾選至少一列，或填入期初庫存的成本');
      return;
    }
    const btn = $('#bk-commit');
    const restore = window.pdBusy ? window.pdBusy(btn, '寫入中…') : () => {};
    const actionsCsv = pendingActionsCsv();

    const results = [];
    let stopped = null;
    let cancelled = false;
    for (const step of plan) {
      try {
        const r = await commitStep(step, actionsCsv);
        if (r && r.cancelled) { cancelled = true; stopped = step.kind; break; }
        results.push([step.kind, r]);
        /* A row the owner wanted and did not get — refused (rejected) OR dropped at the
           re-check (a blocked skipped_rows entry) — stops the sequence: the later kinds are
           checked against positions the earlier ones build (I-8). */
        if (stepSummary(step.kind, r).stopped) { stopped = step.kind; break; }
      } catch (err) {
        results.push([step.kind, { error: (err && err.message) || '寫入失敗' }]);
        stopped = step.kind;
        break;
      }
    }
    restore();
    renderCommitResult(results, stopped, plan.length, cancelled);
    await loadBatches();
    if (window.pdLedgerRefresh) {                       // see the note on the undo path
      try { await window.pdLedgerRefresh(); } catch (e) { /* degrade silently */ }
    }
  }

  /* I-8 (DEF-024's second door): one step's result as the owner reads it. `skipped` used to
     be printed bare as 「跳過 N 筆」, which covered two different events — rows the owner left
     unticked, and rows the re-check dropped (a sell whose covering buy was deselected) — so
     a step that wrote nothing it was asked to read like a choice. The split is
     input.js's commitOutcome (window.pdCommitOutcome, shared on this page): 「未勾選」 for the
     owner's choice, 「被擋下」 with one line per row and the server's reason for the rest. */
  function stepSummary(kind, r, co) {
    const reader = co || window.pdCommitOutcome;
    const label = KIND_ZH[kind] || kind;
    if (!reader) {
      /* input.js absent (never on trades.html): counts only, and nothing claimed as skipped. */
      return { text: label + ' 寫入 ' + (r.written || 0) + ' 筆', lines: [],
               stopped: (r.rejected || 0) };
    }
    const o = reader.read(r);
    const bits = [label + ' 寫入 ' + o.written + ' 筆'];
    if (o.duplicates) bits.push('已匯入過 ' + o.duplicates + ' 筆');
    if (o.deselected) bits.push('未勾選 ' + o.deselected + ' 筆');
    const stopped = o.rejected + o.blocked.length;
    if (stopped) bits.push('被擋下 ' + stopped + ' 筆');
    return { text: bits.join('・'), lines: reader.lines(o), stopped: stopped };
  }

  function renderCommitResult(results, stopped, planned, cancelled) {
    const box = $('#bk-report');
    const card = el('div', 'result-banner');
    card.appendChild(el('div', null, stopped ? (cancelled ? '⚠ 寫入取消' : '⚠ 寫入中止') : '✓ 寫入完成'));
    results.forEach(([kind, r]) => {
      if (r.error) {
        card.appendChild(el('div', 'panel-sub', (KIND_ZH[kind] || kind) + '：' + r.error));
        return;
      }
      const s = stepSummary(kind, r);
      card.appendChild(el('div', 'panel-sub', s.text));
      s.lines.slice(0, 10).forEach((line) => {
        card.appendChild(el('div', 'hint', '　' + line));
      });
    });
    if (stopped) {
      card.appendChild(el('div', 'panel-sub',
        '在「' + (KIND_ZH[stopped] || stopped) + '」這一步' + (cancelled ? '取消了' : '停下來了') + '，後面 '
        + (planned - results.length - (cancelled ? 1 : 0)) + ' 個類型沒有送出。'
        + '後面的資料要對得上前面建立的部位，所以不會在缺一段的情況下硬寫進去。'));
      card.appendChild(el('div', 'hint',
        '已經寫進去的批次列在下方，可以逐批「復原」回到匯入前的狀態。'
        + '修正問題後重新上傳整份匯出檔即可 —— 已寫入的列會自動略過，不會變成兩筆。'));
    }
    box.replaceChildren(card);
    ticks = {};
    setCommitEnabled(false);
  }

  // ---------------------------------------------------------------- init

  /* DEF-029: the account picker lists ONLY the accounts whose broker matches the chosen
     statement format (`accounts_by_broker`, owned by the server's registry). A Schwab file
     cannot be aimed at the Moomoo or the TW account from this page at all; the server
     refuses the pairing independently, so the two never disagree. */
  function fillAccountOptions() {
    const bsel = $('#bk-broker');
    const asel = $('#bk-account');
    const names = window.pdNames;
    const allowed = accountsByBroker[bsel.value] || [];
    const keep = asel.value;
    asel.replaceChildren();
    const eligible = accounts.filter((a) => allowed.indexOf(a.id) !== -1);
    if (!eligible.length) {
      const o = el('option', null, '（沒有屬於這家券商的帳戶）');
      o.value = '';
      o.disabled = true;
      o.selected = true;
      asel.appendChild(o);
      return;
    }
    eligible.forEach((a) => {
      const o = el('option', null, names ? names.accountOption(a) : a.id);
      o.value = a.id;
      asel.appendChild(o);
    });
    asel.value = eligible.some((a) => a.id === keep) ? keep : eligible[0].id;
  }

  async function initBroker() {
    const bsel = $('#bk-broker');
    const asel = $('#bk-account');
    if (!bsel || !asel) return;
    try {
      const [b, ctx] = await Promise.all([
        api.get('/api/broker/adapters'),
        api.get('/api/input/context'),
      ]);
      /* M5-b (demo audit, second full re-verification 2026-09-22): both pickers take their
         labels from web/names.js. They were the last surface rendering the context list's
         English `a.name` + the raw id — 「TW Broker（tw_broker）」 on the same page as
         #m-account's 「台灣券商（TWD）」 — because the guard's file list named cash.js and
         input.js by hand and never reached this file. The id is the no-names.js fallback. */
      const names = window.pdNames;
      (b.brokers || []).forEach((id) => {
        const o = el('option', null, names ? names.broker(id) : id);
        o.value = id;
        bsel.appendChild(o);
      });
      accountsByBroker = (b && b.accounts_by_broker) || {};
      accounts = (ctx && ctx.accounts) || [];
      fillAccountOptions();
    } catch (err) { /* the pane still renders; the selects are simply empty */ }

    const note = $('#bk-note');
    if (note) note.textContent = '只列出屬於所選券商的帳戶。轉換後仍會逐列檢核，寫入的每一批都可以復原。';

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
    bsel.addEventListener('change', () => { fillAccountOptions(); if (files.length) runConvert(); });
    $('#bk-commit').addEventListener('click', commitAll);
    $('#bk-clear').addEventListener('click', () => {
      files = []; conversion = null; ticks = {};
      setFiles();
      $('#bk-report').replaceChildren();
      setCommitEnabled(false);
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
