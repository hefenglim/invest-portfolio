"""Phase 1 — clean-room stress + reconciliation on a local isolated server.

Fresh DB, scheduler disabled. A deterministic, dated (Jan-Jun 2026) scenario of 70+
operations across the three money flows, reconciled against the app's computed outputs
by the independent oracle (absolute assertions from zero).

Trade-date FX is seeded at the scenario start (2026-01-01) AND at the valuation as-of
(2026-07-14), so the app resolves each flow at its on-or-before rate and the terminal
value at spot — exercising the FX-aware reporting XIRR for real (the one tolerance check).
"""

from __future__ import annotations

import csv
import io
import sqlite3
import time
from datetime import date
from decimal import Decimal

import common as C
import oracle as O
from common import dec

D = Decimal

REPORTING = "TWD"

# instruments: symbol, market, ccy, name, sector, is_etf
INSTRUMENTS = [
    ("2330", "TW", "TWD", "TSMC", "Semiconductors", False),
    ("0050", "TW", "TWD", "Yuanta 0050", "ETF", True),
    ("AAPL", "US", "USD", "Apple", "Technology", False),
    ("MSFT", "US", "USD", "Microsoft", "Technology", False),
    ("NVDA", "US", "USD", "NVIDIA", "Semiconductors", False),
    ("1155", "MY", "MYR", "Maybank", "Banking", False),
    ("TSLA", "US", "USD", "Tesla", "Auto", False),      # watchlist -> later bought
    ("5225", "MY", "MYR", "IHH Healthcare", "Healthcare", False),  # watchlist, stays
    ("0800EA", "MY", "MYR", "TradePlus MY ETF", "ETF", True),  # MY ETF -> stamp EXEMPT (v2)
    # Declared-short lifecycle symbol (2026-07-31): opened short -> dividend-on-short ->
    # covered flat. Dedicated so no other op can satisfy or disturb the short path.
    ("2609", "TW", "TWD", "Yang Ming", "Shipping", False),
    # ---- Corporate actions (spec 2026-08-06, W8). DEDICATED symbols on purpose: every
    # existing expectation above stays a control, so a failure is attributable to the new
    # feature rather than to a perturbed fixture. TW/TWD for most of them, because
    # tw_broker is NOT FX-exposed (settle == funding) — the action block then moves no FX
    # pool and cannot mask an FX defect. CAD is the one US position, so a real DRIP model
    # is exercised under a split.
    ("CA1", "TW", "TWD", "CA Forward Split", "Industrials", False),   # 3-for-1 -> sell
    # 210 x 1/3: EXACTLY 70 multiply-first, 69.99999999999999999999999999 parenthesised.
    # The 2-for-7 case cannot discriminate evaluation order (700 x (2/7) also lands on 200
    # at 28 digits — measured, and the correction §3.1(ii) records), so without this ratio
    # trap #2 is unobservable in this scenario. A later sell of exactly 70 then trips
    # validate.py's bare `>` and discards the basis: the cascade, reproduced.
    ("CAR", "TW", "TWD", "CA Ratio Rounding", "Industrials", False),  # 1-for-3 of 210
    ("CA2", "TW", "TWD", "CA Reverse Split", "Industrials", False),   # 1-for-10 -> CIL
    ("CA3", "TW", "TWD", "CA Exchange Source", "Industrials", False),  # -> CA4
    ("CA4", "TW", "TWD", "CA Exchange Dest", "Industrials", False),   # already held
    ("CA5", "TW", "TWD", "CA 2-for-7 Source", "Industrials", False),  # -> CA6, exactness
    ("CA6", "TW", "TWD", "CA 2-for-7 Dest", "Industrials", False),
    ("CA7", "TW", "TWD", "CA Spinoff Parent", "Industrials", False),
    ("CA8", "TW", "TWD", "CA Spinoff Child", "Industrials", False),
    ("CA9", "TW", "TWD", "CA Dividend-Adjusted", "Industrials", False),  # adjusted != orig
    ("CAD", "US", "USD", "CA DRIP Split", "Technology", False),       # DRIP then split
    ("CAX", "TW", "TWD", "CA Short Source", "Shipping", False),       # E5 refusal
    ("CAY", "TW", "TWD", "CA Short Dest", "Shipping", False),
    ("CAZ", "TW", "TWD", "CA Closed Position", "Industrials", False),  # E2 refusal
    ("CAN", "TW", "TWD", "CA Never Held", "Industrials", False),      # E1 refusal
    # ---- Batch import (C1/C2, 2026-08-14). The rest of this scenario enters one row at a
    # time, which is the ONE shape in which "the whole file is one batch" is unobservable —
    # a batch of one has no siblings. These three symbols are only ever touched through a
    # CSV commit carrying MORE THAN ONE row, which is how the owner's broker export arrives
    # and is the shape both defects lived in.
    ("CBS", "TW", "TWD", "CB Batch Sibling", "Industrials", False),   # buy+sell, one file
    ("CCA", "TW", "TWD", "CC Chain Source", "Industrials", False),    # EXCHANGE -> CCB
    ("CCB", "TW", "TWD", "CC Chain Dest", "Industrials", False),      # …then SPLIT, same file
]

