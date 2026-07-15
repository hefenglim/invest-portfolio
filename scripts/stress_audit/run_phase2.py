"""Phase 2 runner: investor-realistic UI-first stress on the live demo (additive).

The demo/base URL is a REAL host detail — it lives in the git-ignored docs/human_noted/
deployment note, never in this committed file. Pass it with ``--base-url``; the
placeholder default below is intentionally non-functional so a missing URL fails loudly.
"""

from __future__ import annotations

import argparse
import sys

import common as C
import phase2 as P2
from phase2 import Ops2, delta_asserts, reconcile_abs, snapshot

# Placeholder — the real demo URL is in docs/human_noted/; supply it via --base-url.
DEMO_PLACEHOLDER = "https://invest-demo.example.ts.net"


def run(ev: C.Evidence, api: C.Api, ui):
    op = Ops2(ev, api, ui)
    touched_pools = set()

    # ---- baseline snapshot (BEFORE any mutation) ----
    base = snapshot(api)
    # absolute-from-zero delta coverage: only symbols truly absent from baseline holdings
    _base_syms = {k[1] for k in base["reported_hold"]}
    new_symbols = {s[0] for s in P2.NEW_INSTRUMENTS if s[0] not in _base_syms}
    ev.op("phase2", "API", "baseline.snapshot", {},
          {"holdings": len(base["reported_hold"]), "cash_pools": len(base["reported_cash"]),
           "txs": len(base["facts"].txs)})

    # ---- setup: register NEW instruments as watchlist (API; provider-quote fetch) ----
    for sym, mkt, ccy, name, sector, etf in P2.NEW_INSTRUMENTS:
        op.register(sym, mkt, ccy, name, sector, etf)

    # ---- capture any cash-page JS error (a broken confirm handler blocks the UI) ----
    if ui is not None:
        cash_errs = ui.page_errors_on("/cash.html")
        ev.op("phase2", "UI", "finding.cash_page_jserror", {"path": "/cash.html"},
              {"pageerrors": cash_errs})
        ev.check("ui.cash_page.no_js_error", "cash.html loads without uncaught JS error",
                 [], cash_errs, "phase2")

    # ---- cash flows (API; UI blocked when the cash page has an uncaught JS error) ----
    op.cash_move("tw_broker", "deposit", "TWD", "2026-01-05", 2000000)
    op.cash_move("schwab", "deposit", "USD", "2026-01-05", 100000)
    op.cash_move("moomoo_my_us", "deposit", "MYR", "2026-01-05", 100000)
    op.cash_move("moomoo_my_my", "deposit", "MYR", "2026-01-05", 50000)
    op.fx("moomoo_my_us", "2026-01-07", "MYR", 45000, "USD", 10000)
    op.cash_move("tw_broker", "withdraw", "TWD", "2026-06-25", 50000)
    touched_pools |= {("tw_broker", "TWD"), ("schwab", "USD"),
                      ("moomoo_my_us", "MYR"), ("moomoo_my_us", "USD"),
                      ("moomoo_my_my", "MYR")}

    # ---- buys (UI-first, mix of API) ----
    op.trade("tw_broker", "3008", "buy", "2026-01-10", 5, 600, via_ui=True)      # min-fee odd lot
    op.trade("tw_broker", "3008", "buy", "2026-02-10", 1000, 610, via_ui=True)
    op.trade("schwab", "MSFT", "buy", "2026-01-15", 10, 400, via_ui=True)
    op.trade("schwab", "MSFT", "buy", "2026-02-20", 5, 410)                       # API
    op.trade("schwab", "TSLA", "buy", "2026-03-01", 5, 250, via_ui=True)
    op.trade("schwab", "TSLA", "sell", "2026-03-20", 5, 260, via_ui=True)         # sell-all
    op.trade("schwab", "TSLA", "buy", "2026-04-01", 3, 240)                       # rebuy (API)
    op.trade("moomoo_my_my", "5225", "buy", "2026-01-30", 1000, "2.50", via_ui=True)

    # ---- mid checkpoint ----
    mid = snapshot(api)
    reconcile_abs(ev, api, "mid", mid)

    # ---- sells + boundary + correction ----
    op.trade("schwab", "MSFT", "sell", "2026-05-15", 4, 415, via_ui=True)         # partial
    op.trade("tw_broker", "3008", "sell", "2026-06-01", 200, 620)                 # partial (API)
    # oversell attempt -> 422 block (verify guard; NOT force-written, demo stays clean)
    r = op.trade("schwab", "TSLA", "sell", "2026-06-10", 100, 260, expect=422, fee_check=False)
    ev.check("guard.oversell_blocks", "schwab/TSLA sell 100>held",
             "422", str(r.get("status")), "phase2")
    # correction: edit the MSFT 5@410 buy -> 5@412 (price fix; explicit fee/tax given)
    msft_buy_id = None
    for r2 in api.get("/api/ledgers/transactions", limit=500).json().get("rows", []):
        if (r2["account_id"] == "schwab" and r2["symbol"] == "MSFT"
                and r2["side"] == "buy" and r2["shares"] in ("5", "5.0")
                and r2["price"] in ("410", "410.0", "410.00")):
            msft_buy_id = r2["id"]
            break
    if msft_buy_id is not None:
        op.edit_tx(msft_buy_id, "schwab", "MSFT", "buy", "2026-02-20", 5, 412, 0, 0)

    # ---- dividends (UI form; all three account models) ----
    op.dividend_ui("tw_broker", "tw", "3008", "2026-06-05", 3000)                 # TW cash
    op.dividend_ui("schwab", "drip", "MSFT", "2026-06-06", 100, reinvest_price=400)  # US DRIP
    op.dividend_ui("moomoo_my_my", "net", "5225", "2026-06-07", 200)              # MY net

    # ---- dividend-inbox refresh + confirm (UI; real provider scan, best-effort) ----
    try:
        op.inbox_refresh_confirm(max_confirm=3)
    except Exception as exc:  # noqa: BLE001
        ev.op("phase2", "UI", "dividend_inbox.error", {}, {"error": str(exc)[:200]})

    # ---- final snapshot + reconcile + delta ----
    post = snapshot(api)
    reconcile_abs(ev, api, "final", post)
    delta_asserts(ev, base, post, touched_pools, new_symbols)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=DEMO_PLACEHOLDER,
                    help="live demo base URL (real value in docs/human_noted/)")
    ap.add_argument("--no-ui", action="store_true", help="API-only (debug)")
    args = ap.parse_args()

    if args.base_url == DEMO_PLACEHOLDER:
        sys.exit("Phase 2 needs a real --base-url (the demo URL from docs/human_noted/); "
                 "the committed default is a non-functional placeholder.")

    ev = C.Evidence(oplog=C.EVIDENCE / "oplog_phase2.jsonl",
                    assertions=C.EVIDENCE / "assertions_phase2.jsonl", reset=True)
    api = C.Api(args.base_url, verify=False)
    ui = None
    try:
        if not args.no_ui:
            import ui as UI
            ui = UI.UiDriver(args.base_url)
            ui.start()
        run(ev, api, ui)
    finally:
        if ui is not None:
            ui.stop()
        api.close()

    print(f"[phase2] ops={ev.op_n} pass={ev.n_pass} fail={ev.n_fail}")
    for f in ev.fails[:60]:
        print("  FAIL", f["check"], "|", f["scope"], "| exp=", f["expected"], "got=", f["actual"])


if __name__ == "__main__":
    main()
