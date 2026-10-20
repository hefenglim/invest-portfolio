"""Phase 1 runner: deterministic scenario over the local isolated server."""

from __future__ import annotations

import argparse
import io
import sqlite3
import sys
import zipfile
from datetime import timedelta

import common as C
import oracle as O
import phase1 as P1
from common import dec
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
    # PERF spec §18: a back-dated edit on a SETTLED store -> the stale range asserted -> a run.
    run_backdated_edit(ev, api, db_path, op, tid)
    # PERF §18: a CLOSED prior year (Y ≡ December, Σ months ≡ year ≡ its tax package).
    run_prior_year(ev, api, db_path, op, buy, sell)

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
    # R6 / review ⑧ — a TW 配股 whose ex-date and payment date are a month apart. Permanent
    # scenario op (accumulation rule ①): without one, the ex-date replay agrees with the
    # oracle by both never exercising it. The oracle derives the same STOCK-only rule
    # independently, so the two must agree on the share count between the two dates.
    op.dividend("tw_broker", "2330", "2026-05-30", "STOCK", 0,
                reinvest_shares=100, ex_date="2026-05-02")

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
    # XIRR (AI-D42, 2026-08-24 — this paragraph SUPERSEDES the D1=A note that stood here, and
    # the claim it rested on: ``xirr_reporting`` now DOES take cash movements, so the guarantee
    # is no longer "held by the signature"). The four rows below straddle the new line, which
    # is why all four are here rather than only the ones that count:
    #   * ``rebate`` / ``interest_expense`` / ``broker_fee`` — costs of TRADING and FINANCING;
    #     they enter the flow series and the oracle must reproduce each sign independently.
    #   * ``interest`` — pool income, but the principal earning it never entered XIRR's
    #     denominator, so it stays OUT. A test that only covered the included kinds could not
    #     tell a correct exclusion from a forgotten one.
    # ``rebate`` is the row that matters most and had no coverage until now: FE-D1's
    # charge-first model refunds 77% of a TW commission next month, which is 0.229% of capital
    # per round trip and the owner's ordinary billing — the single largest figure this change
    # makes visible.
    op.cash_move("schwab", "interest", "USD", "2026-06-27", "12.34")
    op.cash_move("schwab", "interest_expense", "USD", "2026-06-28", "5.67")
    op.cash_move("schwab", "broker_fee", "USD", "2026-06-29", "8.90")
    op.cash_move("schwab", "rebate", "USD", "2026-06-30", "21.50")

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

    # PERF spec §11.2 / §18 "Period closes": a split the first trading day after a month end,
    # a split ON a weekend month end, a held stock with no close at a month end, a cash
    # dividend between its ex-date and its pay date across a month end.
    run_month_end(ev, api, db_path, op, buy, sell)
    # CR-11: a 賣超 netted back to zero still withholds XIRR, named, until resolved.
    run_flat_oversell(ev, api, db_path, op, buy, sell)
    run_corporate_actions(ev, api, db_path, op, buy, sell)
    run_batch_import(ev, api, db_path, op)
    # PERF spec §11.4: a correction dated LAST MONTH changes last month's row at once. Placed
    # right after a checkpoint, so the store is clean and the correction is the only change.
    run_live_window(ev, api, db_path, op)
    # Options run BEFORE the refused-dividend block on purpose: that block leaves two refused
    # payouts in the ledger until `final`, which marks every later trend day incomplete and
    # withholds B — and would mask exactly what the options checkpoints must see (R3: an open
    # contract is excluded from the trend WITHOUT marking the day incomplete; B is withheld by
    # a refused close and by nothing else the options block does).
    run_options(ev, api, db_path, op)
    run_refused_dividends(ev, api, db_path, op, buy, sell)
    # Spec §11.2 "FX": a month end with no rate for a held currency (temporary checkpoint).
    run_missing_fx(ev, api, db_path, op)

    reconcile(ev, api, db_path, "final", valuation=True, reports=True)


def run_backdated_edit(ev: C.Evidence, api: C.Api, db_path, op: Ops, tid: dict[str, int]):
    """PERF spec §18 "Stress audit": a back-dated edit -> the stale range asserted -> ``POST
    /api/settlement/run`` -> wait -> reconciled.

    The store is clean here (``checkpoint1`` just settled), so ONE transaction moved two days
    EARLIER is the only change, and the trigger's day can be pinned, not merely bounded: a row
    moved to another date marks from the earlier of its two days (spec §7) — 2026-03-18. That
    day lies before the live window, so the days ``[2026-03-18, W − 1]`` are the previous
    generation's (stale, PERF-D3) and the prompt is due from that day until a run settles them.
    ``reconcile`` then re-asserts the same through its generic rules (and, with the read path,
    that the stale days ARE the previous generation's), runs the settlement and reconciles.
    """
    ph = "phase1:backdated_edit"
    txn = tid["B21"]                      # schwab AAPL buy 25 @185, 2026-03-20
    fee, tax = C.read_fee_tax_from_db(db_path, txn)
    r = op.edit_tx(txn, "schwab", "AAPL", "buy", "2026-03-18", 25, 185, fee, tax)
    ev.check("backdated_edit.accepted", "B21 2026-03-20 -> 2026-03-18", True,
             r.get("status") in (200, 201), ph)
    st = P1._status(api)
    w = P1.window_start(P1.server_today())
    ev.check("backdated_edit.dirty_from", "B21 2026-03-20 -> 2026-03-18", "2026-03-18",
             st.get("dirty_from"), ph)
    ev.check("backdated_edit.stale_range", f"[2026-03-18, W - 1] with W = {w}",
             ["2026-03-18", (w - timedelta(days=1)).isoformat()],
             [st.get("stale_from"), st.get("stale_to")], ph)
    ev.check("backdated_edit.live_from", f"W = {w}", w.isoformat(), st.get("live_from"), ph)
    ev.check("backdated_edit.prompt", "due from the edited day", [True, "2026-03-18"],
             [bool(st.get("needs_prompt")), (st.get("prompt") or {}).get("from")], ph)
    ev.check("backdated_edit.state", "待結算 until a run", "pending", st.get("state"), ph)
    reconcile(ev, api, db_path, "backdated_edit", valuation=True)