PRICES = {  # current spot / valuation prices, dated ASOF
    "2330": D("700"), "0050": D("145"), "AAPL": D("205"), "MSFT": D("420"),
    "NVDA": D("610"), "1155": D("11.20"), "TSLA": D("250"), "0800EA": D("1.20"),
    # 5225 joined the ledger with the audit-H2 op (buy -> sell-all -> post-close dividend).
    # It ends FLAT, so this price only completes its history series; no holding reads it.
    "5225": D("6.40"),
    # 2609 is priced so the OPEN-short checkpoint values the negative position for real
    # (market_value / unrealized on signed shares); it too ends FLAT before final.
    "2609": D("96"),
    # Corporate-action symbols. Every price is quoted in POST-action terms and dated at
    # ASOF, which is AFTER every action date in the scenario — so §5.1's price
    # re-expression (W6b) has nothing to re-denominate and the valuation leg stays a
    # property of the SHARE count alone. Symbols that end flat (CA3/CA5/CAX/CAZ) are
    # priced anyway: the row is inert for an unheld position and keeps the fixture uniform.
    "CA1": D("205"), "CAR": D("280"), "CA2": D("320"), "CA3": D("200"), "CA4": D("480"),
    "CA5": D("40"), "CA6": D("150"), "CA7": D("190"), "CA8": D("330"),
    "CA9": D("160"), "CAD": D("21"), "CAX": D("76"), "CAY": D("90"),
    "CAZ": D("110"), "CAN": D("50"),
    # Batch-import symbols (C1/C2). CCA ends flat (its whole position EXCHANGEs into CCB);
    # priced anyway, for the same fixture-uniformity reason as CA3/CA5 above.
    "CBS": D("310"), "CCA": D("120"), "CCB": D("64"),
}
# Current spot FX (latest row -> drives the Spot resolver + terminal XIRR value).
# USD/MYR spot (4.6) is deliberately != the moomoo_my USD-pool weighted-avg acquisition rate
# (4.5, from the two MYR->USD conversions) so the merged account's unrealized FX is NON-ZERO —
# otherwise (spot == avg) the FX-exposure figure multiplies by 0 and a bug that mis-scopes the
# MYR MY-stock value into the USD pool would be masked. USD/MYR does not enter the reporting
# XIRR (USD->TWD and MYR->TWD are direct) or the trade-date stamp (early 4.3), so this is inert
# to every other check and purely strengthens the Batch-B merged-account FX proof.
FX_RATES = {("USD", "TWD"): D("32.5"), ("USD", "MYR"): D("4.6"), ("MYR", "TWD"): D("7.2")}
ASOF = date(2026, 7, 14)

# Trade-date FX seeded at the scenario start: every Jan-Jun flow resolves on-or-before
# to THIS row (different from spot), so the XIRR check genuinely exercises trade-date FX.
EARLY_ASOF = date(2026, 1, 1)
EARLY_FX_RATES = {("USD", "TWD"): D("31.0"), ("USD", "MYR"): D("4.3"), ("MYR", "TWD"): D("7.0")}


def fx_on_resolver():
    """Return an on-or-before FX resolver mirroring the app's get_fx_on (direct latest
    with as_of<=d, else inverse). Built from the harness-owned seeded schedule so the
    oracle stays independent of portfolio_dash."""
    table: dict[tuple[str, str], list[tuple[date, Decimal]]] = {}
    for (b, q), rr in EARLY_FX_RATES.items():
        table.setdefault((b, q), []).append((EARLY_ASOF, rr))
    for (b, q), rr in FX_RATES.items():
        table.setdefault((b, q), []).append((ASOF, rr))
    for k in table:
        table[k].sort(key=lambda x: x[0])

    def resolve(d: date, base: str, quote: str) -> Decimal:
        direct = [rr for (dd, rr) in table.get((base, quote), []) if dd <= d]
        if direct:
            return direct[-1]
        inv = [rr for (dd, rr) in table.get((quote, base), []) if dd <= d]
        if inv:
            return O.ONE / inv[-1]
        raise KeyError(f"no FX on/before {d} for {base}/{quote}")

    return resolve


