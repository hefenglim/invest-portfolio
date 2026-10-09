/* portfolio-dash — the dashboard's 選擇權 section (options spec §7.1, W5).

   Rendered from `/api/dashboard`'s `options` block, read through the ONE shared dashboard
   promise (window.pdDashboard — app.js / charts.js / this file race-safely share a single
   request). The block exists only when the ledger carries an option, and so does this
   section: on every other ledger nothing here touches the page (invariant 1 — the section is
   CREATED, never hidden markup left in index.html).

   MONEY DISCIPLINE: every figure — premiums, 口數, 股當量, the obligation, the realized
   totals — is a server Decimal STRING formatted through window.fmt. Nothing here adds,
   multiplies or converts 口 ↔ 股當量. The §6.1 words for the three option CODES the block
   carries (direction, right, close reason) come from window.pdOptionWords (format.js, pinned
   to shared/options.py); every other word is a server string (`closed_how`, the notes, the
   refused-close sentences).

   Layout (spec §7.1): 未平倉 sorted by 剩餘天數 ↑ (the server's order) with the leading
   columns 合約・方向/口數・剩餘天數・履約價（標的現價）・已收權利金・未實現・履約義務, the 股當量 and
   最後成交日 in a row expander, the 「履約義務合計」 subtotal, ≤ 640 px as cards (CSS); a
   `past_expiry` row carries the 「已過到期日・待登錄」 badge and 「登錄到期作廢」; 已實現 defaults
   to 依合約 with reason chips, tabs 依年度／依標的; the R3 note sits under the section. */