def _seed_month_end_series(ev: C.Evidence, db_path, phase: str, symbols) -> None:
    """The fixed weekday closes of *symbols* (``phase1.MONTH_END_SERIES``) — written AFTER
    their trades: the price trigger admits only ledger symbols (spec §7), and these rows must
    count as changes the settlement takes. Recorded in ``phase1.SEEDED_SERIES``, the fixture's
    own account of what it wrote."""
    rows: dict = {}
    for sym in symbols:
        market = P1.MONTH_END_SERIES[sym][0]
        for day, close in P1.month_end_series(sym).items():
            rows.setdefault(day, []).append((sym, market, close))
    n = C.seed_prices_daily(db_path, rows, source="stress-fixture-month-end")
    P1.SEEDED_SERIES.update(symbols)
    ev.op(phase, "DB", "prices.month_end_series", {"symbols": sorted(symbols)}, {"rows": n})


def _holding_literal(h: dict | None) -> list:
    h = h or {}
    return [P1._cv(dec(h[k])) if h.get(k) is not None else None
            for k in ("shares", "price")] + [h.get("price_as_of"),
                                             P1._cv(dec(h["market_value"]))
                                             if h.get("market_value") is not None else None]


def run_prior_year(ev: C.Evidence, api: C.Api, db_path, op: Ops, buy, sell):
    """PERF spec §18 "Period closes" — a CLOSED year. Rows in 2025 (TWD only: the fixture has
    no FX row before 2026 on purpose) make the ledger span two years like prod's: a Y 2025 row
    whose KPIs ≡ the December 2025 row's, whose flow activity ≡ Σ its two months, and which ties
    out sheet by sheet with the 2025 tax package (realized, dividends). Placed after the first
    checkpoints, so the whole 2025 history arrives as a back-dated change on a settled store.
    """
    ph = "phase1:prior_year"
    op.cash_move("tw_broker", "deposit", "TWD", "2025-11-03", 500000)
    buy("YRAB", "tw_broker", "YRA", "2025-11-04", 1000, 120)
    op.dividend("tw_broker", "YRA", "2025-12-10", "CASH", 3000)
    sell("YRAS1", "tw_broker", "YRA", "2025-12-16", 400, 125)
    sell("YRAS2", "tw_broker", "YRA", "2026-02-17", 600, 121)
    _seed_month_end_series(ev, db_path, ph, ("YRA",))
    reconcile(ev, api, db_path, "prior_year", valuation=True)
    if P1.READ_PATH:
        years = [str(r.get("period")) for r in
                 (P1._get_rows(api, "/api/settlement/periods", kind="Y")[1] or [])]
        ev.check("prior_year.anchor.closed_year_row", "Y rows include 2025", True,
                 "2025" in years, ph)
    r = api.post("/api/export/tax-package", {"year": 2025})
    sheets = {}
    if r.status_code == 200:
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            sheets = {n: P1._parse_csv(z.read(n).decode("utf-8-sig")) for n in z.namelist()
                      if n.endswith(".csv")}
    ev.check("prior_year.anchor.tax_2025", "realized rows / dividend rows (YRA)",
             [["YRA"], ["YRA"]],
             [[x["symbol"] for x in sheets.get("realized_gains_2025.csv", [])],
              [x["symbol"] for x in sheets.get("dividends_2025.csv", [])]], ph)


def run_flat_oversell(ev: C.Evidence, api: C.Api, db_path, op: Ops, buy, sell):
    """CR-11: a 賣超 netted back to EXACTLY zero. The position is flat — no holdings row — but
    the BOOK records it (its basis is gone for good), so XIRR stays withheld with the position
    named, live and in every close from the oversell's day, until the oversell is resolved
    (here: the acknowledged sell deleted, the owner's real remedy)."""
    ph = "phase1:flat_oversell"
    buy("OVFB", "tw_broker", "OVF", "2026-02-02", 100, 33)
    r = op.trade("tw_broker", "OVF", "sell", "2026-02-09", 150, 34, ack=True,
                 expect_status=201, fee_check=False)
    j = r.get("json")
    oversell_id = j.get("txn_id") if isinstance(j, dict) else None
    ev.check("flat_oversell.acked_sell_written", "tw_broker/OVF sell 150 > held 100", 201,
             r.get("status"), ph)
    buy("OVFC", "tw_broker", "OVF", "2026-02-16", 50, "33.5")
    res = reconcile(ev, api, db_path, "flat_oversell", valuation=True)
    ev.check("flat_oversell.oracle_records_it", "the book keeps a flat 賣超",
             ["tw_broker|OVF"], sorted("|".join(k) for k in res.oversold_keys), ph)
    ev.check("flat_oversell.flat", "no holdings row", False,
             ("tw_broker", "OVF") in res.holdings, ph)
    if oversell_id is not None:
        op.delete_tx(oversell_id, ack=True)


