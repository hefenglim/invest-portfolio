/* portfolio-dash — CSV export framework: RETIRED (owner directive 2026-07-14).
 *
 * This file used to build client-side CSVs by scraping rendered/display values out of
 * the DOM (toCsv / download / rowsFromTable / headersFromTable / button / tableButton).
 * That path is gone: every 匯出 CSV now goes through the BACKEND reconciliation channel
 * (`window.pdApi.download('/api/export/*', body)`), so exported numbers come straight
 * from the Decimal calculation core at source precision — never from formatted cells.
 *
 * The file is intentionally kept (still loaded, still version-stamped) but INERT: it has
 * no remaining callers. `window.pdExport` is a frozen empty object so any stray legacy
 * reference fails loudly rather than silently dumping display values again. Per-surface
 * wiring lives at each call site (app.js / detail.js / trades.html / insights.html /
 * settings.html) using the shared `pdApi.download` + `pdBusy` + fail-toast house pattern.
 */
window.pdExport = Object.freeze({});
