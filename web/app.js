/* portfolio-dash — DOM rendering. All rows are generated from /api/dashboard.
   The dashboard payload is fetched ONCE via a shared promise (window.pdDashboard)
   that app.js / charts.js / alerts.js race-safely reuse (they load in different
   orders; whichever runs first creates the single in-flight request). Money/price/
   rate values arrive as Decimal STRINGS — the frontend never computes money; all
   numbers route through window.fmt (which coerces internally for display only). */
(function () {
  'use strict';
  let D;                       // set in boot() from the shared /api/dashboard promise
  const f = window.fmt;

  /* Account zh-TW display name — single source of truth is web/names.js (FU-D37,
     window.pdNames). Local delegator with a graceful no-op (id fallback) when names.js
     has not loaded on this page yet (index.html's <script> tag is added by the
     orchestrator sweep). Server-side account.display_name is the planned successor. */
  const acctZh = (id) => (window.pdNames ? window.pdNames.account(id) : id);
  const MARKET_ZH = { TW: '台股', US: '美股', MY: '馬股' };
  const CCY_COLOR = { TWD: '#58a6dd', USD: '#9b86d8', MYR: '#d9a13f' };

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  /* Bar-fill width, clamped to 0..100 — the ONE helper for EVERY segmented bar in this
     file: the holdings table's `.mini-bar .fill` and the currency-mix `.ccy-stack .seg`.

     Neither declares a width in CSS, so the inline value is the only source — and an
     INVALID inline width is not clamped by the browser, it is DISCARDED. The element then
     falls back to its auto size: a full-width block for `.fill`, 0px (i.e. gone) for a flex
     `.seg`. A net-short position carries a NEGATIVE weight by construction (net-exposure
     convention, domain-ledger.md), which is how both bars met an out-of-range value:
       · 2026-08-05 — `width: -2.29%` was dropped and a −2.29% holding drew a 100% bar,
         visually identical to the 99.33% holding above it while its own label said −2.29%.
       · 2026-08-29 — the currency stack assigned `180.80%` / `-137.23%` verbatim. The
         negative segment vanished, AND — the container being `display:flex` — the
         over-range sibling made flex-shrink RESCALE every other segment: a legal 56.43%
         currency rendered at 23.8% of the track (measured in Chromium). One out-of-range
         value corrupts the WHOLE bar, not just its own segment.
     ONE helper on purpose: the second bar went unclamped because this lived inside
     `renderHoldings`. The label carries the sign; the bar only ever claims magnitude. */
  const barWidth = (value, max) => {
    const pct = max ? (value / max) * 100 : 0;
    return (Number.isFinite(pct) ? Math.min(100, Math.max(0, pct)) : 0) + '%';
  };

  /* M1-03 / D21 — the provenance sentence for a SPINOFF child's 股利回收率. Both totals were
     scaled by the same cost_carry, so the child's ratio IS the parent's, built on dividends
     the child never paid; the server says whose (payback_from_symbol) and how much was
     carried in at the spinoff vs. paid by the child itself — both server Decimal strings.
     The frontend composes TEXT here, never money. Same wording as the drawer and 股利總覽. */
  function paybackProvenance(h) {
    return '承接自 ' + h.payback_from_symbol + '：分拆時承接配息 '
      + f.money(h.payback_carried_dividends, h.quote_ccy) + '・自身配息 '
      + f.money(h.payback_own_dividends, h.quote_ccy);
  }

  /* ============ A. Header ============ */
  function renderHeader() {
    $('#asof-value').textContent = f.datetime(D.as_of);
    $('#report-ccy').textContent = '報告幣別 ' + D.reporting_currency;
    const chip = $('#fresh-chip');
    if (D.freshness && D.freshness.any_stale) {
      chip.className = 'badge badge-fresh-stale';
      chip.innerHTML = '<span class="dot"></span>部分過期';
      chip.title = '部分價格或匯率資料已過期，點擊查看資料新鮮度明細';
      // Restored, not assumed: the else-branch below REMOVES the href, so a refresh that
      // went fresh -> stale left a chip that says 「點擊查看」 and is no longer a link.
      chip.href = '#freshness';
    } else {
      chip.className = 'badge badge-fresh-ok';
      chip.innerHTML = '<span class="dot"></span>資料新鮮';
      chip.removeAttribute('href');
    }
    renderUnregisteredBanner();
  }

  /* Unregistered-symbol warning (2026-07-02): ledger rows whose symbol has no
     instrument registration are EXCLUDED from every number on this page — surface
     that loudly with a fix link, or the exclusion would look like silent data loss. */
  function renderUnregisteredBanner() {
    const syms = (D.freshness && D.freshness.unregistered_symbols) || [];
    const page = document.querySelector('.page');
    const old = document.getElementById('unreg-banner');
    if (old) old.remove();
    if (!syms.length || !page) return;
    const bar = el('div', 'unreg-banner');
    bar.id = 'unreg-banner';
    bar.appendChild(el('span', 'unreg-ico', '⚠'));
    const txt = el('span', 'unreg-text',
      '帳本中有 ' + syms.length + ' 檔未註冊標的（' + syms.join('、') +
      '）— 相關交易未納入任何統計。');
    bar.appendChild(txt);
    const link = el('a', 'unreg-link', '前往標的管理註冊');
    link.href = 'instruments.html';
    bar.appendChild(link);
    page.insertBefore(bar, page.firstChild);
  }

  /* ============ B. KPI band v3 — 7 cards, ONE grammar (方案 A, owner ruling 2026-09-01) ==
     Supersedes the v2 band (3 hero + 2 combo on five fixed `fr` tracks). v2's 資產損益 card
     answered FOUR unrelated questions — 我賺多少 / 報酬率 / 含匯兌口徑 / 跟指數比 — as seven
     inline `flex-wrap` lines inside the second-narrowest of five tracks (249px at a 1,440px
     viewport, against 220px for the card holding ONE number). The wrapping was decided by
     the flex container, not by us: 「其中本金匯率效果」 stayed on one line while its 「−13,187」
     landed alone at the head of the next, and a 「·」 opened a row that was really a
     continuation. v3 gives every card the same shape:

         label → [one big value, hero cards only] → right-aligned key/value rows
               → [server-authored note, verbatim] → caption pinned to the card floor

     Card ORDER is unchanged from v2 (owner ruling 順序-1) so the dashboard's first cell is
     still 總市值; only `grid-column: span n` changes per tier, never `order:`, so the visual
     order and the reading order cannot drift apart. The tier ladder and its measured
     boundaries live in `styles.css` under "KPI band v3".

     Every figure is a server Decimal STRING; this layer only formats and never sums. A, B
     and B−A stay in separate cards, presented side by side — adding the 換匯損益 card or
     本金匯率效果 to A double-counts the cross term (`domain-ledger.md` §AI-D41). */
  function renderKpis() {
    const k = D.kpis;
    const ccy = k ? k.reporting_currency : D.reporting_currency;
    const band = $('#kpi-band');
    band.classList.remove('is-loading');  // M1-01: the answer is in — before any branch
    band.replaceChildren();

    const nil = (v) => v === null || v === undefined;
    const mkValue = (v, render, signed) => {
      const value = el('div', 'kpi-value num');
      if (nil(v)) { value.textContent = f.NULL_GLYPH; value.classList.add('sign-nil'); }
      else {
        value.textContent = render(v);
        if (signed) value.classList.add(f.signClass(v));
      }
      return value;
    };
    /* A status flag — 「資料不足」, 「觀察期 N 天・短窗參考」 — placed UNDER the value, never
       in the label. v2 put it in the label, where it wraps to a second line in a narrow
       card: at 900px that made the XIRR card's label one line taller than its neighbours'
       and dropped its big number 17px below theirs, which is exactly the "cards do not line
       up" complaint this band was rebuilt to answer. Under the value it also reads better —
       it qualifies the figure, and 標籤-5's rule is that a qualifier sits next to the thing
       it qualifies. `test_kpi_band_tiers::test_cards_in_one_row_align` is the guard: put a
       badge back in a label and it fails at 900px.
       The VISIBLE text stays a short zh label; the (often long, English, technical) backend
       reason rides in the tooltip and is shown in full in the 資料新鮮度 panel, where there
       is room — rendering the raw reason here once gave this card a 348px nowrap label
       inside a ~150px one (audit M1). */
    const addFlag = (card, text, reason, cls) => {
      let host = card.querySelector('.kpi-flag');
      if (!host) card.appendChild(host = el('div', 'kpi-flag'));
      const b = el('span', 'badge ' + (cls || 'badge-stale-mini'), text);
      b.title = reason || text;
      host.appendChild(b);
    };

    /* --- the card factory: one grammar, two sizes -----------------------------------
       `hero` carries a big value and spans 4 of 12 columns; `detail` carries rows only and
       spans 3. `extra` is a span modifier for the two tiers where one card must go full
       width — `kpi-wide-sm` (<=860px) and `kpi-wide-xs` (<=480px). Both are measured, not
       chosen: see the ladder comment in styles.css. */
    const mkCard = (role, title, extra) => {
      const card = el('div', 'kpi-card ' + (role === 'hero' ? 'kpi-hero' : 'kpi-combo')
        + (extra ? ' ' + extra : ''));
      card.appendChild(el('div', 'kpi-label', title));
      band.appendChild(card);
      return card;
    };
    /* An aligned key/value row — the band's ONLY data grammar. The value is pinned to the
       card's right edge, so a long key wraps downward inside its own column and a break can
       never land between a label and its number.
       `sub` is the 標籤-5 qualifier (owner ruling 2026-09-01): a block under the key rather
       than words appended to it, because the full inline key needs 234px and the tightest
       tier gives 171px. It stays beside the figure it qualifies instead of being demoted to
       the card's footer, and it wraps without pushing the value.
       `wi` prefixes 「其中 」 on a subordinate row; it is hidden at <=480px, where the indent
       and its corner glyph already carry the meaning and the word costs width we lack. */
    const addRow = (card, key, v, opts) => {
      const o = opts || {};
      const row = el('div', 'combo-row' + (o.indent ? ' combo-ind' : ''));
      const kk = el('span', 'k');
      if (o.wi) kk.appendChild(el('em', 'combo-wi', '其中 '));
      kk.appendChild(document.createTextNode(key));
      if (o.sub) kk.appendChild(el('em', 'combo-sub', o.sub));
      row.appendChild(kk);
      const vv = el('span', 'v');
      if (nil(v)) {
        vv.textContent = f.NULL_GLYPH;
        vv.classList.add('sign-nil');
        vv.title = o.reason || '資料不足';
      } else {
        vv.textContent = o.pct ? f.signedPct(v) : f.signed(v, ccy);
        vv.classList.add(f.signClass(v));
      }
      row.appendChild(vv);
      card.appendChild(row);
    };
    /* A SERVER-authored explanation, rendered verbatim — this layer never composes one. It
       is how a PARTIAL total says it is partial (QA-02), so it shows even when the values
       beside it are present. */
    const addNote = (card, note) => {
      if (note) card.appendChild(el('div', 'kpi-note', note));
    };
    /* Our own static footnote. Pinned to the card floor by CSS so a row of unequal-length
       cards lines its captions up, and rendered at a different SIZE and COLOUR from the
       data — not the same 11px at `opacity: .75` that made v2's coverage disclaimer look
       like one more figure. */
    const addCap = (card, text) => card.appendChild(el('div', 'kpi-cap', text));
    /* The signed accent bar. Via `f.signClass`, which parses the Decimal STRING, rather
       than `v > 0` (which coerces through a float) — same reason the rest of this layer
       never does arithmetic on a money value. */
    const sign = (card, v) => {
      const cls = f.signClass(v);
      if (cls === 'sign-up') card.classList.add('kpi-up');
      if (cls === 'sign-down') card.classList.add('kpi-down');
    };

    /* 1 · 總市值 — the anchor, and the band's only unsigned figure. */
    {
      const card = mkCard('hero', '總市值');
      const value = mkValue(k && k.total_market_value, (v) => f.money(v, ccy), false);
      if (!nil(k && k.total_market_value)) {
        value.appendChild(el('span', 'kpi-unit', ' ' + ccy));
      }
      card.appendChild(value);
      if (nil(k && k.total_market_value)) addFlag(card, '匯率不足', '匯率資料不足');
    }

    /* 2 · 資產損益 (A). AI-D41: `total_return` applies today's spot to each currency's NET
       P&L, so the rate never reaches the PRINCIPAL — the label says so. B and B−A sit in
       總損益口徑 below, beside this figure and never added to it. */
    {
      const card = mkCard('hero', '資產損益（不含本金匯率）');
      card.appendChild(mkValue(k && k.total_return, (v) => f.signed(v, ccy), true));
      if (nil(k && k.total_return)) addFlag(card, '資料不足');
      addRow(card, '累計報酬率', k && k.total_return_rate,
        { sub: '對原始投入成本', pct: true });
      addCap(card, '拆解、匯率口徑與指數對照見下列。');
      sign(card, k && k.total_return);
    }

    /* 3 · 年化報酬 (XIRR) — the decision metric, so it is the card that goes full width
       first when the band folds (`kpi-wide-sm`, <=860px). */
    {
      const card = mkCard('hero', '年化報酬 (XIRR)', 'kpi-wide-sm');
      const xirrNil = nil(k && k.xirr);
      card.appendChild(mkValue(k && k.xirr, f.signedPct, true));
      /* short flag, full reason in the tooltip (the 資料新鮮度 panel prints it in full). */
      if (xirrNil) {
        addFlag(card, '資料不足',
          (D.freshness && D.freshness.xirr_unavailable_reason) || '資料不足');
      }
      /* Short-window confidence hint: XIRR annualizes, so a sub-year observation window
         makes the figure volatile. window_days is a plain count (not money) — safe to
         compare/render directly. Only shown when an XIRR value is present. */
      const xirrWin = k && k.xirr_window_days;
      if (!xirrNil && xirrWin !== null && xirrWin !== undefined && xirrWin < 365) {
        addFlag(card, '觀察期 ' + xirrWin + ' 天・短窗參考',
          '觀察期不足一年，年化 XIRR 波動較大，僅供參考。', 'badge-window-mini');
      }
      addCap(card, '資金加權・FX-aware・決策主指標');
      sign(card, k && k.xirr);
    }

    /* 4 · 損益拆解 — the two halves of card 2's figure. They were a whole card away from it
       in v2, with XIRR in between, so the identity was invisible; the caption states it
       rather than printing the sum again. */
    {
      const card = mkCard('detail', '損益拆解（' + ccy + '）');
      addRow(card, '已實現', k && k.realized_total);
      addRow(card, '未實現', k && k.unrealized_total);
      addCap(card, '兩者合計即上方「資產損益」。');
    }

    /* 5 · 換匯損益 — an attribution breakdown of the reporting-currency result, never an
       extra gain added on top of it (`domain-ledger.md`); the caption says so on the card.
       Two DIFFERENT server reasons, and this card must show whichever exists:
       `fx.reporting_unavailable_reason` = the rollup ran but SOME account could not be
       expressed in the reporting currency (the figures below are real but partial);
       `freshness.fx_unavailable_reason` = the whole section failed and both figures are
       null. Before QA-01/QA-02 neither existed, so a partial total and a vanished one both
       rendered as a bare number / a bare 「—」 with nothing to explain them. */
    {
      const fxWhy = (D.fx && D.fx.reporting_unavailable_reason)
        || (D.freshness && D.freshness.fx_unavailable_reason) || null;
      const card = mkCard('detail', '換匯損益（歸因拆分）');
      addRow(card, '已實現', k && k.fx_realized, { reason: fxWhy || '匯率資料不足' });
      addRow(card, '未實現', k && k.fx_unrealized, { reason: fxWhy || '匯率資料不足' });
      addNote(card, fxWhy);
      addCap(card, '外幣部位的歸因拆分，不與損益相加。');
    }

    /* 6 · 總損益口徑 — B (`total_return_fx_complete`) and the two terms that separate it
       from A. AI-D48: B = A + 本金匯率效果 + 交易與融資成本, so the broker cost is shown
       BESIDE the FX effect rather than hiding inside a residual labelled 本金匯率效果.
       The B row renders whenever the server had anything to say about it — the value when
       it could be measured, otherwise 「—」 plus the server's reason (owner ruling
       2026-08-25: a row that silently disappears is indistinguishable from a feature that
       was never built, and B is absent on any day a held symbol lacks a price).
       ZERO trading cost renders NOTHING: a ledger with no rebate / margin interest / broker
       fee has nothing to disclose, and 「交易與融資成本 $0」 on every such account is noise.
       `sign-nil` covers an older payload without the field (canned e2e fixtures). */
    {
      const card = mkCard('detail', '總損益口徑', 'kpi-wide-xs');
      const hasB = !nil(k && k.total_return_fx_complete);
      addRow(card, '含匯兌總損益', k && k.total_return_fx_complete,
        { reason: (k && k.fx_complete_reason) || '資料不足' });
      if (hasB && !nil(k.principal_fx_effect)) {
        addRow(card, '本金匯率效果', k.principal_fx_effect, { indent: true, wi: true });
      }
      const tfc = k && k.trading_financing_cost;
      const tfcSign = f.signClass(tfc);
      if (hasB && tfcSign !== 'sign-nil' && tfcSign !== 'sign-flat') {
        addRow(card, '交易與融資成本', tfc, { indent: true, wi: true });
      }
      if (!hasB) addNote(card, k && k.fx_complete_reason);
      addCap(card, '與上方「資產損益」並列呈現，不相加。');
    }

    /* 7 · 指數對照 — R4 / AI-D43, the counterfactual A is measured against. Every number is
       server-computed; this layer never names an index and never subtracts.
       Partial coverage must NOT print a bare 「差額」 — the same discipline as covered_ratio:
       the LABEL degrades and the caption names the markets left out. v2 said that in a
       parenthetical and a full sentence at the same weight as the data; here the
       degradation is the key's own qualifier and the sentence is a caption, but neither the
       wording nor the rule has changed.
       Tolerates an older payload with no `benchmark` at all (canned e2e fixtures): the card
       still renders, with 「—」 — the 2026-08-25 ruling applied to the card as well as the
       row. */
    {
      const card = mkCard('detail', '指數對照', 'kpi-wide-xs');
      const bm = D.benchmark || null;
      const ok = !!(bm && bm.available && !nil(bm.benchmark_return));
      const why = (bm && bm.reason) || '資料不足';
      const partial = ok && !nil(bm.uncovered_ratio) && Number(bm.uncovered_ratio) > 0;
      addRow(card, '同期指數', ok ? bm.benchmark_return : null,
        { sub: ok ? ((bm.by_market || []).map((m) => m.label).join('／') || null) : null,
          reason: why });
      addRow(card, '差額', (ok && !nil(bm.excess)) ? bm.excess : null,
        { sub: partial ? '部分涵蓋' : null, reason: why });
      if (!ok) addNote(card, bm && bm.reason);   /* server-authored, verbatim */
      if (partial) {
        addCap(card, (bm.uncovered_markets || []).join('／') + ' 無對應指數，'
          + f.pct(bm.uncovered_ratio) + ' 的投入金額未納入比較');
      }
    }
  }

  /* ============ B2. 各幣別報酬拆分 ============ */
  function renderCcyReturns() {
    const host = document.getElementById('ccyret-body');
    if (!host) return;
    const r = D.returns;
    const wrap = document.getElementById('ccyret-wrap');
    if (!r || !r.by_currency) {
      if (wrap) {
        wrap.replaceChildren(emptyState('尚無各幣別報酬資料'));
      }
      return;
    }
    host.replaceChildren();
    Object.keys(r.by_currency).forEach((ccy) => {
      const row = r.by_currency[ccy];
      const tr = el('tr');
      tr.appendChild(el('td', null, ccy));
      tr.appendChild(el('td', 'num ' + f.signClass(row.realized), f.signed(row.realized, ccy)));
      tr.appendChild(el('td', 'num ' + f.signClass(row.unrealized), f.signed(row.unrealized, ccy)));
      tr.appendChild(el('td', 'num ' + f.signClass(row.total_return), f.signed(row.total_return, ccy)));
      tr.appendChild(el('td', 'num', f.money(row.gross_invested, ccy)));
      tr.appendChild(el('td', 'num ' + f.signClass(row.rate), f.signedPct(row.rate)));
      host.appendChild(tr);
    });
    const chips = document.getElementById('ccyret-chips');
    if (chips) {
      chips.replaceChildren();
      Object.keys(r.by_currency).forEach((ccy) => {
        const row = r.by_currency[ccy];
        const chip = el('span', 'ccy-chip');
        chip.appendChild(el('span', null, ccy + ' '));
        chip.appendChild(el('b', f.signClass(row.rate), f.signedPct(row.rate)));
        chips.appendChild(chip);
      });
    }
  }

  /* ============ D. Holdings table ============ */
  const holdingsState = { account: 'all', market: 'all', sortKey: null, sortDir: -1 };

  function renderFilterChips() {
    const bar = $('#filter-bar');
    bar.replaceChildren();
    const mkChip = (group, value, label) => {
      const c = el('button', 'chip', label);
      c.type = 'button';
      if (holdingsState[group] === value) c.classList.add('active');
      c.addEventListener('click', () => {
        holdingsState[group] = value;
        renderFilterChips();
        renderHoldings();
      });
      return c;
    };
    bar.appendChild(el('span', 'group-label', '帳戶'));
    bar.appendChild(mkChip('account', 'all', '全部'));
    const seen = [];
    D.holdings.forEach((h) => { if (!seen.includes(h.account_id)) seen.push(h.account_id); });
    seen.forEach((id) => bar.appendChild(mkChip('account', id, acctZh(id))));
    bar.appendChild(el('span', 'divider'));
    bar.appendChild(el('span', 'group-label', '市場'));
    bar.appendChild(mkChip('market', 'all', '全部'));
    ['TW', 'US', 'MY'].forEach((m) => bar.appendChild(mkChip('market', m, MARKET_ZH[m])));
  }

  const HOLDING_COLS = [
    { key: 'symbol', label: '代號 / 名稱', text: true },
    { key: 'market', label: '市場', text: true },
    { key: 'account_id', label: '帳戶', text: true },
    { key: 'shares', label: '股數' },
    { key: 'original_avg', label: '原始均價' },
    { key: 'adjusted_avg', label: '調整均價' },
    { key: 'market_price', label: '現價' },
    { key: '_spark', label: '30 日走勢', nosort: true },
    /* 市值 / 未實現損益 are ORIGINAL-CURRENCY Decimal strings, so a bare numeric compare
       ranked 32,340 MYR above 26,915 USD while the 權重 column on the same rows said the
       opposite (M1-02). The frontend may not convert money (CLAUDE.md), so:
       · 市值 sorts by `weight` — already the report-currency market-value share, so its
         order IS the report-currency market-value order, at zero computation.
       · 未實現損益 has no such report-currency twin in the payload, and inventing one would
         mean a new API field. It therefore sorts CURRENCY-FIRST, value-second: the ranking
         is honest inside each currency and never claims a cross-currency one. */
    { key: 'market_value', label: '市值', sortBy: 'weight',
      note: '依報告幣別市值排序（＝權重順序）' },
    { key: 'unrealized_pnl', label: '未實現損益', ccyScoped: true,
      note: '原幣金額：排序先分幣別、同幣別內比大小（前端不做匯率換算）' },
    { key: 'payback_ratio', label: '股利回收率' },
    { key: 'weight', label: '權重' }
  ];

  function renderHoldingsHead() {
    const tr = $('#holdings-head');
    tr.replaceChildren();
    HOLDING_COLS.forEach((c) => {
      if (c.nosort) {
        tr.appendChild(el('th', c.text ? 'col-text' : null, c.label));
        return;
      }
      const th = el('th', 'sortable' + (c.text ? ' col-text' : ''), c.label);
      /* M1-02: a column whose sort is not the naive numeric one says so on hover, and — while
         it IS the active sort — in the header itself, so the ordering on screen is never
         unexplained. */
      if (c.note) th.title = c.note;
      if (holdingsState.sortKey === c.key) {
        th.appendChild(el('span', 'arrow', holdingsState.sortDir > 0 ? '▲' : '▼'));
        if (c.ccyScoped) {
          const m = el('span', 'sort-scope', '（分幣別）');
          m.style.cssText = 'font-weight:400;color:var(--text-3);font-size:10px;margin-left:2px';
          th.appendChild(m);
        }
      }
      th.addEventListener('click', () => {
        if (holdingsState.sortKey === c.key) holdingsState.sortDir *= -1;
        else { holdingsState.sortKey = c.key; holdingsState.sortDir = c.text ? 1 : -1; }
        renderHoldingsHead();
        renderHoldings();
      });
      tr.appendChild(th);
    });
  }

  function sortedFilteredHoldings() {
    let rows = D.holdings.filter((h) =>
      (holdingsState.account === 'all' || h.account_id === holdingsState.account) &&
      (holdingsState.market === 'all' || h.market === holdingsState.market));
    const k = holdingsState.sortKey;
    if (k) {
      const dir = holdingsState.sortDir;
      /* Numeric columns now arrive as Decimal STRINGS over the wire, so we cannot rely
         on typeof to pick string-vs-number compare. Use the column's declared `text`
         flag instead: text columns sort lexically, numeric columns compare as numbers
         (string−string coerces to a number; that is display-ordering, not money math). */
      const col = HOLDING_COLS.find((c) => c.key === k);
      const isText = !!(col && col.text);
      /* A column may sort on a DIFFERENT field than it displays (`sortBy`) — 市值 borrows
         `weight`, the report-currency share of exactly that quantity, because the frontend
         must not convert currencies to compare them (M1-02). */
      const sk = (col && col.sortBy) || k;
      const ccyScoped = !!(col && col.ccyScoped);
      rows = rows.slice().sort((a, b) => {
        const av = a[sk], bv = b[sk];
        if (av === null || av === undefined) return 1;   /* nulls last */
        if (bv === null || bv === undefined) return -1;
        if (isText) return String(av).localeCompare(String(bv)) * dir;
        /* currency-scoped: group by quote_ccy FIRST (a fixed A→Z order, independent of the
           sort direction, so the currency blocks never reshuffle), then rank within it. */
        if (ccyScoped) {
          const ac = String(a.quote_ccy || ''), bc = String(b.quote_ccy || '');
          if (ac !== bc) return ac.localeCompare(bc);
        }
        return (Number(av) - Number(bv)) * dir;
      });
    }
    return rows;
  }

  /* E2: 30日迷你走勢圖（inline SVG，紅漲綠跌依 30 日變動）
     Consumes the holding's spark_30d (a Decimal-STRING array from /api/dashboard);
     each point is mapped through Number() for the SVG geometry only (the coordinate
     math is display-derived, not money of record). */
  function sparkline(spark) {
    if (!Array.isArray(spark) || spark.length < 2) {
      const sp = el('span', 'sign-nil', f.NULL_GLYPH);
      sp.title = '無歷史價格';
      return sp;
    }
    const pts = spark.slice(-22).map((p) => Number(p));
    const w = 72, hh = 22, pad = 2;
    const min = Math.min(...pts), max = Math.max(...pts);
    const span = max - min || 1;
    const step = (w - pad * 2) / (pts.length - 1);
    const coords = pts.map((v, i) =>
      (pad + i * step).toFixed(1) + ',' + (hh - pad - ((v - min) / span) * (hh - pad * 2)).toFixed(1));
    const chg = (pts[pts.length - 1] - pts[0]) / pts[0];
    const color = chg > 0 ? 'var(--up)' : chg < 0 ? 'var(--down)' : 'var(--text-3)';
    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('width', w);
    svg.setAttribute('height', hh);
    svg.setAttribute('viewBox', '0 0 ' + w + ' ' + hh);
    svg.classList.add('sparkline');
    const poly = document.createElementNS(svgNS, 'polyline');
    poly.setAttribute('points', coords.join(' '));
    poly.setAttribute('fill', 'none');
    poly.setAttribute('stroke', color);
    poly.setAttribute('stroke-width', '1.3');
    svg.appendChild(poly);
    const dot = document.createElementNS(svgNS, 'circle');
    const lastXY = coords[coords.length - 1].split(',');
    dot.setAttribute('cx', lastXY[0]);
    dot.setAttribute('cy', lastXY[1]);
    dot.setAttribute('r', '1.8');
    dot.setAttribute('fill', color);
    svg.appendChild(dot);
    const wrap = el('span', 'spark-wrap');
    wrap.title = '30 日 ' + f.signedPct(chg);
    wrap.appendChild(svg);
    return wrap;
  }

  /* The 合計 footer FOLLOWS the active (account, market) filter. The server pre-aggregates
     every filter cell in the reporting currency (D.holdings_subtotals); the client only
     SELECTS the matching cell and PRINTS its Decimal STRINGS — it never sums money in JS
     (money is computed server-side). 'all' on an axis maps to null (the server's "all"
     marker), so (all, all) is the grand cell and equals the KPI totals — unchanged from
     before. A combo the server emitted no cell for (a filter with no holdings) falls back to
     an honest zero cell so the footer stays truthful. */
  function holdingsSubtotal() {
    const norm = (v) => (v === undefined || v === null ? null : v);
    const acct = holdingsState.account === 'all' ? null : holdingsState.account;
    const mkt = holdingsState.market === 'all' ? null : holdingsState.market;
    const list = (D && D.holdings_subtotals) || [];
    const hit = list.find((s) => norm(s.account_id) === acct && norm(s.market) === mkt);
    return hit || { total_market_value: '0', unrealized_total: '0' };
  }

  /* Body for the filtered holdings exports (CSV / 報告): the active chips, with 'all' on an
     axis OMITTED (undefined -> dropped by JSON.stringify) so the backend serves the full set
     for that axis. Sent via pdApi.download -> POST /api/export/*; the backend re-computes the
     filtered snapshot (no client math). */
  function holdingsFilterBody() {
    return {
      account: holdingsState.account === 'all' ? undefined : holdingsState.account,
      market: holdingsState.market === 'all' ? undefined : holdingsState.market
    };
  }

  function renderHoldings() {
    const tbody = $('#holdings-body');
    /* M1-01: the loading text lives on the .table-wrap (never inside <tbody>); clear it
       first, so an empty `rows` draws the real empty table, not 載入中…. */
    const wrap = tbody.closest('.table-wrap');
    if (wrap) wrap.classList.remove('is-loading');
    tbody.replaceChildren();
    const rows = sortedFilteredHoldings();
    const maxWeight = Math.max(...D.holdings.map((h) => h.weight || 0));
    const maxPayback = Math.max(...D.holdings.map((h) => h.payback_ratio || 0));
    /* Mini-bar fill widths go through the module-scope `barWidth` clamp — see its note. */

    rows.forEach((h) => {
      const tr = el('tr');
      if (h.market_price === null || h.market_price === undefined || h.price_stale) {
        tr.classList.add('row-stale');
        tr.title = h.market_price === null || h.market_price === undefined
          ? '缺價 — 此列數字不可信' : '價格過期 — 損益以舊價計算';
      }
      /* Two different negative-share states, and they must never look alike:
         `oversold` is an unresolved DATA problem (basis discarded, 待釐清), while
         `short_open` is a real declared short with a real basis and real P&L.
         The chain shows ONE state, so it runs in descending severity: a discarded basis,
         then a wrong SHARE COUNT (unbookable_action — every valued figure is off by the
         action's ratio), then a missing payout (unbookable_dividend — the share count is
         right and the row is short by exactly one dividend), then a healthy short. */
      if (h.oversold) {
        tr.classList.add('row-stale');
        tr.title = '賣超：賣出數量超過持股，部位為負、損益待釐清'
          + '（請補記期初庫存或遺漏的買進）';
      } else if (h.unbookable_action) {
        tr.classList.add('row-stale');
        tr.title = '公司行動未套用：股數仍是行動前的數字，價格卻已是行動後的，'
          + '市值與未實現損益因此失真（請修正該筆公司行動或補齊持倉紀錄）';
      } else if (h.unbookable_dividend) {
        tr.classList.add('row-stale');
        tr.title = '放空期間有股利紀錄：放空方需支付股利，此筆未列入計算，'
          + '本列數字少計該筆金額（請改以現金收支登錄）';
      } else if (h.revived_by_dividend) {
        /* QA-06 — informs, does NOT mark the row stale: nothing here is 待釐清. */
        tr.title = '配息復活：此部位原本已清空，是配股／DRIP 再投入才重新有股數，'
          + '成本基礎為零，因此整筆市值都會顯示為未實現獲利（請確認確有收到這些股份）';
      } else if (h.short_open) {
        tr.title = '放空中：已宣告的放空部位，成本基礎為賣出時收到的價款；'
          + '買回時以買回價結算損益';
      }

      /* 代號 + 名稱 + board badge — 點擊開啟個股詳情 */
      const tdSym = el('td', 'col-text');
      const cell = el('div', 'sym-cell sym-link');
      cell.title = '點擊查看個股詳情（價格與成本、配息史、試算）';
      cell.appendChild(el('span', 'sym-code', h.symbol));
      cell.appendChild(el('span', 'sym-name', h.name));
      if (h.board) cell.appendChild(el('span', 'board-badge', h.board));
      if (h.oversold) {
        const ob = el('span', 'badge badge-missing', '賣超');
        ob.title = '賣出數量超過持股，部位為負、損益待釐清';
        cell.appendChild(ob);
      } else if (h.unbookable_action) {
        const ab = el('span', 'badge badge-missing', '股數待釐清');
        ab.title = '公司行動未套用：股數為行動前、價格為行動後，市值與損益失真';
        cell.appendChild(ab);
      } else if (h.unbookable_dividend) {
        const db = el('span', 'badge badge-missing', '股利待釐清');
        db.title = '放空期間出現股利紀錄，未列入計算（放空方需支付股利）';
        cell.appendChild(db);
      } else if (h.revived_by_dividend) {
        const rb = el('span', 'badge badge-revived', '配息復活');
        rb.title = '部位已清空後由配股／DRIP 重新產生，成本基礎為零，市值全數列為未實現獲利';
        cell.appendChild(rb);
      } else if (h.short_open) {
        const sb = el('span', 'badge badge-short', '放空中');
        sb.title = '已宣告的放空部位；成本基礎為賣出價款，買回時結算損益';
        cell.appendChild(sb);
      }
      cell.addEventListener('click', () => {
        /* Hand the drawer the ROW this click came from (identity lookup into the payload
           order the drawer cycles over — no math). A symbol held in two accounts occupies
           two rows, so ←/→ afterwards must continue from THIS one; without the hint it
           resumed from the first row carrying the symbol (M2-04). */
        if (window.openSymbolDrawer) {
          window.openSymbolDrawer(h.symbol, { index: D.holdings.indexOf(h) });
        }
      });
      tdSym.appendChild(cell);
      tr.appendChild(tdSym);

      tr.appendChild(el('td', 'col-text', MARKET_ZH[h.market] || h.market));
      tr.appendChild(el('td', 'col-text', acctZh(h.account_id)));
      tr.appendChild(el('td', 'num', f.shares(h.shares)));
      tr.appendChild(el('td', 'num', f.price(h.original_avg, h.quote_ccy)));
      tr.appendChild(el('td', 'num', f.price(h.adjusted_avg, h.quote_ccy)));

      /* 現價 (+ 過期 / 缺價 badge) */
      const tdPrice = el('td', 'num');
      if (h.market_price === null || h.market_price === undefined) {
        tdPrice.appendChild(el('span', 'sign-nil', f.NULL_GLYPH + ' '));
        const b = el('span', 'badge badge-missing', '缺價');
        b.title = '無法取得價格資料';
        tdPrice.appendChild(b);
      } else {
        tdPrice.appendChild(el('span', null, f.price(h.market_price, h.quote_ccy)));
        if (h.price_stale) {
          tdPrice.appendChild(document.createTextNode(' '));
          const b = el('span', 'badge badge-stale-mini', '過期');
          b.title = '價格日期 ' + f.date(h.price_as_of);
          tdPrice.appendChild(b);
        }
      }
      tr.appendChild(tdPrice);

      /* 30日 sparkline (E2) — from the holding's spark_30d (Decimal-string array) */
      const tdSpark = el('td', 'spark-cell');
      tdSpark.appendChild(sparkline(h.spark_30d));
      tr.appendChild(tdSpark);

      /* 市值 (native ccy) */
      const tdMv = el('td', 'num');
      if (h.market_value === null || h.market_value === undefined) {
        tdMv.textContent = f.NULL_GLYPH;
        tdMv.classList.add('sign-nil');
      } else {
        tdMv.textContent = f.money(h.market_value, h.quote_ccy);
        tdMv.appendChild(el('span', 'kpi-unit', ' ' + h.quote_ccy));
      }
      tr.appendChild(tdMv);

      /* 未實現損益 value + %, both SERVER-computed.

         The drawer was moved onto `unrealized_pct` by audit H1 (2026-07-26); this table
         kept its own `unrealized_pnl / adjusted_cost_total` divide and so kept the bug the
         drawer's comment describes. Two ways it lies, both legal states, not edge cases:
         a fully-recovered holding has adjusted cost <= 0 (domain-ledger.md: dividends may
         push it below zero, never floored) and an open SHORT carries a negative basis by
         construction. Either flips the ratio's sign. Measured 2026-08-05 on a one-short
         ledger: −75.98 USD of unrealized LOSS rendered as "+3.17%", in the same cell.
         `unrealized_pct` divides by abs(ORIGINAL cost) — the same basis as 回本進度 and the
         KPI 累計報酬率 — so the whole page now reads on one basis, and CLAUDE.md's "the
         frontend never computes money or returns" holds here too. */
      const tdPnl = el('td', 'num ' + f.signClass(h.unrealized_pnl));
      if (h.unrealized_pnl === null || h.unrealized_pnl === undefined) {
        tdPnl.textContent = f.NULL_GLYPH;
      } else {
        tdPnl.appendChild(el('span', null, f.signed(h.unrealized_pnl, h.quote_ccy)));
        if (h.unrealized_pct !== null && h.unrealized_pct !== undefined) {
          tdPnl.appendChild(el('span', 'subpct', f.signedPct(h.unrealized_pct)));
        }
      }
      tr.appendChild(tdPnl);

      /* 股利回收率 mini progress */
      const tdPb = el('td', 'num');
      if (h.payback_ratio === null || h.payback_ratio === undefined) {
        tdPb.textContent = f.NULL_GLYPH;
        tdPb.classList.add('sign-nil');
      } else {
        const wrap = el('span', 'mini-bar');
        const track = el('span', 'track');
        const fill = el('span', 'fill payback');
        fill.style.width = barWidth(h.payback_ratio, maxPayback);
        track.appendChild(fill);
        wrap.appendChild(track);
        wrap.appendChild(el('span', null, f.pct(h.payback_ratio)));
        /* M1-03 / D21 — the column is too narrow for the sentence (owner ruling: chip on
           the narrow column, full text on the drawer and 股利總覽), so: a muted 「承接」 chip,
           the whole provenance on hover. */
        if (h.payback_from_symbol) {
          const chip = el('span', 'badge badge-window-mini', '承接');
          chip.title = paybackProvenance(h);
          wrap.appendChild(chip);
        }
        tdPb.appendChild(wrap);
      }
      tr.appendChild(tdPb);

      /* 權重 mini bar + % */
      const tdW = el('td', 'num');
      if (h.weight === null || h.weight === undefined) {
        tdW.textContent = f.NULL_GLYPH;
        tdW.classList.add('sign-nil');
      } else {
        const wrap = el('span', 'mini-bar');
        const track = el('span', 'track');
        const fill = el('span', 'fill');
        fill.style.width = barWidth(h.weight, maxWeight);
        track.appendChild(fill);
        wrap.appendChild(track);
        wrap.appendChild(el('span', null, f.pct(h.weight)));
        tdW.appendChild(wrap);
      }
      tr.appendChild(tdW);

      tbody.appendChild(tr);
    });

    /* totals row — the subtotal FOLLOWS the active filter, selected from the server's
       per-cell reporting-currency aggregation (holdingsSubtotal()). No client money math. */
    const tfoot = $('#holdings-foot');
    tfoot.replaceChildren();
    const tr = el('tr');
    const tdLabel = el('td', 'col-text', '合計（' + D.reporting_currency + '，缺價標的除外）');
    tdLabel.colSpan = 8;
    tr.appendChild(tdLabel);
    const sub = holdingsSubtotal();
    const tdMv = el('td', 'num');
    tdMv.textContent = f.money(sub.total_market_value, D.reporting_currency);
    tr.appendChild(tdMv);
    const pnl = sub.unrealized_total;
    const tdPnl = el('td', 'num ' + f.signClass(pnl), f.signed(pnl, D.reporting_currency));
    tr.appendChild(tdPnl);
    const tdRest = el('td');
    tdRest.colSpan = 2;
    tr.appendChild(tdRest);
    tfoot.appendChild(tr);
    /* Row count changed the holdings-table width — refresh the scroll fade state
       (hoisted; safe to call before its declaration further down). */
    enhanceTableScroll();
  }

  /* ============ E2. 幣別組成 ============ */
  function renderCurrencyView() {
    const panel = $('#ccy-content');
    panel.replaceChildren();
    const cv = D.currency_view;
    if (!cv) {
      panel.appendChild(emptyState('匯率資料不足，無法合併計價'));
      return;
    }
    const head = el('div', 'ccy-headline');
    const big = el('span', 'num', f.money(cv.reporting_total_value, cv.reporting_currency));
    head.appendChild(big);
    head.appendChild(el('span', 'cap', cv.reporting_currency + ' 合併計價總值'));
    panel.appendChild(head);

    /* share of each currency derived from holdings[].weight (reporting terms);
       holdings with null weight (缺價) are excluded. weight is a RATIO (not money)
       and arrives as a Decimal STRING — summing with `+` concatenated strings and
       rendered NaN% whenever a currency held 2+ positions (fixed 2026-07-03).
       Coercing a display-only ratio is the documented input-side exception. */
    const shareByCcy = {};
    let excluded = 0;
    D.holdings.forEach((h) => {
      const w = Number(h.weight);
      if (h.weight === null || h.weight === undefined || !isFinite(w)) {
        excluded += 1;
        return;
      }
      shareByCcy[h.quote_ccy] = (shareByCcy[h.quote_ccy] || 0) + w;
    });

    const stack = el('div', 'ccy-stack');
    Object.keys(cv.by_currency_value).forEach((ccy) => {
      if (!shareByCcy[ccy]) return;
      const seg = el('span', 'seg');
      /* The share is already a fraction of 1, so the whole bar IS the denominator. Same
         clamp as the mini-bars — an unclamped share here does not merely mis-draw its own
         segment, it discards the negative one and rescales all the rest (see `barWidth`). */
      seg.style.width = barWidth(shareByCcy[ccy], 1);
      seg.style.background = CCY_COLOR[ccy] || '#777';
      seg.title = ccy + ' ' + f.pct(shareByCcy[ccy]);
      stack.appendChild(seg);
    });
    panel.appendChild(stack);

    const rows = el('div', 'ccy-rows');
    Object.keys(cv.by_currency_value).forEach((ccy) => {
      const row = el('div', 'ccy-row');
      const key = el('div', 'ccy-key');
      const sw = el('span', 'ccy-swatch');
      sw.style.background = CCY_COLOR[ccy] || '#777';
      key.appendChild(sw);
      key.appendChild(el('span', 'ccy-code', ccy));
      row.appendChild(key);
      row.appendChild(el('span', 'ccy-share',
        shareByCcy[ccy] !== undefined ? '權重 ' + f.pct(shareByCcy[ccy]) : ''));
      const amt = el('span', 'ccy-amt', f.money(cv.by_currency_value[ccy], ccy));
      amt.appendChild(el('span', 'kpi-unit', ' ' + ccy));
      row.appendChild(amt);
      rows.appendChild(row);
    });
    panel.appendChild(rows);
    panel.appendChild(el('div', 'ccy-note',
      '各列為原幣金額；權重以報告幣別市值計算' +
      (excluded ? '，缺價標的（' + excluded + '）不計入權重。' : '。')));
  }

  /* ============ F2. 各帳戶現金 (R6 item 7) ============
     Separate lightweight GET /api/cash (dashboard payload untouched); degrades
     to a hint on failure. Amounts are Decimal STRINGS via fmt. */
  async function renderCashMini() {
    const host = $('#cash-mini');
    if (!host) return;
    let resp;
    try {
      resp = await window.pdApi.get('/api/cash');
    } catch (err) {
      host.replaceChildren(el('div', 'hint', '現金資料載入失敗'));
      return;
    }
    host.replaceChildren();
    const balances = (resp && resp.balances) || [];
    if (!balances.length) {
      host.appendChild(el('div', 'hint',
        '尚無現金紀錄 — 到「資金管理」補一筆初始入金，現金池就會開始追蹤。'));
      return;
    }
    const grid = el('div', 'cash-mini-grid');
    const byAcct = new Map();
    balances.forEach((b) => {
      /* G-01: the card header resolves through acctZh (names.js), NOT the payload's English
         `account` — this card sat one panel away from the 持倉表, whose 帳戶 column already
         said 嘉信 Schwab while this one said Charles Schwab. */
      if (!byAcct.has(b.account_id)) {
        byAcct.set(b.account_id, { name: acctZh(b.account_id), lines: [] });
      }
      byAcct.get(b.account_id).lines.push(b);
    });
    byAcct.forEach((entry) => {
      const card = el('div', 'cash-mini-card');
      card.appendChild(el('div', 'acct', entry.name));
      entry.lines.forEach((b) => {
        const line = el('div', 'line num');
        const neg = String(b.amount).indexOf('-') === 0;
        line.textContent = b.ccy + '  ' + f.money(b.amount, b.ccy);
        if (neg) {
          line.classList.add('neg');
          line.title = '負現金 — 通常代表漏記入金或換匯';
        }
        card.appendChild(line);
      });
      grid.appendChild(card);
    });
    host.appendChild(grid);
    if (resp.reporting_total != null) {
      host.appendChild(el('div', 'hint',
        '合併現金（' + resp.reporting_currency + '）: ' +
        f.money(resp.reporting_total, resp.reporting_currency) + ' ' + resp.reporting_currency));
    }
  }

  /* ============ I2. 月度成績 (R6 item 8) ============ */
  async function renderSnapshots() {
    const tbody = $('#snapshots-body');
    if (!tbody) return;
    let resp;
    try {
      resp = await window.pdApi.get('/api/snapshots', { limit: 12 });
    } catch (err) {
      return;  // non-critical panel: stay empty on failure
    }
    const rows = (resp && resp.rows) || [];
    tbody.replaceChildren();
    if (!rows.length) {
      const tr = el('tr');
      const td = el('td', 'sign-nil', '尚無快照 — 排程每晚 23:50 產生當月快照，月底值即定格。');
      td.colSpan = 5;
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    rows.forEach((s) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'col-text num', s.month));
      const cell = (v, pct) => {
        const td = el('td', 'num');
        if (v == null) { td.textContent = f.NULL_GLYPH; td.classList.add('sign-nil'); }
        else td.textContent = pct ? f.signedPct(v) : f.money(v, s.reporting_ccy);
        return td;
      };
      tr.appendChild(cell(s.total_value, false));
      const ret = el('td', 'num ' + f.signClass(s.total_return));
      ret.textContent = s.total_return == null ? f.NULL_GLYPH : f.signed(s.total_return, s.reporting_ccy);
      tr.appendChild(ret);
      tr.appendChild(cell(s.total_return_rate, true));
      tr.appendChild(cell(s.xirr, true));
      tbody.appendChild(tr);
    });
  }

  /* ============ F. 換匯損益 ============ */
  function renderFx() {
    const grid = $('#fx-grid');
    const footer = $('#fx-footer');
    grid.replaceChildren();
    footer.replaceChildren();
    const fx = D.fx;
    if (!fx) {
      grid.appendChild(emptyState('匯率資料不足，無法合併計價'));
      /* The section is gone — say WHICH rate took it (server-authored, verbatim). Until
         QA-01 this state carried no explanation at all, which is how one empty account's
         missing rate erased a correct 33,000 TWD without anyone noticing. */
      const why = D.freshness && D.freshness.fx_unavailable_reason;
      if (why) {
        const note = el('div', 'fx-note', why);
        note.style.gridColumn = '1 / -1';
        grid.appendChild(note);
      }
      return;
    }
    Object.keys(fx.by_account).forEach((id) => {
      const a = fx.by_account[id];
      const card = el('div', 'fx-card');
      const head = el('div', 'fx-card-head');
      head.appendChild(el('span', 'fx-account', acctZh(a.account_id)));
      head.appendChild(el('span', 'fx-pair', a.foreign_ccy + ' → ' + a.home_ccy));
      card.appendChild(head);

      const rates = el('div', 'fx-rates');
      const b1 = el('div', 'fx-rate-block');
      b1.appendChild(el('span', 'fx-rate-label', '平均取得匯率'));
      b1.appendChild(el('span', 'fx-rate-val', f.rate(a.avg_rate)));
      rates.appendChild(b1);
      rates.appendChild(el('span', 'fx-arrow', '→'));
      const b2 = el('div', 'fx-rate-block');
      b2.appendChild(el('span', 'fx-rate-label', '現時匯率'));
      b2.appendChild(el('span', 'fx-rate-val', f.rate(a.current_spot)));
      rates.appendChild(b2);
      /* Rate delta (現時 − 平均取得) is SERVER-computed (`spot_delta`, audit L2 2026-07-26 —
         supersedes the earlier "left intentionally" note). Rates are formally not money, but
         subtracting two Decimal STRINGS in JS relies on implicit coercion and yields a silent
         NaN if either side is ever non-numeric; the server has both values exactly. */
      if (a.spot_delta !== null && a.spot_delta !== undefined) {
        rates.appendChild(el('span', 'fx-delta ' + f.signClass(a.spot_delta),
          f.signedNum(a.spot_delta, Number(a.current_spot) < 10 ? 4 : 2)));
      }
      card.appendChild(rates);

      const stats = el('div', 'fx-stats');
      /* Flag on the server's CAUSE boolean, never on `fx_basis_gap != 0`: the gap is an
         amount and collapses to zero on an empty pool, while covered_ratio (and the scaled
         stock leg) can still be incomplete. `gapAmt` only decides whether the amount is
         worth naming in the note — tested on the STRING via signClass, no JS coercion. */
      const hasGap = !!a.fx_basis_incomplete;
      const gapAmt = a.fx_basis_gap != null && f.signClass(a.fx_basis_gap) !== 'sign-flat';
      /* 未實現匯損益（合計）is server-computed (unrealized_fx_total, Decimal string =
         stocks + cash, null when either is null). The frontend only DISPLAYS it — it must
         NEVER re-sum the two component strings in JS (that is float money math over exact
         Decimal values; the locked invariant forbids money arithmetic in the frontend). */
      const items = [
        ['外幣現金', a.foreign_cash, a.foreign_ccy, false],
        ['外幣股票市值', a.foreign_stock_value, a.foreign_ccy, false],
        ['已實現匯損益', a.realized_fx, a.home_ccy, true],
        ['未實現匯損益（股票）', a.unrealized_fx_stocks, a.home_ccy, true],
        ['未實現匯損益（現金）', a.unrealized_fx_cash, a.home_ccy, true],
        ['未實現匯損益（合計）', a.unrealized_fx_total ?? null, a.home_ccy, true]
      ];
      items.forEach(([k, v, ccy, isSigned]) => {
        const st = el('div', 'fx-stat');
        st.appendChild(el('span', 'k', k));
        const vv = el('span', 'v');
        if (v === null || v === undefined) {
          vv.textContent = f.NULL_GLYPH;
          vv.title = '無換匯紀錄或匯率資料不足';
          vv.classList.add('sign-nil');
        } else {
          vv.textContent = (isSigned ? f.signed : f.money)(v, ccy) + ' ' + ccy;
          if (isSigned) vv.classList.add(f.signClass(v));
        }
        /* Two INDEPENDENT server-decided flags (spec 2026-07-30):
           - `fx_basis_gap` (CAUSE) — some foreign funding carries no acquisition cost, so
             `avg_rate` came from an incomplete population and BOTH unrealized legs are
             scaled by `covered_ratio`. Fires whether or not the pool is negative; the old
             symptom-only flag went silent the moment the pool crossed back above zero.
           - `foreign_cash_negative` (SYMPTOM) — the pool itself is below zero. Now that
             movements are counted this equals the funds view, so it means the ledger is
             inconsistent, not merely incomplete. */
        if (k === '外幣現金' && (hasGap || a.foreign_cash_negative)) {
          vv.classList.add('fx-flagged');
          vv.title = hasGap ? '部分外幣資金流沒有取得成本，未計入匯損益基數'
                            : '外幣現金池為負：帳本不一致';
        }
        if (hasGap && (k === '未實現匯損益（股票）' || k === '未實現匯損益（現金）'
                       || k === '未實現匯損益（合計）')) {
          vv.classList.add('fx-flagged');
          vv.title = '成本基礎不完整：本欄以覆蓋率縮放後計算';
        }
        /* 已實現 is NOT scaled — a reconversion really happened and its home proceeds are
           an actual amount. But its COST side uses the same incomplete avg_rate, so the
           figure is still only as good as the basis; flag it rather than imply otherwise. */
        if (hasGap && k === '已實現匯損益') {
          vv.classList.add('fx-flagged');
          vv.title = '成本基礎不完整：本欄的成本側用的是同一個不完整的平均取得匯率';
        }
        st.appendChild(vv);
        stats.appendChild(st);
      });
      card.appendChild(stats);
      if (hasGap) {
        card.appendChild(el('div', 'fx-note',
          '⚠ 匯率成本基礎不完整 — 本帳戶'
          + (gapAmt ? '有 ' + f.money(a.fx_basis_gap, a.foreign_ccy) + ' ' + a.foreign_ccy
                      + ' 的外幣資金流' : '的部分外幣資金流')
          + '沒有取得成本，未納入加權平均。'
          + '「平均取得匯率」因此是以不完整的母體計算：未實現匯損益的'
          + '「股票」與「現金」兩條腿都已依覆蓋率 ' + f.pct(a.covered_ratio)
          + ' 縮放後呈現，已實現匯損益的成本側也用同一個不完整的均價。'
          + '請至資金管理為該筆入金／期初資金補上取得成本。'));
      }
      if (a.foreign_cash_negative) {
        card.appendChild(el('div', 'fx-note',
          '⚠ 外幣現金為負 — 帳上不可能出現負現金，代表此帳戶的帳本不一致'
          + '（漏記入金／換匯，或有回溯編輯）。請至資金管理核對該幣別的流水。'));
      }
      grid.appendChild(card);
    });

    /* PARTIAL ROLLUP (QA-02): one or more accounts hold money that could not be expressed
       in the reporting currency, so the two totals below are short by exactly that much.
       The banner carries the server's own wording (which accounts, which FX pair, which of
       the two figures) verbatim; it spans the whole grid so it reads as a panel-level
       warning rather than one account's card note. */
    if (fx.reporting_unavailable_reason) {
      const note = el('div', 'fx-note', fx.reporting_unavailable_reason);
      note.style.gridColumn = '1 / -1';
      grid.appendChild(note);
    }

    const mk = (label, v) => {
      const s = el('span', null, label + ' ');
      const vv = el('span', 'v ' + f.signClass(v), f.signed(v, fx.reporting_currency) + ' ' + fx.reporting_currency);
      s.appendChild(vv);
      return s;
    };
    /* The LABEL degrades with the figure — a partial sum must not be headed 「合計」 flat
       (same discipline as the benchmark card's 「差額（部分涵蓋）」). */
    footer.appendChild(el('span', null, fx.reporting_unavailable_reason
      ? '報告幣別合計（部分帳戶未納入）：' : '報告幣別合計：'));
    footer.appendChild(mk('已實現', fx.reporting_realized_fx));
    footer.appendChild(mk('未實現', fx.reporting_unrealized_fx));

    /* collapsed-state summary chips */
    const sum = $('#fx-summary');
    if (sum) {
      sum.replaceChildren();
      [['已實現', fx.reporting_realized_fx], ['未實現', fx.reporting_unrealized_fx]].forEach(([k, v]) => {
        const chip = el('span', 'ccy-chip');
        chip.appendChild(el('span', null, k + ' '));
        chip.appendChild(el('b', f.signClass(v), f.signed(v, fx.reporting_currency) + ' ' + fx.reporting_currency));
        sum.appendChild(chip);
      });
      /* The chips are what a COLLAPSED panel shows, so the partial marker has to travel
         with them — otherwise the collapsed state is the one place a partial total still
         reads as complete (QA-02). Reason verbatim in the tooltip. */
      if (fx.reporting_unavailable_reason) {
        const warn = el('span', 'ccy-chip', '部分帳戶未納入');
        warn.title = fx.reporting_unavailable_reason;
        sum.appendChild(warn);
      }
    }
  }

  /* ============ G. 已實現損益 ============ */
  function renderRealized() {
    const tbody = $('#realized-body');
    tbody.replaceChildren();
    D.realized.rows.forEach((r) => {
      const tr = el('tr');
      const tdSym = el('td', 'col-text');
      const cell = el('div', 'sym-cell sym-link');
      cell.title = '點擊查看個股詳情';
      cell.appendChild(el('span', 'sym-code', r.symbol));
      cell.addEventListener('click', () => {
        if (window.openSymbolDrawer) window.openSymbolDrawer(r.symbol);
      });
      tdSym.appendChild(cell);
      /* A post-close cash dividend is realized INCOME, not a sale (audit H2). Mark it, and
         show 賣出股數 / 調整成本移除 as 不適用 rather than a misleading 0. */
      const isDiv = r.kind === 'dividend';
      if (isDiv) {
        const chip = el('span', 'rz-kind', '股利');
        chip.title = '清倉後入帳的現金股利 — 已無成本可沖減，列為已實現收益';
        tdSym.appendChild(chip);
      }
      tr.appendChild(tdSym);
      tr.appendChild(el('td', 'col-text', acctZh(r.account_id)));
      tr.appendChild(el('td', 'num', isDiv ? f.NULL_GLYPH : f.shares(r.shares_sold)));
      const tdProceeds = el('td', 'num', f.money(r.proceeds_net, r.quote_ccy));
      tdProceeds.appendChild(el('span', 'kpi-unit', ' ' + r.quote_ccy));
      tr.appendChild(tdProceeds);
      tr.appendChild(el('td', 'num',
        isDiv ? f.NULL_GLYPH : f.money(r.adjusted_cost_removed, r.quote_ccy)));
      tr.appendChild(el('td', 'num ' + f.signClass(r.realized), f.signed(r.realized, r.quote_ccy)));
      tbody.appendChild(tr);
    });
    const footer = $('#realized-footer');
    footer.replaceChildren();
    footer.appendChild(el('span', null, '各幣別合計'));
    Object.keys(D.realized.by_currency).forEach((ccy) => {
      const v = D.realized.by_currency[ccy];
      const chip = el('span', 'ccy-chip');
      chip.appendChild(el('span', null, ccy + ' '));
      const b = el('b', f.signClass(v), f.signed(v, ccy));
      chip.appendChild(b);
      footer.appendChild(chip);
    });
  }

  /* ============ H. 股利區 ============
     REMOVED (FU-D47, 2026-07-19): the legacy 年度股利 chips + 除息日曆 (list/month views,
     client-side 入帳預覽/年內預估 float math) were consolidated into the single
     #dividend-income-card surface rendered by dividends-card.js — which composes ONLY
     server-computed Decimal strings (dividend_projection replaces the client estimates). */

  /* ============ I. AI 洞察 ============ */
  function renderInsights() {
    const grid = $('#insight-grid');
    grid.replaceChildren();
    /* 額度 chip 已改為頂欄常駐（alerts.js）；面板內不再重複顯示 */
    if (!D.insights || D.insights.length === 0) {
      grid.appendChild(emptyState('尚無 AI 洞察 — 洞察卡片由排程批次產生'));
      return;
    }
    D.insights.forEach((ins) => {
      const card = el('div', 'insight-card');
      const head = el('div', 'insight-head');
      head.appendChild(el('span', 'badge badge-ai', 'AI'));
      head.appendChild(el('h3', 'insight-title', ins.title));
      if (ins.unreadable) {
        /* M7-08: the server could not read this card's stored prediction back (schema
           drift or a corrupt blob) and serves the narrative flagged. Same badge as the
           holdings table's 待釐清 marks — a data problem is shown, never hidden. */
        const pill = el('span', 'badge badge-missing', '預測待釐清');
        pill.title = '此卡片儲存的預測資料無法讀取，僅顯示敘述內容';
        head.appendChild(pill);
      }
      card.appendChild(head);
      /* Task-1.5 card shape: summary = concise body (body_md is the full markdown). */
      card.appendChild(el('p', 'insight-body', ins.summary));
      const foot = el('div', 'insight-foot');
      foot.appendChild(el('span', 'insight-time', f.datetime(ins.created_at)));
      /* cost_usd is a Decimal STRING — format via fmt (NOT .toFixed). "0" is a valid
         truthy-safe value, so nil-check with != null (catches null + undefined only). */
      /* Unified AI attribution (2026-07-07): model · token N · $cost — via fmt.aiAttrib;
         segments degrade when absent (legacy cards lack token counts). */
      const attrib = f.aiAttrib(ins.model, ins.tokens_in, ins.tokens_out, ins.cost_usd);
      if (attrib) {
        foot.appendChild(el('span', 'insight-cost ai-attrib num', attrib));
      }
      card.appendChild(foot);
      grid.appendChild(card);
    });
  }
  /* Empty-state variant for design review: set D.insights = [] and reload,
     or run renderInsightsEmptyPreview() from the console. */
  window.renderInsightsEmptyPreview = function () {
    const saved = D.insights;
    D.insights = [];
    renderInsights();
    D.insights = saved;
  };

  /* ============ J. 資料新鮮度 ============ */
  function renderFreshness() {
    const fr = D.freshness;
    if (!fr) return;
    const chips = $('#fresh-chips');
    chips.replaceChildren();
    (fr.missing_prices || []).forEach((s) => {
      chips.appendChild(el('span', 'badge badge-missing', '缺價 ' + s));
    });
    (fr.missing_fx || []).forEach((p) => {
      chips.appendChild(el('span', 'badge badge-missing', '缺匯率 ' + p));
    });

    const priceBody = $('#fresh-prices');
    priceBody.replaceChildren();
    fr.prices.forEach((p) => {
      const tr = el('tr');
      tr.appendChild(el('td', null, p.symbol));
      tr.appendChild(el('td', null, f.date(p.as_of)));
      const td = el('td');
      if (p.stale) td.appendChild(el('span', 'badge badge-stale-mini', '過期'));
      else td.appendChild(el('span', 'sign-nil', '—'));
      tr.appendChild(td);
      priceBody.appendChild(tr);
    });

    const fxBody = $('#fresh-fx');
    fxBody.replaceChildren();
    fr.fx.forEach((p) => {
      const tr = el('tr');
      tr.appendChild(el('td', null, p.base + '/' + p.quote));
      tr.appendChild(el('td', null, f.date(p.as_of)));
      const td = el('td');
      if (p.stale) td.appendChild(el('span', 'badge badge-stale-mini', '過期'));
      else td.appendChild(el('span', 'sign-nil', '—'));
      tr.appendChild(td);
      fxBody.appendChild(tr);
    });

    const notes = $('#fresh-notes');
    notes.replaceChildren();
    [fr.xirr_unavailable_reason, fr.trend_unavailable_reason].forEach((reason) => {
      if (reason) notes.appendChild(el('div', 'fresh-note', reason));
    });
  }

  /* shared empty state */
  function emptyState(msg) {
    const wrap = el('div', 'empty-state');
    wrap.appendChild(el('div', 'glyph', '∅'));
    wrap.appendChild(el('div', 'msg', msg));
    return wrap;
  }
  window.emptyState = emptyState;

  /* ============ E6: 鍵盤導航 (j/k/↑/↓ 移動、Enter 開抽屜) ============ */
  let kbIndex = -1;
  function kbRows() { return Array.from(document.querySelectorAll('#holdings-body tr')); }
  function kbHighlight() {
    kbRows().forEach((r, i) => r.classList.toggle('kb-focus', i === kbIndex));
  }
  document.addEventListener('keydown', (e) => {
    if (e.target.closest('input, textarea, select')) return;
    if (document.querySelector('.sd-backdrop') || document.querySelector('.search-backdrop')) return;
    const rows = kbRows();
    if (!rows.length) return;
    if (e.key === 'j' || e.key === 'ArrowDown') {
      if (e.key === 'ArrowDown' && kbIndex < 0) return; /* don't hijack page scroll until engaged */
      e.preventDefault();
      kbIndex = Math.min(kbIndex + 1, rows.length - 1);
      kbHighlight();
    } else if (e.key === 'k' || (e.key === 'ArrowUp' && kbIndex >= 0)) {
      e.preventDefault();
      kbIndex = Math.max(kbIndex - 1, 0);
      kbHighlight();
    } else if (e.key === 'Enter' && kbIndex >= 0 && kbIndex < rows.length) {
      const sym = rows[kbIndex].querySelector('.sym-code');
      if (sym && window.openSymbolDrawer) window.openSymbolDrawer(sym.textContent);
    } else if (e.key === 'Escape') {
      kbIndex = -1;
      kbHighlight();
    }
  });

  /* ============ 匯出按鈕（對帳級 CSV，直接由後端計算核心產生） ============ */
  /* Owner directive 2026-07-14: every 匯出 CSV goes through the backend reconciliation
     channel (pdApi.download → /api/export/*); the frontend no longer dumps rendered/
     display values. House style: silent on success, fail toast, pdBusy guards double
     clicks; the filename comes from the backend Content-Disposition. */
  function csvExportButton(label, path, bodyFn) {
    const b = el('button', 'btn btn-sm btn-export');
    b.type = 'button';
    b.title = '匯出對帳級 CSV（由後端計算核心產生）';
    b.appendChild(el('span', 'ico', '⬇'));
    b.appendChild(el('span', null, label));
    b.addEventListener('click', async () => {
      const restore = window.pdBusy ? window.pdBusy(b, '匯出中…') : function () {};
      try {
        await window.pdApi.download(path, bodyFn ? bodyFn() : {});
      } catch (err) {
        if (window.toast) window.toast(err && err.message ? err.message : '匯出失敗', 'fail', err && err.code);
      } finally {
        restore();
      }
    });
    return b;
  }

  function wireExports() {
    /* 持倉明細 → POST /api/export/holdings (existing reconciliation endpoint) */
    const holdingsHead = document.querySelector('#holdings-table');
    if (holdingsHead) {
      const panelHead = holdingsHead.closest('.panel').querySelector('.panel-head');
      panelHead.appendChild(csvExportButton('匯出 CSV', '/api/export/holdings',
        () => holdingsFilterBody()));
      /* 匯出報告: print-optimized 持倉報告 (self-contained HTML from the backend). Server
         recomputes everything (no client math). House style: silent on success, fail toast,
         busy state guards double-clicks. Compact tier + leading ⎙ icon so the whole holdings
         action row (再平衡試算 / 匯出 CSV / 匯出報告) reads as one coherent set. */
      const reportBtn = el('button', 'btn btn-sm pd-holdings-report-btn');
      reportBtn.appendChild(el('span', 'ico', '⎙'));
      reportBtn.appendChild(el('span', null, '匯出報告'));
      reportBtn.type = 'button';
      reportBtn.title = '下載持倉報告（可列印 HTML，含 KPI、持倉明細與配置）';
      reportBtn.addEventListener('click', async () => {
        const restore = window.pdBusy(reportBtn, '產出中…');
        try {
          await window.pdApi.download('/api/export/holdings-report', holdingsFilterBody());
        } catch (err) {
          if (window.toast) {
            window.toast(err && err.message ? err.message : '匯出報告失敗', 'fail', err && err.code);
          }
        } finally {
          restore();
        }
      });
      panelHead.appendChild(reportBtn);
    }
    /* 已實現損益 → POST /api/export/realized (new reconciliation endpoint) */
    const realizedBody = document.getElementById('realized-body');
    if (realizedBody) {
      const panelHead = realizedBody.closest('.panel').querySelector('.panel-head');
      panelHead.appendChild(csvExportButton('匯出 CSV', '/api/export/realized', () => ({})));
    }
  }

  /* ============ boot ============ */
  /* All renders depend on D, so fetch the shared /api/dashboard payload first, then
     run the render sequence. The promise is shared with charts.js / alerts.js via
     window.pdDashboard so exactly ONE request is made regardless of script order.
     On failure (api.js already handles 401 → login redirect) we render a graceful
     empty state instead of letting an unhandled rejection hit the console (the e2e
     smoke asserts ZERO console errors / pageerrors). */
  /* ============ Mobile wide-table scroll affordance (FU-D26.5) ============
     Wide data tables scroll horizontally on phones. Two cues, both lightweight and
     layout-shift-free (the fade is a CSS mask toggled by class; the hint pill is
     position:fixed, so neither reflows content — the #holdings/#tx CLS reservations
     survive):
       1. The right-edge fade (styles.css, ≤640px) is REMOVED via `.tw-end` once the
          wrap is scrolled to its end, and via `.tw-noscroll` when it does not overflow.
       2. A one-time, session-dismissed hint pill 「← 左右捲動看更多 →」 appears the first
          time a scrollable table is seen on a narrow screen. */
  const TW_HINT_KEY = 'pd_tw_scroll_hint';
  let twHintDismiss = null;
  function twUpdate(wrap) {
    const overflow = wrap.scrollWidth - wrap.clientWidth;
    if (overflow <= 2) {
      wrap.classList.add('tw-noscroll');
      wrap.classList.remove('tw-end');
      return false;
    }
    wrap.classList.remove('tw-noscroll');
    wrap.classList.toggle('tw-end', wrap.scrollLeft >= overflow - 2);
    return true;
  }
  function twMaybeHint(anyScrollable) {
    if (!anyScrollable || twHintDismiss) return;
    if (!window.matchMedia || !window.matchMedia('(max-width: 640px)').matches) return;
    try { if (sessionStorage.getItem(TW_HINT_KEY)) return; } catch (e) { return; }
    const pill = el('div', 'tw-hint-pill show', '← 左右捲動看更多 →');
    twHintDismiss = () => {
      try { sessionStorage.setItem(TW_HINT_KEY, '1'); } catch (e) { /* noop */ }
      pill.remove();
    };
    pill.addEventListener('click', twHintDismiss);
    document.body.appendChild(pill);
    setTimeout(() => { if (twHintDismiss) twHintDismiss(); }, 6000);
  }
  function enhanceTableScroll() {
    let anyScrollable = false;
    document.querySelectorAll('.table-wrap').forEach((wrap) => {
      if (!wrap.__twBound) {
        wrap.__twBound = true;
        wrap.addEventListener('scroll', () => {
          twUpdate(wrap);
          if (twHintDismiss) twHintDismiss();
        }, { passive: true });
      }
      if (twUpdate(wrap)) anyScrollable = true;
    });
    twMaybeHint(anyScrollable);
  }

  async function boot() {
    try {
      D = await (window.pdDashboard || (window.pdDashboard = window.pdApi.get('/api/dashboard')));
    } catch (err) {
      bootError(err);
      return;
    }
    renderHeader();
    renderKpis();
    renderCcyReturns();
    renderFilterChips();
    renderHoldingsHead();
    renderHoldings();
    renderCurrencyView();
    renderFx();
    renderCashMini();
    renderSnapshots();
    renderRealized();
    renderInsights();
    renderFreshness();
    wireExports();
    /* Tables are populated above; measure overflow now (and re-measure on resize). */
    enhanceTableScroll();
    window.addEventListener('resize', enhanceTableScroll);
  }

  /* Graceful degradation when the dashboard payload cannot be loaded (non-401; 401 is
     handled by api.js). Show a single empty state in the holdings area; never throw. */
  function bootError(err) {
    const body = $('#holdings-body');
    if (body) body.replaceChildren(el('tr', null, ''));
    /* M1-01: a failure IS an answer — the two hosts this file owns stop saying 載入中… so
       #dash-load-error below stands alone (charts.js clears its own two the same way). A
       placeholder that outlives its fetch is exactly the lie the loading state exists to
       avoid — the same reset `instruments.js` performs on `listKnown` in its catch. */
    document.querySelectorAll('#kpi-band.is-loading, .table-wrap.is-loading')
      .forEach((n) => n.classList.remove('is-loading'));
    const host = document.querySelector('.page');
    if (host && window.emptyState && !document.getElementById('dash-load-error')) {
      const box = emptyState('儀表板資料載入失敗，請稍後重新整理。');
      box.id = 'dash-load-error';
      host.insertBefore(box, host.firstChild);
    }
    if (window.toast) {
      window.toast('儀表板資料載入失敗', 'fail', err && err.message ? err.message : undefined);
    }
  }

  boot();
})();