def run_month_end(ev: C.Evidence, api: C.Api, db_path, op: Ops, buy, sell):
    """PERF spec §11.2 / §18 "Period closes" — the month-end edges, through the real doors.

    * ``MPX`` is held across month end 2026-02-28 with no close before 2026-03-02: the
      February close lists it in ``notes.missing_prices``, leaves it out of 總市值 while its
      cost stays in the rate's denominator, and withholds XIRR (all-or-nothing).
    * ``SPA`` splits 2-for-1 on Mon 2026-06-01, the first trading day after month end
      2026-05-31; ``SPB`` splits 2-for-1 ON that month end, a Sunday. Both series are AS
      TRADED (``phase1.MONTH_END_SERIES``), so entering either split must leave the May close
      exactly where it was — SPA because the split is after D, SPB because the read divides
      Friday's pre-split close by the split dated on D (§11.2 "a non-trading D carries the last
      close forward"). Pinned as a before / after equality and as hand-computed literals.
    * ``SPD`` splits 2-for-1 on Tue 2026-03-31, a TRADING-DAY month end with its own
      post-split close: 200 @ 30.0725 at D.
    * ``SPE`` is held in schwab AND moomoo_my; its 2-for-1 dated Sat 2026-02-28 (a weekend
      month end) is recorded in both — 2:1 and 4:2. One market event: Friday's 40.265 reads
      20.1325 at D, re-expressed ONCE (the reduced-ratio dedupe). SPD and SPE are split and
      sold BEFORE the first checkpoint, so between the two checkpoints only SPA's and SPB's
      splits change and "the May close is unchanged" isolates them.
    * a 2330 CASH dividend with its ex-date in April and its pay date in May: the April close
      shows it in ``notes.dividends_receivable`` — shown, never counted.
    * ``STL``'s closes have a hole before each of two month ends: 6 days old at 2026-04-30
      (``notes.stale_prices``) and exactly 4 days old at 2026-05-31 (NOT stale: "more than 4
      days") — the boundary itself, which no daily-seeded symbol can reach.

    Every series DRIFTS 0.005 a calendar day, so a close read off the wrong day moves money.
    """
    ph = "phase1:month_end"
    op.cash_move("tw_broker", "deposit", "TWD", "2026-02-02", 200000)
    buy("MPXB", "tw_broker", "MPX", "2026-02-16", 100, 50)
    sell("MPXS", "tw_broker", "MPX", "2026-03-16", 100, 55)
    buy("SPAB", "tw_broker", "SPA", "2026-05-04", 1000, 100)
    buy("SPBB", "tw_broker", "SPB", "2026-05-05", 500, 80)
    buy("STLB", "tw_broker", "STL", "2026-04-01", 300, 60)
    buy("SPDB", "schwab", "SPD", "2026-03-02", 100, 60)
    buy("SPEB", "schwab", "SPE", "2026-01-12", 100, 40)
    buy("SPEM", "moomoo_my", "SPE", "2026-01-13", 50, 40)
    op.dividend("tw_broker", "2330", "2026-05-15", "CASH", 1500, ex_date="2026-04-28")
    _seed_month_end_series(ev, db_path, ph, ("SPA", "SPB", "MPX", "STL", "SPD", "SPE"))
    # SPD and SPE complete their life here — the splits, then the sales — so that between
    # the two checkpoints below ONLY SPA's and SPB's splits change, and "the May close is
    # unchanged" isolates them.
    op.corporate_action("schwab", "SPLIT", "2026-03-31", "SPD", "SPD", 2, 1,
                        note="2-for-1 ON month end 2026-03-31, a trading day")
    op.corporate_action("schwab", "SPLIT", "2026-02-28", "SPE", "SPE", 2, 1,
                        note="2-for-1 ON month end 2026-02-28 (Sat), schwab's record")
    op.corporate_action("moomoo_my", "SPLIT", "2026-02-28", "SPE", "SPE", 4, 2,
                        note="the SAME 2-for-1, moomoo_my's record, unreduced 4:2")
    sell("SPES", "schwab", "SPE", "2026-03-16", 200, 20)
    sell("SPEN", "moomoo_my", "SPE", "2026-03-16", 100, 20)
    sell("SPDS", "schwab", "SPD", "2026-04-15", 200, 30)
    reconcile(ev, api, db_path, "month_end_unsplit", valuation=True)
    before = {"2026-05": P1.period_kpi_of(api, "M", "2026-05")} if P1.READ_PATH else {}
    if P1.READ_PATH:
        feb = P1.period_kpi_of(api, "M", "2026-02") or {}
        ev.check("month_end.anchor.missing_price", "2026-02 notes.missing_prices", ["MPX"],
                 sorted((feb.get("notes") or {}).get("missing_prices") or []), ph)
        ev.check("month_end.anchor.missing_price_withholds_xirr", "2026-02 xirr", None,
                 feb.get("xirr"), ph)
        apr = P1.period_kpi_of(api, "M", "2026-04") or {}
        recv = (apr.get("notes") or {}).get("dividends_receivable") or []
        ev.check("month_end.anchor.receivable", "2026-04 dividends_receivable",
                 ["tw_broker|2330|2026-04-28|2026-05-15"],
                 sorted(f"{r.get('account_id')}|{r.get('symbol')}|{r.get('ex_date')}|"
                        f"{r.get('pay_date')}" for r in recv), ph)
        for period, kpi, want in (("2026-04", apr, ["STL@2026-04-24"]),
                                  ("2026-05", before["2026-05"], [])):
            stale = sorted(f"{p.get('symbol')}@{p.get('price_date')}"
                           for p in ((kpi or {}).get("notes") or {}).get("stale_prices") or []
                           if p.get("symbol") == "STL")
            ev.check("month_end.anchor.stale_boundary", f"{period} STL", want, stale, ph)
        # Before SPA's and SPB's splits are entered: Friday 05-29's close, pre-split counts.
        # SPE (ON a Saturday D, recorded in two accounts — re-expressed ONCE) and SPD (ON a
        # Tuesday D, with D's own post-split close) are already split.
        for period, acct, sym, want in (
                ("2026-05", "tw_broker", "SPA", ["1000", "100.72", "2026-05-29", "100720"]),
                ("2026-05", "tw_broker", "SPB", ["500", "80.72", "2026-05-29", "40360"]),
                ("2026-02", "schwab", "SPE", ["200", "20.1325", "2026-02-27", "4026.5"]),
                ("2026-02", "moomoo_my", "SPE", ["100", "20.1325", "2026-02-27", "2013.25"]),
                ("2026-03", "schwab", "SPD", ["200", "30.0725", "2026-03-31", "6014.5"])):
            ev.check("month_end.anchor.split", f"{period} {acct}/{sym}", want,
                     _holding_literal(P1.period_holding_of(api, period, acct, sym)), ph)
    op.corporate_action("tw_broker", "SPLIT", "2026-06-01", "SPA", "SPA", 2, 1,
                        note="2-for-1 the first trading day after month end 2026-05-31")
    op.corporate_action("tw_broker", "SPLIT", "2026-05-31", "SPB", "SPB", 2, 1,
                        note="2-for-1 ON month end 2026-05-31, a Sunday")
    reconcile(ev, api, db_path, "month_end_splits", valuation=True)
    if P1.READ_PATH:
        after = P1.period_kpi_of(api, "M", "2026-05") or {}
        for field in ("total_market_value", "total_return", "total_return_rate", "xirr"):
            ev.check("month_end.split_leaves_close_unchanged", f"2026-05 {field}",
                     (before.get("2026-05") or {}).get(field), after.get(field), ph)
        # SPA: the split is after D — nothing about the position at D moved. SPB: ON D (a
        # Sunday) — twice the shares at half Friday's close.
        for period, acct, sym, want in (
                ("2026-05", "tw_broker", "SPA", ["1000", "100.72", "2026-05-29", "100720"]),
                ("2026-05", "tw_broker", "SPB", ["1000", "40.36", "2026-05-29", "40360"])):
            ev.check("month_end.anchor.split", f"{period} {acct}/{sym}", want,
                     _holding_literal(P1.period_holding_of(api, period, acct, sym)), ph)
    sell("SPAS", "tw_broker", "SPA", "2026-06-15", 2000, 50)
    sell("SPBS", "tw_broker", "SPB", "2026-06-15", 1000, 40)
    sell("STLS", "tw_broker", "STL", "2026-06-15", 300, 60)


