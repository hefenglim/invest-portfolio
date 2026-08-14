"""Phase 1 runner: deterministic scenario over the local isolated server."""

from __future__ import annotations

import argparse
import sqlite3
import sys

import common as C
import phase1 as P1
from phase1 import Ops, _json, reconcile


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
    # Batch B merged-account proof: BOTH MYR deposits fund the SAME dual-market moomoo_my — its
    # MYR pool feeds MY-market MYR buys directly AND the MYR->USD conversions that fund the US pool.
    op.cash_move("moomoo_my", "deposit", "MYR", "2026-01-05", 120000)
    op.cash_move("moomoo_my", "deposit", "MYR", "2026-01-05", 100000)

    # ---- fx conversions (multiple rates -> non-trivial weighted avg) ----
    op.fx("schwab", "2026-01-06", "TWD", 320000, "USD", 10000, via_ui=True)   # 32.0
    op.fx("schwab", "2026-02-10", "TWD", 2310000, "USD", 70000)               # 33.0 -> avg 32.875
    # MYR->USD feeding the merged moomoo_my USD pool (funds the US-market NVDA buys below).
    op.fx("moomoo_my", "2026-01-07", "MYR", 44000, "USD", 10000)           # 4.4
    op.fx("moomoo_my", "2026-03-05", "MYR", 46000, "USD", 10000)           # 4.6 -> avg 4.5

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
    # Batch B merged-account proof (on ONE account, moomoo_my): US-market NVDA trades book USD
    # fees (commission + platform + settlement + CAT, SELL: SEC/TAF, + MY stamp booked USD, FE-D2)
    # via the moomoo_us rule; MY-market 1155/0800EA trades book MYR fees (commission + platform RM3
    # + clearing + SST 8%; stamp step, ETF-exempt) via the moomoo_my rule. The oracle routes each
    # by the instrument's market (fee_rule_for) and reconciles both, plus the USD FX pool (scoped
    # to USD holdings only) and both dividend models (US DRIP + MY cash), against the app.
    buy("B10", "moomoo_my", "NVDA", "2026-01-25", 30, 500)     # US: moomoo_us fees + USD FX pool
    buy("B19", "moomoo_my", "NVDA", "2026-02-12", 10, 520)
    buy("B11", "moomoo_my", "NVDA", "2026-03-10", 20, 550)
    buy("B12", "moomoo_my", "1155", "2026-01-30", 1000, "9.50")   # MY: moomoo_my fees (MYR)
    buy("B13", "moomoo_my", "1155", "2026-02-25", 500, "10.20")
    buy("B18", "moomoo_my", "1155", "2026-03-15", 200, "10.50")
    # MY ETF buy -> fee-engine v2 stamp EXEMPTION: tax must be RM0 despite the step formula.
    buy("BME", "moomoo_my", "0800EA", "2026-02-08", 1000, "1.15")

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
    sell("S6", "moomoo_my", "1155", "2026-05-20", 400, "11.00")        # partial
    sell("S5", "moomoo_my", "NVDA", "2026-06-01", 25, 600)             # partial

    # ---- dividends (all three account models; the merged moomoo_my exercises BOTH US DRIP
    #      and MY cash on ONE account — US market -> drip_us, MY market -> cash) ----
    op.dividend("schwab", "MSFT", "2026-03-15", "DRIP", 100, reinvest_price=350)
    op.dividend("schwab", "AAPL", "2026-04-05", "DRIP", 50, reinvest_price=200)
    op.dividend("moomoo_my", "NVDA", "2026-04-08", "DRIP", 60, reinvest_price=560)  # US DRIP
    op.dividend("moomoo_my", "1155", "2026-04-15", "NET", 300)                      # MY cash
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

    # ---- the three broker-statement cash kinds (2026-08-13, P1a/B1) --------------------
    # Booked in USD on schwab ON PURPOSE: that pool has two FX conversions behind it, so it
    # is the only place where the SECOND axis is observable. Each of these is a cash movement
    # that is NOT an acquisition of foreign currency, and the oracle carries its own
    # ``DEBIT_KINDS`` / ``ACQUIRING_KINDS`` written independently of the app's table.
    #
    # What this scenario is here to catch, and what a TWD-only version would miss entirely:
    #   * a BROKER_FEE or INTEREST_EXPENSE that INCREASES the balance — the old predicate was
    #     "WITHDRAW vs everything else", so a third debit kind silently credited the pool;
    #   * an INTEREST credit that DILUTES ``covered_ratio`` — income arising inside the pool
    #     inherits the pool average (like sale proceeds and foreign cash dividends), so a USD
    #     account that never left a conversion unbased must still report a ratio of exactly 1
    #     after being paid interest. Booking it as an unbased acquisition would raise a FALSE
    #     "basis incomplete" flag over the whole foreign exposure, cash AND stocks (F3).
    # None of the three enters XIRR (D1=A) — ``xirr_reporting`` does not take cash movements
    # at all, so that is held by its signature rather than by a filter these rows could slip.
    op.cash_move("schwab", "interest", "USD", "2026-06-27", "12.34")
    op.cash_move("schwab", "interest_expense", "USD", "2026-06-28", "5.67")
    op.cash_move("schwab", "broker_fee", "USD", "2026-06-29", "8.90")

    buy("B14", "schwab", "MSFT", "2026-05-05", 20, 410)
    sell("S7", "schwab", "MSFT", "2026-06-05", 10, 420)
    buy("B15", "schwab", "AAPL", "2026-06-15", 30, 210)
    rdup = op.trade("schwab", "AAPL", "buy", "2026-06-15", 30, 210, fee_check=False)
    ev.check("dup.creates_second_row", "schwab/AAPL 30@210 duplicate",
             "201", str(rdup.get("status")), "phase1")

    op.dividend("tw_broker", "2330", "2026-03-01", "CASH", 2000)
    op.dividend("moomoo_my", "1155", "2026-06-14", "NET", 150)
    op.cash_move("schwab", "deposit", "TWD", "2026-04-01", 300000)  # top-up deposit
    buy("B22", "tw_broker", "2330", "2026-04-15", 100, 630)
    buy("B23", "moomoo_my", "NVDA", "2026-05-28", 5, 580)
    op.dividend("schwab", "MSFT", "2026-06-08", "DRIP", 40, reinvest_price=415)
    # P1b (2026-08-13): the SAME US position pays plain cash in another quarter. Before this
    # the `drip_us` model accepted only DRIP, so every real US cash payout was rejected row by
    # row as a `dividend_type_mismatch`. It needs no accounting change — a CASH row is in
    # `CASH_DIVIDEND_TYPES` and reduces `adjusted_total` like any other (D35) — which is
    # exactly why it belongs here: the claim "no accounting change" is only worth anything if
    # an independent oracle recomputes the position and agrees.
    op.dividend("schwab", "AAPL", "2026-06-10", "CASH", 25, withholding="7.50")
    sell("S8", "tw_broker", "2330", "2026-06-22", 100, 710)
    sell("S9", "schwab", "AAPL", "2026-06-23", 20, 208)
    sell("S10", "moomoo_my", "1155", "2026-06-24", 100, "11.50")
    sell("S11", "tw_broker", "2330", "2026-06-26", 50, 715)

    # ---- Found-bug op #3 (audit H2, 2026-07-26) — a CASH-family dividend paid AFTER the
    #      position is fully closed. TW/MY pay weeks after the ex-date, so holding on the
    #      ex-date and being flat by the payment date is ordinary. The payout used to be
    #      absorbed into the zero-share position and then DISCARDED with it: 股利總覽 and the
    #      XIRR flows counted it, 總報酬 / 已實現 did not. It is now a realized row
    #      (kind="dividend"). 5225 is otherwise untraded, so the position genuinely reaches
    #      zero and the op cannot be satisfied by any other code path.
    buy("BH2", "moomoo_my", "5225", "2026-05-04", 200, "6.00")
    sell("SH2", "moomoo_my", "5225", "2026-05-20", 200, "6.50")   # position -> 0
    op.dividend("moomoo_my", "5225", "2026-06-16", "NET", 120)    # lands AFTER the close

    # ---- Found-bug op #2 — TW daytrade sell taxes at 0.15% (not 0.3%). Two surfaces:
    #      (a) the MANUAL entry API body flag; (b) a CSV `daytrade` column row. Each is a
    #      same-day buy+sell so no position is left dangling.
    buy("DTB", "tw_broker", "2330", "2026-06-27", 100, 700)                # same-day buy
    op.trade("tw_broker", "2330", "sell", "2026-06-27", 100, 720, daytrade=True)  # manual flag
    op.daytrade_csv("tw_broker", "2330", "2026-06-28", 100, 705, 725)      # CSV daytrade column

    # ---- Declared-short lifecycle (2026-07-31) + date-aware 賣超 guard ----
    # (a) BACK-DATED sell: schwab/MSFT is well covered TODAY, but on 2026-01-01 (before
    #     its first buy, 2026-01-20) it held nothing — the date-aware guard must 422 and
    #     NAME the short date (a net-only check would have let this through).
    r = op.trade("schwab", "MSFT", "sell", "2026-01-01", 5, 400,
                 expect_status=422, fee_check=False)
    ev.check("guard.backdated_sell_blocks", "schwab/MSFT sell 5@2026-01-01 (buys are later)",
             "422", str(r.get("status")), "phase1")
    _issues = ((r.get("json") or {}).get("error") or {}).get("issues") or []
    _txt = " ".join(str(i.get("text", "")) for i in _issues)
    ev.check("guard.backdated_names_date", "422 message names the sell's own date",
             True, "2026-01-01" in _txt, "phase1")
    # (b) an UNDECLARED sell on an empty position stays blocked — short is never inferred.
    r = op.trade("tw_broker", "2609", "sell", "2026-06-29", 2000, 100,
                 expect_status=422, fee_check=False)
    ev.check("guard.short_needs_declaration", "tw_broker/2609 undeclared sell, empty position",
             "422", str(r.get("status")), "phase1")
    # (c) DECLARED short open: sells past zero with no ack; fee/tax book like any TW sell.
    op.trade("tw_broker", "2609", "sell", "2026-06-29", 2000, 100, short=True)
    # (d) a dividend recorded while the short is open — the app must NOT book it
    #     (skip + flag on the dashboard path); the oracle mirrors the skip.
    op.dividend("tw_broker", "2609", "2026-06-30", "CASH", 3000)
    reconcile(ev, api, db_path, "short_open", valuation=True)
    # (e) STRICT path probe (op-logged, no assertion): 重算 replays without allow_oversell,
    #     where the dividend-on-short is specified to raise. A 5xx here is an app finding
    #     (never-500 rule) — recorded in the oplog for the report.
    rr = api.post("/api/actions/recompute", {})
    ev.op("phase1", "API", "probe.recompute_strict_short_dividend", {},
          {"status": rr.status_code, "json": _json(rr)},
          note="expected a 4xx degradation; see report if 5xx")
    # (f) user-resolution flow: remove the unbookable dividend row, then cover the short.
    _c = sqlite3.connect(str(db_path))
    try:
        _row = _c.execute("SELECT id FROM dividends WHERE account_id=? AND symbol=?",
                          ("tw_broker", "2609")).fetchone()
    finally:
        _c.close()
    rd = api.delete(f"/api/ledgers/dividends/{_row[0]}")
    ev.op("phase1", "API", "delete.dividend", {"id": _row[0]},
          {"status": rd.status_code, "json": _json(rd)}, note="remove unbookable dividend")
    ev.check("short.unbookable_dividend_removable", "delete the skipped dividend row",
             "200", str(rd.status_code), "phase1")
    # (g) partial cover then full cover -> realized short_cover rows dated the cover days;
    #     the position ends FLAT so the final KPI/XIRR reconcile runs at full strength.
    buy("SC1", "tw_broker", "2609", "2026-07-01", 800, 95)
    buy("SC2", "tw_broker", "2609", "2026-07-06", 1200, 98)

    run_corporate_actions(ev, api, db_path, op, buy, sell)
    run_batch_import(ev, api, db_path, op)

    reconcile(ev, api, db_path, "final", valuation=True, reports=True)


