/* portfolio-dash — 輸入中心 (wired to /api/input/* + /api/import/*, spec 19/12).

   The five input modes (manual transaction · CSV import · AI input · dividend ·
   FX/opening) all source their structural data — accounts, instruments, fee-rule
   context, holdings — from GET /api/input/context (no more window.INPUT_DATA).

   MONEY DISCIPLINE (spec data-and-pricing.md):
   - SERVER-returned amounts (manual-preview fee/tax/gross/total, CSV preview-row
     amounts, AI cost_usd) arrive as Decimal STRINGS and are rendered via window.fmt
     ONLY — never `bareString.toFixed()`. The frontend NEVER computes money of record.
   - USER-INPUT local estimates (the fee/tax prefill while typing, the DRIP net calc,
     the FX implied-rate what-if) operate on the user's own numeric entry and are SENT
     to the backend, which then computes the value of record. Those `.toFixed` calls on
     user-entered numbers are the documented input-side exception and are retained.

   Write paths:
   - Manual transaction: live preview (POST /input/manual/preview) + commit
     (POST /input/manual/commit; 422 unacked-oversell -> confirmDialog -> re-commit
     with ack_oversell:true).
   - CSV import: POST /import/preview (real table) + POST /import/commit ({written,skipped}).
   - AI input: POST /input/ai/preview (preview + meta; 402/409/503 -> degraded toast).
   - Dividend / FX-conversion / Opening-inventory have NO single-entry commit endpoint
     (those ledgers are written via the CSV import path). Their dropdowns are sourced
     from /input/context and their local calc previews are kept, but the COMMIT button is
     a clearly-labelled design-preview toast pointing the user at CSV import. */
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

  /* Structural context from GET /api/input/context (replaces window.INPUT_DATA).
     Starts empty so any pre-fetch render is blank; populated on boot. */
  let ctx = { accounts: [], fee_rules: {}, instruments: [], holdings: {} };
  const acc = (id) => ctx.accounts.find((a) => a.id === id);
  const inst = (sym) => {
    const s = (sym || '').trim();
    const up = s.toUpperCase();
    return ctx.instruments.find((i) => i.symbol === up || i.symbol === s);
  };

  /* ===== tabs ===== */
  const TABS = ['manual', 'csv', 'ai', 'div', 'fxopen'];
  function showTab(t) {
    TABS.forEach((x) => {
      const pane = $('#pane-' + x);
      const tab = $('#tab-' + x);
      if (pane) pane.classList.toggle('active', x === t);
      if (tab) tab.classList.toggle('active', x === t);
    });
  }
  TABS.forEach((t) => {
    const tab = $('#tab-' + t);
    if (tab) tab.addEventListener('click', () => showTab(t));
  });

  /* ================= Tab 1 手動交易 ================= */
  const m = { side: 'buy', feeOverride: false, taxOverride: false, acked: false };
  /* Latest server preview (Decimal STRINGS) — null until the first preview lands. */
  let mPreview = null;
  /* Today (local) — the natural default trade date; retires the design-stub
     2026-06-11 / 2330 / 1000 / 612.5 fake prefill (2026-07-02). */
  const TODAY = (() => {
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  })();

  function initManual() {
    const accSel = $('#m-account');
    ctx.accounts.forEach((a) => {
      const o = el('option', null, a.name + '（' + a.ccy + '）');
      o.value = a.id;
      accSel.appendChild(o);
    });
    const dl = $('#m-symbols');
    ctx.instruments.forEach((i) => {
      const o = el('option'); o.value = i.symbol; o.label = i.name;
      dl.appendChild(o);
    });
    $('#m-date').value = TODAY;
    $('#m-symbol').value = '';
    $('#m-shares').value = '';
    $('#m-price').value = '';

    $('#m-side-buy').addEventListener('click', () => setSide('buy'));
    $('#m-side-sell').addEventListener('click', () => setSide('sell'));
    ['m-account', 'm-symbol', 'm-shares', 'm-price', 'm-date'].forEach((id) => {
      $('#' + id).addEventListener('input', schedulePreview);
    });
    $('#m-fee-pencil').addEventListener('click', () => {
      m.feeOverride = true;
      $('#m-fee').readOnly = false;
      $('#m-fee').focus();
      schedulePreview();
    });
    $('#m-tax-pencil').addEventListener('click', () => {
      m.taxOverride = true;
      $('#m-tax').readOnly = false;
      $('#m-tax').focus();
      schedulePreview();
    });
    $('#m-fee').addEventListener('input', schedulePreview);
    $('#m-tax').addEventListener('input', schedulePreview);
    $('#m-confirm').addEventListener('click', commitManual);
    schedulePreview();
  }
  function setSide(s) {
    m.side = s;
    $('#m-side-buy').classList.toggle('active', s === 'buy');
    $('#m-side-buy').classList.toggle('buy-on', s === 'buy');
    $('#m-side-sell').classList.toggle('active', s === 'sell');
    $('#m-side-sell').classList.toggle('sell-on', s === 'sell');
    schedulePreview();
  }

  /* Build the ManualBody for /input/manual/preview & /commit. fee/tax overrides ride
     through as the user's raw string (the input-side numeric is sent; the backend
     computes the value of record). Empty/blank => omit (let the backend auto-fill). */
  function manualBody() {
    const sym = $('#m-symbol').value.trim();
    const sharesRaw = $('#m-shares').value.trim();
    const priceRaw = $('#m-price').value.trim();
    const body = {
      account_id: $('#m-account').value || (ctx.accounts[0] && ctx.accounts[0].id) || '',
      symbol: sym,
      side: m.side,
      date: $('#m-date').value || TODAY,
      shares: sharesRaw === '' ? '0' : sharesRaw,
      price: priceRaw === '' ? '0' : priceRaw,
    };
    if (m.feeOverride) {
      const fv = $('#m-fee').value.trim();
      if (fv !== '') body.fee_override = fv;
    }
    if (m.taxOverride) {
      const tv = $('#m-tax').value.trim();
      if (tv !== '') body.tax_override = tv;
    }
    return body;
  }

  /* Debounce the live preview so each keystroke does not fire a request. */
  let previewTimer = null;
  function schedulePreview() {
    renderSymbolHint();          // local, instant
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(runManualPreview, 180);
  }

  function renderSymbolHint() {
    const sym = $('#m-symbol').value.trim();
    const it = inst(sym);
    const symHint = $('#m-sym-hint');
    symHint.replaceChildren();
    if (sym && !it) {
      symHint.textContent = '未註冊 — 寫入時將自動查詢並註冊（依帳戶判定市場）';
    } else if (it) {
      symHint.textContent = it.name + '・' + it.ccy + (it.etf ? '・ETF' : '');
    }
  }

  /* Fetch the server preview (computed fee/tax + issues) and render. Local-only field
     validation (empty symbol / non-positive shares-price) short-circuits before the
     network call so the obviously-invalid draft does not spam the endpoint. */
  async function runManualPreview() {
    const a = acc($('#m-account').value) || ctx.accounts[0];
    if (!a) { renderManual(null, [], false); return; }
    const sym = $('#m-symbol').value.trim();
    const shares = Number($('#m-shares').value) || 0;
    const price = Number($('#m-price').value) || 0;

    /* pristine form (boots empty since 2026-07-02): no red errors on an untouched
       page — render the neutral empty state with the confirm disabled. */
    if (!sym && $('#m-shares').value.trim() === '' && $('#m-price').value.trim() === '') {
      mPreview = null;
      renderManual(null, [], false);
      return;
    }

    const localIssues = [];
    if (!sym) localIssues.push({ sev: 'error', text: '請輸入代號', field: 'm-symbol' });
    if (shares <= 0) localIssues.push({ sev: 'error', text: '股數必須大於 0', field: 'm-shares' });
    if (price <= 0) localIssues.push({ sev: 'error', text: '價格必須大於 0', field: 'm-price' });
    if (localIssues.length) {
      mPreview = null;
      renderManual(null, localIssues, false);
      return;
    }

    const ctrl = api.abortable('manual-preview');
    let resp;
    try {
      resp = await api.post('/api/input/manual/preview', manualBody(), { signal: ctrl.signal });
    } catch (err) {
      if (err && err.name === 'AbortError') return;  // superseded by a newer keystroke
      mPreview = null;
      renderManual(null, [{ sev: 'error', text: (err && err.message) || '預覽失敗', field: null }], false);
      return;
    }
    mPreview = resp;
    /* Server amounts are Decimal STRINGS -> reflect computed fee/tax into the
       (read-only) input fields via fmt; when overridden, the user's own value stays. */
    const ccy = a.ccy;
    if (!m.feeOverride) $('#m-fee').value = resp.fee !== undefined ? f.money(resp.fee, ccy) : '0';
    if (!m.taxOverride) $('#m-tax').value = resp.tax !== undefined ? f.money(resp.tax, ccy) : '0';
    if (resp.fee_rule_label) $('#m-fee-rule').textContent = resp.fee_rule_label;
    renderManual(resp, (resp.issues || []), true);
  }

  /* Render the preview card + issues from the SERVER preview (or local-only issues
     when the draft is too incomplete to send). `serverOk` => a valid server preview is
     present; the confirm button enables only then with no hard issues + ack satisfied. */
  function renderManual(preview, issues, serverOk) {
    const a = acc($('#m-account').value) || ctx.accounts[0];
    const ccy = a ? a.ccy : '';
    $('#m-fee-ovr').hidden = !m.feeOverride;
    $('#m-tax-ovr').hidden = !m.taxOverride;

    /* split server issues: hard (error) gates the confirm; soft (warn, e.g. oversell)
       needs an ack; info (e.g. 未註冊將自動註冊) is a notice only — never gates. */
    const hard = issues.filter((i) => i.sev === 'error');
    const soft = issues.filter((i) => i.sev === 'warn');
    const infos = issues.filter((i) => i.sev === 'info');
    const oversell = soft.find((i) => i.code === 'sell_exceeds_holdings') || soft[0] || null;

    /* field-error highlight from issue.field (mapped to the m-* input ids) */
    const FIELD_ID = { symbol: 'm-symbol', shares: 'm-shares', price: 'm-price' };
    ['m-symbol', 'm-shares', 'm-price'].forEach((id) => $('#' + id).classList.remove('field-error'));
    issues.forEach((i) => {
      const id = i.field && (FIELD_ID[i.field] || (i.field.indexOf('m-') === 0 ? i.field : null));
      if (id) $('#' + id).classList.add('field-error');
    });

    /* preview card big value + rows from SERVER Decimal strings (via fmt). The card
       shows the MAGNITUDE (the 總成本 / 淨收款 label carries the sign meaning); the
       backend `total` is negative for BUY (cashflow sign), so strip a leading minus as
       a STRING op — no arithmetic on the money string — before handing it to fmt. */
    const hasServer = serverOk && preview && preview.total !== undefined && preview.total !== null;
    const totalAbs = hasServer ? String(preview.total).replace(/^-/, '') : null;
    $('#m-pc-label').textContent = m.side === 'buy' ? '總成本（含費稅）' : '淨收款（扣費稅）';
    $('#m-pc-value').textContent = hasServer ? f.money(totalAbs, ccy) : f.NULL_GLYPH;
    $('#m-pc-ccy').textContent = ccy;
    const rows = $('#m-pc-rows');
    rows.replaceChildren();
    if (hasServer) {
      [['成交金額', preview.gross], ['手續費' + (m.feeOverride ? '（已覆寫）' : ''), preview.fee],
       ['交易稅' + (m.taxOverride ? '（已覆寫）' : ''), preview.tax]].forEach(([k, v]) => {
        const row = el('div', 'pc-row');
        row.appendChild(el('span', 'k', k));
        row.appendChild(el('span', 'v', f.money(v, ccy) + ' ' + ccy));
        rows.appendChild(row);
      });
    }

    /* issue list */
    const issueBox = $('#m-issues');
    issueBox.replaceChildren();
    hard.forEach((i) => {
      const div = el('div', 'issue issue-error');
      div.appendChild(el('span', null, '✕'));
      div.appendChild(el('span', null, i.text));
      issueBox.appendChild(div);
    });
    infos.forEach((i) => {
      const div = el('div', 'issue issue-info');
      div.appendChild(el('span', null, 'ℹ'));
      div.appendChild(el('span', null, i.text));
      issueBox.appendChild(div);
    });
    let ackOk = true;
    if (oversell) {
      const div = el('div', 'issue issue-warn');
      div.appendChild(el('span', null, '⚠'));
      const lab = el('label');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.id = 'm-ack';
      cb.checked = m.acked;
      cb.addEventListener('change', () => { m.acked = cb.checked; renderManual(mPreview, issues, serverOk); });
      lab.appendChild(cb);
      lab.appendChild(el('span', null, oversell.text + ' 我了解，仍要寫入。'));
      div.appendChild(lab);
      issueBox.appendChild(div);
      ackOk = m.acked;
    } else {
      m.acked = false;
    }
    if (hasServer && !hard.length && !oversell) {
      const div = el('div', 'issue issue-ok');
      div.appendChild(el('span', null, '✓'));
      div.appendChild(el('span', null, '草稿檢核通過，可寫入'));
      issueBox.appendChild(div);
    }
    $('#m-confirm').disabled = !hasServer || hard.length > 0 || !ackOk;
  }

  /* Commit the manual transaction. 201 -> success toast + reset draft state; 422
     oversell_unacknowledged -> confirmDialog -> re-commit with ack_oversell:true;
     400 / other PdApiError -> error toast carrying the backend message + code. */
  async function commitManual() {
    const body = manualBody();
    body.ack_oversell = m.acked;
    /* busy state: the commit may auto-register an unknown symbol (real provider
       fetch, seconds) — the button must show that work, not appear frozen. */
    const restore = window.pdBusy ? window.pdBusy($('#m-confirm'), '寫入中…') : () => {};
    try {
      const resp = await api.post('/api/input/manual/commit', body);
      restore();
      onManualWritten(resp);
    } catch (err) {
      restore();
      if (err && err.status === 422 && err.code === 'oversell_unacknowledged') {
        const msg = (err.issues && err.issues[0] && err.issues[0].text) || '賣出股數超過持有 — 確認後寫入？';
        window.confirmDialog({
          title: '賣超確認',
          body: msg + '（輸入錯誤還是放空？）',
          confirmLabel: '我了解，仍要寫入',
          danger: true,
          onConfirm: async () => {
            const acked = manualBody();
            acked.ack_oversell = true;
            try {
              const resp = await api.post('/api/input/manual/commit', acked);
              onManualWritten(resp);
            } catch (e2) {
              if (window.toast) window.toast((e2 && e2.message) || '寫入失敗', 'fail', e2 && e2.code);
            }
          }
        });
        return;
      }
      if (window.toast) window.toast((err && err.message) || '寫入失敗', 'fail', err && err.code);
    }
  }

  function onManualWritten(resp) {
    if (window.toast) {
      const id = resp && resp.txn_id !== undefined ? '（#' + resp.txn_id + '）' : '';
      const ar = resp && resp.auto_registered;
      const arTxt = ar
        ? '；已自動註冊 ' + ar.symbol + (ar.name ? ' ' + ar.name : '') +
          (ar.last != null ? '（現價 ' + ar.last + '）' : '')
        : '';
      window.toast('寫入成功', 'ok', '交易已寫入帳本 ' + id + arTxt);
    }
    /* reset draft state and re-preview a clean form */
    m.feeOverride = false; m.taxOverride = false; m.acked = false;
    $('#m-fee').readOnly = true; $('#m-tax').readOnly = true;
    $('#m-shares').value = '';
    $('#m-price').value = '';
    schedulePreview();
  }

  /* ================= Tab 2 CSV 匯入 ================= */
  /* kind chips map the UI label to the import endpoint `kind`. */
  const CSV_KINDS = [['交易', 'transactions'], ['股利', 'dividends'], ['換匯', 'fx'], ['期初', 'openings']];
  let csvKind = 'transactions';

  function initCsv() {
    const bar = $('#csv-kinds');
    CSV_KINDS.forEach(([label, kind], i) => {
      const c = el('button', 'chip' + (i === 0 ? ' active' : ''), label);
      c.type = 'button';
      c.addEventListener('click', () => {
        bar.querySelectorAll('.chip').forEach((x) => x.classList.remove('active'));
        c.classList.add('active');
        csvKind = kind;
        const note = $('#csv-kind-note');
        if (note) note.textContent = kind === 'transactions' ? '' : '（' + label + ' CSV：解析同此模式）';
      });
      bar.appendChild(c);
    });

    const paste = $('#csv-paste');
    if (paste) paste.addEventListener('input', scheduleCsvPreview);
    $('#csv-confirm').addEventListener('click', commitCsv);
    $('#csv-confirm').disabled = true;
  }

  let csvTimer = null;
  function scheduleCsvPreview() {
    if (csvTimer) clearTimeout(csvTimer);
    csvTimer = setTimeout(runCsvPreview, 250);
  }

  async function runCsvPreview() {
    const paste = $('#csv-paste');
    const csvText = paste ? paste.value.trim() : '';
    const tbody = $('#csv-body');
    if (!csvText) {
      if (tbody) tbody.replaceChildren();
      $('#csv-counts').textContent = '';
      $('#csv-file').textContent = '';
      $('#csv-confirm').disabled = true;
      return;
    }
    let resp;
    try {
      resp = await api.post('/api/import/preview', { kind: csvKind, csv_text: csvText });
    } catch (err) {
      if (window.toast) window.toast((err && err.message) || '解析失敗', 'fail', err && err.code);
      return;
    }
    renderCsvPreview(resp);
  }

  /* Render the REAL preview table from the server rows {n, status, reason, data}.
     The per-row money in `data` (price / shares / fee / tax) is Decimal STRINGS now,
     so amounts go through fmt / Number — NOT `.toFixed()` on a wire string (Finding 5). */
  function renderCsvPreview(preview) {
    $('#csv-file').textContent = '貼上 CSV';
    const tbody = $('#csv-body');
    tbody.replaceChildren();
    const ST = { ok: ['✓ 可寫入', 'st-ok'], warn: ['⚠ 警告', 'st-warn'], error: ['✕ 錯誤', 'st-error'] };
    (preview.rows || []).forEach((r) => {
      const d = r.data || {};
      const tr = el('tr', r.status === 'error' ? 'row-error' : '');
      const tdCb = el('td');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.checked = r.status !== 'error';
      cb.disabled = r.status === 'error';
      tdCb.appendChild(cb);
      tr.appendChild(tdCb);
      tr.appendChild(el('td', 'num', '#' + ((r.n || 0) + 1)));
      tr.appendChild(el('td', 'num', f.date(d.trade_date || d.date)));
      tr.appendChild(el('td', 'col-text', d.account_id || d.account || ''));
      const side = (d.side || '').toString().toLowerCase();
      const tdSide = el('td', 'col-text');
      tdSide.appendChild(el('span', 'dir-chip ' + (side === 'buy' ? 'dir-buy' : 'dir-sell'),
        side === 'buy' ? '買' : '賣'));
      tr.appendChild(tdSide);
      const symbol = d.symbol || '';
      tr.appendChild(el('td', 'col-text num', symbol));
      const it = inst(symbol);
      const ccy = it ? it.ccy : '';
      tr.appendChild(el('td', 'num', f.num(d.quantity !== undefined ? d.quantity : d.shares)));
      tr.appendChild(el('td', 'num', f.price(d.price, ccy)));   // Decimal string -> fmt
      const st = ST[r.status] || ST.ok;
      tr.appendChild(el('td', 'col-text ' + st[1], st[0]));
      tr.appendChild(el('td', 'err-msg', r.reason || ''));
      tbody.appendChild(tr);
    });
    const s = preview.summary || { ok: 0, warn: 0, error: 0 };
    $('#csv-counts').textContent =
      '可寫入 ' + (s.ok || 0) + '・警告 ' + (s.warn || 0) + '・錯誤 ' + (s.error || 0);
    /* confirm enables when there is anything non-error to write */
    $('#csv-confirm').disabled = ((s.ok || 0) + (s.warn || 0)) === 0;
  }

  /* Commit the pasted CSV. The backend re-derives from csv_text (re-validates vs the
     current ledger) and returns {written, skipped} as ints (safe). 422
     warnings_unacknowledged -> confirmDialog -> re-commit with ack_warnings:true. */
  async function commitCsv() {
    const paste = $('#csv-paste');
    const csvText = paste ? paste.value.trim() : '';
    if (!csvText) return;
    try {
      const resp = await api.post('/api/import/commit', { kind: csvKind, csv_text: csvText, ack_warnings: false });
      onCsvWritten(resp);
    } catch (err) {
      if (err && err.status === 422 && err.code === 'warnings_unacknowledged') {
        window.confirmDialog({
          title: '匯入警告確認',
          body: '部分列有警告（如賣超 / 模糊代號）— 確認後一併寫入？',
          confirmLabel: '確認寫入',
          onConfirm: async () => {
            try {
              const resp = await api.post('/api/import/commit', { kind: csvKind, csv_text: csvText, ack_warnings: true });
              onCsvWritten(resp);
            } catch (e2) {
              if (window.toast) window.toast((e2 && e2.message) || '匯入失敗', 'fail', e2 && e2.code);
            }
          }
        });
        return;
      }
      if (window.toast) window.toast((err && err.message) || '匯入失敗', 'fail', err && err.code);
    }
  }

  function onCsvWritten(resp) {
    const written = resp && resp.written !== undefined ? resp.written : 0;
    const skipped = resp && resp.skipped !== undefined ? resp.skipped : 0;
    const banner = $('#csv-result');
    if (banner) {
      banner.hidden = false;
      banner.replaceChildren();
      banner.appendChild(el('div', null, '✓ 寫入完成：成功 ' + written + ' 筆・跳過 ' + skipped + ' 筆'));
    }
    if (window.toast) window.toast('寫入成功', 'ok', '成功 ' + written + ' 筆・跳過 ' + skipped + ' 筆');
  }

  /* ================= Tab 3 AI 輸入 ================= */
  function initAi() {
    /* design-review state switcher (local-only preview of the degraded states) */
    const states = [['normal', '正常'], ['off', 'AI 未啟用'], ['quota', '額度用盡'], ['down', '服務不可用']];
    const sw = $('#ai-states');
    states.forEach(([id, label], i) => {
      const c = el('button', 'chip' + (i === 0 ? ' active' : ''), label);
      c.type = 'button';
      c.addEventListener('click', () => {
        sw.querySelectorAll('.chip').forEach((x) => x.classList.remove('active'));
        c.classList.add('active');
        showAiState(id);
      });
      sw.appendChild(c);
    });
    function showAiState(id) {
      $('#ai-normal').hidden = id !== 'normal';
      $('#ai-degrade-off').hidden = id !== 'off';
      $('#ai-degrade-quota').hidden = id !== 'quota';
      $('#ai-degrade-down').hidden = id !== 'down';
    }
    showAiState('normal');
    $('#ai-parse').addEventListener('click', runAiPreview);
    const writeAll = $('#ai-write-all');
    if (writeAll) writeAll.addEventListener('click', commitAi);
  }

  /* The CSV text the AI run returns; written via the import/commit path on 寫入全部. */
  let aiCsvText = '';

  /* Map a PdApiError code to the matching degraded panel. */
  function showAiDegrade(code) {
    const id = code === 'budget_exceeded' ? 'quota'
      : code === 'ai_not_activated' ? 'off'
        : 'down';
    $('#ai-normal').hidden = false;  // keep the result region; just clear the table
    $('#ai-degrade-off').hidden = id !== 'off';
    $('#ai-degrade-quota').hidden = id !== 'quota';
    $('#ai-degrade-down').hidden = id !== 'down';
  }

  async function runAiPreview() {
    const text = ($('#ai-text') && $('#ai-text').value || '').trim();
    if (!text) {
      if (window.toast) window.toast('請先貼上對帳單文字', 'fail');
      return;
    }
    let resp;
    try {
      resp = await api.post('/api/input/ai/preview', { text: text });
    } catch (err) {
      /* graceful degradation: 402 額度 / 409 未啟用 / 503 不可用 -> degraded panel + toast */
      if (err && (err.status === 402 || err.status === 409 || err.status === 503)) {
        showAiDegrade(err.code);
      }
      if (window.toast) window.toast((err && err.message) || 'AI 解析失敗', 'fail', err && err.code);
      return;
    }
    renderAiPreview(resp);
  }

  /* Render the AI preview rows + meta. cost_usd is a Decimal STRING -> f.num (never
     .toFixed). The per-row money in `data` is Decimal STRINGS -> fmt, same as CSV. */
  function renderAiPreview(preview) {
    $('#ai-degrade-off').hidden = true;
    $('#ai-degrade-quota').hidden = true;
    $('#ai-degrade-down').hidden = true;
    $('#ai-normal').hidden = false;
    aiCsvText = preview.csv_text || '';
    const meta = preview.meta || {};
    if ($('#ai-source')) {
      const cost = meta.cost_usd !== undefined && meta.cost_usd !== null
        ? '・成本 $' + f.num(meta.cost_usd, 4) : '';
      $('#ai-source').textContent = (meta.via || 'litellm') + cost;
    }
    if ($('#ai-model')) $('#ai-model').textContent = meta.model || '';

    const tbody = $('#ai-body');
    tbody.replaceChildren();
    (preview.rows || []).forEach((r) => {
      const d = r.data || {};
      const tr = el('tr');
      const tdCb = el('td');
      const cb = el('input'); cb.type = 'checkbox'; cb.checked = r.status !== 'error';
      cb.disabled = r.status === 'error';
      tdCb.appendChild(cb);
      tr.appendChild(tdCb);
      tr.appendChild(el('td', 'col-text', d.account_id || ''));
      tr.appendChild(el('td', 'col-text', f.date(d.trade_date || d.date)));
      const side = (d.side || '').toString().toLowerCase();
      const tdSide = el('td', 'col-text');
      tdSide.appendChild(el('span', 'dir-chip ' + (side === 'buy' ? 'dir-buy' : 'dir-sell'),
        side === 'buy' ? '買' : '賣'));
      tr.appendChild(tdSide);
      const symbol = d.symbol || '';
      const it = inst(symbol);
      const ccy = it ? it.ccy : '';
      const tdSym = el('td', 'col-text');
      const cell = el('div', 'sym-cell');
      cell.appendChild(el('span', 'sym-code', symbol));
      cell.appendChild(el('span', 'sym-name', it ? it.name : ''));
      tdSym.appendChild(cell);
      tr.appendChild(tdSym);
      tr.appendChild(el('td', 'num', f.num(d.quantity !== undefined ? d.quantity : d.shares)));
      tr.appendChild(el('td', 'num', f.price(d.price, ccy)));        // Decimal string -> fmt
      tr.appendChild(el('td', 'num', d.fee !== undefined ? f.money(d.fee, ccy) : f.NULL_GLYPH));
      tr.appendChild(el('td', 'num', d.tax !== undefined ? f.money(d.tax, ccy) : f.NULL_GLYPH));
      const tdNote = el('td', 'err-msg');
      if (r.reason) tdNote.appendChild(el('span', 'st-warn', '⚠ ' + r.reason));
      else tdNote.appendChild(el('span', 'st-ok', '✓ 解析完整'));
      tr.appendChild(tdNote);
      tr.appendChild(el('td'));
      tbody.appendChild(tr);
    });
    const s = preview.summary || {};
    if (window.toast) window.toast('解析完成', 'ok', '共 ' + (s.total || 0) + ' 筆草稿');
  }

  /* Write the AI-parsed drafts: the run returns a canonical csv_text, so the commit
     reuses the SAME import/commit transaction path (single write seam). */
  async function commitAi() {
    if (!aiCsvText) {
      if (window.toast) window.toast('請先解析', 'fail');
      return;
    }
    try {
      const resp = await api.post('/api/import/commit',
        { kind: 'transactions', csv_text: aiCsvText, ack_warnings: false });
      onCsvWritten(resp);
    } catch (err) {
      if (err && err.status === 422 && err.code === 'warnings_unacknowledged') {
        window.confirmDialog({
          title: '匯入警告確認',
          body: 'AI 草稿中部分列有警告 — 確認後一併寫入？',
          confirmLabel: '確認寫入',
          onConfirm: async () => {
            try {
              const resp = await api.post('/api/import/commit',
                { kind: 'transactions', csv_text: aiCsvText, ack_warnings: true });
              onCsvWritten(resp);
            } catch (e2) {
              if (window.toast) window.toast((e2 && e2.message) || '寫入失敗', 'fail', e2 && e2.code);
            }
          }
        });
        return;
      }
      if (window.toast) window.toast((err && err.message) || '寫入失敗', 'fail', err && err.code);
    }
  }

  /* ================= Tab 4 股利 ================= */
  /* NO single-entry dividend commit endpoint exists — dividends are written via the
     CSV import path (kind=dividends). Dropdowns come from /input/context; the local
     DRIP/net calc preview (user-input numbers) is kept; commit is a labelled design
     toast pointing the user at CSV import. */
  function initDiv() {
    const accSel = $('#d-account');
    ctx.accounts.forEach((a) => {
      const o = el('option', null, a.name + '（' + a.ccy + '）');
      o.value = a.id;
      accSel.appendChild(o);
    });
    accSel.addEventListener('change', renderDivForm);
    $('#d-date').value = TODAY;
    const typeSeg = document.querySelectorAll('#d-tw .segmented button');
    typeSeg.forEach((b) => b.addEventListener('click', () => {
      typeSeg.forEach((x) => x.classList.toggle('active', x === b));
      const stock = b.textContent.indexOf('配股') >= 0;
      $('#d-model-note').textContent = stock
        ? '台股模式（配股）：以 $0 成本股數入帳，調整均價下降；現金欄位改填配股股數。'
        : '台股模式：現金股利沖減成本（調整均價下降）；配股以 $0 成本股數入帳。';
    }));
    renderDivForm();
    $('#d-confirm').addEventListener('click', () =>
      window.toast('單筆股利請改用 CSV 匯入', 'fail',
        '後端未提供單筆股利寫入端點 — 請於「CSV 匯入 › 股利」貼上 CSV 寫入帳本'));
  }
  function renderDivForm() {
    const a = acc($('#d-account').value) || ctx.accounts[0];
    const model = a ? a.div_model : 'tw';
    ['d-tw', 'd-drip', 'd-net'].forEach((id) => { $('#' + id).hidden = true; });
    const note = $('#d-model-note');
    if (model === 'tw') {
      $('#d-tw').hidden = false;
      note.textContent = '台股模式：現金股利沖減成本（調整均價下降）；配股以 $0 成本股數入帳。';
    } else if (model === 'drip') {
      $('#d-drip').hidden = false;
      note.textContent = 'DRIP 模式：預扣 30%，net 將以 $0 成本股數入帳（再投資股數 × 再投資價格僅供對帳）。';
    } else {
      $('#d-net').hidden = false;
      note.textContent = '馬股模式：單一淨額入帳（無預扣層級）。';
    }
    /* DRIP gross live recompute — USER-INPUT estimate (documented input-side calc;
       the value of record is computed by the backend on CSV import, not here). */
    $('#d-drip-gross').oninput = () => {
      const g = parseFloat($('#d-drip-gross').value) || 0;
      const wh = g * 0.30;
      $('#d-drip-wh').value = wh.toFixed(2);
      $('#d-drip-net').value = (g - wh).toFixed(2);
    };
  }

  /* ================= Tab 5 換匯 + 期初 ================= */
  /* NO single-entry FX / opening commit endpoint exists — both are written via the CSV
     import path (kind=fx / kind=openings). Dropdowns come from /input/context; the local
     implied-rate what-if (user-input numbers) is kept; commit is a labelled design toast. */
  function initFxOpen() {
    const accSel = $('#fx-account');
    ctx.accounts.forEach((a) => {
      const o = el('option', null, a.name); o.value = a.id;
      accSel.appendChild(o);
    });
    $('#fx-date').value = TODAY;
    const upd = () => {
      const fromA = parseFloat($('#fx-from-amt').value) || 0;
      const toA = parseFloat($('#fx-to-amt').value) || 0;
      const fromC = $('#fx-from-ccy').value;
      const toC = $('#fx-to-ccy').value;
      /* implied-rate what-if on the USER's own entry (documented input-side calc). */
      if (fromA > 0 && toA > 0) {
        $('#fx-implied').textContent = '1 ' + toC + ' = ' + (fromA / toA).toFixed(4) + ' ' + fromC;
      } else {
        $('#fx-implied').textContent = f.NULL_GLYPH;
      }
    };
    ['fx-from-amt', 'fx-to-amt', 'fx-from-ccy', 'fx-to-ccy'].forEach((id) =>
      $('#' + id).addEventListener('input', upd));
    upd();
    $('#fx-confirm').addEventListener('click', () =>
      window.toast('單筆換匯請改用 CSV 匯入', 'fail',
        '後端未提供單筆換匯寫入端點 — 請於「CSV 匯入 › 換匯」貼上 CSV 寫入帳本'));

    const oAccSel = $('#o-account');
    ctx.accounts.forEach((a) => {
      const o = el('option', null, a.name); o.value = a.id;
      oAccSel.appendChild(o);
    });
    $('#o-date').value = TODAY;
    $('#o-confirm').addEventListener('click', () =>
      window.toast('單筆期初請改用 CSV 匯入', 'fail',
        '後端未提供單筆期初寫入端點 — 請於「CSV 匯入 › 期初」貼上 CSV 建檔'));
  }

  /* ===== boot: fetch /input/context, then init every tab. Graceful: on failure leave
     the forms empty + surface ONE toast (never an unhandled rejection — the e2e smoke
     asserts ZERO console errors). 401 is handled inside api.js. ===== */
  async function boot() {
    try {
      const resp = await api.get('/api/input/context');
      ctx = {
        accounts: (resp && resp.accounts) || [],
        fee_rules: (resp && resp.fee_rules) || {},
        instruments: (resp && resp.instruments) || [],
        holdings: (resp && resp.holdings) || {},
      };
    } catch (err) {
      if (window.toast) window.toast('輸入中心載入失敗', 'fail', (err && err.message) || undefined);
      /* fall through with empty ctx so the page still renders an (empty) shell */
    }
    initManual();
    initCsv();
    initAi();
    initDiv();
    initFxOpen();
    showTab('manual');
  }

  boot();

  /* 拖放區：設計預覽回饋（後端接線後換為真實上傳；目前以貼上 CSV / 文字示範） */
  document.querySelectorAll('.dropzone').forEach((dz) => {
    dz.style.cursor = 'pointer';
    dz.addEventListener('click', () => {
      if (window.toast) window.toast('檔案上傳為設計預覽', 'ok', '目前請以下方「貼上 CSV / 文字」示範流程');
    });
  });
})();