def run_live_window(ev: C.Evidence, api: C.Api, db_path, op: Ops):
    """PERF spec §11.4: "a correction dated last month changes last month's row at once (a test
    pins it)". The month is the SERVER's last month (the live window is anchored on today), so
    the date moves with the clock; everything asserted is derived from it.

    A missed trade entered on the 15th of last month, then corrected (shares 10 -> 12): after
    each, WITHOUT a run, the status shows the change inside the live window (dirty from that
    day, nothing stale, no prompt) and last month's close and 月度成績 row already equal the
    oracle over the changed ledger (``phase1.check_live_window_row``). Then a checkpoint.
    """
    today = P1.server_today()
    P1.CHECKPOINT.today = today
    day = (today.replace(day=1) - timedelta(days=1)).replace(day=15)
    r = op.trade("schwab", "AAPL", "buy", day.isoformat(), 10, 205)
    txn = (r.get("json") or {}).get("txn_id") if isinstance(r.get("json"), dict) else None
    ev.check("live_window.entry_written", f"schwab AAPL 10 @205 on {day}", 201,
             r.get("status"), "phase1:live_window")
    P1.check_live_window_row(ev, api, db_path, "live_window_entry", day)
    if txn is None:
        return
    fee, tax = C.read_fee_tax_from_db(db_path, txn)
    r = op.edit_tx(txn, "schwab", "AAPL", "buy", day.isoformat(), 12, 205, fee, tax)
    ev.check("live_window.correction_written", f"id={txn} shares 10 -> 12", True,
             r.get("status") in (200, 201), "phase1:live_window")
    P1.check_live_window_row(ev, api, db_path, "live_window_correction", day)
    reconcile(ev, api, db_path, "live_window", valuation=True)


def run_missing_fx(ev: C.Evidence, api: C.Api, db_path, op: Ops):
    """Spec §11.2 "FX": a currency with NO rate on or before a month end. A Malaysian buy dated
    2025-12-15 (the fixture's first MYR/TWD row is 2026-01-01): the December 2025 close has
    no MYR/TWD rate, so 總市值, 總報酬 and the rate are blank and the pair is named in
    ``notes.missing_fx``; the flow itself has no rate either, so the whole trend is
    unavailable (and with it B, every trend figure and every period TWR) and XIRR has no
    series. Reconciled in that state, then removed — a temporary checkpoint, so the rest of the
    scenario keeps its trend."""
    ph = "phase1:missing_fx"
    r = op.trade("moomoo_my", "1155", "buy", "2025-12-15", 100, "9.00")
    j = r.get("json")
    txn = j.get("txn_id") if isinstance(j, dict) else None
    ev.check("missing_fx.row_written", "moomoo_my 1155 100 @9.00 on 2025-12-15", 201,
             r.get("status"), ph)
    reconcile(ev, api, db_path, "missing_fx", valuation=True)
    if P1.READ_PATH:
        dec25 = P1.period_kpi_of(api, "M", "2025-12") or {}
        notes = dec25.get("notes") or {}
        ev.check("missing_fx.anchor", "2025-12: 總市值 / 總報酬 / rate blank, MYR/TWD missing",
                 [None, None, None, ["MYR/TWD"], True],
                 [dec25.get("total_market_value"), dec25.get("total_return"),
                  dec25.get("total_return_rate"), sorted(notes.get("missing_fx") or []),
                  bool(notes.get("trend_unavailable"))], ph)
    if txn is not None:
        op.delete_tx(txn)


