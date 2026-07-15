"""Phase 1 runner: deterministic scenario over the local isolated server."""

from __future__ import annotations

import argparse

import common as C
import phase1 as P1
from phase1 import Ops, reconcile


def run_scenario(ev: C.Evidence, api: C.Api, db_path, ui=None):
    op = Ops(ev, api, db_path, phase="phase1", ui=ui)
    tid: dict[str, int] = {}

    def buy(label, acct, sym, d, sh, px, **kw):
        r = op.trade(acct, sym, "buy", d, sh, px, **kw)
        j = r.get("json")
        if isinstance(j, dict) and j.get("txn_id"):
            tid[label] = j["txn_id"]
        return r

    def sell(label, acct, sym, d, sh, px, **kw):
        r = op.trade(acct, sym, "sell", d, sh, px, **kw)
        j = r.get("json")
        if isinstance(j, dict) and j.get("txn_id"):
            tid[label] = j["txn_id"]
        return r

    # ---- deposits (funding) ----
    op.cash_move("tw_broker", "deposit", "TWD", "2026-01-05", 2000000, via_ui=True)
    op.cash_move("schwab", "deposit", "TWD", "2026-01-05", 2700000)
    op.cash_move("moomoo_my_us", "deposit", "MYR", "2026-01-05", 120000)
    op.cash_move("moomoo_my_my", "deposit", "MYR", "2026-01-05", 100000)

    # ---- fx conversions (multiple rates -> non-trivial weighted avg) ----
    op.fx("schwab", "2026-01-06", "TWD", 320000, "USD", 10000, via_ui=True)   # 32.0
    op.fx("schwab", "2026-02-10", "TWD", 2310000, "USD", 70000)               # 33.0 -> avg 32.875
    op.fx("moomoo_my_us", "2026-01-07", "MYR", 44000, "USD", 10000)           # 4.4
    op.fx("moomoo_my_us", "2026-03-05", "MYR", 46000, "USD", 10000)           # 4.6 -> avg 4.5

    # ---- BUY wave 1 (buys precede dependent sells) ----
    buy("B1", "tw_broker", "2330", "2026-01-10", 1000, 600, via_ui=True)
    buy("B16", "tw_broker", "2330", "2026-02-05", 300, 620)
    buy("B2", "tw_broker", "2330", "2026-02-15", 500, 640)
    buy("B3", "tw_broker", "0050", "2026-01-12", 10, 130)      # min-fee (odd lot)
    buy("B4", "tw_broker", "0050", "2026-02-01", 100, 132)     # min-fee
    buy("B20", "tw_broker", "0050", "2026-05-10", 50, 138)
    buy("B5", "schwab", "AAPL", "2026-01-15", 100, 180, via_ui=True)
    buy("B6", "schwab", "AAPL", "2026-02-20", 50, 190)
    buy("B21", "schwab", "AAPL", "2026-03-20", 25, 185)
    buy("B7", "schwab", "MSFT", "2026-01-20", 40, 400)
    buy("B17", "schwab", "MSFT", "2026-02-28", 15, 405)
    buy("B10", "moomoo_my_us", "NVDA", "2026-01-25", 30, 500)
    buy("B19", "moomoo_my_us", "NVDA", "2026-02-12", 10, 520)
    buy("B11", "moomoo_my_us", "NVDA", "2026-03-10", 20, 550)
    buy("B12", "moomoo_my_my", "1155", "2026-01-30", 1000, "9.50")
    buy("B13", "moomoo_my_my", "1155", "2026-02-25", 500, "10.20")
    buy("B18", "moomoo_my_my", "1155", "2026-03-15", 200, "10.50")

    reconcile(ev, api, db_path, "checkpoint1", valuation=True)

    # ---- SELL wave + special shapes ----
    sell("S1", "tw_broker", "2330", "2026-03-20", 300, 700, via_ui=True)   # partial
    buy("SDB", "tw_broker", "2330", "2026-03-25", 200, 660)                # same-day buy
    sell("SDS", "tw_broker", "2330", "2026-03-25", 200, 665)               # same-day sell
    # Found-bug op #1 — ETF sell via the MANUAL entry API: the instrument REGISTRY
    # (0050 is_etf=True) must drive the 0.1% 現股 ETF sell tax (not the 0.3% normal rate).
    sell("S2", "tw_broker", "0050", "2026-04-10", 50, 140)                 # ETF sell (0.1% tax)
    buy("B8", "schwab", "TSLA", "2026-04-01", 20, 250)                     # watchlist -> held
    sell("S4", "schwab", "TSLA", "2026-04-20", 20, 260)                    # sell-all
    buy("B9", "schwab", "TSLA", "2026-05-01", 10, 240)                     # rebuy
    sell("S3", "schwab", "AAPL", "2026-05-15", 60, 200)                    # partial
    sell("S6", "moomoo_my_my", "1155", "2026-05-20", 400, "11.00")        # partial
    sell("S5", "moomoo_my_us", "NVDA", "2026-06-01", 25, 600)             # partial

    # ---- dividends (all three account models) ----
    op.dividend("schwab", "MSFT", "2026-03-15", "DRIP", 100, reinvest_price=350)
    op.dividend("schwab", "AAPL", "2026-04-05", "DRIP", 50, reinvest_price=200)
    op.dividend("moomoo_my_us", "NVDA", "2026-04-08", "DRIP", 60, reinvest_price=560)
    op.dividend("moomoo_my_my", "1155", "2026-04-15", "NET", 300)
    op.dividend("tw_broker", "2330", "2026-06-10", "CASH", 5000)
    op.dividend("tw_broker", "0050", "2026-06-12", "CASH", 800)

    # ---- oversell attempt -> block, then ack-write (creates 賣超 holding) ----
    # 0050 held here = B3(10)+B4(100)+B20(50)-S2(50) = 110; sell 200 > 110 -> oversell.
    r = op.trade("tw_broker", "0050", "sell", "2026-06-18", 200, 141, ack=False,
                 expect_status=422, fee_check=False)
    ev.check("guard.oversell_blocks", "tw_broker/0050 sell 200>held 110",
             "422", str(r.get("status")), "phase1")
    r2 = op.trade("tw_broker", "0050", "sell", "2026-06-18", 200, 141, ack=True,
                  expect_status=201, fee_check=False)
    j2 = r2.get("json")
    if isinstance(j2, dict):
        tid["OVS"] = j2.get("txn_id")

    reconcile(ev, api, db_path, "checkpoint2", valuation=False)

    # ---- corrections + late ops ----
    op.delete_tx(tid["OVS"])
    op.edit_tx(tid["B2"], "tw_broker", "2330", "buy", "2026-02-15", 500, 645, 460, 0)
    op.delete_tx(tid["B13"])

    op.fx("schwab", "2026-06-20", "USD", 5000, "TWD", 162000)  # realized FX -2375
    op.cash_move("tw_broker", "withdraw", "TWD", "2026-06-25", 50000)

    buy("B14", "schwab", "MSFT", "2026-05-05", 20, 410)
    sell("S7", "schwab", "MSFT", "2026-06-05", 10, 420)
    buy("B15", "schwab", "AAPL", "2026-06-15", 30, 210)
    rdup = op.trade("schwab", "AAPL", "buy", "2026-06-15", 30, 210, fee_check=False)
    ev.check("dup.creates_second_row", "schwab/AAPL 30@210 duplicate",
             "201", str(rdup.get("status")), "phase1")

    op.dividend("tw_broker", "2330", "2026-03-01", "CASH", 2000)
    op.dividend("moomoo_my_my", "1155", "2026-06-14", "NET", 150)
    op.cash_move("schwab", "deposit", "TWD", "2026-04-01", 300000)  # top-up deposit
    buy("B22", "tw_broker", "2330", "2026-04-15", 100, 630)
    buy("B23", "moomoo_my_us", "NVDA", "2026-05-28", 5, 580)
    op.dividend("schwab", "MSFT", "2026-06-08", "DRIP", 40, reinvest_price=415)
    sell("S8", "tw_broker", "2330", "2026-06-22", 100, 710)
    sell("S9", "schwab", "AAPL", "2026-06-23", 20, 208)
    sell("S10", "moomoo_my_my", "1155", "2026-06-24", 100, "11.50")
    sell("S11", "tw_broker", "2330", "2026-06-26", 50, 715)

    # ---- Found-bug op #2 — TW daytrade sell taxes at 0.15% (not 0.3%). Two surfaces:
    #      (a) the MANUAL entry API body flag; (b) a CSV `daytrade` column row. Each is a
    #      same-day buy+sell so no position is left dangling.
    buy("DTB", "tw_broker", "2330", "2026-06-27", 100, 700)                # same-day buy
    op.trade("tw_broker", "2330", "sell", "2026-06-27", 100, 720, daytrade=True)  # manual flag
    op.daytrade_csv("tw_broker", "2330", "2026-06-28", 100, 705, 725)      # CSV daytrade column

    reconcile(ev, api, db_path, "final", valuation=True, reports=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ui", action="store_true")
    ap.add_argument("--keep-data", action=argparse.BooleanOptionalAction, default=True,
                    help="keep the fresh phase-1 DB under evidence/ after the run "
                         "(gitignored; always rebuilt clean at the next start)")
    args = ap.parse_args()

    db_path = C.EVIDENCE / "phase1.db"
    for p in (db_path, db_path.with_suffix(".uvicorn.log")):
        if p.exists():
            p.unlink()

    ev = C.Evidence(reset=True)
    srv = C.LocalServer(db_path)
    base = srv.start()
    print("server:", base)
    api = C.Api(base, verify=False)
    ui = None
    try:
        P1.seed_all(db_path)
        if args.ui:
            import ui as UI
            ui = UI.UiDriver(base)
            ui.start()
        run_scenario(ev, api, db_path, ui=ui)
        if ui is not None:
            import ui as UI
            UI.dom_readback(ui, ev, api)
    finally:
        if ui is not None:
            ui.stop()
        api.close()
        srv.stop()

    if not args.keep_data:
        for p in (db_path, db_path.with_suffix(".uvicorn.log")):
            if p.exists():
                p.unlink()

    print(f"ops={ev.op_n} pass={ev.n_pass} fail={ev.n_fail}")
    for f in ev.fails[:60]:
        print("  FAIL", f["check"], "|", f["scope"], "| exp=", f["expected"], "got=", f["actual"])


if __name__ == "__main__":
    main()
