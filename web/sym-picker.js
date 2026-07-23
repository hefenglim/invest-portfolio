/* portfolio-dash — shared grouped 代號 picker (Round-8.1 Wave C, owner 需求七 / Fable F5-F7).

   ONE reusable dropdown that replaces the three ~200-line copy-paste twins (the manual
   picker, the dividend picker, and the opening-inventory datalist). It owns the whole
   open/type/filter/render/fetch/close state machine so a single fix lands everywhere:

   • OPEN-vs-TYPE (Fable F5): opening the picker by FOCUS or CLICK shows the FULL grouped
     list — the field's current value is IGNORED. The query filter applies ONLY once the
     user genuinely types (a keyboard `input` event since the last open). This kills the
     "re-open filters down to the one already-selected symbol" bug at the source.
   • ARCHIVED (Fable F7): archived instruments never appear in the 未持有 candidate group
     (a held position in an archived symbol still shows — held rows come from the holdings
     read, not the registry). Requires the context wire to carry `archived` per instrument.
   • FETCH DEDUP (Fable F9a): focus + click both fire open on a cold cache; the shared
     holdings loader (`loadHoldings`) is expected to dedup in-flight requests, so one open
     issues ONE `/api/input/holdings`.
   • POST-COMMIT REFETCH (Fable F9b): a keystroke after the holdings cache was cleared
     (afterCommitRefresh) triggers a refetch and re-render, never a stale empty held group.

   Groups by mode:
     'held-unheld'  (manual / opening) — 已持有 (holdings.held) + 未持有 (registry − held,
                    market-filtered by the account, archived excluded). Held rows annotated
                    股數 + 均價; 未持有 rows on a MERGED account carry a market tag. Footer =
                    「＋新增標的」 → window.pdInstQuickAdd add-mode.
     'held-closed'  (dividend) — 已持有 + 已清倉 (holdings.closed, only when the toggle is on).
                    Held rows annotated 股數 + 均價 too (new in Wave C). Footer = the
                    「顯示已清倉標的」 toggle.

   ASSISTIVE ONLY: selecting a row writes input.value + calls onPick(symbol) — the caller's
   existing pipeline (preview / hints / model switch) stays the single source of downstream
   behaviour. No money is computed here (股數 / 均價 are SERVER Decimal strings via window.fmt).

   window.pdSymPicker.create(cfg) → { open, close, render, select } controller. */