def run_options(ev: C.Evidence, api: C.Api, db_path, op: Ops):
    """US equity options (spec 2026-10-09, plan W7a) — every §4.1 row through the real doors.

    STO→BTC · STO→expire · BTO→STC (moomoo_my, CSV, statement fees — its rule set has no
    per-contract rate) · BTO→expire · a 1→3 ``OPTION_ADJUST`` on a SHORT (then a partial BTC
    on the new contract) · a 1:1 rename on a LONG · a sold-put assignment (two legs, one
    request) · a partial call EXERCISE (two legs) and the rest expiring · a same-day BTC +
    expiry · an open contract left past its expiry · a §4.4 close the replay must refuse
    (legacy data, then deleted through the door) · the cash tie per pool. Every §4.1 row.

    Every figure is reconciled by ``reconcile`` (section O and the KPI / XIRR / FX / trend
    families) against the oracle's OWN replay; on top of that, the scenario pins:

    * hand-computed LITERALS for each close's realized amount (an anchor, like corp.anchor.*:
      two replays agreeing cannot prove either landed on the statement's figure);
    * the §6.1 ``option_side`` of every option row — the ledger wire AND the cash statement
      lines — against the label this scenario knows it wrote (one table, two surfaces);
    * the cash tie per USD pool: the statement's option lines sum to the oracle's net
      premiums, line for line.

    Dates: every option event is in Jul–Sep 2026, before the run's clock (2026-10), so nothing
    here is a future-dated row. Expiries in the future (Nov / Dec 2026) are only ever OPEN.
    """
    ph = "phase1:options"
    labels: dict[int, str] = {}     # txn id -> the §6.1 option_side this scenario wrote

    PUT45 = "OPA260918P00045000"    # STO 2 -> BTC 2
    CALL55 = "OPA260821C00055000"   # STO 1 -> expires worthless
    CALL30 = "OPB260918C00030000"   # BTO 3 -> STC 3 (moomoo_my, CSV door, supplied fees)
    PUT25 = "OPB260821P00025000"    # BTO 2 -> expires worthless (a LONG expiry)
    PUT60 = "OPA261218P00060000"    # STO 1 -> OPTION_ADJUST 1->3 into PUT20
    PUT20 = "OPA261218P00020000"    #   60 x 1 == 20 x 3; later BTC 1 of the 3
    CALLC = "OPC261120C00010000"    # BTO 2 -> OPTION_ADJUST 1:1 rename into CALLD
    CALLD = "OPD261120C00010000"
    PUT40 = "OPA260918P00040000"    # STO 2 -> ASSIGNED (option close + buy 200 OPA @ 40)
    PUT20B = "OPB260821P00020000"   # STO 2 -> same day: BTC 1, then the expiry of the other 1
    CALL35 = "OPB260904C00035000"   # STO 1 -> never closed: past expiry at `final`
    CALL42 = "OPA260918C00042000"   # BTO 2 -> EXERCISE 1 (option close + buy 100 OPA @ 42),
    #                                  then the other 1 expires (a partial exercise, §4.2)
    PUTM = "OPB260918P00028000"     # BTO 1 -> STC 1 (moomoo_my, NO statement fee: the
    #                                  moomoo_us default rate, owner ruling 2026-10-10)

    def wrote(resp, label: str, scope: str) -> int | None:
        ev.check("options.door_accepts", scope, 201, resp.get("status"), ph)
        j = resp.get("json") if isinstance(resp, dict) else None
        t = j.get("txn_id") if isinstance(j, dict) else None
        if t:
            labels[int(t)] = label
        return t

    def tx_count() -> int:
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        finally:
            conn.close()

    # ---- the fee parameters the oracle transcribed (OPT-D6) are the app's EFFECTIVE ones ----
    eff = C.read_effective_fee_rules(api)
    ev.check("options.fee_param.option_per_contract", "schwab",
             O.FEE_RULES["schwab"]["option_per_contract"],
             (eff.get("schwab") or {}).get("option_per_contract"), ph)
    # Owner ruling 2026-10-10: one default per rule set — moomoo_us is 0.8633, not "unset".
    ev.check("options.fee_param.option_per_contract", "moomoo_us (default 0.8633)",
             O.FEE_RULES["moomoo_us"]["option_per_contract"],
             (eff.get("moomoo_us") or {}).get("option_per_contract"), ph)

    # No funding op on purpose: schwab's USD pool already holds ~17,000 (the assignment's leg
    # is 8,000) and moomoo_my's ~830 (the BTO is 631.50). A USD deposit would be an UNBASED
    # foreign acquisition and move schwab's covered_ratio (F2/F3) — a different feature.

    # ---- door probe: OPT-D6, owner ruling 2026-10-10 — every rule set has ONE default
    # per-contract rate, so a moomoo_us option trade with NO statement fee is ACCEPTED and
    # priced at it (the probe used to expect 400 `option_fee_unset`). A contract of its own,
    # bought and sold back, so it leaves no open position behind for the anchors below.
    # Literal fees: BTO 1 @ 0.50 -> 1 x 0.8633 = 0.86; STC 1 @ 0.65 -> 0.86 + SEC
    # max(0.0000206 x 65, 0.01) = 0.01 -> 0.87 (op.option_trade also checks both vs the oracle).
    bto_m = wrote(op.option_trade("moomoo_my", PUTM, "2026-07-22", "BTO", 1, "0.50"),
                  "買進開倉", "BTO PUTM (moomoo_us, no statement fee)")
    stc_m = wrote(op.option_trade("moomoo_my", PUTM, "2026-07-29", "STC", 1, "0.65"),
                  "賣出平倉", "STC PUTM (moomoo_us, no statement fee)")
    for tid, want in ((bto_m, "0.86"), (stc_m, "0.87")):
        got = C.read_fee_tax_from_db(db_path, tid)[0] if tid else None
        ev.check("options.default_fee.moomoo_us", f"PUTM id={tid} (literal)", want, got, ph)

    # ---- opens -------------------------------------------------------------------------
    wrote(op.option_trade("schwab", PUT45, "2026-07-20", "STO", 2, "1.50"), "賣出開倉",
          "STO PUT45")
    wrote(op.option_trade("schwab", CALL55, "2026-07-21", "STO", 1, "0.80"), "賣出開倉",
          "STO CALL55")
    # BTO on moomoo_my through the CSV door: shares in SHARE-EQUIVALENTS (3 口 = 300), the
    # statement fee supplied, tax blank (an option carries none).
    r = op.import_csv("transactions", "option_bto_moomoo",
                      ["account", "symbol", "side", "date", "shares", "price", "fee", "tax"],
                      [["moomoo_my", CALL30, "buy", "2026-07-22", "300", "2.10", "1.50", ""]])
    ev.check("options.csv_writes", "BTO CALL30 via CSV", 1, _written(r), ph)
    bto30 = op._latest_tx_id("moomoo_my", CALL30, "buy")
    if bto30:
        labels[int(bto30)] = "買進開倉"
        op._fee_check(bto30, "moomoo_my", "BUY", 300, "2.10", CALL30, "2026-07-22",
                      supplied_fee="1.50")
    wrote(op.option_trade("schwab", PUT25, "2026-07-23", "BTO", 2, "0.55"), "買進開倉",
          "BTO PUT25")
    wrote(op.option_trade("schwab", PUT60, "2026-07-24", "STO", 1, "3.00"), "賣出開倉",
          "STO PUT60")
    wrote(op.option_trade("schwab", CALLC, "2026-07-27", "BTO", 2, "1.25"), "買進開倉",
          "BTO CALLC")
    wrote(op.option_trade("schwab", PUT40, "2026-07-28", "STO", 2, "1.10"), "賣出開倉",
          "STO PUT40")
    wrote(op.option_trade("schwab", PUT20B, "2026-07-29", "STO", 2, "0.70"), "賣出開倉",
          "STO PUT20B")
    wrote(op.option_trade("schwab", CALL35, "2026-08-03", "STO", 1, "0.60"), "賣出開倉",
          "STO CALL35")
    wrote(op.option_trade("schwab", CALL42, "2026-07-30", "BTO", 2, "1.40"), "買進開倉",
          "BTO CALL42")

    # ---- §4.3 OPTION_ADJUST through the real corporate-action door ------------------------
    r = op.option_adjust("schwab", "2026-08-03", PUT60, PUT20, 3)
    ev.check("options.adjust_accepted", "1->3 on a SHORT (60 x 1 == 20 x 3)",
             201, r.get("status"), ph)
    r = op.option_adjust("schwab", "2026-08-10", CALLC, CALLD, 1)
    ev.check("options.adjust_accepted", "1:1 rename on a LONG", 201, r.get("status"), ph)

    # ---- door probe: OPT-D14 — 買進平倉 beyond the short is refused, nothing written ---------
    before = tx_count()
    r = op.option_trade("schwab", PUT45, "2026-08-14", "BTC", 3, "0.40", expect_status=400)
    ev.check("guard.option_btc_beyond_short_refused", "BTC 3 on a 2-contract short",
             400, r.get("status"), ph)
    ev.check("guard.option_btc_beyond_short_writes_nothing", "BTC 3 on 2", before,
             tx_count(), ph)

    res = P1.reconcile(ev, api, db_path, "options_open", valuation=True)
    # Literal anchors (hand-computed from the spec, not the oracle):
    # PUT60 STO 1 @ 3.00 -> 1->3 into PUT20: 3 contracts SHORT, valued at 3.00 / 3 = 1.00 and
    # dated the predecessor's trade (§5); the strike is conserved, so the obligation is
    # 20 x 100 x 3 = 6,000.
    _option_anchor(ev, api, "schwab", PUT20, "contracts", "3", "options_open")
    _option_anchor(ev, api, "schwab", PUT20, "direction", "short", "options_open")
    _option_anchor(ev, api, "schwab", PUT20, "last_price", "1", "options_open")
    _option_anchor(ev, api, "schwab", PUT20, "last_price_date", "2026-07-24", "options_open")
    _option_anchor(ev, api, "schwab", PUT20, "obligation", "6000", "options_open")
    # CALLC BTO 2 @ 1.25 -> 1:1 into CALLD: 2 contracts LONG at the predecessor's 1.25. (Not
    # 1.20: with it the three contracts still open at `final` — PUT20 −180, CALLD +240,
    # CALL35 −60 — net to exactly 0, and a block total of 0 agrees with too many mistakes.)
    _option_anchor(ev, api, "schwab", CALLD, "contracts", "2", "options_open")
    _option_anchor(ev, api, "schwab", CALLD, "last_price", "1.25", "options_open")
    _option_anchor(ev, api, "schwab", CALLD, "obligation", None, "options_open")
    # The two adjusted-away contracts hold nothing.
    ev.check("options.adjust_source_flat", "PUT60 / CALLC after their adjustments", [],
             sorted(k[1] for k, h in res.holdings.items()
                    if k[1] in (PUT60, CALLC) and h.shares != O.ZERO), "phase1:options_open")
    ev.check("options.refusal_codes", "options_open", [],
             [c for c, _ in res.action_refusals], "phase1:options_open")

    # ---- closes ------------------------------------------------------------------------
    wrote(op.option_trade("schwab", PUT45, "2026-08-14", "BTC", 2, "0.40"), "買進平倉",
          "BTC PUT45")
    wrote(op.option_close("schwab", CALL55, "2026-08-21", "expired", 1), "到期作廢",
          "expire CALL55 (short)")
    wrote(op.option_close("schwab", PUT25, "2026-08-21", "expired", 2), "到期作廢",
          "expire PUT25 (long)")
    # Same day: BTC 1 first, then the expiry of the remaining 1 — write order is the intraday
    # order (DEF-012), so the expiry's quantity is |open| AFTER the BTC.
    wrote(op.option_trade("schwab", PUT20B, "2026-08-21", "BTC", 1, "0.05"), "買進平倉",
          "BTC PUT20B (same day as its expiry)")
    wrote(op.option_close("schwab", PUT20B, "2026-08-21", "expired", 1), "到期作廢",
          "expire PUT20B after the same-day BTC")
    r = op.import_csv("transactions", "option_stc_moomoo",
                      ["account", "symbol", "side", "date", "shares", "price", "fee", "tax"],
                      [["moomoo_my", CALL30, "sell", "2026-09-01", "300", "3.05", "1.60", ""]])
    ev.check("options.csv_writes", "STC CALL30 via CSV", 1, _written(r), ph)
    stc30 = op._latest_tx_id("moomoo_my", CALL30, "sell")
    if stc30:
        labels[int(stc30)] = "賣出平倉"
        op._fee_check(stc30, "moomoo_my", "SELL", 300, "3.05", CALL30, "2026-09-01",
                      supplied_fee="1.60")
    wrote(op.option_trade("schwab", PUT20, "2026-09-02", "BTC", 1, "0.90"), "買進平倉",
          "BTC 1 of the 3 adjusted PUT20")
    # A PARTIAL exercise (0 < quantity <= |open|): the option SELL at 0 + the stock BUY of the
    # underlying at the strike, then the remaining contract expires on its expiry date.
    r = op.option_close("schwab", CALL42, "2026-09-11", "exercised", 1)
    wrote(r, "履約", "exercised 1 of 2 CALL42 (two legs)")
    j = r.get("json") if isinstance(r.get("json"), dict) else {}
    ev.check("options.exercise_two_legs", "txn_ids = [option, stock]", 2,
             len(j.get("txn_ids") or []), ph)
    wrote(op.option_close("schwab", CALL42, "2026-09-18", "expired", 1), "到期作廢",
          "expire the other CALL42 (long)")
    r = op.option_close("schwab", PUT40, "2026-09-18", "assigned", 2)
    wrote(r, "被指派", "assigned PUT40 (two legs)")
    j = r.get("json") if isinstance(r.get("json"), dict) else {}
    ev.check("options.assignment_two_legs", "txn_ids = [option, stock]", 2,
             len(j.get("txn_ids") or []), ph)

    res = P1.reconcile(ev, api, db_path, "options_closed", valuation=True)
    # Realized LITERALS (spec §4.1; schwab 0.65/contract + SEC on a sell, min 0.01):
    #   PUT45  STO 2 @ 1.50: 300 − 1.31 = 298.69 in;  BTC 2 @ 0.40: 80 + 1.30 = 81.30 out
    #   CALL55 STO 1 @ 0.80: 80 − 0.66 = 79.34 — the whole premium on the expiry
    #   PUT25  BTO 2 @ 0.55: 110 + 1.30 = 111.30 paid — the whole basis lost on the expiry
    #   PUT20B STO 2 @ 0.70: 140 − 1.31 = 138.69; BTC 1 @ 0.05 (5.65) -> 69.345 − 5.65; the
    #          expiry of the other 100 realizes the remaining 69.345
    #   CALL30 (moomoo, statement fees) 915 − 1.60 − (630 + 1.50)
    #   PUT20  1 of 3 @ 0.90: 299.34 × 100/300 = 99.78 − 90.65
    #   PUT40  STO 2 @ 1.10: 220 − 1.31 = 218.69 — the premium stays option P&L (R5)
    #   CALL42 BTO 2 @ 1.40: 280 + 1.30 = 281.30 paid; each contract's half (140.65) is lost —
    #          on the exercise (R5: the premium paid is the option's realized loss, never folded
    #          into the stock) and on the other contract's expiry
    for acct, sym, d, reason, want in (
            ("schwab", CALL42, "2026-09-11", "exercised", "-140.65"),
            ("schwab", CALL42, "2026-09-18", "expired", "-140.65"),
            ("schwab", PUT45, "2026-08-14", None, "217.39"),
            ("schwab", CALL55, "2026-08-21", "expired", "79.34"),
            ("schwab", PUT25, "2026-08-21", "expired", "-111.30"),
            ("schwab", PUT20B, "2026-08-21", None, "63.695"),
            ("schwab", PUT20B, "2026-08-21", "expired", "69.345"),
            ("moomoo_my", CALL30, "2026-09-01", None, "281.90"),
            ("schwab", PUT20, "2026-09-02", None, "9.13"),
            ("schwab", PUT40, "2026-09-18", "assigned", "218.69")):
        _option_realized_anchor(ev, api, acct, sym, d, reason, want)
    # The two stock legs: 100 OPA at the 42 strike (exercise) + 200 OPA at the 40 strike
    # (assignment), schwab's $0 commission — an ordinary stock holding of 300 at 4,200 + 8,000;
    # neither premium is folded into its cost (R5).
    P1.anchor(ev, api, "options.anchor.stock_legs_shares", "schwab", "OPA", "shares",
              "300", phase="phase1:options_closed")
    P1.anchor(ev, api, "options.anchor.stock_legs_cost", "schwab", "OPA",
              "original_cost_total", "12200", phase="phase1:options_closed")
    _option_side_anchors(ev, api, labels, "phase1:options_closed")
    _option_cash_tie(ev, api, db_path, labels, "phase1:options_closed")

    # ---- §4.4: a close the replay must REFUSE — legacy data --------------------------------
    # Every door refuses it (§4.2: an `expired` row must equal |open|, and CALL30 is flat), so
    # it can only exist below the validators. A BUY close on a flat contract would OPEN a
    # long lot: the replay books nothing, records it, flags the (flat) position, and
    # withholds XIRR and B. Then the owner's remedy — delete it through the door.
    rid = C.write_legacy_transaction(db_path, "moomoo_my", CALL30, "BUY", "2026-09-18",
                                     C.dec("100"), C.dec("0"), close_reason="expired")
    labels[int(rid)] = "未入帳（待釐清）"
    ev.op("phase1", "DB", "legacy.option_close_without_position",
          {"account_id": "moomoo_my", "symbol": CALL30, "side": "BUY", "date": "2026-09-18",
           "quantity": "100", "close_reason": "expired"},
          {"status": 201, "json": {"txn_id": rid}},
          note="legacy-data scenario: no door accepts this row (spec §4.2)")
    ev.check("options.refused_close_dashboard_200", "a stored refused close must not 500",
             "200", str(api.get("/api/dashboard").status_code), "phase1:options_refused_close")
    res = P1.reconcile(ev, api, db_path, "options_refused_close", valuation=True)
    ev.check("options.oracle_refuses_the_close", "options_refused_close", [rid],
             [rc.txn_id for rc in res.refused_closes], "phase1:options_refused_close")
    _option_side_anchors(ev, api, labels, "phase1:options_refused_close")
    _option_cash_tie(ev, api, db_path, labels, "phase1:options_refused_close")
    rd = api.delete(f"/api/ledgers/transactions/{rid}")
    ev.op("phase1", "API", "delete.transaction", {"id": rid},
          {"status": rd.status_code, "json": _json(rd)}, note="remove the refused close")
    ev.check("options.refused_close_removable", "delete the legacy refused close",
             "200", str(rd.status_code), "phase1:options_refused_close")
    labels.pop(int(rid), None)


