/* portfolio-dash — CSV export framework: RETIRED (owner directive 2026-07-14).
 *
 * This file used to build client-side CSVs by scraping rendered/display values out of
 * the DOM (toCsv / download / rowsFromTable / headersFromTable / button / tableButton).
 * That path is gone: every 匯出 CSV now goes through the BACKEND reconciliation channel
 * (`window.pdApi.download('/api/export/*', body)`), so exported numbers come straight
 * from the Decimal calculation core at source precision — never from formatted cells.
 *
 * The file stayed loaded but INERT for a year, exporting a frozen EMPTY object so any stray
 * legacy reference failed loudly rather than silently dumping display values again. It is no
 * longer empty (see the tax-year helper at the bottom, 2026-08-27) but it is still frozen and
 * still exports nothing that touches a number. Per-surface wiring lives at each call site
 * (app.js / detail.js / trades.html / insights.html / settings.html / settings-alerts.js)
 * using the shared `pdApi.download` + `pdBusy` + fail-toast house pattern.
 */

/* ------------------------------------------------------------------------------------
   2026-08-27 — ONE function returns, and the retirement above still stands.
   What was retired is BUILDING a CSV out of rendered cells. `taxYears()` builds nothing,
   reads no DOM, and computes no money: it answers 「which years can a 稅務包 cover」 from the
   ledgers' own oldest rows.

   It lives here because BOTH surfaces offering that export need the same answer — the 帳本頁
   export slot and the 設定頁 匯出中心 — and two copies of a derived year range is how one of
   them ends up typed by hand. That had already happened: the 匯出中心 shipped a literal
   ['2026','2025','2024'], which is wrong in both directions the moment a ledger starts
   earlier or the year rolls over.

   Degradation is deliberate: if `/api/db-stats` is unreachable the caller still gets
   今年/去年 rather than an empty menu, because the export endpoint itself is unaffected —
   only the menu is narrower.
   ------------------------------------------------------------------------------------ */
(function () {
  /* The two ledgers a tax package is built from (realized gains come from the transaction
     replay, income from the dividend ledger). The FX-realized sheet derives from
     fx_conversions, which cannot predate the trades that needed the currency. */
  var TAX_LEDGERS = { transactions: 1, dividends: 1 };

  function span(first) {
    var now = new Date().getFullYear();
    if (!(first > 1900) || first > now) first = now - 1;
    var years = [];
    for (var y = now; y >= first; y--) years.push(y);
    /* 去年 when the ledger reaches back that far, else the oldest year on offer. A filing is
       filed for the year that CLOSED; defaulting to 今年 hands over an authoritative-looking
       part-year package. */
    return { years: years, preferred: (now - 1 >= first ? now - 1 : first) };
  }

  async function taxYears() {
    try {
      var st = await window.pdApi.get('/api/db-stats');
      var groups = (st && st.portfolio && st.portfolio.groups) || [];
      var first = 0;
      groups.forEach(function (g) {
        (g.tables || []).forEach(function (t) {
          /* `oldest` is an ISO prefix ("2026-03-15…"), so the year is its first 4 chars. */
          if (!TAX_LEDGERS[t.name] || !t.oldest) return;
          var y = parseInt(String(t.oldest).slice(0, 4), 10);
          if (y > 1900 && (!first || y < first)) first = y;
        });
      });
      return span(first);
    } catch (err) {
      return span(0);
    }
  }

  /* Fills a <select> in place and selects the preferred year. Returns the same promise so a
     caller can await the menu being ready (the e2e does). */
  function fillTaxYearSelect(sel) {
    return taxYears().then(function (r) {
      sel.textContent = '';
      r.years.forEach(function (y) {
        var o = document.createElement('option');
        o.value = String(y);
        o.textContent = y + ' 年度';
        sel.appendChild(o);
      });
      sel.value = String(r.preferred);
      return r;
    });
  }

  window.pdExport = Object.freeze({ taxYears: taxYears, fillTaxYearSelect: fillTaxYearSelect });
})();
