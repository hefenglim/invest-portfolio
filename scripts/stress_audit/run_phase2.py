"""Phase 2 runner: investor-realistic UI-first stress on the live demo (additive).

The demo/base URL is a REAL host detail — it lives in the git-ignored docs/human_noted/
deployment note, never in this committed file. Pass it with ``--base-url``; the
placeholder default below is intentionally non-functional so a missing URL fails loudly.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from decimal import Decimal as D

import common as C
import oracle as O
import phase2 as P2
from phase2 import Ops2, delta_asserts, reconcile_abs, snapshot

# Placeholder — the real demo URL is in docs/human_noted/; supply it via --base-url.
DEMO_PLACEHOLDER = "https://invest-demo.example.ts.net"


def _share_events(api: C.Api, account_id: str, symbol: str):
    """All dated share deltas for one (account, symbol), from the live ledger FACTS.

    ACCUMULATION SAFETY: destructive-path premises are derived from these events at run
    time (net position, earliest event date, shares-through-a-date), never hard-coded —
    the lesson from the back-dated sell that once destroyed a symbol's cost basis.
    """
    facts = C.load_facts_from_api(api)
    evs: list[tuple] = []
    for o in facts.openings:
        if o.account_id == account_id and o.symbol == symbol:
            evs.append((o.build_date, o.shares))
    for t in facts.txs:
        if t.account_id == account_id and t.symbol == symbol:
            evs.append((t.trade_date, t.qty if t.side == "BUY" else -t.qty))
    for dv in facts.divs:
        if dv.account_id == account_id and dv.symbol == symbol and dv.reinvest_shares:
            evs.append((dv.d, dv.reinvest_shares))
    return evs


def _net_shares(api: C.Api, account_id: str, symbol: str) -> D:
    return sum((q for _d, q in _share_events(api, account_id, symbol)), D("0"))


def _dash_row(api: C.Api, account_id: str, symbol: str):
    for h in api.get("/api/dashboard").json()["holdings"]:
        if h["account_id"] == account_id and h["symbol"] == symbol:
            return h
    return None


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
    # Batch B: both MYR deposits + the MYR->USD conversion fund the SAME merged dual-market
    # moomoo_my (its two pools: USD from the conversion, MYR from deposits + MY-market trades).
    op.cash_move("tw_broker", "deposit", "TWD", "2026-01-05", 2000000)
    op.cash_move("schwab", "deposit", "USD", "2026-01-05", 100000)
    op.cash_move("moomoo_my", "deposit", "MYR", "2026-01-05", 100000)
    op.cash_move("moomoo_my", "deposit", "MYR", "2026-01-05", 50000)
    op.fx("moomoo_my", "2026-01-07", "MYR", 45000, "USD", 10000)
    op.cash_move("tw_broker", "withdraw", "TWD", "2026-06-25", 50000)
    # ---- FX cost basis (spec 2026-07-30): a foreign credit WITH a recorded acquisition
    # cost, alongside the 100,000 USD deposit above which has NONE — schwab's USD pool
    # then carries covered_ratio < 1 while its balance stays POSITIVE, the silent case
    # fx_basis_gap exists to disclose. Reconciled in reconcile_abs (ratio/gap/flags).
    op.cash_move("schwab", "deposit", "USD", "2026-01-06", 20000, acq_home_amount=650000)
    # Entry-side guards for the 取得成本 field (each must 400 without writing):
    r = op.cash_move("schwab", "withdraw", "USD", "2026-06-20", 100, acq_home_amount=3250)
    ev.check("guard.acq_on_withdraw_rejected", "出金是處分,不帶取得成本",
             "400", str(r.get("status")), "phase2")
    r = op.cash_move("tw_broker", "deposit", "TWD", "2026-06-20", 1000, acq_home_amount=1000)
    ev.check("guard.acq_on_home_ccy_rejected", "home-ccy deposit carries no acquisition cost",
             "400", str(r.get("status")), "phase2")
    r = op.cash_move("schwab", "deposit", "USD", "2026-06-20", 100,
                     acq_home_amount=3250, acq_rate="32.5")
    ev.check("guard.acq_amount_and_rate_rejected", "amount XOR rate, never both",
             "400", str(r.get("status")), "phase2")
    # moomoo_my now contributes exactly TWO pools (USD + MYR) instead of the legacy three.
    touched_pools |= {("tw_broker", "TWD"), ("schwab", "USD"),
                      ("moomoo_my", "MYR"), ("moomoo_my", "USD")}

    # ---- buys (UI-first, mix of API) ----
    op.trade("tw_broker", "3008", "buy", "2026-01-10", 5, 600, via_ui=True)      # min-fee odd lot
    op.trade("tw_broker", "3008", "buy", "2026-02-10", 1000, 610, via_ui=True)
    op.trade("schwab", "MSFT", "buy", "2026-01-15", 10, 400, via_ui=True)
    op.trade("schwab", "MSFT", "buy", "2026-02-20", 5, 410)                       # API
    op.trade("schwab", "TSLA", "buy", "2026-03-01", 5, 250, via_ui=True)
    op.trade("schwab", "TSLA", "sell", "2026-03-20", 5, 260, via_ui=True)         # sell-all
    op.trade("schwab", "TSLA", "buy", "2026-04-01", 3, 240)                       # rebuy (API)
    op.trade("moomoo_my", "5225", "buy", "2026-01-30", 1000, "2.50", via_ui=True)

    # ---- mid checkpoint ----
    mid = snapshot(api)
    reconcile_abs(ev, api, "mid", mid)

    # ---- sells + boundary + correction ----
    op.trade("schwab", "MSFT", "sell", "2026-05-15", 4, 415, via_ui=True)         # partial
    op.trade("tw_broker", "3008", "sell", "2026-06-01", 200, 620)                 # partial (API)
    # oversell attempt -> 422 block (verify guard; NOT force-written, demo stays clean).
    # ACCUMULATION-SAFE (2026-07-30): the quantity is derived from the position the app
    # reports RIGHT NOW, not hard-coded. On the accumulating demo a fixed 100 stopped being
    # an oversell once earlier runs left a larger net position — the guard then legitimately
    # allowed it and the "guard test" WROTE a back-dated sell that wrecked the symbol's cost
    # basis. A scenario that mutates the ledger when its premise no longer holds is not a
    # test; derive the premise from live state so the op is either a real oversell or skipped.
    held_tsla = None
    for h in api.get("/api/input/holdings", account="schwab").json().get("held", []):
        if h.get("symbol") == "TSLA":
            held_tsla = D(str(h.get("shares") or "0"))
            break
    if held_tsla is None:
        ev.op("phase2", "API", "guard.oversell_skipped", {"symbol": "TSLA"},
              {"reason": "schwab holds no TSLA — nothing to oversell"})
    else:
        qty = held_tsla + D("1")          # guaranteed to exceed the net position
        r = op.trade("schwab", "TSLA", "sell", "2026-06-10", qty, 260,
                     expect=422, fee_check=False)
        ev.check("guard.oversell_blocks", f"schwab/TSLA sell {qty}>held {held_tsla}",
                 "422", str(r.get("status")), "phase2")
    # BACK-DATED sell -> DATE-AWARE guard (2026-07-31). Premise derived from live facts:
    # a date strictly before schwab/MSFT's earliest share event has held-through = 0 there
    # while the CURRENT net covers the quantity — exactly what the old net-only check
    # waved through (and what once destroyed a symbol's cost basis on this demo).
    msft_evs = _share_events(api, "schwab", "MSFT")
    msft_net = sum((q for _d, q in msft_evs), D("0"))
    if not msft_evs or msft_net < 1:
        ev.op("phase2", "API", "guard.backdated_skipped", {"symbol": "MSFT"},
              {"reason": f"premise not establishable (net={msft_net})"})
    else:
        d0 = (min(d for d, _q in msft_evs) - timedelta(days=1)).isoformat()
        r = op.trade("schwab", "MSFT", "sell", d0, 1, 400, expect=422, fee_check=False)
        ev.check("guard.backdated_sell_blocks",
                 f"schwab/MSFT sell 1 dated {d0} (held_then=0, net={msft_net})",
                 "422", str(r.get("status")), "phase2")
        _issues = ((r.get("json") or {}).get("error") or {}).get("issues") or []
        _txt = " ".join(str(i.get("text", "")) for i in _issues)
        ev.check("guard.backdated_names_date", "422 message names the sell's own date",
                 True, d0 in _txt, "phase2")
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
    op.dividend_ui("moomoo_my", "net", "5225", "2026-06-07", 200)              # MY net

    # ---- declared short (2026-07-31): open -> flags -> dividend-on-short -> covers ----
    # (a) undeclared oversell on the dedicated symbol stays blocked (short is never
    #     inferred). Quantity derived from the live net so the premise holds even if an
    #     interrupted earlier run left residue.
    net_2609 = _net_shares(api, "tw_broker", "2609")
    qty_short = (net_2609 if net_2609 > D("0") else D("0")) + D("2000")
    r = op.trade("tw_broker", "2609", "sell", "2026-06-20", qty_short, 100,
                 expect=422, fee_check=False)
    ev.check("guard.short_needs_declaration",
             f"tw_broker/2609 undeclared sell {qty_short} > net {net_2609}",
             "422", str(r.get("status")), "phase2")
    # (b) DECLARED short open — no ack needed; TW sell fee+tax book on the row.
    op.trade("tw_broker", "2609", "sell", "2026-06-20", qty_short, 100, short=True)
    row = _dash_row(api, "tw_broker", "2609")
    ev.check("short.reported_negative", "tw_broker/2609 shares < 0 while short open",
             True, row is not None and str(row.get("shares", "")).startswith("-"), "phase2")
    if row is not None:
        ev.check("short.flag_short_open", "tw_broker/2609", True,
                 row.get("short_open"), "phase2")
        ev.check("short.not_flagged_oversold", "a declared short is NOT 賣超", False,
                 row.get("oversold"), "phase2")
    # (c) drawer: 已回本 must be GATED off on a short (its basis is negative by
    #     construction, without a single dividend paid) — remediation #3.
    pos = api.get("/api/symbol/2609/detail").json().get("position") or {}
    ev.check("short.drawer_fully_recovered_gated",
             "negative short basis must not read 已回本", False,
             pos.get("fully_recovered"), "phase2")
    ev.check("short.drawer_short_open", "2609 drawer flag", True,
             pos.get("short_open"), "phase2")
    # (d) a dividend recorded while the short is open is NOT booked: realized rows stay
    #     unchanged, the adjusted basis stays unchanged, the position is flagged 待釐清.
    dash_before = api.get("/api/dashboard").json()
    real_before = len(dash_before["realized"]["rows"])
    op.dividend_csv("tw_broker", "2609", "2026-06-22", "CASH", 3000)
    dash_after = api.get("/api/dashboard").json()
    ev.check("short.dividend_not_booked_as_income",
             "realized row count unchanged by the on-short dividend",
             real_before, len(dash_after["realized"]["rows"]), "phase2")
    row2 = next((h for h in dash_after["holdings"]
                 if h["account_id"] == "tw_broker" and h["symbol"] == "2609"), None)
    ev.check("short.flag_unbookable_dividend", "tw_broker/2609", True,
             (row2 or {}).get("unbookable_dividend"), "phase2")
    if row is not None and row2 is not None:
        ev.check("short.dividend_not_folded_into_cost", "adjusted basis unchanged",
                 row.get("adjusted_cost_total"), row2.get("adjusted_cost_total"), "phase2")
    # (e) FULL reconcile with the short open and the unbookable dividend in the ledger.
    reconcile_abs(ev, api, "short_open", snapshot(api))
    # (f) user-resolution flow: remove the unbookable dividend row; the flag clears.
    div_id = op.find_dividend_id("tw_broker", "2609", "2026-06-22")
    rdel = op.delete_dividend(div_id)
    ev.check("short.unbookable_dividend_removable", f"delete dividend #{div_id}",
             "200", str(rdel.get("status")), "phase2")
    row3 = _dash_row(api, "tw_broker", "2609")
    ev.check("short.flag_clears_after_removal", "tw_broker/2609", False,
             (row3 or {}).get("unbookable_dividend"), "phase2")
    # (g) partial cover, then the EXACT remainder (derived live — an interrupted earlier
    #     run can never leave a residual short behind; the demo self-heals to flat).
    op.trade("tw_broker", "2609", "buy", "2026-06-25", 800, 95)
    net_after = _net_shares(api, "tw_broker", "2609")
    if net_after < D("0"):
        op.trade("tw_broker", "2609", "buy", "2026-07-05", -net_after, 98)
    ev.check("short.fully_covered_leaves_no_position",
             "2609 drops out of holdings once flat", True,
             _dash_row(api, "tw_broker", "2609") is None, "phase2")

    # ---- dividend-inbox refresh + confirm (UI; real provider scan, best-effort) ----
    try:
        op.inbox_refresh_confirm(max_confirm=3)
    except Exception as exc:  # noqa: BLE001
        ev.op("phase2", "UI", "dividend_inbox.error", {}, {"error": str(exc)[:200]})

    # ---- final snapshot + reconcile + delta ----
    post = snapshot(api)
    reconcile_abs(ev, api, "final", post)
    delta_asserts(ev, base, post, touched_pools, new_symbols)

    # ---- fee-rule conflict warning (discount<1 together with rebate_rate>0) ----
    # Runs LAST so no trade books while the override is on; the override is REVERTED and
    # re-verified (owner requirement: discount must end at the default 1).
    def _tw_rule():
        rs = api.get("/api/fee-rules").json()["rule_sets"]
        return next(rr for rr in rs if rr["name"] == "tw")

    tw0 = _tw_rule()
    f0 = {f["key"]: f for f in tw0["fields"]}
    disc0 = f0.get("discount", {})
    ev.check("feerule.discount_starts_default", "tw discount effective 1 before the test",
             "1", str(disc0.get("effective")), "phase2")
    ev.check("feerule.no_conflict_at_default", "conflicts empty at discount=1",
             0, len(tw0.get("conflicts") or []), "phase2")
    rebate0 = C.dec(f0.get("rebate_rate", {}).get("effective") or "0")
    if rebate0 <= D("0"):
        ev.op("phase2", "API", "feerule.conflict_skipped", {},
              {"reason": f"rebate_rate={rebate0} — the conflict needs rebate_rate>0"})
    else:
        put = api.put("/api/fee-rules/tw", {"overrides": {"discount": "0.23"}})
        conf = (put.json() or {}).get("conflicts") or []
        ev.op("phase2", "API", "feerule.set_discount", {"discount": "0.23"},
              {"status": put.status_code, "conflicts": len(conf)})
        ev.check("feerule.conflict_fires", "discount<1 with rebate_rate>0 warns",
                 True, bool(conf) and conf[0].get("fields") == ["discount", "rebate_rate"],
                 "phase2")
        restore = disc0.get("effective") if disc0.get("overridden") else None
        put2 = api.put("/api/fee-rules/tw", {"overrides": {"discount": restore}})
        ev.op("phase2", "API", "feerule.revert_discount", {"discount": restore},
              {"status": put2.status_code})
    tw1 = _tw_rule()
    f1 = {f["key"]: f for f in tw1["fields"]}
    ev.check("feerule.discount_reverted", "tw discount is 1 again (owner requirement)",
             "1", str(f1.get("discount", {}).get("effective")), "phase2")
    ev.check("feerule.no_conflict_after_revert", "conflicts empty after revert",
             0, len(tw1.get("conflicts") or []), "phase2")


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
    # Assert what the instance will ACTUALLY charge: seed defaults + its settings overrides.
    # Only rates move; fee_tax's formulas stay independently derived.
    for diff in O.apply_effective_rules(C.read_effective_fee_rules(api)):
        print(f"[phase2] effective fee rule differs from seed — {diff}")
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