def _option_anchor(ev, api, account_id, symbol, field, expected, label):
    """One `options.open[]` field against a hand-computed LITERAL (see P1.anchor)."""
    block = api.get("/api/dashboard").json().get("options") or {}
    row = next((r for r in block.get("open") or []
                if r.get("account_id") == account_id and r.get("symbol") == symbol), None)
    ev.check(f"options.anchor.open.{field}", f"{account_id}/{symbol}", expected,
             None if row is None else row.get(field), f"phase1:{label}")


def _option_realized_anchor(ev, api, account_id, symbol, sell_date, reason, expected):
    """One option realized row (matched by account, contract, date and close reason) against
    a hand-computed LITERAL."""
    block = api.get("/api/dashboard").json().get("options") or {}
    rows = [r for r in (block.get("realized") or {}).get("rows") or []
            if r.get("account_id") == account_id and r.get("symbol") == symbol
            and r.get("sell_date") == sell_date and r.get("close_reason") == reason]
    ev.check("options.anchor.realized", f"{account_id}/{symbol}@{sell_date} {reason}",
             expected, rows[0].get("realized") if len(rows) == 1 else f"{len(rows)} rows",
             "phase1:options_closed")


def _option_side_anchors(ev, api, labels: dict[int, str], phase: str) -> None:
    """Spec §6.1: every option row's `option_side` on the ledger wire is the label this
    scenario knows it wrote — one assertion per row."""
    rows = {r["id"]: r for r in api.get("/api/ledgers/transactions", limit=500).json()["rows"]}
    for tid, want in sorted(labels.items()):
        ev.check("options.option_side.ledger", f"id={tid}", want,
                 (rows.get(tid) or {}).get("option_side"), phase)