def run_batch_import(ev: C.Evidence, api: C.Api, db_path, op: Ops):
    """A CSV batch is validated against the ledger PLUS its own siblings (C1/C2, 2026-08-14).

    Every other op in this scenario writes ONE row, which is the one shape in which "the
    whole file is one batch" is unobservable — a batch of one has no siblings. Both defects
    this block exists for were measured on a synthetic broker export and both are invisible
    to a single-row door:

    **C1 — a sell covered by a buy in the SAME file.** ``build_transaction_preview``
    validated each row against the STORED ledger alone, so it raised 賣超 on a position the
    file itself creates (7 of 47 rows). The numbers end up right if the owner confirms, and
    that is precisely the harm: 賣超 is the ONE confirmation whose acknowledgement
    permanently discards a cost basis, so every false one trains the owner to click it.

    **C2 — a corporate-action CHAIN in the same file.** The importer built its
    ``ActionIndex`` from stored rows only, so a SPLIT whose shares arrive from an EXCHANGE
    earlier in the same file was hard-rejected ``no_position_on_action_date`` — and
    ``commit_preview`` reported it as merely 「跳過」, so the import announced success while
    the share count stayed wrong by the whole ratio. Both of the owner's real chains have
    this shape.

    The oracle needs no new arithmetic for either: what changed is which ROWS reach the
    ledger, and it replays whatever is there. That is the point — before C2 the app and the
    oracle would have DISAGREED on CCB's share count, which is exactly what this harness is
    for. Placed last so everything above stays a control.
    """
    op.cash_move("tw_broker", "deposit", "TWD", "2026-01-05", 200000)

    # ---- C1: buy then sell, ONE file. The sell is covered only by its sibling. ----------
    sibling_csv = ("account,symbol,side,date,shares,price\n"
                   "tw_broker,CBS,buy,2026-05-04,300,300\n"
                   "tw_broker,CBS,sell,2026-05-20,200,320\n")
    # ⚠ PREVIEW FIRST, on a ledger that does not hold these rows yet. Probing after the
    # commit measures a re-upload, where both rows raise the M7 `duplicate_trade` warning —
    # a true statement about a different question, and it made the first version of this
    # check fail for a reason that had nothing to do with the guard under test.
    warns = _preview_warnings(api, "transactions", sibling_csv)
    ev.check("batch.sibling_cover_no_oversell", "buy+sell in one file raises no 賣超",
             0, len(warns), "phase1:batch")
    r = op.import_csv(
        "transactions", "batch_sibling_cover",
        ["account", "symbol", "side", "date", "shares", "price"],
        [["tw_broker", "CBS", "buy", "2026-05-04", "300", "300"],
         ["tw_broker", "CBS", "sell", "2026-05-20", "200", "320"]],
    )
    body = r.get("json") or {}
    # The row count alone would pass even with the guard firing — a 賣超 warning is
    # confirmable and `ack_warnings=True` writes it anyway. That is what the probe above is
    # for; this asserts the rows actually landed.
    ev.check("batch.sibling_cover_writes_both", "both rows of the sibling file are written",
             2, body.get("written"), "phase1:batch")

    # ---- C2: EXCHANGE then SPLIT on the destination, ONE file --------------------------
    op.import_csv(
        "transactions", "batch_chain_position",
        ["account", "symbol", "side", "date", "shares", "price"],
        [["tw_broker", "CCA", "buy", "2026-05-05", "400", "120"]],
    )
    r = op.import_csv(
        "corporate_actions", "batch_action_chain",
        ["account", "date", "kind", "from_symbol", "to_symbol",
         "ratio_to", "ratio_from", "cost_carry", "note"],
        [["tw_broker", "2026-05-25", "EXCHANGE", "CCA", "CCB", "1", "1", "", "chain leg 1"],
         ["tw_broker", "2026-06-05", "SPLIT", "CCB", "CCB", "2", "1", "", "chain leg 2"]],
    )
    body = r.get("json") or {}
    ev.check("batch.action_chain_writes_both", "both legs of an in-file action chain are written",
             2, body.get("written"), "phase1:batch")
    # ⚠ The load-bearing one. Before C2 the second leg was REJECTED and reported as
    # 「跳過」 — `written` alone read 1 and nothing said why. `rejected` is absent when zero
    # (the `duplicates` convention), so `or 0` is the honest read of a clean response.
    ev.check("batch.action_chain_nothing_rejected", "no row of the chain is rejected",
             0, body.get("rejected") or 0, "phase1:batch")

    res = reconcile(ev, api, db_path, "batch_import", valuation=True)
    # 400 CCA → 1:1 EXCHANGE → 400 CCB → 2-for-1 SPLIT → 800. If the chain's second leg is
    # dropped the app reports 400 here while the oracle says 800: the disagreement the
    # reconcile above would surface, pinned to a literal so it is also visible on its own.
    P1.anchor(ev, api, "batch.anchor.chain_split_applied", "tw_broker", "CCB", "shares", "800")
    P1.anchor(ev, api, "batch.anchor.sibling_cover_left", "tw_broker", "CBS", "shares", "100")
    ev.check("batch.refusal_codes", "batch_import", [], [c for c, _ in res.action_refusals],
             "phase1:batch_import")