(function () {
  'use strict';
  const f = window.fmt;
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  /* Markets bound to an account come from the per-market `markets` wire (Batch B). A stale
     ctx lacking it yields [] → the market filter degrades to a no-op (old behaviour). */
  const acctMarkets = (a) =>
    (a && a.markets && typeof a.markets === 'object') ? Object.keys(a.markets) : [];

  function styleShell(cfg) {
    const { panel, empty, foot, addNew, closedToggle } = cfg;
    if (panel) {
      panel.style.cssText = 'position:absolute;left:0;right:0;top:100%;z-index:40;margin-top:4px;'
        + 'background:var(--panel-2,#141821);border:1px solid var(--border,#2a2f3a);'
        + 'border-radius:8px;box-shadow:0 10px 30px rgba(0,0,0,.45);max-height:280px;'
        + 'overflow:auto;padding:4px;';
    }
    if (empty) empty.style.cssText = 'padding:8px 10px;color:var(--text-3,#8a92a3);font-size:11px;';
    if (foot) {
      foot.style.cssText = 'border-top:1px solid var(--border,#2a2f3a);margin-top:4px;'
        + (closedToggle ? 'padding:7px 10px 3px;' : 'padding:4px;');
    }
    if (addNew && addNew.button) {
      const add = addNew.button;
      add.style.cssText = 'display:flex;align-items:center;gap:7px;width:100%;text-align:left;'
        + 'background:none;border:none;padding:7px 8px;cursor:pointer;color:var(--accent,#58a6dd);'
        + 'font:inherit;font-weight:600;border-radius:6px;';
      add.addEventListener('mouseenter', () => { add.style.background = 'rgba(255,255,255,.06)'; });
      add.addEventListener('mouseleave', () => { add.style.background = 'none'; });
    }
    if (closedToggle && closedToggle.checkbox) {
      const tog = closedToggle.checkbox.closest('label') || closedToggle.checkbox.parentElement;
      if (tog) {
        tog.style.cssText = 'display:flex;align-items:center;gap:7px;font-size:11px;'
          + 'color:var(--text-2,#c2c8d2);cursor:pointer;';
      }
    }
  }

  window.pdSymPicker = {
    create: function (cfg) {
      cfg = cfg || {};
      const { input, field, panel, list, empty, foot } = cfg;
      const instOf = cfg.instOf || (() => undefined);
      const instrumentsOf = cfg.instrumentsOf || (() => []);
      const accountOf = cfg.accountOf || (() => null);
      const cachedHoldings = cfg.cachedHoldings || (() => null);
      const loadHoldings = cfg.loadHoldings || (() => Promise.resolve({ held: [], closed: [] }));
      const mode = cfg.mode || 'held-unheld';
      const marketFilter = cfg.marketFilter !== false && mode === 'held-unheld';
      const annotateHeld = cfg.annotateHeld !== false;
      const addNew = cfg.addNew || null;
      const closedToggle = cfg.closedToggle || null;
      const emptyText = cfg.emptyText || ((built, q) => (q ? '無相符標的 — 可直接輸入代號' : '尚無標的'));
      const onPick = cfg.onPick || (() => {});
      const onType = cfg.onType || null;

      styleShell(cfg);

      let isOpen = false;
      /* FIX F5: the query filter is dormant until the user actually types since the last
         open. A focus/click open resets it to false → the FULL list is shown. */
      let queryActive = false;
      const effectiveQuery = () => (queryActive ? (input.value || '').trim() : '');

      /* ---- row + group rendering ---------------------------------------------------- */
      function rowShares(sharesRaw) {
        return (sharesRaw != null && String(sharesRaw).indexOf('.') >= 0)
          ? f.num(sharesRaw, 4) : f.num(sharesRaw);
      }
      function rowEl(row) {
        const b = el('button', null, null);
        b.type = 'button';
        b.style.cssText = 'display:flex;align-items:baseline;gap:8px;width:100%;text-align:left;'
          + 'background:none;border:none;padding:6px 8px;cursor:pointer;color:inherit;'
          + 'border-radius:6px;font:inherit;';
        b.addEventListener('mouseenter', () => { b.style.background = 'rgba(255,255,255,.06)'; });
        b.addEventListener('mouseleave', () => { b.style.background = 'none'; });
        const code = el('span', null, row.symbol);
        code.style.cssText = 'font-weight:600;font-variant-numeric:tabular-nums;flex:none;';
        b.appendChild(code);
        const name = el('span', null, row.name || '');
        name.style.cssText = 'color:var(--text-3,#8a92a3);font-size:11px;overflow:hidden;'
          + 'text-overflow:ellipsis;white-space:nowrap;';
        b.appendChild(name);
        if (row.anno === 'shares') {
          const ann = el('span', null, null);
          ann.style.cssText = 'margin-left:auto;display:flex;gap:10px;flex:none;font-size:10.5px;'
            + 'font-variant-numeric:tabular-nums;color:var(--text-2,#c2c8d2);';
          ann.appendChild(el('span', null, rowShares(row.shares) + ' 股'));
          if (row.adjusted_avg != null) {
            ann.appendChild(el('span', null, '均價 ' + f.price(row.adjusted_avg, row.ccy)));
          }
          b.appendChild(ann);
        } else if (row.anno === 'market' && row.market) {
          const tag = el('span', null, row.market);
          tag.style.cssText = 'margin-left:auto;color:var(--text-3,#8a92a3);font-size:10px;'
            + 'border:1px solid var(--border,#2a2f3a);border-radius:4px;padding:0 6px;flex:none;';
          b.appendChild(tag);
        } else if (row.anno === 'closed') {
          const tag = el('span', null, '已清倉');
          tag.style.cssText = 'margin-left:auto;color:var(--text-3,#8a92a3);font-size:10px;'
            + 'border:1px solid var(--border,#2a2f3a);border-radius:4px;padding:0 6px;flex:none;';
          b.appendChild(tag);
        }
        /* mousedown+preventDefault so the value lands BEFORE the input's focusout (which would
           otherwise race the close and swallow the click). */
        b.addEventListener('mousedown', (e) => { e.preventDefault(); select(row.symbol); });
        return b;
      }
      function headerEl(txt) {
        const h = el('div', null, txt);
        h.style.cssText = 'padding:6px 10px 3px;color:var(--text-3,#8a92a3);font-size:10px;'
          + 'letter-spacing:.04em;font-weight:700;';
        return h;
      }
      function dividerEl() {
        const d = el('div', null);
        d.style.cssText = 'border-top:1px dashed var(--border,#2a2f3a);margin:4px 0;';
        return d;
      }

      /* Build the (unfiltered) groups for the account from its holdings data + the registry.
         Returns { groups:[{header, rows, divider}], held, closed, showClosed }. */
      function buildGroups(data) {
        const a = accountOf();
        const held = (data && data.held) || [];
        const heldRow = (h) => {
          const it = instOf(h.symbol);
          return {
            symbol: h.symbol,
            name: h.name || (it ? it.name : '') || '',
            ccy: (it && it.ccy) || h.ccy || '',
            market: (it && it.market) || h.market || '',
            anno: annotateHeld ? 'shares' : null,
            shares: h.shares,
            adjusted_avg: h.adjusted_avg,
          };
        };
        if (mode === 'held-closed') {
          const closed = (data && data.closed) || [];
          const showClosed = !!(closedToggle && closedToggle.checkbox && closedToggle.checkbox.checked)
            && closed.length > 0;
          const groups = [{ header: '已持有', rows: held.map(heldRow) }];
          if (showClosed) {
            groups.push({
              header: null, divider: true,
              rows: closed.map((c) => {
                const it = instOf(c.symbol);
                return { symbol: c.symbol, name: c.name || (it ? it.name : '') || '', anno: 'closed' };
              }),
            });
          }
          return { groups, held, closed, showClosed };
        }
        // held-unheld (manual / opening)
        const heldSet = {};
        held.forEach((h) => { heldSet[h.symbol] = true; });
        const markets = acctMarkets(a);
        const multi = markets.length > 1;
        const inMarket = (mk) => !marketFilter || markets.length === 0 || markets.indexOf(mk) >= 0;
        const notHeld = instrumentsOf()
          .filter((i) => !heldSet[i.symbol] && !i.archived && inMarket(i.market))
          .map((i) => ({
            symbol: i.symbol, name: i.name || '', ccy: i.ccy || '', market: i.market || '',
            anno: (multi && i.market) ? 'market' : null,
          }));
        return {
          groups: [
            { header: '已持有', rows: held.map(heldRow) },
            { header: '未持有', rows: notHeld },
          ],
          held, notHeld,
        };
      }

      function matches(row, q) {
        if (!q) return true;
        const Q = q.toUpperCase();
        return (row.symbol || '').toUpperCase().indexOf(Q) >= 0
          || (row.name || '').toUpperCase().indexOf(Q) >= 0;
      }

      function renderGroups(data, q) {
        if (!list) return;
        list.replaceChildren();
        const a = accountOf();
        if (!a) {
          if (empty) { empty.hidden = false; empty.textContent = '請先選擇帳戶'; }
          if (foot) foot.hidden = true;
          return;
        }
        const built = buildGroups(data);
        /* footer visibility: the closed toggle only where there IS closed history; the
           ＋新增標的 footer is always available. */
        if (foot) {
          if (closedToggle) foot.hidden = (built.closed || []).length === 0;
          else if (addNew) foot.hidden = false;
        }
        let total = 0;
        let rendered = 0;
        built.groups.forEach((g) => {
          const rows = g.rows.filter((r) => matches(r, q));
          if (!rows.length) return;
          if (g.divider && rendered > 0) list.appendChild(dividerEl());
          if (g.header) list.appendChild(headerEl(g.header));
          rows.forEach((r) => list.appendChild(rowEl(r)));
          total += rows.length;
          rendered += 1;
        });
        if (empty) {
          if (total === 0) { empty.hidden = false; empty.textContent = emptyText(built, q, a); }
          else empty.hidden = true;
        }
      }

      /* Cache-first paint (instant) then refresh from the deduped holdings fetch (F9a/F9b:
         a cleared cache refetches; a cold cache issues one request). */
      function paintAndRefresh() {
        const a = accountOf();
        const cached = a ? cachedHoldings(a.id) : null;
        renderGroups(cached || { held: [], closed: [] }, effectiveQuery());
        if (!a) return;
        loadHoldings(a.id, false).then((d) => {
          if (isOpen) renderGroups(d, effectiveQuery());
        }).catch(() => {});
      }

      function ensureOpen() {
        if (panel) panel.hidden = false;
        isOpen = true;
      }
      function open() {
        if (!panel) return;
        ensureOpen();
        queryActive = false;   // FIX F5: focus/click → full list
        paintAndRefresh();
      }
      function typeFilter() {
        if (onType) onType();
        ensureOpen();
        queryActive = true;    // genuine keystroke → filter
        paintAndRefresh();
      }
      function close() {
        if (panel) panel.hidden = true;
        isOpen = false;
      }
      function render() {
        if (isOpen) renderGroups((accountOf() && cachedHoldings(accountOf().id))
          || { held: [], closed: [] }, effectiveQuery());
      }
      /* Programmatic selection (row click + the add-new success path): set the field, close,
         and drive the caller's downstream pipeline. */
      function select(symbol) {
        if (input) input.value = symbol;
        close();
        onPick(symbol);
      }

      /* ---- wiring ------------------------------------------------------------------- */
      if (input) {
        input.addEventListener('focus', open);
        input.addEventListener('click', open);
        input.addEventListener('input', typeFilter);
        input.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
      }
      if (field) {
        field.addEventListener('focusout', (e) => {
          const to = e.relatedTarget;
          if (to && field.contains(to)) return;   // focus still inside (e.g. the toggle) → stay open
          close();
        });
      }
      if (addNew && addNew.button && addNew.onAdd) {
        addNew.button.addEventListener('mousedown', (e) => {
          e.preventDefault();
          addNew.onAdd((input && input.value || '').trim());
        });
      }
      if (closedToggle && closedToggle.checkbox) {
        closedToggle.checkbox.addEventListener('change', render);
      }

      return { open, close, render, select };
    },
  };
})();
