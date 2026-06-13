/* portfolio-dash — 輸入中心: 5 tabs, Draft→Confirm everywhere (design mock). */
(function () {
  'use strict';
  const D = window.INPUT_DATA;
  const f = window.fmt;
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const acc = (id) => D.accounts.find((a) => a.id === id);
  const inst = (sym) => D.instruments.find((i) => i.symbol === (sym || '').trim().toUpperCase() || i.symbol === (sym || '').trim());

  /* ===== tabs ===== */
  const TABS = ['manual', 'csv', 'ai', 'div', 'fxopen'];
  function showTab(t) {
    TABS.forEach((x) => {
      $('#pane-' + x).classList.toggle('active', x === t);
      $('#tab-' + x).classList.toggle('active', x === t);
    });
  }
  TABS.forEach((t) => $('#tab-' + t).addEventListener('click', () => showTab(t)));

  /* ================= Tab 1 手動交易 ================= */
  const m = { side: 'buy', feeOverride: false, taxOverride: false };

  function initManual() {
    const accSel = $('#m-account');
    D.accounts.forEach((a) => {
      const o = el('option', null, a.name + '（' + a.ccy + '）');
      o.value = a.id;
      accSel.appendChild(o);
    });
    const dl = $('#m-symbols');
    D.instruments.forEach((i) => {
      const o = el('option'); o.value = i.symbol; o.label = i.name;
      dl.appendChild(o);
    });
    $('#m-date').value = '2026-06-11';
    /* brief mock: 2330 買 1,000 @ 612.5 */
    $('#m-symbol').value = '2330';
    $('#m-shares').value = '1000';
    $('#m-price').value = '612.5';

    $('#m-side-buy').addEventListener('click', () => setSide('buy'));
    $('#m-side-sell').addEventListener('click', () => setSide('sell'));
    ['m-account', 'm-symbol', 'm-shares', 'm-price', 'm-date'].forEach((id) => {
      $('#' + id).addEventListener('input', renderManual);
    });
    $('#m-fee-pencil').addEventListener('click', () => {
      m.feeOverride = true;
      $('#m-fee').readOnly = false;
      $('#m-fee').focus();
      renderManual();
    });
    $('#m-tax-pencil').addEventListener('click', () => {
      m.taxOverride = true;
      $('#m-tax').readOnly = false;
      $('#m-tax').focus();
      renderManual();
    });
    $('#m-fee').addEventListener('input', renderManual);
    $('#m-tax').addEventListener('input', renderManual);
    $('#m-confirm').addEventListener('click', () => {
      window.toast('寫入成功', 'ok', '交易已寫入帳本（設計稿 — 未實際儲存）');
    });
    renderManual();
  }
  function setSide(s) {
    m.side = s;
    $('#m-side-buy').classList.toggle('active', s === 'buy');
    $('#m-side-buy').classList.toggle('buy-on', s === 'buy');
    $('#m-side-sell').classList.toggle('active', s === 'sell');
    $('#m-side-sell').classList.toggle('sell-on', s === 'sell');
    renderManual();
  }
  function calcFees(accountId, symbol, gross, side) {
    const r = D.fee_rules[accountId];
    const it = inst(symbol);
    let fee = gross * r.rate * r.discount;
    if (gross > 0) fee = Math.max(fee, r.min_fee);
    if (r.round_int) fee = Math.round(fee);
    let tax = 0;
    if (side === 'sell') {
      const taxRate = it && it.etf && r.tax_sell_etf !== undefined ? r.tax_sell_etf : r.tax_sell;
      tax = gross * taxRate;
      if (r.round_int) tax = Math.round(tax);
    }
    return { fee, tax, rule: r };
  }

  function renderManual() {
    const a = acc($('#m-account').value || 'tw_broker');
    const ccy = a.ccy;
    const sym = $('#m-symbol').value.trim();
    const it = inst(sym);
    const shares = parseFloat($('#m-shares').value) || 0;
    const price = parseFloat($('#m-price').value) || 0;
    const gross = shares * price;
    const calc = calcFees(a.id, sym, gross, m.side);

    if (!m.feeOverride) $('#m-fee').value = calc.fee ? calc.fee.toFixed(ccy === 'TWD' ? 0 : 2) : '0';
    if (!m.taxOverride) $('#m-tax').value = calc.tax ? calc.tax.toFixed(ccy === 'TWD' ? 0 : 2) : '0';
    const fee = parseFloat($('#m-fee').value) || 0;
    const tax = parseFloat($('#m-tax').value) || 0;
    $('#m-fee-ovr').hidden = !m.feeOverride;
    $('#m-tax-ovr').hidden = !m.taxOverride;
    $('#m-fee-rule').textContent = calc.rule.label;

    /* symbol helper */
    const symHint = $('#m-sym-hint');
    symHint.replaceChildren();
    if (sym && !it) {
      symHint.appendChild(document.createTextNode('未註冊 — '));
      const link = el('a', 'hint-link', '前往標的管理');
      link.href = 'instruments.html';
      symHint.appendChild(link);
    } else if (it) {
      symHint.textContent = it.name + '・' + it.ccy + (it.etf ? '・ETF' : '');
    }

    /* issues */
    const issues = [];
    if (!sym) issues.push({ sev: 'error', text: '請輸入代號', field: 'm-symbol' });
    if (shares <= 0) issues.push({ sev: 'error', text: '股數必須大於 0', field: 'm-shares' });
    if (price <= 0) issues.push({ sev: 'error', text: '價格必須大於 0', field: 'm-price' });
    let softWarn = null;
    if (m.side === 'sell' && it) {
      const held = (D.holdings[a.id] || {})[it.symbol] || 0;
      if (shares > held) {
        softWarn = '賣出股數 ' + f.num(shares) + ' 超過持有 ' + f.num(held) + ' — 輸入錯誤還是放空？';
      }
    }
    ['m-symbol', 'm-shares', 'm-price'].forEach((id) => {
      $('#' + id).classList.toggle('field-error',
        issues.some((i) => i.field === id));
    });

    /* preview card */
    const total = m.side === 'buy' ? gross + fee + tax : gross - fee - tax;
    $('#m-pc-label').textContent = m.side === 'buy' ? '總成本（含費稅）' : '淨收款（扣費稅）';
    $('#m-pc-value').textContent = gross > 0 ? f.money(total, ccy) : f.NULL_GLYPH;
    $('#m-pc-ccy').textContent = ccy;
    const rows = $('#m-pc-rows');
    rows.replaceChildren();
    [['成交金額', gross], ['手續費' + (m.feeOverride ? '（已覆寫）' : ''), fee],
     ['交易稅' + (m.taxOverride ? '（已覆寫）' : ''), tax]].forEach(([k, v]) => {
      const row = el('div', 'pc-row');
      row.appendChild(el('span', 'k', k));
      row.appendChild(el('span', 'v', gross > 0 ? f.money(v, ccy) + ' ' + ccy : f.NULL_GLYPH));
      rows.appendChild(row);
    });

    const issueBox = $('#m-issues');
    issueBox.replaceChildren();
    issues.forEach((i) => {
      const div = el('div', 'issue issue-error');
      div.appendChild(el('span', null, '✕'));
      div.appendChild(el('span', null, i.text));
      issueBox.appendChild(div);
    });
    let ackOk = true;
    if (softWarn) {
      const div = el('div', 'issue issue-warn');
      div.appendChild(el('span', null, '⚠'));
      const lab = el('label');
      const cb = el('input');
      cb.type = 'checkbox';
      cb.id = 'm-ack';
      cb.checked = m.acked || false;
      cb.addEventListener('change', () => { m.acked = cb.checked; renderManual(); });
      lab.appendChild(cb);
      lab.appendChild(el('span', null, softWarn + ' 我了解，仍要寫入。'));
      div.appendChild(lab);
      issueBox.appendChild(div);
      ackOk = m.acked || false;
    } else {
      m.acked = false;
    }
    if (!issues.length && !softWarn && gross > 0) {
      const div = el('div', 'issue issue-ok');
      div.appendChild(el('span', null, '✓'));
      div.appendChild(el('span', null, '草稿檢核通過，可寫入'));
      issueBox.appendChild(div);
    }
    $('#m-confirm').disabled = issues.length > 0 || gross <= 0 || !ackOk;
  }

  /* ================= Tab 2 CSV 匯入 ================= */
  function initCsv() {
    const kinds = ['交易', '股利', '換匯', '期初'];
    const bar = $('#csv-kinds');
    kinds.forEach((k, i) => {
      const c = el('button', 'chip' + (i === 0 ? ' active' : ''), k);
      c.type = 'button';
      c.addEventListener('click', () => {
        bar.querySelectorAll('.chip').forEach((x) => x.classList.remove('active'));
        c.classList.add('active');
        $('#csv-kind-note').textContent = k === '交易' ? '' :
          '（' + k + ' CSV 的解析預覽同此模式 — 設計稿僅示範交易）';
      });
      bar.appendChild(c);
    });

    const P = D.csv_preview;
    $('#csv-file').textContent = P.filename;
    const tbody = $('#csv-body');
    const checks = [];
    P.rows.forEach((r) => {
      const tr = el('tr', r.status === 'error' ? 'row-error' : '');
      const tdCb = el('td');
      const cb = el('input');
      cb.type = 'checkbox';
      if (r.status === 'error') { cb.disabled = true; cb.checked = false; }
      else cb.checked = true;
      cb.addEventListener('change', updateCsvCounts);
      checks.push({ cb, r });
      tdCb.appendChild(cb);
      tr.appendChild(tdCb);
      tr.appendChild(el('td', 'num', '#' + r.n));
      tr.appendChild(el('td', 'num', r.date));
      tr.appendChild(el('td', 'col-text', r.account));
      const tdSide = el('td', 'col-text');
      tdSide.appendChild(el('span', 'dir-chip ' + (r.side === 'buy' ? 'dir-buy' : 'dir-sell'),
        r.side === 'buy' ? '買' : '賣'));
      tr.appendChild(tdSide);
      tr.appendChild(el('td', 'col-text num', r.symbol));
      tr.appendChild(el('td', 'num', f.num(r.shares)));
      tr.appendChild(el('td', 'num', f.num(r.price, 2)));
      const ST = { ok: ['✓ 可寫入', 'st-ok'], warn: ['⚠ 警告', 'st-warn'], error: ['✕ 錯誤', 'st-error'] };
      tr.appendChild(el('td', 'col-text ' + ST[r.status][1], ST[r.status][0]));
      tr.appendChild(el('td', 'err-msg', r.reason || ''));
      tbody.appendChild(tr);
    });

    function updateCsvCounts() {
      const ok = P.rows.filter((r) => r.status === 'ok').length;
      const warn = P.rows.filter((r) => r.status === 'warn').length;
      const err = P.rows.filter((r) => r.status === 'error').length;
      const selected = checks.filter((c) => c.cb.checked).length;
      $('#csv-counts').textContent =
        '可寫入 ' + ok + '・警告 ' + warn + '・錯誤 ' + err + '・已勾選 ' + selected;
      $('#csv-confirm').disabled = selected === 0;
    }
    updateCsvCounts();

    $('#csv-confirm').addEventListener('click', () => {
      const selected = checks.filter((c) => c.cb.checked);
      const skipped = P.rows.length - selected.length;
      const banner = $('#csv-result');
      banner.hidden = false;
      banner.replaceChildren();
      banner.appendChild(el('div', null, '✓ 寫入完成：成功 ' + selected.length + ' 筆・跳過 ' + skipped + ' 筆'));
      const det = el('details');
      det.appendChild(el('summary', null, '展開跳過原因'));
      P.rows.filter((r) => !checks.find((c) => c.r === r).cb.checked).forEach((r) => {
        det.appendChild(el('div', null, '#' + r.n + ' ' + r.symbol + '：' + (r.reason || '未勾選')));
      });
      banner.appendChild(det);
      window.toast('寫入成功', 'ok', '成功 ' + selected.length + ' 筆・跳過 ' + skipped + ' 筆');
    });
  }

  /* ================= Tab 3 AI 輸入 ================= */
  function initAi() {
    /* design-review state switcher */
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

    $('#ai-source').textContent = D.ai_drafts.source_label;
    $('#ai-model').textContent = D.ai_drafts.model;
    const tbody = $('#ai-body');
    D.ai_drafts.rows.forEach((r) => {
      const tr = el('tr');
      const tdCb = el('td');
      const cb = el('input'); cb.type = 'checkbox'; cb.checked = true;
      tdCb.appendChild(cb);
      tr.appendChild(tdCb);
      const mkIn = (val, w, align) => {
        const td = el('td', 'num');
        const inp = el('input', 'input');
        inp.value = val;
        inp.style.width = w || '84px';
        inp.style.textAlign = align || 'right';
        td.appendChild(inp);
        return td;
      };
      const tdAcc = el('td', 'col-text');
      const sel = el('select', 'select');
      D.accounts.forEach((a) => {
        const o = el('option', null, a.name); o.value = a.id;
        if (a.id === r.account_id) o.selected = true;
        sel.appendChild(o);
      });
      sel.style.width = '128px';
      tdAcc.appendChild(sel);
      tr.appendChild(tdAcc);
      tr.appendChild(mkIn(r.date, '104px', 'left'));
      const tdSide = el('td', 'col-text');
      tdSide.appendChild(el('span', 'dir-chip ' + (r.side === 'buy' ? 'dir-buy' : 'dir-sell'),
        r.side === 'buy' ? '買' : '賣'));
      tr.appendChild(tdSide);
      const tdSym = el('td', 'col-text');
      const cell = el('div', 'sym-cell');
      cell.appendChild(el('span', 'sym-code', r.symbol));
      cell.appendChild(el('span', 'sym-name', r.name));
      tdSym.appendChild(cell);
      tr.appendChild(tdSym);
      tr.appendChild(mkIn(f.num(r.shares), '70px'));
      tr.appendChild(mkIn(r.price.toFixed(2), '84px'));
      tr.appendChild(mkIn(r.fee, '60px'));
      tr.appendChild(mkIn(r.tax, '60px'));
      const tdNote = el('td', 'err-msg');
      if (r.note) {
        tdNote.appendChild(el('span', 'st-warn', '⚠ ' + r.note));
      } else {
        tdNote.appendChild(el('span', 'st-ok', '✓ 解析完整'));
      }
      tr.appendChild(tdNote);
      const tdAct = el('td');
      const w = el('button', 'btn', '寫入');
      w.type = 'button';
      w.addEventListener('click', () => window.toast('寫入成功', 'ok', r.symbol + ' 草稿已寫入（設計稿）'));
      tdAct.appendChild(w);
      tr.appendChild(tdAct);
      tbody.appendChild(tr);
    });
    $('#ai-write-all').addEventListener('click', () =>
      window.toast('寫入成功', 'ok', '已寫入 2 筆勾選草稿（設計稿）'));
    $('#ai-parse').addEventListener('click', () =>
      window.toast('解析完成', 'ok', '2 筆草稿（claude-sonnet・Vision）'));
  }

  /* ================= Tab 4 股利 ================= */
  function initDiv() {
    const accSel = $('#d-account');
    D.accounts.forEach((a) => {
      const o = el('option', null, a.name + '（' + a.ccy + '）');
      o.value = a.id;
      accSel.appendChild(o);
    });
    accSel.addEventListener('change', renderDivForm);
    $('#d-date').value = '2026-06-11';
    /* 台股模式：現金股利 / 配股 類型切換（設計稿 — 僅切換狀態與說明） */
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
      window.toast('寫入成功', 'ok', '股利已寫入帳本（設計稿）'));
  }
  function renderDivForm() {
    const a = acc($('#d-account').value || 'tw_broker');
    const model = a.div_model;
    ['d-tw', 'd-drip', 'd-net'].forEach((id) => { $('#' + id).hidden = true; });
    const note = $('#d-model-note');
    if (model === 'tw') {
      $('#d-tw').hidden = false;
      $('#d-symbol').value = D.dividend_defaults.tw.symbol;
      $('#d-tw-gross').value = D.dividend_defaults.tw.gross;
      $('#d-tw-net').value = D.dividend_defaults.tw.net;
      note.textContent = '台股模式：現金股利沖減成本（調整均價下降）；配股以 $0 成本股數入帳。';
    } else if (model === 'drip') {
      $('#d-drip').hidden = false;
      $('#d-symbol').value = D.dividend_defaults.drip.symbol;
      const g = D.dividend_defaults.drip.gross;
      $('#d-drip-gross').value = g.toFixed(2);
      const wh = g * D.dividend_defaults.drip.withhold_rate;
      $('#d-drip-wh').value = wh.toFixed(2);
      $('#d-drip-net').value = (g - wh).toFixed(2);
      $('#d-drip-shares').value = D.dividend_defaults.drip.reinvest_shares;
      $('#d-drip-price').value = D.dividend_defaults.drip.reinvest_price.toFixed(2);
      note.textContent = 'DRIP 模式：預扣 30%，net 將以 $0 成本股數入帳（再投資股數 × 再投資價格僅供對帳）。';
    } else {
      $('#d-net').hidden = false;
      $('#d-symbol').value = D.dividend_defaults.net.symbol;
      $('#d-net-amt').value = D.dividend_defaults.net.net.toFixed(2);
      note.textContent = '馬股模式：單一淨額入帳（無預扣層級）。';
    }
    /* gross live recompute for DRIP */
    $('#d-drip-gross').oninput = () => {
      const g = parseFloat($('#d-drip-gross').value) || 0;
      const wh = g * 0.30;
      $('#d-drip-wh').value = wh.toFixed(2);
      $('#d-drip-net').value = (g - wh).toFixed(2);
    };
  }

  /* ================= Tab 5 換匯 + 期初 ================= */
  function initFxOpen() {
    const accSel = $('#fx-account');
    D.accounts.forEach((a) => {
      const o = el('option', null, a.name); o.value = a.id;
      accSel.appendChild(o);
    });
    $('#fx-date').value = '2026-06-11';
    $('#fx-from-amt').value = '32000';
    $('#fx-to-amt').value = '1000';
    const upd = () => {
      const fromA = parseFloat($('#fx-from-amt').value) || 0;
      const toA = parseFloat($('#fx-to-amt').value) || 0;
      const fromC = $('#fx-from-ccy').value;
      const toC = $('#fx-to-ccy').value;
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
      window.toast('寫入成功', 'ok', '換匯已寫入帳本（設計稿）'));

    const oAccSel = $('#o-account');
    D.accounts.forEach((a) => {
      const o = el('option', null, a.name); o.value = a.id;
      oAccSel.appendChild(o);
    });
    $('#o-date').value = '2026-01-02';
    $('#o-confirm').addEventListener('click', () =>
      window.toast('寫入成功', 'ok', '期初庫存已建檔（設計稿）'));
  }

  initManual();
  initCsv();
  initAi();
  initDiv();
  initFxOpen();
  showTab('manual');

  /* 拖放區：設計預覽回饋（後端接線後換為真實上傳） */
  document.querySelectorAll('.dropzone').forEach((dz) => {
    dz.style.cursor = 'pointer';
    dz.addEventListener('click', () => {
      if (window.toast) window.toast('檔案上傳為設計預覽', 'ok', '後端接線後支援拖放與選取檔案；目前以下方解析預覽示範流程');
    });
  });
})();