def _option_cash_tie(ev, api, db_path, labels: dict[int, str], phase: str) -> None:
    """The cash tie per pool (plan W7a): the statement's OPTION lines of each USD pool are
    the oracle's net premiums, line for line, with the same §6.1 label the ledger wire has.

    Expected lines come from the stored rows (each option trade's settled 價金 ± fee ± tax,
    the oracle's own `settled`) and the scenario's label table; actual lines are the
    statement rows carrying an `option_side`. Lines that move no money (a price-0 close) are
    compared through the ledger wire instead: whether a statement LISTS a zero line is not
    a money fact.
    """
    facts = C.load_facts_from_db(db_path)
    for acct in ("schwab", "moomoo_my"):
        want: list = []
        net = O.ZERO
        for t in facts.txs:
            if t.account_id != acct or not O.is_option(t.symbol):
                continue
            gross = O.settled(t.qty, t.price, "USD")
            delta = -(gross + t.fee + t.tax) if t.side == "BUY" else gross - t.fee - t.tax
            net += delta
            if delta != O.ZERO:
                want.append((t.trade_date.isoformat(), t.symbol, labels.get(t.id), delta))
        st = api.get("/api/cash/statement", account=acct, ccy="USD", limit=500).json()
        got_rows = [r for r in st.get("rows") or [] if r.get("option_side") is not None]
        got = [(r["date"], r.get("symbol"), r.get("option_side"), C.dec(r["delta"]))
               for r in got_rows if C.dec(r["delta"]) != O.ZERO]
        ev.check("options.cash_tie.net", f"{acct}|USD option lines", net,
                 sum((C.dec(r["delta"]) for r in got_rows), O.ZERO), phase)
        ev.check("options.cash_tie.lines", f"{acct}|USD option lines", P1._cv(sorted(want)),
                 P1._cv(sorted(got)), phase)


def _written(resp) -> int:
    """How many rows a CSV commit wrote (0 when the door refused every row)."""
    body = resp.get("json") if isinstance(resp, dict) else None
    return int((body or {}).get("written") or 0) if isinstance(body, dict) else 0