def _preview_warnings(api: C.Api, kind: str, csv_text: str) -> list[dict]:
    """Preview-only probe: the rows a commit would ask the owner to confirm.

    Read-only, and it is the ONLY way to see the guard from outside — a committed row that
    was confirmed looks identical to one that never raised anything.
    """
    r = api.post("/api/import/preview", {"kind": kind, "csv_text": csv_text})
    try:
        rows = (r.json() or {}).get("rows", [])
    except Exception:
        return []
    return [row for row in rows if row.get("status") == "warn"]


def run_corporate_actions(ev: C.Evidence, api: C.Api, db_path, op: Ops, buy, sell):
    """SPLIT / EXCHANGE / SPINOFF — spec 2026-08-06 §7.4's required scenario set.

    Placed last so every existing expectation above runs as a CONTROL: the ledger is
    settled and flat at this point (the declared short is covered), so the corporate-action
    checkpoint reconciles at full strength — holdings, realized, cash, FX, KPIs and XIRR —
    and any failure above is attributable to something other than this block.

    Dedicated symbols (CA*), funded by their own TWD deposit. tw_broker is not FX-exposed,
    so the whole block moves no FX pool.

    §7.4's list, and where each item is: forward split -> sell (CA1) · reverse split ->
    cash-in-lieu sell (CA2) · exchange into an EXISTING position (CA3 -> CA4) · spinoff ->
    sell the child (CA7 -> CA8) · split on a dividend-adjusted position, adjusted != original
    (CA9) and on a DRIP position (CAD) · a 2-for-7 exchange, the §3.1(ii) exactness case
    (CA5 -> CA6) · rejection paths (E5 on CAX, E2 on CAZ, E1 on CAN).
    """
    op.cash_move("tw_broker", "deposit", "TWD", "2026-01-05", 400000)

    # ---- positions the actions will act on (all pre-action, all before any action date) --
    buy("CA1B", "tw_broker", "CA1", "2026-02-03", 100, 600)     # -> 3-for-1 split
    buy("CARB", "tw_broker", "CAR", "2026-02-06", 210, 90)      # -> 1-for-3, exactly 70
    buy("CA2B", "tw_broker", "CA2", "2026-02-05", 705, 30)      # -> 1-for-10 reverse split
    buy("CA4B", "tw_broker", "CA4", "2026-02-11", 40, 500)      # EXCHANGE DESTINATION,
    buy("CA3B", "tw_broker", "CA3", "2026-02-12", 100, 200)     #   already held: merge
    buy("CA5B", "tw_broker", "CA5", "2026-02-17", 700, 40)      # -> 2-for-7 exchange
    buy("CA7B", "tw_broker", "CA7", "2026-02-19", 400, 250)     # -> spinoff parent
    buy("CA9B", "tw_broker", "CA9", "2026-02-21", 200, 300)     # -> split, adjusted != orig
    buy("CADB", "schwab", "CAD", "2026-02-24", 80, 25)          # -> split on DRIP history
    # CA9 takes a CASH dividend so its adjusted_total diverges from original_total BEFORE
    # the split: the split must move neither, and dividend_portion / 回本進度 must be
    # identical either side of it (§4.1). CAD takes a US DRIP, so the split also lands on
    # a position whose share count already includes zero-cost reinvested shares.
    op.dividend("tw_broker", "CA9", "2026-03-05", "CASH", 4000)
    op.dividend("schwab", "CAD", "2026-03-06", "DRIP", 40, reinvest_price=25)
    # ---- SAME-DAY trades, on an action's own effective date -------------------------
    # §4: an action is effective at the START of its date, so a same-day trade is quoted
    # in POST-action terms. Without these two rows the whole EventPriority ordering is
    # unobservable — every other date in this block has an action alone on it, and a
    # replay that ran CORPORATE_ACTION after BUY would produce identical numbers.
    #   sell 40 CA1 @ 205 — a post-SPLIT price and quantity on the split's own date;
    #   buy 100 CA7 @ 190 — after the SPINOFF, so the child is carved from 400 shares and
    #   the parent then holds 500. Ordered the other way the child would be 125.
    sell("CA1SD", "tw_broker", "CA1", "2026-03-02", 40, 205)
    buy("CA7SD", "tw_broker", "CA7", "2026-03-24", 100, 190)

    # ---- the action rows (spec §3's ledger; ratio as TWO INTEGER TERMS, never a quotient)
    op.corporate_action("tw_broker", "SPLIT", "2026-03-02", "CA1", "CA1", 3, 1,
                        note="forward split 3-for-1")
    op.corporate_action("tw_broker", "SPLIT", "2026-03-14", "CAR", "CAR", 1, 3,
                        note="1-for-3 of 210 — evaluation order decides the integer")
    op.corporate_action("tw_broker", "SPLIT", "2026-03-10", "CA2", "CA2", 1, 10,
                        note="reverse split 1-for-10; fraction paid in cash (§3.2)")
    op.corporate_action("tw_broker", "EXCHANGE", "2026-03-16", "CA3", "CA4", 1, 2,
                        note="merger into an already-held destination")
    op.corporate_action("tw_broker", "EXCHANGE", "2026-03-18", "CA5", "CA6", 2, 7,
                        note="2-for-7 — the §3.1(ii) exactness case")
    op.corporate_action("tw_broker", "SPINOFF", "2026-03-24", "CA7", "CA8", 1, 4,
                        cost_carry="0.30", note="8-K allocation 30% to the child")
    op.corporate_action("tw_broker", "SPLIT", "2026-04-02", "CA9", "CA9", 2, 1,
                        note="split on a dividend-adjusted position")
    op.corporate_action("schwab", "SPLIT", "2026-04-04", "CAD", "CAD", 5, 4,
                        note="split on a DRIP-grown position")

    res = reconcile(ev, api, db_path, "corp_applied", valuation=True)

    # ---- ABSOLUTE anchors: the app's number vs a hand-computed literal ----------------
    # Oracle-vs-app agreement proves both replays match; it cannot prove either landed on
    # the share count the statement says, because a rounded ratio in BOTH agrees at 199.99.
    # These literals come from the ratio and the pre-action count, nothing else.
    # 100 x 3 / 1 = 300, then the SAME-DAY sell of 40 (SELL runs after CORPORATE_ACTION).
    P1.anchor(ev, api, "corp.anchor.split_forward", "tw_broker", "CA1", "shares", "260")
    P1.anchor(ev, api, "corp.anchor.split_reverse", "tw_broker", "CA2", "shares", "70.5")
    # 210 x 1 / 3 = EXACTLY 70. Written 210 x (1/3) it is 69.99999999999999999999999999,
    # and the sell of exactly 70 below then fails validate.py's bare `>` (spec §3.1(ii)(a)).
    P1.anchor(ev, api, "corp.anchor.split_ratio_exact", "tw_broker", "CAR", "shares", "70")
    P1.anchor(ev, api, "corp.anchor.exchange_merge", "tw_broker", "CA4", "shares", "90")
    # 700 x 2 / 7 == exactly 200. Written 700 x (2/7) it is 200.0000000000000000000000000
    # here but a hair under an integer for 3,530 other measured (shares, to, from) triples,
    # and THAT is what trips validate.py's bare `>` on the next sell (spec §3.1(ii)).
    P1.anchor(ev, api, "corp.anchor.exchange_2for7", "tw_broker", "CA6", "shares", "200")
    # The child is carved from the 400 shares held BEFORE the same-day buy; the parent
    # then holds 400 + 100. Run CORPORATE_ACTION after BUY and these become 125 and 500.
    P1.anchor(ev, api, "corp.anchor.spinoff_child", "tw_broker", "CA8", "shares", "100")
    P1.anchor(ev, api, "corp.anchor.spinoff_parent", "tw_broker", "CA7", "shares", "500")
    # The parent's basis is `total - carved`, never `total x (1-c)` (§4.3): buy 400 @ 250
    # = 100,000 + TW fee floor(100000 x 0.001425) = 142  ->  100,142 all-in; the child
    # carries 30% = 30,042.60 and the parent keeps exactly the remainder, 70,099.40, plus
    # the same-day buy's all-in 19,000 + floor(19000 x 0.001425)=27  ->  89,126.40.
    P1.anchor(ev, api, "corp.anchor.spinoff_child_basis", "tw_broker", "CA8",
              "original_cost_total", "30042.60")
    P1.anchor(ev, api, "corp.anchor.spinoff_parent_basis", "tw_broker", "CA7",
              "original_cost_total", "89126.40")
    # A split moves NEITHER total, so the dividend portion and 回本進度 are untouched: the
    # 4,000 cash dividend is still exactly 4,000 of the cost recovered after a 2-for-1.
    P1.anchor(ev, api, "corp.anchor.split_keeps_dividend_portion", "tw_broker", "CA9",
              "dividend_portion", "4000")
    P1.anchor(ev, api, "corp.anchor.split_shares_dividend_adj", "tw_broker", "CA9",
              "shares", "400")
    ev.check("corp.refusal_codes", "corp_applied", [], [c for c, _ in res.action_refusals],
             "phase1:corp_applied")

    # ---- dependent trades: the sells that only make sense post-action -----------------
    # CA1: 50 <= the PRE-split count still unsold, so this one commits regardless of
    # whether the date-aware guard is action-aware yet; its P&L is still computed against
    # the POST-split adjusted average (a third of the pre-split one), which is the point.
    sell("CA1S", "tw_broker", "CA1", "2026-04-03", 50, 210)
    # CAR: a sell of EXACTLY the post-split count. `validate.py` compares with a bare `>`
    # and no epsilon, so a share count a hair under 70 — which is what `210 x (1/3)` gives —
    # rejects this row, the owner acknowledges it, and the STICKY 賣超 guard discards the
    # cost basis permanently. That is the §1 disaster produced by the arithmetic alone.
    r = sell("CARS", "tw_broker", "CAR", "2026-05-18", 70, 280)
    ev.check("corp.sell_exact_ratio_accepted", "tw_broker/CAR sell 70 (== 210 x 1/3)",
             "201", str(r.get("status")), "phase1:corp")
    # CA2: the reverse split leaves 70.5 shares. The broker pays cash for the 0.5 and that
    # is a REAL disposal with real realized P&L, so it is an ORDINARY SELL at the implied
    # price (§3.2) — never folded into the action row, which must apply the ratio exactly.
    sell("CA2S", "tw_broker", "CA2", "2026-03-12", "0.5", 300)
    # CA6/CA8 exist ONLY because of an action, so these two sells prove the date-aware
    # guard walks the action-aware share path (W4/§6.2, E1a.1). A sell of exactly 200 is
    # also §3.1(ii)'s named case: it must pass validate.py's bare `>` with no epsilon.
    r = sell("CA6S", "tw_broker", "CA6", "2026-04-20", 200, 150)
    ev.check("corp.sell_exact_200_accepted", "tw_broker/CA6 sell 200 (== 700 x 2/7)",
             "201", str(r.get("status")), "phase1:corp")
    r = sell("CA8S", "tw_broker", "CA8", "2026-04-28", 60, 320)
    ev.check("corp.sell_spinoff_child_accepted", "tw_broker/CA8 sell 60 of the child",
             "201", str(r.get("status")), "phase1:corp")
    sell("CADS", "schwab", "CAD", "2026-05-06", 40, 22)
    # PROBE (op-logged, no assertion): a sell that EXCEEDS the pre-split count and is
    # covered only by the split — the headline case of spec §1. It commits only when the
    # oversell guard walks the action-aware path; a 422 here is a W4 state report, not a
    # harness failure, and either outcome reconciles because the oracle replays whatever
    # actually committed.
    rp = op.trade("tw_broker", "CA1", "sell", "2026-04-06", 150, 205, fee_check=False)
    ev.op("phase1", "API", "probe.sell_past_pre_split_count",
          {"symbol": "CA1", "shares": 150, "pre_split_remaining": 10},
          {"status": rp.get("status")},
          note="201 = the date-aware guard is corporate-action aware (W4); 422 = it is not")

    # ---- REJECTION paths: the action is NOT applied (spec §5) -------------------------
    # E5 — EXCHANGE out of an OPEN DECLARED SHORT. No honest booking exists (precedent:
    # dividend-on-short), so the event is skipped and the position flagged; the shares stay
    # in pre-action terms, which is exactly what the flag announces.
    op.trade("tw_broker", "CAX", "sell", "2026-03-01", 500, 80, short=True)
    cax_id = op.corporate_action("tw_broker", "EXCHANGE", "2026-03-20", "CAX", "CAY", 1, 1,
                                 note="E5: refused — source holds an open declared short")
    # E2 — an action on a CLOSED (0-share, 0-basis) position. Scaling nothing gives
    # nothing; re-animating it would invent a ghost.
    buy("CAZB", "tw_broker", "CAZ", "2026-02-26", 50, 100)
    sell("CAZS", "tw_broker", "CAZ", "2026-03-04", 50, 110)
    caz_id = op.corporate_action("tw_broker", "SPLIT", "2026-03-26", "CAZ", "CAZ", 2, 1,
                                 note="E2: refused — position closed before the action date")
    # E1/E1a — an action whose from_symbol was NEVER held. The dashboard calls build_book
    # with no try/except, so this row must SKIP, not raise: a 500 here is the never-500
    # rule breached at a call site the standing rule already covers.
    can_id = op.corporate_action("tw_broker", "SPLIT", "2026-03-28", "CAN", "CAN", 3, 1,
                                 note="E1: refused — no position on the action date")
    ev.check("corp.e1a_dashboard_200", "action on a never-held symbol must not 500",
             "200", str(api.get("/api/dashboard").status_code), "phase1:corp")

    res = reconcile(ev, api, db_path, "corp_refused", valuation=False)
    ev.check("corp.refusal_codes", "corp_refused", ["E5", "E2", "E1"],
             [c for c, _ in res.action_refusals], "phase1:corp_refused")
    # The refused source keeps its position AND carries the flag; the refusal changed no
    # money at all, only the disclosure. (Asserted per-holding by reconcile's
    # holding.unbookable_action / holding.shares comparisons.)
    P1.anchor(ev, api, "corp.anchor.e5_source_unmoved", "tw_broker", "CAX", "shares",
              "-500", phase="phase1:corp_refused")
    P1.anchor(ev, api, "corp.anchor.e5_source_flagged", "tw_broker", "CAX",
              "unbookable_action", True, phase="phase1:corp_refused")

    # ---- XIRR is blanked PORTFOLIO-WIDE by an unapplied action, and says which row ----
    # The deliberate blast radius (D38 invariant 2): everywhere else one skipped action
    # damages exactly one stock, but XIRR is a single figure over a terminal value that
    # sums every holding, so one pre-action share count makes the whole sum wrong. Two
    # of the three refusals here have NO surviving position to carry a flag (E2's source
    # is zero-share and dropped, E1's never existed), so a holdings-level check cannot see
    # them at all — this assertion is the only one that does.
    d_ref = api.get("/api/dashboard").json()
    ev.check("corp.xirr_blanked_by_unapplied", "3 unapplied actions -> xirr is None",
             None, d_ref["kpis"].get("xirr"), "phase1:corp_refused")
    _reason = (d_ref.get("freshness") or {}).get("xirr_unavailable_reason") or ""
    for _sym, _acct, _date in (("CAX", "tw_broker", "2026-03-20"),
                               ("CAZ", "tw_broker", "2026-03-26"),
                               ("CAN", "tw_broker", "2026-03-28")):
        # Naming the account, symbol and date is the requirement — a bare "something is
        # wrong" would force the owner to hunt the row across a multi-account book.
        ev.check("corp.xirr_reason_names_row", f"{_acct}/{_sym}@{_date}", True,
                 (_sym in _reason and _acct in _reason and _date in _reason),
                 "phase1:corp_refused")

    # ---- resolve, so the final checkpoint reconciles at full strength ------------------
    # Cover the short, then remove all three rows that could not be applied. Each is an
    # action that should not have been entered against this ledger (a short source, a
    # closed position, a symbol never held), so deletion is the owner's real remedy —
    # the same shape as the unbookable-dividend resolution earlier in this scenario.
    # E16: deleting an action RE-COMPUTES history, nothing was snapshotted, so the
    # refusals disappear from the next replay rather than lingering as a stored verdict —
    # which is what lets the `final` checkpoint compare XIRR at full strength again.
    buy("CAXC", "tw_broker", "CAX", "2026-05-12", 500, 76)
    op.delete_corporate_action(cax_id)
    op.delete_corporate_action(caz_id)
    op.delete_corporate_action(can_id)


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
        # ``scope`` is free text and many of them are Traditional Chinese, so on a cp1252
        # console this line RAISED — and only ever on a run that had a failure to report,
        # which is the one run whose output matters. Encode-safe rather than encoding-
        # dependent: the summary above still printed, so the crash looked like a harness
        # bug rather than the failure detail going missing (measured 2026-08-14).
        line = (f"  FAIL {f['check']} | {f['scope']} | "
                f"exp= {f['expected']} got= {f['actual']}")
        print(line.encode(sys.stdout.encoding or "utf-8", "replace")
                  .decode(sys.stdout.encoding or "utf-8", "replace"))


if __name__ == "__main__":
    main()