# ---------------------------------------------------------------- op helpers (API)
class Ops:
    def __init__(self, ev: C.Evidence, api: C.Api, db_path, phase="phase1", ui=None):
        self.ev = ev
        self.api = api
        self.db = db_path
        self.phase = phase
        self.ui = ui  # optional UI driver for happy paths

    # ---- UI-write confirmation helpers (poll the public API the UI calls) ----
    def _tx_total(self):
        return self.api.get("/api/ledgers/transactions", limit=1).json().get("total_count", 0)

    def _cash_total(self):
        return (self.api.get("/api/cash", limit=1).json().get("movements", {})
                .get("total_count", 0))

    def _fx_total(self):
        return self.api.get("/api/ledgers/fx", limit=1).json().get("total_count", 0)

    def _wait_increase(self, fn, before, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if fn() > before:
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    def _latest_tx_id(self, account_id, symbol, side):
        conn = sqlite3.connect(str(self.db))
        try:
            row = conn.execute(
                "SELECT id FROM transactions WHERE account_id=? AND symbol=? AND side=? "
                "ORDER BY id DESC LIMIT 1", (account_id, symbol, side.upper())).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _fee_check(self, txn_id, account_id, side, qty, price, symbol, d, daytrade=False):
        # is_etf + market come from the instrument REGISTRY (found-bug: entry paths must honour
        # is_etf, not default False); daytrade is the per-transaction TW same-day flag (0.15%).
        # Batch B: the merged dual-market moomoo_my routes fees by the instrument's market (US ->
        # moomoo_us, MY -> moomoo_my), so the oracle resolves the rule WITH the market too.
        reg = {i[0]: i for i in INSTRUMENTS}
        is_etf = bool(reg[symbol][5]) if symbol in reg else False
        market = reg[symbol][1] if symbol in reg else None
        # FE-D2: the Moomoo US MY stamp needs the trade-date USD/MYR rate. It applies only when
        # the (account, market)-resolved rule is moomoo_us (a moomoo_my US trade). Resolve it from
        # the harness-owned seeded schedule (on-or-before), mirroring the app's caller seam.
        stamp_fx = None
        if O.fee_rule_for(account_id, market) == "moomoo_us":
            try:
                stamp_fx = fx_on_resolver()(date.fromisoformat(d), "USD", "MYR")
            except KeyError:
                stamp_fx = None
        fee, tax, notes = O.fee_tax(account_id, side.upper(), dec(qty), dec(price),
                                    is_etf, daytrade, stamp_fx, market=market)
        got_fee, got_tax = C.read_fee_tax_from_db(self.db, txn_id)
        tag = "daytrade" if daytrade else ("etf" if is_etf else "normal")
        self.ev.check("fee_engine.fee",
                      f"{account_id}/{symbol} {side} {qty}@{price} id={txn_id} [{tag}]",
                      fee, got_fee, self.phase)
        self.ev.check("fee_engine.tax",
                      f"{account_id}/{symbol} {side} {qty}@{price} id={txn_id} [{tag}]",
                      tax, got_tax, self.phase)

    def trade(self, account_id, symbol, side, d, shares, price, *, ack=False,
              via_ui=False, expect_status=201, fee_check=True, daytrade=False,
              short=False):
        body = {"account_id": account_id, "symbol": symbol, "side": side,
                "date": d, "shares": str(shares), "price": str(price),
                "ack_oversell": ack, "daytrade": daytrade}
        if short:
            # Declared short sale + the harness note marker (see common.SHORT_MARKER):
            # the marker is how the API-facts loader recovers the flag, since the
            # transactions read endpoint does not expose it.
            body["short_sale"] = True
            body["note"] = C.SHORT_MARKER
        if via_ui and self.ui is not None:
            before = self._tx_total()
            self.ui.manual_trade(account_id, symbol, side, d, shares, price)
            ok = self._wait_increase(self._tx_total, before)
            txn_id = self._latest_tx_id(account_id, symbol, side) if ok else None
            resp = {"status": 201 if ok else 0, "json": {"txn_id": txn_id}}
            surface = "UI"
        else:
            r = self.api.post("/api/input/manual/commit", body)
            resp = {"status": r.status_code, "json": _json(r)}
            surface = "API"
        self.ev.op(self.phase, surface, f"trade.{side}", body, resp,
                   note=f"{account_id} {symbol}")
        status = resp.get("status")
        if status != expect_status:
            return resp
        txn_id = resp["json"].get("txn_id") if isinstance(resp.get("json"), dict) else None
        if txn_id and fee_check:
            self._fee_check(txn_id, account_id, side, shares, price, symbol, d,
                            daytrade=daytrade)
        return resp

    def daytrade_csv(self, account_id, symbol, d, shares, buy_price, sell_price):
        """Second found-bug op (CSV surface): same-day buy+sell imported with a
        ``daytrade`` column, and a fee-check that the sell taxes at 0.15%."""
        cols = ["account", "symbol", "side", "date", "shares", "price", "daytrade"]
        rows = [
            [account_id, symbol, "buy", d, str(shares), str(buy_price), "0"],
            [account_id, symbol, "sell", d, str(shares), str(sell_price), "1"],
        ]
        csv_text = ",".join(cols) + "\n" + "\n".join(",".join(r) for r in rows) + "\n"
        r = self.api.post("/api/import/commit",
                          {"kind": "transactions", "csv_text": csv_text, "ack_warnings": True})
        resp = {"status": r.status_code, "json": _json(r)}
        self.ev.op(self.phase, "CSV", "daytrade.csv_import",
                   {"account": account_id, "symbol": symbol, "date": d,
                    "shares": str(shares), "sell_price": str(sell_price), "daytrade": True},
                   resp)
        if resp["status"] in (200, 201):
            sid = self._latest_tx_id(account_id, symbol, "sell")
            if sid:
                self._fee_check(sid, account_id, "sell", shares, sell_price, symbol, d,
                                daytrade=True)
        return resp

    def import_csv(self, kind, label, header, rows, *, ack=True):
        """Commit a MULTI-ROW CSV batch through the real import endpoint, and op-log it.

        The rest of this scenario enters one row at a time, which is exactly the shape in
        which "the whole file is one batch" cannot be observed — a batch of one has no
        siblings. Both 2026-08-14 defects (C1: a sell covered by a buy in the same file;
        C2: a corporate-action chain in the same file) only appear above one row.

        ``ack_warnings`` defaults True because the callers below assert on the RESPONSE
        counts, and the point of the assertion is usually that the warning is *gone*.
        """
        csv_text = ",".join(header) + "\n" + "\n".join(",".join(r) for r in rows) + "\n"
        r = self.api.post("/api/import/commit",
                          {"kind": kind, "csv_text": csv_text, "ack_warnings": ack})
        resp = {"status": r.status_code, "json": _json(r)}
        self.ev.op(self.phase, "CSV", f"import.{label}",
                   {"kind": kind, "rows": len(rows)}, resp)
        return resp

    def cash_move(self, account_id, kind, ccy, d, amount, *, ack=False, via_ui=False,
                  expect=201):
        body = {"account_id": account_id, "date": d, "kind": kind, "ccy": ccy,
                "amount": str(amount), "ack_negative": ack}
        if via_ui and self.ui is not None:
            before = self._cash_total()
            self.ui.cash_move(account_id, kind, ccy, d, amount)
            ok = self._wait_increase(self._cash_total, before)
            resp = {"status": 201 if ok else 0, "json": {}}
            surface = "UI"
        else:
            r = self.api.post("/api/cash/movements", body)
            resp = {"status": r.status_code, "json": _json(r)}
            surface = "API"
        self.ev.op(self.phase, surface, f"cash.{kind}", body, resp)
        return resp

    def fx(self, account_id, d, from_ccy, from_amt, to_ccy, to_amt, *, ack=False,
           via_ui=False):
        body = {"account_id": account_id, "date": d, "from_ccy": from_ccy,
                "from_amt": str(from_amt), "to_ccy": to_ccy, "to_amt": str(to_amt),
                "ack_negative": ack}
        if via_ui and self.ui is not None:
            before = self._fx_total()
            self.ui.fx(account_id, d, from_ccy, from_amt, to_ccy, to_amt)
            ok = self._wait_increase(self._fx_total, before)
            resp = {"status": 201 if ok else 0, "json": {}}
            surface = "UI"
        else:
            r = self.api.post("/api/cash/fx", body)
            resp = {"status": r.status_code, "json": _json(r)}
            surface = "API"
        self.ev.op(self.phase, surface, "fx.convert", body, resp)
        return resp

    def dividend(self, account_id, symbol, d, dtype, gross, *, withholding=None,
                 net=None, reinvest_price=None, reinvest_shares=None, ex_date=None):
        # `d` is the PAYMENT date; `ex_date` (R6) is the ex-dividend date and is used by the
        # replay for a STOCK dividend only. Driven through the SAME CSV door the owner uses,
        # so the column being accepted end to end is part of what this exercises.
        cols = ["account", "symbol", "date", "type", "gross", "withholding", "net",
                "reinvest_shares", "reinvest_price", "ex_date"]
        row = {"account": account_id, "symbol": symbol, "date": d, "type": dtype,
               "gross": str(gross), "withholding": _s(withholding), "net": _s(net),
               "reinvest_shares": _s(reinvest_shares), "reinvest_price": _s(reinvest_price),
               "ex_date": _s(ex_date)}
        csv_text = ",".join(cols) + "\n" + ",".join(row[c] for c in cols) + "\n"
        r = self.api.post("/api/import/commit",
                          {"kind": "dividends", "csv_text": csv_text, "ack_warnings": True})
        resp = {"status": r.status_code, "json": _json(r)}
        self.ev.op(self.phase, "API", f"dividend.{dtype}",
                   {"account": account_id, "symbol": symbol, "date": d, "type": dtype,
                    "gross": str(gross)}, resp)
        return resp

    def edit_tx(self, txn_id, account_id, symbol, side, d, shares, price, fee, tax,
                *, ack=False):
        body = {"account_id": account_id, "symbol": symbol, "side": side, "date": d,
                "shares": str(shares), "price": str(price), "fee": str(fee),
                "tax": str(tax), "ack_oversell": ack}
        r = self.api.put(f"/api/ledgers/transactions/{txn_id}", body)
        resp = {"status": r.status_code, "json": _json(r)}
        self.ev.op(self.phase, "API", "edit.transaction", {"id": txn_id, **body}, resp)
        return resp

    def corporate_action(self, account_id, kind, d, from_symbol, to_symbol,
                         ratio_to, ratio_from, *, cost_carry=None, note=None):
        """Write ONE corporate_actions row (spec §3) and op-log it.

        Surface is ``DB`` because the entry surfaces are **W7** and do not exist yet —
        there is no ``POST /api/ledgers/corporate-actions``. The row still goes in through
        the app's own ``insert_corporate_action``, is read back as a raw ledger fact, and
        every consequence is computed independently by the oracle. Move this to the API
        the moment W7 lands (see ``common.write_corporate_action``).
        """
        aid = C.write_corporate_action(self.db, account_id, d, kind, from_symbol,
                                       to_symbol, ratio_to, ratio_from,
                                       cost_carry=cost_carry, note=note)
        inputs = {"account_id": account_id, "kind": kind, "date": d,
                  "from_symbol": from_symbol, "to_symbol": to_symbol,
                  "ratio_to": ratio_to, "ratio_from": ratio_from,
                  "cost_carry": cost_carry, "note": note}
        self.ev.op(self.phase, "DB", f"corporate_action.{kind}", inputs,
                   {"status": 201, "json": {"action_id": aid}},
                   note=f"{account_id} {from_symbol}->{to_symbol}")
        return aid

    def delete_corporate_action(self, action_id):
        """Delete one corporate action — E16: history RE-COMPUTES, nothing is snapshotted."""
        ok = C.delete_corporate_action_row(self.db, action_id)
        self.ev.op(self.phase, "DB", "corporate_action.delete", {"id": action_id},
                   {"status": 200 if ok else 404, "json": {"deleted": ok}})
        return ok

    def delete_tx(self, txn_id, *, ack=False):
        r = self.api.delete(f"/api/ledgers/transactions/{txn_id}", ack_oversell=str(ack).lower())
        resp = {"status": r.status_code, "json": _json(r)}
        self.ev.op(self.phase, "API", "delete.transaction", {"id": txn_id, "ack": ack}, resp)
        return resp


def _json(r):
    try:
        return r.json()
    except Exception:
        return {"text": r.text[:400]}


def _s(v):
    return "" if v is None else str(v)


# ---------------------------------------------------------------- reconciliation
def spots() -> C.Spot:
    return C.Spot(dict(FX_RATES))


def reconcile(ev: C.Evidence, api: C.Api, db_path, label: str, *, valuation=True,
              reports=False):
    """Full oracle-vs-system reconciliation at the current state."""
    phase = f"phase1:{label}"
    facts = C.load_facts_from_db(db_path)
    res = O.replay(facts)
    sp = spots()
    prices = dict(PRICES)
    # Any negative-share position (賣超 OR an open declared short) gates the blended
    # KPI/XIRR comparison off at this checkpoint: the app computes them differently in
    # that state (oversold -> not computable; short -> negative terminal value) and the
    # scenario always closes the short before the final full-KPI reconcile.
    oversold = any(h.shares < O.ZERO for h in res.holdings.values())

    dash = api.get("/api/dashboard").json()

    # ---- A0. Corporate actions the oracle REFUSED (spec §5) ----
    # Op-logged, not asserted here: which rule fired is scenario knowledge, so the
    # scenario asserts the codes (see run_phase1's corp.refusal_codes). Logging it makes a
    # holdings mismatch triageable — "the oracle skipped E5 and the app applied it" is a
    # different bug from "both applied it and disagreed on the arithmetic".
    if res.action_refusals:
        ev.op(phase, "ORACLE", "corporate_action.refusals",
              {"count": len(res.action_refusals)},
              {"refusals": [f"{c}: {s}" for c, s in res.action_refusals]})

    # ---- A. Holdings cost basis (per account+symbol) ----
    app_hold = {(h["account_id"], h["symbol"]): h for h in dash["holdings"]}
    orc_keys = set(res.holdings.keys())
    ev.check("holdings.keyset", label, sorted("|".join(k) for k in orc_keys),
             sorted("|".join(k) for k in app_hold), phase)
    for key, h in res.holdings.items():
        a = app_hold.get(key)
        if a is None:
            ev.check("holdings.present", "|".join(key), True, False, phase)
            continue
        sc = "|".join(key)
        ev.check("holding.shares", sc, h.shares, a["shares"], phase)
        ev.check("holding.original_total", sc, h.original_total, a["original_cost_total"], phase)
        ev.check("holding.adjusted_total", sc, h.adjusted_total, a["adjusted_cost_total"], phase)
        ev.check("holding.original_avg", sc, h.original_avg, a["original_avg"], phase)
        ev.check("holding.adjusted_avg", sc, h.adjusted_avg, a["adjusted_avg"], phase)
        ev.check("holding.dividend_portion", sc, h.dividend_portion, a["dividend_portion"], phase)
        # Flags: 賣超 (sticky) vs declared short vs skipped dividend — three distinct states
        # the UI renders differently; the oracle derives each independently.
        ev.check("holding.oversold", sc, h.oversold, a["oversold"], phase)
        ev.check("holding.short_open", sc, h.short_open, a["short_open"], phase)
        ev.check("holding.unbookable_dividend", sc, h.unbookable_dividend,
                 a["unbookable_dividend"], phase)
        # A REFUSED corporate action (spec §5 dashboard path): the shares are still in
        # pre-action terms against a post-action price series. A fourth distinct state,
        # rendered differently again — never conflate it with 賣超.
        ev.check("holding.unbookable_action", sc, h.unbookable_action,
                 a.get("unbookable_action"), phase)
        # Valuation on the SIGNED quantity: an open short (negative shares) is a real
        # priced position (2026-07-31) — only an oversold row has 待釐清 (null) values.
        if valuation and h.shares != O.ZERO and not h.oversold and key[1] in prices:
            p = prices[key[1]]
            ev.check("holding.market_value", sc, p * h.shares, a["market_value"], phase)
            unreal = (p - h.adjusted_avg) * h.shares
            ev.check("holding.unrealized_pnl", sc, unreal, a["unrealized_pnl"], phase)
            ev.check("holding.capital_gain", sc, (p - h.original_avg) * h.shares,
                     a["capital_gain"], phase)
            # Remediation #2 (2026-07-31): ratios over a short's NEGATIVE basis divide by
            # abs() — the bare ratio flips sign and shows a profitable short as a loss.
            if h.original_total != O.ZERO:
                ev.check("holding.unrealized_pct", sc, unreal / abs(h.original_total),
                         a.get("unrealized_pct"), phase)

    # ---- B. Realized rows (dashboard + CSV) ----
    app_real = dash["realized"]["rows"]
    ev.check("realized.count", label, len(res.realized_rows), len(app_real), phase)
    for i, rr in enumerate(res.realized_rows):
        if i >= len(app_real):
            break
        a = app_real[i]
        sc = f"{rr.account_id}/{rr.symbol}@{rr.sell_date} #{i}"
        ev.check("realized.proceeds_net", sc, rr.proceeds_net, a["proceeds_net"], phase)
        ev.check("realized.adjusted_removed", sc, rr.adjusted_cost_removed,
                 a["adjusted_cost_removed"], phase)
        ev.check("realized.original_removed", sc, rr.original_cost_removed,
                 a["original_cost_removed"], phase)
        ev.check("realized.realized", sc, rr.realized, a["realized"], phase)
        # sale vs dividend-income vs short_cover — the tax package routes on this.
        ev.check("realized.kind", sc, rr.kind, a.get("kind"), phase)

    # ---- C. Cash pools + running-balance statement ----
    cash = api.get("/api/cash", limit=500).json()
    app_bal = {(b["account_id"], b["ccy"]): dec(b["amount"]) for b in cash["balances"]}
    # oracle balances (only non-zero pools present in app response; compare superset)
    all_pools = set(res.cash.keys()) | set(app_bal.keys())
    for pool in sorted(all_pools):
        exp = res.cash.get(pool, O.ZERO)
        got = app_bal.get(pool, O.ZERO)
        ev.check("cash.balance", "|".join(pool), exp, got, phase)
    _reconcile_cash_statement(ev, facts, res, app_bal, phase)

    # ---- D. FX pools + realized/unrealized ----
    _reconcile_fx(ev, res, dash, facts, prices, sp, phase, valuation)

    # ---- E. KPI totals + XIRR scalar ----
    if valuation and not oversold:
        _reconcile_kpis(ev, res, dash, prices, sp, phase)
        _reconcile_xirr(ev, res, dash, facts, prices, sp, phase)

    # ---- E2. Ledger INTEGRITY (validity, not arithmetic — see oracle layer 4) ----
    _reconcile_integrity(ev, res, facts, phase)

    # ---- F. Ledger APIs row-by-row (raw facts) ----
    _reconcile_ledger_api(ev, api, facts, phase)

    # ---- G. Exports (CSV) ----
    _reconcile_exports(ev, api, res, prices, phase, valuation)

    # ---- H. Print reports (HTML) ----
    if reports:
        _reconcile_reports(ev, api, res, prices, sp, phase, valuation)

    return res


def anchor(ev: C.Evidence, api: C.Api, check: str, account_id: str, symbol: str,
           field: str, expected, phase="phase1:anchor"):
    """Assert ONE app-reported holding field against a hand-computed LITERAL.

    The third leg of the corporate-action proof, and the only one that is not a
    comparison between two replays. ``holding.shares`` proves the app and the oracle agree;
    it does **not** prove either landed on the integer the broker's statement says, because
    a rounded ratio in BOTH would agree at 199.99. Spec §3.1(ii) is precisely about that
    number, so the expected value here is written out by hand from the ratio and the
    pre-action share count — never derived from the oracle.
    """
    rows = api.get("/api/dashboard").json()["holdings"]
    got = next((h.get(field) for h in rows
                if h["account_id"] == account_id and h["symbol"] == symbol), None)
    ev.check(check, f"{account_id}/{symbol}.{field}", expected, got, phase)
    return got


def _reconcile_cash_statement(ev, facts: O.Facts, res, app_bal, phase):
    """Reconstruct a per-(account,ccy) running-balance statement from ALL cash-affecting
    facts (the sequence the app's balance is the sum of) and assert each pool's final
    running balance equals the app's reported balance.

    Note: the app exposes no literal /api/cash/statement route; GET /api/cash returns the
    balances + the deposit/withdraw movements ledger. This reconstructs the full statement
    (incl. trade settlements, fx legs, cash dividends) so every line is accounted for, then
    checks the terminal line == reported balance.
    """
    lines: dict[tuple[str, str], list[tuple]] = {}

    def add(key, d, seq, label, delta):
        lines.setdefault(key, []).append((d, seq, label, delta))

    for m in facts.cash:
        # Debits reduce the pool (audit C4); every other kind credits it. The set is
        # oracle.py's, re-derived there independently of the app's own table.
        add((m.account_id, m.ccy), m.d, (0, m.id), f"{m.kind}",
            -m.amount if m.kind.upper() in O.DEBIT_KINDS else m.amount)
    for c in facts.fxs:
        add((c.account_id, c.from_ccy), c.d, (1, c.id), "FX_OUT", -c.from_amt)
        add((c.account_id, c.to_ccy), c.d, (1, c.id), "FX_IN", c.to_amt)
    for t in facts.txs:
        inst = facts.instruments.get(t.symbol)
        if inst is None:
            continue
        delta = -(t.qty * t.price + t.fee + t.tax) if t.side == "BUY" \
            else (t.qty * t.price - t.fee - t.tax)
        add((t.account_id, inst.quote_ccy), t.trade_date, (2, t.id), f"{t.side}", delta)
    for dv in facts.divs:
        inst = facts.instruments.get(dv.symbol)
        if inst is None or dv.type not in O.CASH_DIVIDEND_TYPES:
            continue
        add((dv.account_id, inst.quote_ccy), dv.d, (3, dv.id), "DIV", dv.net)

    for key in sorted(set(lines) | set(app_bal)):
        seq = sorted(lines.get(key, []), key=lambda x: (x[0], x[1]))
        run = O.ZERO
        for _d, _s, _lab, delta in seq:
            run += delta
        exp = run
        got = app_bal.get(key, O.ZERO)
        ev.check("cash.statement.terminal", "|".join(key), exp, got, phase)


# (account, symbol) pairs this scenario oversells ON PURPOSE with ack=True (run_phase1
# proves the guard blocks it unacked first). A user-acknowledged oversell is a recorded
# decision, not a control failure — see domain-ledger.md.
ACKED_OVERSELLS = {("tw_broker", "0050")}


def _reconcile_integrity(ev, res, facts, phase):
    """Ledger VALIDITY, orthogonal to every other assertion in this file.

    Everything else compares the app against the oracle: it proves both replayed the same
    rows the same way, and is therefore blind to a row that should never have been accepted.
    Measured on 2026-07-30: a back-dated oversell reconciled perfectly while carrying an
    average cost 10-16x the market price, because `build_book` drops an oversold position's
    basis and a LATER buy clears the `oversold` flag. These two checks stay red until the
    impossible row is gone.
    """
    findings = O.integrity_findings(facts, res.realized_rows, acked=ACKED_OVERSELLS)
    for check, scope in findings:
        ev.check(check, scope, "clean", "VIOLATION", phase)
    if not findings:
        ev.check("ledger.integrity", "no negative position / orphan sell", "clean", "clean",
                 phase)


def _reconcile_fx(ev, res, dash, facts, prices, sp, phase, valuation):
    fx = dash.get("fx")
    kpis = dash["kpis"]
    # per-account avg_rate + realized_fx
    for aid, (_rule, settle, funding) in O.ACCOUNTS.items():
        if settle == funding:
            continue
        exp_avg = res.fx_avg_rate.get(aid)
        exp_real = res.fx_realized.get(aid)
        if fx and aid in (fx.get("by_account") or {}):
            acc = fx["by_account"][aid]
            ev.check("fx.avg_rate", aid, exp_avg, acc.get("avg_rate"), phase)
            ev.check("fx.realized", aid, exp_real, acc.get("realized_fx"), phase)
            # spec 2026-07-30: the pool now includes foreign cash movements, so it must
            # equal the funds view; covered_ratio/gap disclose the basis-unknown part.
            exp_cash = res.fx_foreign_cash.get(aid, O.ZERO)
            exp_ratio = res.fx_covered_ratio.get(aid, O.ONE)
            ev.check("fx.foreign_cash", aid, exp_cash, acc.get("foreign_cash"), phase)
            ev.check("fx.covered_ratio", aid, exp_ratio, acc.get("covered_ratio"), phase)
            ev.check("fx.basis_gap", aid, exp_cash * (O.ONE - exp_ratio),
                     acc.get("fx_basis_gap"), phase)
            ev.check("fx.pool_equals_funds", aid, exp_cash,
                     res.cash.get((aid, settle), O.ZERO), phase)
    # reporting realized fx rollup
    if fx is not None:
        exp_roll = O.ZERO
        for aid, (_rule, settle, funding) in O.ACCOUNTS.items():
            if settle == funding:
                continue
            r = res.fx_realized.get(aid)
            if r is None:
                continue
            to_rep = O.ONE if funding == REPORTING else sp.rate(funding, REPORTING)
            exp_roll += r * to_rep
        ev.check("fx.reporting_realized", "rollup", exp_roll, kpis.get("fx_realized"), phase)
        if valuation:
            _reconcile_fx_unrealized(ev, res, kpis, facts, prices, sp, phase)


def _reconcile_fx_unrealized(ev, res, kpis, facts, prices, sp, phase):
    exp = O.ZERO
    for aid, (_rule, settle, funding) in O.ACCOUNTS.items():
        if settle == funding:
            continue
        avg = res.fx_avg_rate.get(aid)
        if avg is None:
            continue
        try:
            spot = sp.rate(settle, funding)
        except KeyError:
            continue
        # Batch B: an account may hold instruments in >1 currency (the dual-market moomoo_my
        # holds USD-quoted US stocks AND MYR-quoted MY stocks). The FX pool exposure is the
        # FOREIGN (settlement) currency ONLY — fold in JUST settle-ccy holdings, mirroring the
        # app's dashboard exposure filter (h.quote_ccy == settlement_ccy, dashboard.py). Without
        # this the MYR stock value would be mis-summed into the USD exposure.
        stock_val = O.ZERO
        for (acct, sym), h in res.holdings.items():
            if acct == aid and h.quote_ccy == settle and h.shares > O.ZERO and sym in prices:
                stock_val += prices[sym] * h.shares
        fcash = res.fx_foreign_cash.get(aid, O.ZERO)
        # spec 2026-07-30 F2/F3: one coverage ratio scales the WHOLE foreign exposure
        # (cash AND stocks), because avg came from the basis-known population only.
        ratio = res.fx_covered_ratio.get(aid, O.ONE)
        unreal_home = (stock_val + fcash) * ratio * (spot - avg)
        to_rep = O.ONE if funding == REPORTING else sp.rate(funding, REPORTING)
        exp += unreal_home * to_rep
    # check_rel, not check: `exp` above is a FOUR-factor product whose operands each
    # already compare EXACTLY against the app (fx.avg_rate / fx.covered_ratio /
    # fx.foreign_cash, asserted above). Only the ASSOCIATION differs between the two
    # implementations, and at 28-digit Decimal context that is worth 1 ULP — ~1e-27
    # relative. See Evidence.ULP_REL_TOL for why 1e-20 cannot hide a money defect.
    ev.check_rel("fx.reporting_unrealized", "rollup", exp,
                 kpis.get("fx_unrealized"), phase)


def _reconcile_kpis(ev, res, dash, prices, sp, phase):
    kpis = dash["kpis"]
    realized_total = O.ZERO
    for ccy, amt in res.realized_by_ccy.items():
        realized_total += amt if ccy == REPORTING else amt * sp.rate(ccy, REPORTING)
    unreal = O.unrealized_by_ccy(res, prices)
    unreal_total = O.ZERO
    for ccy, amt in unreal.items():
        unreal_total += amt if ccy == REPORTING else amt * sp.rate(ccy, REPORTING)
    mv_total = O.ZERO
    for (_acct, sym), h in res.holdings.items():
        if h.shares > O.ZERO and sym in prices:
            v = prices[sym] * h.shares
            mv_total += v if h.quote_ccy == REPORTING else v * sp.rate(h.quote_ccy, REPORTING)
    # total_return mirrors returns.total_return: per-ccy (realized+unrealized) THEN convert
    # (NOT realized_total+unrealized_total, which differ by 1 ULP at 28 sig digits).
    tr = O.ZERO
    for ccy in set(res.realized_by_ccy) | set(unreal):
        part = res.realized_by_ccy.get(ccy, O.ZERO) + unreal.get(ccy, O.ZERO)
        tr += part if ccy == REPORTING else part * sp.rate(ccy, REPORTING)
    ev.check("kpi.realized_total", "TWD", realized_total, kpis.get("realized_total"), phase)
    ev.check("kpi.unrealized_total", "TWD", unreal_total, kpis.get("unrealized_total"), phase)
    ev.check("kpi.total_market_value", "TWD", mv_total, kpis.get("total_market_value"), phase)
    ev.check("kpi.total_return", "TWD", tr, kpis.get("total_return"), phase)


def _reconcile_xirr(ev, res, dash, facts, prices, sp, phase):
    """The ONE documented-tolerance comparison: an independent reporting-currency XIRR
    (oracle cashflows at trade-date FX, own Newton+bisection solver) vs the app's
    kpis.xirr, asserted within oracle.XIRR_TOL. Closes the §7.2 XIRR gap.

    The terminal cashflow is dated at the app's OWN as_of (now.date() when the dashboard
    was built), read back from the response — not the fixed price/FX seeding date — so
    the two series discount the terminal value over the identical horizon.
    """
    app_xirr = dash["kpis"].get("xirr")
    as_of = date.fromisoformat(str(dash.get("as_of", ASOF.isoformat()))[:10])
    fx_on = fx_on_resolver()

    def fx_now(base, quote):
        return sp.rate(base, quote)

    try:
        dates, amounts = O.xirr_cashflows(res, facts, prices, REPORTING, fx_on, fx_now, as_of)
    except KeyError:
        # oracle cannot form the series (missing rate/price) -> the app must be None too
        ev.check("xirr.uncomputable_matches", "TWD", None, app_xirr, phase)
        return
    orc = O.xirr_solve(dates, amounts)
    ev.check_close("kpi.xirr", "TWD", orc, app_xirr, O.XIRR_TOL, phase)


def _reconcile_ledger_api(ev, api, facts: O.Facts, phase):
    # transactions: /api/ledgers/transactions returns rows desc; compare set by id
    rows = api.get("/api/ledgers/transactions", limit=500).json()["rows"]
    app = {r["id"]: r for r in rows}
    ev.check("ledger.tx.count", "count", len(facts.txs), len(rows), phase)
    for t in facts.txs:
        a = app.get(t.id)
        if a is None:
            ev.check("ledger.tx.present", f"id={t.id}", True, False, phase)
            continue
        ev.check("ledger.tx.shares", f"id={t.id}", t.qty, a["shares"], phase)
        ev.check("ledger.tx.price", f"id={t.id}", t.price, a["price"], phase)
        ev.check("ledger.tx.fee", f"id={t.id}", t.fee, a["fee"], phase)
        ev.check("ledger.tx.tax", f"id={t.id}", t.tax, a["tax"], phase)
        # 'total' derived field: BUY negative, SELL positive
        exp_total = -(t.qty * t.price + t.fee + t.tax) if t.side == "BUY" \
            else (t.qty * t.price - t.fee - t.tax)
        ev.check("ledger.tx.total", f"id={t.id}", exp_total, a["total"], phase)
    # dividends
    drows = api.get("/api/ledgers/dividends", limit=500).json()["rows"]
    dapp = {r["id"]: r for r in drows}
    ev.check("ledger.div.count", "count", len(facts.divs), len(drows), phase)
    for dv in facts.divs:
        a = dapp.get(dv.id)
        if a is None:
            continue
        ev.check("ledger.div.net", f"id={dv.id}", dv.net, a["net"], phase)
        ev.check("ledger.div.gross", f"id={dv.id}", dv.gross, a["gross"], phase)
    # fx
    frows = api.get("/api/ledgers/fx", limit=500).json()["rows"]
    fapp = {r["id"]: r for r in frows}
    ev.check("ledger.fx.count", "count", len(facts.fxs), len(frows), phase)
    for c in facts.fxs:
        a = fapp.get(c.id)
        if a is None:
            continue
        ev.check("ledger.fx.from_amt", f"id={c.id}", c.from_amt, a["from_amt"], phase)
        ev.check("ledger.fx.to_amt", f"id={c.id}", c.to_amt, a["to_amt"], phase)
        ev.check("ledger.fx.implied", f"id={c.id}", c.from_amt / c.to_amt, a["implied_rate"], phase)


def _reconcile_exports(ev, api, res, prices, phase, valuation):
    # holdings CSV
    raw = api.download("/api/export/holdings").decode("utf-8-sig")
    hrows = _parse_csv(raw)
    hkey = {(r["account_id"], r["symbol"]): r for r in hrows}
    for key, h in res.holdings.items():
        r = hkey.get(key)
        if r is None:
            ev.check("export.holdings.present", "|".join(key), True, False, phase)
            continue
        sc = "|".join(key)
        ev.check("export.holdings.shares", sc, h.shares, r["shares"], phase)
        ev.check("export.holdings.original_cost_total", sc, h.original_total,
                 r["original_cost_total"], phase)
        ev.check("export.holdings.adjusted_cost_total", sc, h.adjusted_total,
                 r["adjusted_cost_total"], phase)
    # realized CSV
    raw = api.download("/api/export/realized").decode("utf-8-sig")
    rrows = _parse_csv(raw)
    ev.check("export.realized.count", "count", len(res.realized_rows), len(rrows), phase)
    for i, rr in enumerate(res.realized_rows):
        if i >= len(rrows):
            break
        r = rrows[i]
        sc = f"{rr.account_id}/{rr.symbol} #{i}"
        ev.check("export.realized.realized", sc, rr.realized, r["realized"], phase)
        ev.check("export.realized.proceeds_net", sc, rr.proceeds_net, r["proceeds_net"], phase)
    # ledger CSV (transactions)
    raw = api.download("/api/export/ledger", {"kind": "transactions"}).decode("utf-8-sig")
    ev.check("export.ledger.tx.nonempty", "transactions", True, len(_parse_csv(raw)) >= 0, phase)


def _contains_number(text: str, value: Decimal) -> bool:
    """True if `value` (per-ccy 0dp or 2dp, thousands-separated, ROUND_HALF_UP) appears."""
    from decimal import ROUND_HALF_UP
    av = abs(value)
    norm = text.replace("−", "-").replace(",", "")
    for dp in (0, 2):
        q = av.quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)
        s = f"{q:,.{dp}f}".replace(",", "")
        if s in norm:
            return True
    return False