def run_refused_dividends(ev: C.Evidence, api: C.Api, db_path, op: Ops, buy, sell):
    """The two dividend-refusal classes the declared-short block above does not reach (YQ3).

    ``domain-ledger.md`` defines three dividends the replay refuses — booked nowhere, no
    realized income, no cash (⑪), no flow (K2), the days marked incomplete (K3), a held
    carrier still valued (Y3):

    * an OPEN declared short (2609 above — removed before its cover, so it is the HELD case);
    * a ticker an EXCHANGE vacated (E24) — here, through the real doors: the dividend door
      accepts the row because VCO WAS held before its exchange, and the replay refuses it.
      A CASH row (no income on a dead ticker) and a 配股 (no zero-cost resurrection). It is
      reconciled twice: with the successor VCN HELD (Y3: its value stays in the trend and net
      worth) and after VCN is sold FLAT (K3: the flag has no carrier, the record stands). It
      is NOT resolved, so the ``final`` checkpoint carries it too — XIRR computes without it.
    * no position at all (CR-02) — a LEGACY-DATA row, see below.

    Placed after every other block so each existing expectation stays a control, and with
    dedicated symbols (VCO / VCN / ORP) so nothing else can satisfy or disturb them.
    """
    op.cash_move("tw_broker", "deposit", "TWD", "2026-01-05", 100000)

    # ---- E24 through the doors -------------------------------------------------------------
    buy("VCOB", "tw_broker", "VCO", "2026-02-09", 1000, 50)
    op.corporate_action("tw_broker", "EXCHANGE", "2026-03-09", "VCO", "VCN", 1, 1,
                        note="E24 setup: VCO's whole position moves to VCN")
    r = op.dividend("tw_broker", "VCO", "2026-04-09", "CASH", 3000)
    ev.check("refused.vacated_cash_door_accepts", "tw_broker/VCO CASH 2026-04-09",
             1, _written(r), "phase1:refused")
    r = op.dividend("tw_broker", "VCO", "2026-04-16", "STOCK", 0, reinvest_shares=50)
    ev.check("refused.vacated_stock_door_accepts", "tw_broker/VCO STOCK 2026-04-16",
             1, _written(r), "phase1:refused")

    res = reconcile(ev, api, db_path, "refused_vacated_held", valuation=True)
    # Scenario knowledge, asserted so a silent oracle cannot pass: both rows ARE refused, and
    # neither is an orphan (VCO was held before its exchange).
    ev.check("refused.oracle_refuses_both_vacated_rows", "refused_vacated_held",
             [("tw_broker", "VCO", "2026-04-09"), ("tw_broker", "VCO", "2026-04-16")],
             [(a, s, d.isoformat()) for a, s, d in res.refused_dividends],
             "phase1:refused_vacated_held")
    ev.check("refused.vacated_rows_are_not_orphans", "refused_vacated_held", [],
             res.orphan_dividends, "phase1:refused_vacated_held")
    # ABSOLUTE anchors (literals, not the oracle): the live successor holds the 1,000 shares
    # and carries the flag; the old ticker was NOT resurrected by the 配股 (no VCO row).
    P1.anchor(ev, api, "refused.anchor.successor_shares", "tw_broker", "VCN", "shares", "1000",
              phase="phase1:refused_vacated_held")
    P1.anchor(ev, api, "refused.anchor.successor_flagged", "tw_broker", "VCN",
              "unbookable_dividend", True, phase="phase1:refused_vacated_held")
    P1.anchor(ev, api, "refused.anchor.no_resurrection", "tw_broker", "VCO", "shares", None,
              phase="phase1:refused_vacated_held")

    # ---- K3: the successor sold out — the flag has no carrier, the refusal stands ------------
    sell("VCNS", "tw_broker", "VCN", "2026-05-11", 1000, 55)
    reconcile(ev, api, db_path, "refused_vacated_flat", valuation=True)

    # ---- CR-02: a dividend with no position — LEGACY DATA ----------------------------------
    # It cannot come through a door: since CR-02 every dividend door refuses a row whose
    # account had opened no position in the symbol by its date (`dividend_without_position`).
    # Proved first, then the same row is written below the validators, as a pre-CR-02 import
    # or a hand-edited database would hold it — the replay must refuse it without a 500.
    r = op.dividend("tw_broker", "ORP", "2026-03-23", "CASH", 900)
    ev.check("refused.orphan_door_refuses", "tw_broker/ORP CASH 2026-03-23 (never held)",
             0, _written(r), "phase1:refused")
    did = C.write_legacy_dividend(db_path, "tw_broker", "ORP", "2026-03-23", "CASH",
                                  C.dec("900"))
    ev.op("phase1", "DB", "legacy.dividend_without_position",
          {"account_id": "tw_broker", "symbol": "ORP", "date": "2026-03-23", "net": "900"},
          {"status": 201, "json": {"dividend_id": did}},
          note="legacy-data scenario: no door accepts this row since CR-02")
    ev.check("refused.legacy_orphan_dashboard_200", "a stored orphan must not 500",
             "200", str(api.get("/api/dashboard").status_code), "phase1:refused")
    res = reconcile(ev, api, db_path, "refused_orphan", valuation=True)
    ev.check("refused.oracle_names_the_orphan", "refused_orphan",
             [("tw_broker", "ORP", "2026-03-23")],
             [(a, s, d.isoformat()) for a, s, d in res.orphan_dividends],
             "phase1:refused_orphan")

    # ---- the owner's remedy for the orphan: delete it, through the door ---------------------
    # (The vacated rows stay: the `final` checkpoint reconciles XIRR, cash and the trend with
    # a refused payout and a flat carrier in the ledger.)
    rd = api.delete(f"/api/ledgers/dividends/{did}")
    ev.op("phase1", "API", "delete.dividend", {"id": did},
          {"status": rd.status_code, "json": _json(rd)}, note="remove the legacy orphan row")
    ev.check("refused.orphan_removable", "delete the legacy orphan dividend",
             "200", str(rd.status_code), "phase1:refused")


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
    ap.add_argument("--wave2-only", action="store_true",
                    help="PERF plan: skip every check that needs package G (the store read "
                         "path through the dashboard, /api/settlement/periods*, the period "
                         "rows of /api/snapshots). ONLY for a run before the wave-3 merge; "
                         "the gate (W4 and later) runs without it.")
    args = ap.parse_args()
    P1.READ_PATH = not args.wave2_only
    P1.reset_settlement()

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
    aborted: str | None = None
    try:
        P1.seed_all(db_path)
        if args.ui:
            import ui as UI
            ui = UI.UiDriver(base)
            ui.start()
        try:
            run_scenario(ev, api, db_path, ui=ui)
        except P1.SettleAbort as exc:
            # One clear failure is already recorded (settle.run_finished_in_time); nothing is
            # reconciled against an unsettled store.
            aborted = str(exc)
        if ui is not None and aborted is None:
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
    if aborted is not None:
        print(f"ABORTED: {aborted}")
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
    # A gate that reads the exit code, never the ``fail=`` line, must see a failing run as
    # failed (fold verification I, 2026-10-20: every mutation run exited 0).
    if aborted is not None or ev.n_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