(function () {
  'use strict';
  const f = window.fmt;
  const W = window.pdOptionWords || { direction: {}, right: {}, closeReason: {}, unit: '口',
                                      shareEq: '股當量' };
  const acctZh = (id) => (window.pdNames ? window.pdNames.account(id) : id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const nil = (v) => v === null || v === undefined;

  /* 「登錄到期作廢」 lands on the manual form in option mode, prefilled (spec §7.1). The form
     lives on trades.html, so from the dashboard this is a navigation; trades.html's input.js
     defines the in-page version of the same global (and reads these query keys on boot). */
  if (!window.pdPrefillOptionClose) {
    window.pdPrefillOptionClose = function (p) {
      const q = new URLSearchParams();
      q.set('option_close', p.close_reason || 'expired');
      q.set('account_id', p.account_id || '');
      q.set('symbol', p.symbol || '');
      if (p.contracts) q.set('contracts', p.contracts);
      if (p.date) q.set('date', p.date);
      window.location.href = 'trades.html?' + q.toString();
    };
  }

  /* ledger.js's deep link; the row's account and date narrow the 交易 table to it. */
  function txnHref(rc) {
    return 'trades.html?ledger=tx&txn_id=' + encodeURIComponent(rc.txn_id)
      + '&account_id=' + encodeURIComponent(rc.account_id || '')
      + '&date=' + encodeURIComponent(rc.date || '');
  }

  /* The section host: a panel created right after 持倉明細 (spec §7.1 「After 持倉明細」). */
  function sectionHost() {
    let sec = document.getElementById('options-panel');
    if (sec) return sec;
    const table = document.getElementById('holdings-table');
    const holdings = table ? table.closest('.panel') : null;
    if (!holdings || !holdings.parentNode) return null;
    sec = el('section', 'panel opt-panel');
    sec.id = 'options-panel';
    sec.dataset.screenLabel = '選擇權';
    holdings.parentNode.insertBefore(sec, holdings.nextSibling);
    return sec;
  }

  function moneyCell(v, ccy, opts) {
    const o = opts || {};
    const td = el('td', 'num');
    if (o.label) td.dataset.label = o.label;
    if (nil(v)) {
      td.textContent = f.NULL_GLYPH;
      td.classList.add('sign-nil');
      if (o.why) td.title = o.why;
      return td;
    }
    if (o.signed) {
      td.classList.add(f.signClass(v));
      td.textContent = f.signed(v, ccy);
    } else {
      td.textContent = f.money(v, ccy);
    }
    return td;
  }

  function daysText(days) {
    const n = Number(days);   // an integer day count, not money
    if (!Number.isFinite(n)) return f.NULL_GLYPH;
    return n >= 0 ? n + ' 天' : '已過 ' + (-n) + ' 天';
  }

  function strikeText(r) {
    let s = f.price(r.strike, 'USD');
    if (!nil(r.underlying_price)) {
      s += '（' + r.underlying + ' ' + f.price(r.underlying_price, 'USD')
        + '・' + f.date(r.underlying_price_date) + '）';
    }
    return s;
  }

  /* ---- 未平倉 --------------------------------------------------------------------------- */
  const OPEN_COLS = ['合約', '方向／口數', '剩餘天數', '履約價（標的現價）', '已收權利金', '未實現',
    '履約義務'];

  function openRow(r, tbody) {
    const tr = el('tr', 'opt-row expandable');
    tr.dataset.symbol = r.symbol;
    tr.dataset.accountId = r.account_id;
    if (r.past_expiry) tr.classList.add('opt-row-past');
    const tdC = el('td', 'col-text opt-c-contract');
    tdC.dataset.label = '合約';
    const caret = el('span', 'opt-caret', '▸');
    tdC.appendChild(caret);
    const cell = el('span', 'sym-link opt-contract', r.contract);
    cell.title = r.symbol + '（點擊查看合約詳情）';
    cell.addEventListener('click', (e) => {
      e.stopPropagation();
      if (window.openSymbolDrawer) window.openSymbolDrawer(r.symbol);
    });
    tdC.appendChild(cell);
    tdC.appendChild(el('span', 'opt-acct', acctZh(r.account_id)));
    if (r.past_expiry) {
      const b = el('span', 'badge badge-opt-past', '已過到期日・待登錄');
      b.title = '到期日 ' + f.date(r.expiry) + ' 已過，帳本尚未登錄這個部位的結清';
      tdC.appendChild(b);
      const btn = el('button', 'btn btn-sm opt-close-btn', '登錄到期作廢');
      btn.type = 'button';
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        window.pdPrefillOptionClose({ account_id: r.account_id, symbol: r.symbol,
          close_reason: 'expired', contracts: r.contracts, date: r.expiry });
      });
      tdC.appendChild(btn);
    }
    if (r.unbookable_close) {
      const b = el('span', 'badge badge-missing', '未入帳（待釐清）');
      b.title = '此合約有一筆到期作廢／被指派／履約列無法入帳，市值與損益待釐清';
      tdC.appendChild(b);
    } else if (r.oversold) {
      const b = el('span', 'badge badge-missing', '賣超');
      b.title = '賣出口數超過持有（未宣告放空），部位為負、損益待釐清';
      tdC.appendChild(b);
    }
    tr.appendChild(tdC);

    const dir = el('td', 'col-text opt-c-dir');
    dir.dataset.label = '方向／口數';
    dir.textContent = (W.direction[r.direction] || r.direction) + ' ' + f.exact(r.contracts)
      + ' ' + W.unit;
    tr.appendChild(dir);
    const days = el('td', 'num opt-c-days', daysText(r.days_to_expiry));
    days.dataset.label = '剩餘天數';
    days.title = '到期日 ' + f.date(r.expiry);
    tr.appendChild(days);
    const strike = el('td', 'num opt-c-strike', strikeText(r));
    strike.dataset.label = '履約價（標的現價）';
    tr.appendChild(strike);
    const prem = moneyCell(r.premium_total, 'USD', { label: '已收權利金' });
    if (r.direction === 'long') {
      prem.title = '買方：此為已付權利金';
      prem.appendChild(el('span', 'opt-paid', '已付'));
    }
    tr.appendChild(prem);
    tr.appendChild(moneyCell(r.unrealized, 'USD', { label: '未實現', signed: true,
      why: r.unbookable_close || r.oversold ? '部位待釐清，不計算未實現損益' : '無最後成交價' }));
    tr.appendChild(moneyCell(r.obligation, 'USD', { label: '履約義務',
      why: r.direction === 'short' && r.right === 'P' ? '部位待釐清' : '僅賣出 Put 有履約義務' }));

    const detail = el('tr', 'opt-detail-row');
    detail.hidden = true;
    const td = el('td');
    td.colSpan = OPEN_COLS.length;
    const kv = el('div', 'opt-kv');
    const pair = (k, v) => {
      const p = el('span', 'opt-kv-item');
      p.appendChild(el('span', 'k', k));
      p.appendChild(el('span', 'v num', v));
      kv.appendChild(p);
    };
    pair(W.shareEq, f.shares(r.share_equivalents));
    pair('平均權利金（每股）', f.price(r.average_premium, 'USD'));
    pair('最後成交價', nil(r.last_price) ? f.NULL_GLYPH : f.price(r.last_price, 'USD'));
    pair('最後成交日', nil(r.last_price_date) ? f.NULL_GLYPH : f.date(r.last_price_date));
    pair('市值', nil(r.market_value) ? f.NULL_GLYPH : f.signed(r.market_value, 'USD') + ' USD');
    pair('到期日', f.date(r.expiry));
    pair('類型', W.right[r.right] || r.right);
    td.appendChild(kv);
    detail.appendChild(td);
    tr.addEventListener('click', (e) => {
      if (e.target.closest('a, button')) return;
      detail.hidden = !detail.hidden;
      caret.textContent = detail.hidden ? '▸' : '▾';
    });
    tbody.appendChild(tr);
    tbody.appendChild(detail);
  }

  function openBlock(O) {
    const box = el('div', 'opt-block opt-open');
    const head = el('div', 'opt-sub-head');
    head.appendChild(el('h3', 'opt-sub-title', '未平倉'));
    head.appendChild(el('span', 'panel-sub', O.open.length ? O.open.length + ' 個部位・依剩餘天數排列' : ''));
    box.appendChild(head);
    if (!O.open.length) {
      box.appendChild(window.emptyState
        ? window.emptyState('目前無未平倉合約')
        : el('div', 'sd-empty', '目前無未平倉合約'));
      return box;
    }
    const wrap = el('div', 'table-wrap');
    const table = el('table', 'data opt-open-table');
    table.id = 'opt-open-table';
    const thead = el('thead');
    const trh = el('tr');
    OPEN_COLS.forEach((c, i) => trh.appendChild(el('th', i < 2 ? 'col-text' : null, c)));
    thead.appendChild(trh);
    table.appendChild(thead);
    const tbody = el('tbody');
    tbody.id = 'opt-open-body';
    O.open.forEach((r) => openRow(r, tbody));
    table.appendChild(tbody);
    wrap.appendChild(table);
    box.appendChild(wrap);
    /* 「履約義務合計」 — the server's per-account subtotal (never summed here). */
    if ((O.obligation_subtotals || []).length) {
      const sub = el('div', 'opt-obligation');
      sub.id = 'opt-obligation';
      sub.appendChild(el('span', 'k', '履約義務合計'));
      O.obligation_subtotals.forEach((s) => {
        const chip = el('span', 'ccy-chip');
        chip.appendChild(el('span', null, acctZh(s.account_id) + ' '));
        chip.appendChild(el('b', null, f.money(s.amount, s.ccy) + ' ' + s.ccy));
        sub.appendChild(chip);
      });
      sub.title = '賣出 Put 若全數被指派須以履約價買入的金額（履約價 × 100 × 口數）';
      box.appendChild(sub);
    }
    return box;
  }

  /* ---- §4.4 refused closes (未入帳（待釐清）) ------------------------------------------- */
  function refusedBlock(O) {
    const rows = O.refused_closes || [];
    if (!rows.length) return null;
    const box = el('div', 'opt-block opt-refused');
    box.id = 'opt-refused';
    box.appendChild(el('div', 'opt-refused-head',
      '⚠ 未入帳（待釐清）：' + rows.length + ' 筆到期作廢／被指派／履約列沒有可結清的部位'));
    const list = el('ul', 'opt-refused-list');
    rows.forEach((rc) => {
      const li = el('li', 'opt-refused-item');
      li.appendChild(el('span', 'opt-refused-meta', acctZh(rc.account_id) + '・' + f.date(rc.date)
        + '・' + (W.closeReason[rc.close_reason] || rc.close_reason)));
      li.appendChild(el('span', 'opt-refused-reason', rc.reason + ' — ' + rc.remedy));
      if (rc.txn_id !== null && rc.txn_id !== undefined) {
        const a = el('a', 'unreg-link', '前往該筆交易');
        a.href = txnHref(rc);
        li.appendChild(a);
      }
      list.appendChild(li);
    });
    box.appendChild(list);
    return box;
  }

  /* ---- 已實現 ----------------------------------------------------------------------------- */
  const RZ_TABS = [['contract', '依合約'], ['year', '依年度'], ['underlying', '依標的']];
  let rzTab = 'contract';

  function byCcyText(m) {
    const by = (m && m.by_currency) || {};
    const parts = Object.keys(by).map((c) => f.signed(by[c], c) + ' ' + c);
    return parts.length ? parts.join('／') : f.NULL_GLYPH;
  }

  function realizedTable(O, rep) {
    const R = O.realized || {};
    const wrap = el('div', 'table-wrap');
    const table = el('table', 'data opt-rz-table');
    table.id = 'opt-rz-table';
    const thead = el('thead');
    const trh = el('tr');
    const tbody = el('tbody');
    const head = (cols) => cols.forEach((c, i) => trh.appendChild(el('th', i === 0 ? 'col-text' : null, c)));
    if (rzTab === 'contract') {
      head(['合約', '開倉日', '結清日', '結清方式', '收入權利金', '支付權利金', '已實現']);
      (R.by_contract || []).forEach((c) => {
        const tr = el('tr');
        const td = el('td', 'col-text');
        const name = el('span', 'sym-link opt-contract', c.contract);
        name.title = c.symbol;
        name.addEventListener('click', () => {
          if (window.openSymbolDrawer) window.openSymbolDrawer(c.symbol);
        });
        td.appendChild(name);
        td.appendChild(el('span', 'opt-acct', acctZh(c.account_id)));
        tr.appendChild(td);
        tr.appendChild(el('td', 'num', nil(c.opened) ? f.NULL_GLYPH : f.date(c.opened)));
        tr.appendChild(el('td', 'num', nil(c.closed) ? '未平倉' : f.date(c.closed)));
        const how = el('td', 'col-text');
        if (c.closed_how) how.appendChild(el('span', 'rz-kind opt-reason', c.closed_how));
        else how.textContent = f.NULL_GLYPH;
        tr.appendChild(how);
        tr.appendChild(moneyCell(c.premium_in, c.ccy));
        tr.appendChild(moneyCell(c.premium_out, c.ccy));
        tr.appendChild(moneyCell(c.realized, c.ccy, { signed: true }));
        tbody.appendChild(tr);
      });
    } else {
      const rows = rzTab === 'year' ? (R.by_year || []) : (R.by_underlying || []);
      head([rzTab === 'year' ? '年度' : '標的', '各幣別', '合計（' + rep + '）']);
      rows.forEach((r) => {
        const tr = el('tr');
        tr.appendChild(el('td', 'col-text', String(rzTab === 'year' ? r.year : r.underlying)));
        tr.appendChild(el('td', 'num', byCcyText(r)));
        tr.appendChild(moneyCell(r.reporting, rep, { signed: true, why: '匯率資料不足' }));
        tbody.appendChild(tr);
      });
    }
    thead.appendChild(trh);
    table.appendChild(thead);
    table.appendChild(tbody);
    wrap.appendChild(table);
    if (!tbody.children.length) {
      return window.emptyState ? window.emptyState('尚無已實現的選擇權損益')
        : el('div', 'sd-empty', '尚無已實現的選擇權損益');
    }
    return wrap;
  }

  function realizedBlock(O, rep) {
    const box = el('div', 'opt-block opt-realized');
    const head = el('div', 'opt-sub-head');
    head.appendChild(el('h3', 'opt-sub-title', '已實現'));
    const total = (O.realized && O.realized.realized_total) || {};
    head.appendChild(el('span', 'panel-sub', '合計 ' + byCcyText(total)
      + (nil(total.reporting) ? '' : '（' + f.signed(total.reporting, rep) + ' ' + rep + '）')));
    head.appendChild(el('span', 'spacer'));
    const tabs = el('div', 'range-group opt-rz-tabs');
    tabs.setAttribute('role', 'tablist');
    const body = el('div', 'opt-rz-body');
    const paint = () => {
      tabs.querySelectorAll('.range-btn').forEach((b) => {
        b.classList.toggle('active', b.dataset.tab === rzTab);
      });
      body.replaceChildren(realizedTable(O, rep));
    };
    RZ_TABS.forEach(([key, label]) => {
      const b = el('button', 'range-btn', label);
      b.type = 'button';
      b.dataset.tab = key;
      b.addEventListener('click', () => { rzTab = key; paint(); });
      tabs.appendChild(b);
    });
    head.appendChild(tabs);
    box.appendChild(head);
    box.appendChild(body);
    paint();
    return box;
  }

  function render(D) {
    const O = D && D.options;
    if (!O) return;            // a ledger with no option: nothing here touches the page
    const sec = sectionHost();
    if (!sec) return;
    const rep = D.reporting_currency || 'TWD';
    sec.replaceChildren();
    const head = el('div', 'panel-head');
    head.appendChild(el('h2', 'panel-title', '選擇權'));
    head.appendChild(el('span', 'panel-sub',
      '美股選擇權・原幣金額・未平倉以最後一筆成交價估值（不抓取報價）'));
    head.appendChild(el('span', 'spacer'));
    sec.appendChild(head);
    sec.appendChild(openBlock(O));
    const refused = refusedBlock(O);
    if (refused) sec.appendChild(refused);
    sec.appendChild(realizedBlock(O, rep));
    if (O.note) {
      const note = el('div', 'opt-note', O.note);
      note.id = 'opt-note';
      sec.appendChild(note);
    }
  }

  (window.pdDashboard || (window.pdDashboard = window.pdApi.get('/api/dashboard')))
    .then(render)
    .catch(() => { /* app.js owns the dashboard's load-failure state */ });
})();