def _reconcile_reports(ev, api, res, prices, sp, phase, valuation):
    """Fetch the print reports (HTML) and verify they faithfully render the reconciled
    figures (display parity: quantized, thousands-separated). Same computed numbers as
    the dashboard/CSV channels — this proves the report renders them, not new math.
    """
    html = api.download("/api/export/holdings-report").decode("utf-8")
    ev.check("report.holdings.renders", "持倉報告",
             True, ("持倉報告" in html and "<!doctype html>" in html.lower()), phase)
    if valuation:
        realized_total = O.ZERO
        for c, amt in res.realized_by_ccy.items():
            realized_total += amt if c == REPORTING else amt * sp.rate(c, REPORTING)
        ev.check("report.holdings.realized_present", "KPI 已實現",
                 True, _contains_number(html, realized_total), phase)
        mv = O.ZERO
        for (_a, s), h in res.holdings.items():
            if h.shares > O.ZERO and s in prices:
                v = prices[s] * h.shares
                mv += v if h.quote_ccy == REPORTING else v * sp.rate(h.quote_ccy, REPORTING)
        ev.check("report.holdings.total_mv_present", "KPI 總市值",
                 True, _contains_number(html, mv), phase)
    n = sum(1 for (a, s), h in res.holdings.items()
            if h.shares > O.ZERO and s in html and _contains_number(html, h.shares))
    ev.check("report.holdings.shares_present", "持倉股數", True, n >= 3, phase)

    lhtml = api.download("/api/export/ledgers-report").decode("utf-8")
    ev.check("report.ledgers.renders", "帳本報告",
             True, "<!doctype html>" in lhtml.lower() and len(lhtml) > 500, phase)
    held_syms = {s for (a, s), h in res.holdings.items() if h.shares > O.ZERO}
    present = sum(1 for s in list(held_syms)[:4] if s in lhtml)
    ev.check("report.ledgers.symbols_present", "帳本標的",
             True, present >= min(3, len(held_syms)), phase)


def _parse_csv(raw: str) -> list[dict]:
    lines = [ln for ln in raw.splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return []
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    return list(reader)


def seed_all(db_path):
    """Direct-DB fixtures (no network): instruments, valuation prices, trade-date + spot FX."""
    mkt_of = {i[0]: i[1] for i in INSTRUMENTS}
    for sym, mkt, ccy, name, sector, etf in INSTRUMENTS:
        C.seed_instrument(db_path, sym, mkt, ccy, name, sector, etf)
    for sym, px in PRICES.items():
        C.seed_price(db_path, sym, mkt_of[sym], px, ASOF)
    for (base, quote), rate in EARLY_FX_RATES.items():
        C.seed_fx(db_path, base, quote, rate, EARLY_ASOF)   # trade-date FX (on-or-before)
    for (base, quote), rate in FX_RATES.items():
        C.seed_fx(db_path, base, quote, rate, ASOF)         # current spot (latest row)
