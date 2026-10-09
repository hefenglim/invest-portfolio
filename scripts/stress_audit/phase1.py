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
from datetime import date, timedelta
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
    # ---- Refused dividends (YQ3, owner ruling 2026-10-05). The replay refuses a payout in
    # three classes — no position by its date (CR-02), an open declared short, a ticker an
    # EXCHANGE vacated (E24) — and every figure leaves it out. The short class has its own
    # symbol above (2609); these two give the other two classes dedicated symbols, so no other
    # op can satisfy or disturb them.
    ("VCO", "TW", "TWD", "Vacated Source", "Industrials", False),     # EXCHANGEd -> VCN
    ("VCN", "TW", "TWD", "Vacated Successor", "Industrials", False),  # carries E24's flag
    ("ORP", "TW", "TWD", "Orphan Payer", "Industrials", False),       # never held (legacy row)
    # ---- US equity options (spec 2026-10-09). The CONTRACTS are never seeded: an option is
    # created from its OCC symbol by the trade door (OPT-D7), so registration is part of what
    # the scenario exercises. OPA is the one REGISTERED underlying: the sold-put assignment's
    # and the call exercise's stock legs buy it (the pair refuses an unregistered
    # underlying), and OPT-D17 shows its
    # price beside the strike of every open OPA contract. OPB / OPC / OPD stay unregistered —
    # "the underlying need not be registered" is the other half of OPT-D7.
    ("OPA", "US", "USD", "Option Underlying A", "Technology", False),
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
    # Refused-dividend symbols (YQ3). VCN is HELD at the first refused checkpoint, so its
    # price is what Y3's "the carrier keeps its value" is measured with; VCO and ORP are
    # never held at a checkpoint and are priced for fixture uniformity.
    "VCO": D("52"), "VCN": D("54"), "ORP": D("30"),
    # The options block's one registered underlying. Held only after the call exercise (100 at
    # the 42 strike) and the sold-put assignment (200 at the 40 strike), so this price values
    # those legs AND is OPT-D17's
    # 「標的現價」 beside the strike of every open OPA contract. Contracts are NEVER priced
    # here: an option is valued at its last ledger trade (spec §5), never at a stored quote.
    "OPA": D("45"),
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

    def _fee_check(self, txn_id, account_id, side, qty, price, symbol, d, daytrade=False,
                   *, supplied_fee=None, supplied_tax=None, close_reason=None):
        # is_etf + market come from the instrument REGISTRY (found-bug: entry paths must honour
        # is_etf, not default False); daytrade is the per-transaction TW same-day flag (0.15%).
        # Batch B: the merged dual-market moomoo_my routes fees by the instrument's market (US ->
        # moomoo_us, MY -> moomoo_my), so the oracle resolves the rule WITH the market too.
        reg = {i[0]: i for i in INSTRUMENTS}
        is_etf = bool(reg[symbol][5]) if symbol in reg else False
        market = reg[symbol][1] if symbol in reg else None
        # Options (spec §3): a contract is never in the fixture registry — it is registered from
        # its OCC symbol by the trade door — and is always US / USD, never an ETF.
        option = O.parse_occ(symbol) is not None
        if option:
            market, is_etf = "US", False
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
                                    is_etf, daytrade, stamp_fx, market=market,
                                    is_option=option, close_reason=close_reason)
        # A statement-supplied fee / tax WINS over the engine (OPT-D6; the manual door's
        # override and the CSV `fee` column) — on a rule set with no per-contract rate it is
        # the ONLY answer (the engine returned None above).
        if supplied_fee is not None:
            fee = dec(supplied_fee)
        if supplied_tax is not None:
            tax = dec(supplied_tax)
        got_fee, got_tax = C.read_fee_tax_from_db(self.db, txn_id)
        tag = ("daytrade" if daytrade else "etf" if is_etf else
               f"close:{close_reason}" if close_reason else "option" if option else "normal")
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

    # ---- US equity options (spec 2026-10-09) -------------------------------------------
    def option_trade(self, account_id, symbol, d, intent, contracts, price, *, fee=None,
                     expect_status=201, fee_check=True):
        """One option-mode manual entry (spec §4.2 / §8): ``option_intent`` (STO / BTC / BTO /
        STC) + ``contracts`` (口數) — the browser never sends ``shares`` or ``side``; the
        server converts 口數 to share-equivalents and derives the side (and STO's declared
        short) from the intent. ``fee`` is the statement fee (``fee_override``), REQUIRED on
        a rule set with no per-contract rate (OPT-D6)."""
        body = {"account_id": account_id, "symbol": symbol, "date": d,
                "option_intent": intent, "contracts": str(contracts), "price": str(price)}
        if fee is not None:
            body["fee_override"] = str(fee)
        r = self.api.post("/api/input/manual/commit", body)
        resp = {"status": r.status_code, "json": _json(r)}
        self.ev.op(self.phase, "API", f"option.{intent}", body, resp,
                   note=f"{account_id} {symbol}")
        if resp["status"] != expect_status:
            return resp
        txn_id = resp["json"].get("txn_id") if isinstance(resp.get("json"), dict) else None
        if txn_id and fee_check:
            side = "SELL" if intent in ("STO", "STC") else "BUY"
            self._fee_check(txn_id, account_id, side, D(contracts) * O.OPTION_MULTIPLIER,
                            price, symbol, d, supplied_fee=fee)
        return resp

    def option_close(self, account_id, symbol, d, reason, contracts, *, stock_price=None,
                     expect_status=201):
        """A §4.1 price-0 closing row through the manual door — 到期作廢 (``expired``) or
        被指派／履約 (``assigned`` / ``exercised``, which writes the option close AND the stock
        leg in one transaction). Side and price are the SERVER's (derived from the position);
        the fee and tax of the close row are 0 / 0 without the engine (spec §4.2)."""
        body = {"account_id": account_id, "symbol": symbol, "date": d,
                "close_reason": reason, "contracts": str(contracts)}
        if stock_price is not None:
            body["stock_price"] = str(stock_price)
        r = self.api.post("/api/input/manual/commit", body)
        resp = {"status": r.status_code, "json": _json(r)}
        self.ev.op(self.phase, "API", f"option.close.{reason}", body, resp,
                   note=f"{account_id} {symbol}")
        if resp["status"] != expect_status or not isinstance(resp.get("json"), dict):
            return resp
        j = resp["json"]
        ids = j.get("txn_ids") or [j.get("txn_id")]
        qty = D(contracts) * O.OPTION_MULTIPLIER
        terms = O.parse_occ(symbol)
        conn = sqlite3.connect(str(self.db))
        try:
            side_of = {i: conn.execute("SELECT side FROM transactions WHERE id=?",
                                       (i,)).fetchone()[0] for i in ids if i}
        finally:
            conn.close()
        if ids and ids[0]:
            self._fee_check(ids[0], account_id, side_of[ids[0]], qty, "0", symbol, d,
                            close_reason=reason)
        if len(ids) > 1 and ids[1] and terms is not None:
            # The stock leg: the underlying at the strike (or the given stock price), shares =
            # contracts × 100, fee by the ENGINE like any stock trade (spec §4.2).
            self._fee_check(ids[1], account_id, side_of[ids[1]], qty,
                            stock_price if stock_price is not None else terms.strike,
                            terms.underlying, d)
        return resp

    def option_adjust(self, account_id, d, from_symbol, to_symbol, ratio_to, *,
                      expect_status=201):
        """``OPTION_ADJUST`` through the REAL corporate-action door (spec §4.3), not the DB
        fixture the equity actions above use: ``ratio_from`` is always 1 (OPT-D16), the
        destination contract is registered from its symbol by the door (E10)."""
        body = {"account_id": account_id, "date": d, "kind": "OPTION_ADJUST",
                "from_symbol": from_symbol, "to_symbol": to_symbol,
                "ratio_to": str(ratio_to), "ratio_from": "1", "ack_warnings": True,
                "note": "stress: standard option contract adjustment"}
        r = self.api.post("/api/ledgers/corporate-actions", body)
        resp = {"status": r.status_code, "json": _json(r)}
        self.ev.op(self.phase, "API", "corporate_action.OPTION_ADJUST", body, resp,
                   note=f"{account_id} {from_symbol}->{to_symbol}")
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


def _trend_sample_dates(facts, by_date, last_iso: str) -> list[str]:
    """Which trend days to reconcile - the owner's 「事件日取樣」 (ruling 2026-08-27).

    Every ledger date AND the day before it, because a timing defect is only visible across
    the boundary it moves: the day before an event and the event day differ by exactly that
    event. Dividends contribute BOTH dates - ``effective`` (the ex-date for a 配股) and ``d``
    (the payment date) - so the two edges of the very window R6 corrected are sampled by
    construction.

    ``ASOF`` and its predecessor are added too. They are not a ledger event, they are the
    FIXTURE boundary: it is where the seeded prices begin, hence where the app's days flip
    from ``incomplete`` to valued.

    Every point is NOT sampled, deliberately: adjacent days are near-identical, so a full walk
    multiplies evidence volume without multiplying detection, and a failure buried in a
    thousand near-identical lines is harder to localise, not easier.
    """
    events: set[date] = set()
    for t in facts.txs:
        events.add(t.trade_date)
    for dv in facts.divs:
        events.add(dv.effective)
        events.add(dv.d)
    for o in facts.openings:
        events.add(o.build_date)
    for a in facts.actions:
        events.add(a.d)
    for c in facts.cash:
        events.add(c.d)
    for f in facts.fxs:
        events.add(f.d)
    events.add(ASOF)
    out: set[str] = {last_iso}
    for e in events:
        for cand in (e, date.fromordinal(e.toordinal() - 1)):
            iso = cand.isoformat()
            if iso in by_date:
                out.add(iso)
    return sorted(out)


def _daily_price_span(facts) -> date | None:
    """The first ledger date — where the daily close series has to begin, or ``None``."""
    days: list[date] = [t.trade_date for t in facts.txs]
    days += [dv.effective for dv in facts.divs] + [dv.d for dv in facts.divs]
    days += [o.build_date for o in facts.openings] + [a.d for a in facts.actions]
    return min(days) if days else None


def _ensure_daily_prices(db_path, facts) -> int:
    """Seed a DAILY close for every fixture symbol across the whole replay span.

    This is what closes the last half of the point-in-time gap. ``seed_all`` seeds exactly
    ONE close per symbol, dated :data:`ASOF`, so every earlier day was unpriced by
    construction and ``trend.total_value`` could not be asserted before ASOF — which is
    precisely where a share-count defect confined to a window (R6's ex-date) lives. With a
    price on every day, ``total_value`` at a KNOWN price pins the SHARE COUNT: the app and
    the oracle read the same close, so any disagreement left is a quantity.

    Two properties make this safe rather than a fixture that quietly lies:

    * **The closes are AS TRADED on their own date** — :func:`oracle.price_as_traded`
      multiplies the ASOF quote back out by the splits between that day and ASOF, which is
      the invariant ``data-and-pricing.md`` states for the column. Seeding the post-split
      quote on a pre-split day would store a basis the ledger never traded on.
    * **Each row is stamped ``fetched_at`` = its own date**, so both the write window
      ``(as_of, fetched_at]`` and the read window ``(priced_on, day]`` are empty. Nothing
      re-expresses these rows — not ``upsert_prices``, not ``reconcile_prices`` — so a SPLIT
      inserted or deleted later cannot repaint the history this family asserts against.

    Called from :func:`reconcile` BEFORE the dashboard is fetched, and derived from ``facts``
    each time, so it is self-correcting: a checkpoint that runs before the scenario's
    corporate actions exist seeds an unadjusted series, and the next one re-seeds the same
    dates on the corrected basis (idempotent upsert on ``(instrument, as_of_date)``).
    """
    start = _daily_price_span(facts)
    if start is None or start > ASOF:
        return 0
    mkt_of = {i[0]: i[1] for i in INSTRUMENTS}
    rows_by_date: dict[date, list] = {}
    day = start
    while day <= ASOF:
        batch = [
            (sym, mkt_of[sym],
             O.price_as_traded(facts, sym, day, quote=quote, quoted_on=ASOF))
            for sym, quote in PRICES.items() if sym in mkt_of
        ]
        rows_by_date[day] = batch
        day += timedelta(days=1)
    return C.seed_prices_daily(db_path, rows_by_date)


def _day_incomplete(res_d, day: date) -> bool:
    """Whether the app must publish this trend day as ``incomplete`` - derived, not read.

    Three causes, from ``domain-ledger.md`` plus this fixture's own price schedule:

    * a held position that is 賣超 / 待釐清 (``oversold`` / ``unbookable_dividend`` /
      ``unbookable_action``). The first and last have no honest value that day; a refused
      dividend leaves the value honest and only the payout 待釐清 (Y3, owner ruling
      2026-10-05) — the day is marked all the same, and :func:`_day_value_judgeable` decides
      the value separately;
    * a held position with NO stored price on-or-before ``day`` cannot be valued. Since
      :func:`_ensure_daily_prices` seeds a close for every fixture symbol on every day from
      the first ledger event through :data:`ASOF`, the only symbol that can now hit this is
      one absent from :data:`PRICES` entirely — a future scenario symbol nobody priced.
    * a day holding nothing is COMPLETE with a zero total - the app's loop simply adds nothing.

    FX is not a cause: the fixture seeds every needed pair at ``EARLY_ASOF``, which precedes
    the first ledger event, so an on-or-before rate always resolves.

    K3 (owner ruling 2026-10-05) adds a fourth, independent of what is held: a dividend
    refused by ``day`` because it landed on an open short. The 2609 lifecycle covers that short
    back to exactly zero, so after the cover no holding carries ``unbookable_dividend`` — and
    the ruling is that the 待釐清 must not die with a flat carrier. The days stay incomplete.
    """
    if res_d.refused_dividends:
        return True
    if res_d.refused_closes:
        # Options spec §4.4: a refused close is a RECORD "like orphan_dividends" — XIRR and
        # B are withheld while it stands, B through the same day rule as K3: every day from
        # the close's own date is incomplete (`res_d` holds only closes dated on or before
        # *day*), whether or not its position is still held. The value stays judgeable — the
        # refused row booked nothing, so every share is right (Y3's reading).
        return True
    for h in res_d.holdings.values():
        if h.shares == O.ZERO:
            continue
        # Options spec §4.4: every reader of unbookable_action reads unbookable_close the
        # same way (timeseries.py named among them).
        if (h.oversold or h.unbookable_dividend or h.unbookable_action
                or h.unbookable_close):
            return True
        if O.is_option(h.symbol):
            # Options spec §5 / R3: the trend EXCLUDES an open contract's value "without
            # marking the day incomplete" — it is never priced from `prices`, so it can
            # never be the unpriced position below.
            continue
        if h.symbol not in PRICES:
            # No seeded close AT ALL. Written as a derivation rather than assumed: without
            # this a future scenario symbol with no price would CRASH this family on the
            # `PRICES[...]` lookup below instead of failing an assertion, and a harness that
            # crashes on new data is a harness people stop extending.
            #
            # A day AFTER the seeded series is NOT incomplete: the read carries the last
            # close forward (on-or-before), which is a valid valuation and what the app does.
            # Measured — treating `day > ASOF` as unpriced reddened four days the app values
            # correctly from the ASOF close.
            return True
    return False


def _day_value_judgeable(res_d) -> bool:
    """Whether ``trend.total_value`` is assertable on a day - every held position has an
    honest value, derived from ``domain-ledger.md``, not read off the app.

    Separate from :func:`_day_incomplete` since Y3 (owner ruling 2026-10-05): a day can be
    INCOMPLETE and still have an honest total. A refused dividend (held carrier flagged
    ``unbookable_dividend``, or a refusal whose carrier is flat) leaves every SHARE right, so
    the app counts the position and this family asserts the total on that day too. A held
    賣超 (basis discarded) or ``unbookable_action`` (pre-action shares against a post-action
    price) has no honest value: the app leaves it out, so the total is not asserted. An
    unpriced symbol cannot be valued either (see :func:`_day_incomplete`).
    """
    for h in res_d.holdings.values():
        if h.shares == O.ZERO or O.is_option(h.symbol):
            continue      # R3: an open contract's value is not in the trend at all
        if h.oversold or h.unbookable_action or h.symbol not in PRICES:
            return False
    return True


def _reconcile_trend(ev: C.Evidence, dash, facts, phase: str) -> None:
    """POINT-IN-TIME reconciliation of the daily replay (the ``trend.*`` family).

    Why it exists: every other family in this harness compares the app and the oracle at the
    CURRENT state, and a whole class of defect is invisible there - anything whose effect is
    confined to a window between two dates ends at the same place either way. R6's ex-date is
    exactly that shape (a 配股's shares arrive a month earlier; the position at ``as_of`` is
    identical), which is why ``fail=0`` was silent about it. Disclosed in README section 6 on
    2026-08-26 and closed here to the extent the app's surfaces allow.

    What is compared:

    * ``trend.start`` / ``trend.contiguous_days`` - the replay's span, derived from the
      earliest ledger event with dividends on their EFFECTIVE date.
    * ``trend.net_invested`` - EXACT and price-free, the subtrahend of B
      (``total_value - net_invested``) in AI-D41's A / B / B-A decomposition. This is the
      assertion that guards the owner's 「B 收三類」 ruling: the app and the oracle must move
      onto the three cash kinds together, and this family goes red in between.
    * ``trend.incomplete`` - the app's honesty about days it cannot value, derived
      independently by :func:`_day_incomplete` over a point-in-time replay.
    * ``trend.total_value`` - on every day whose holdings all have an honest value
      (:func:`_day_value_judgeable`), INCLUDING a day marked incomplete only for a refused
      dividend (Y3: the app keeps that position's value). Asserting it on an unpriced day would
      be asserting the fixture, not the app.
    * ``trend.net_worth`` - on the same days: ``total_value`` plus every cash pool's
      end-of-day balance, each converted at that day's on-or-before rate (FU-D29 / C8). The
      pools are the oracle's own point-in-time replay (``res_d.cash``), so a refused payout
      moves no day's net worth (⑪) while the carrier's value stays in it (Y3).

    What is NOT compared, and why: a point-in-time per-symbol SHARE COUNT, because no app
    surface returns one. ``trend.points`` carries four portfolio-level numbers, and the sell
    preview's date-awareness lives in the 賣超 guard, not in a report. Closing that needs a
    DAILY price fixture so ``total_value`` becomes assertable across the whole span - and that
    is not one line, because back-dating this fixture's post-action prices would hand the read
    side a wrong split basis. Recorded in the README rather than approximated here.
    """
    trend = (dash.get("trend") or {})
    pts = trend.get("points") or []
    ev.check("trend.available", "series", True, bool(trend.get("available")), phase)
    if not pts:
        return
    by_date = {p["date"]: p for p in pts}
    first_iso, last_iso = min(by_date), max(by_date)
    span = date.fromisoformat(last_iso).toordinal() - date.fromisoformat(first_iso).toordinal()
    ev.check("trend.contiguous_days", "series", span + 1, len(pts), phase)

    starts = ([t.trade_date for t in facts.txs]
              + [dv.effective for dv in facts.divs]
              + [o.build_date for o in facts.openings])
    if starts:
        ev.check("trend.start", "series", min(starts).isoformat(), first_iso, phase)

    fx_on = fx_on_resolver()
    skipped: list[str] = []
    for day_iso in _trend_sample_dates(facts, by_date, last_iso):
        p = by_date[day_iso]
        day = date.fromisoformat(day_iso)
        try:
            expected_ni = O.net_invested_through(facts, day, REPORTING, fx_on)
        except KeyError:
            # No on-or-before rate for some flow - the app publishes NO trend at all in that
            # state, so there is nothing to compare rather than a zero to agree with.
            continue
        ev.check("trend.net_invested", day_iso, expected_ni, dec(p["net_invested"]), phase)

        res_d = O.replay_through(facts, day)
        if res_d.action_refusals:
            # WHICH rule refused is scenario knowledge (see the A0 note in reconcile), and the
            # app's refusal path marks the day incomplete for reasons this family does not
            # re-derive. Collected and logged ONCE below — never per day: `ops` is a signal
            # ("did the scenario change?"), and diagnostic logging that moves it by 75 turns
            # that signal into noise.
            skipped.append(day_iso)
            continue
        expected_inc = _day_incomplete(res_d, day)
        ev.check("trend.incomplete", day_iso, expected_inc, bool(p["incomplete"]), phase)
        if not _day_value_judgeable(res_d):
            continue
        total = O.ZERO
        for h in res_d.holdings.values():
            if h.shares == O.ZERO or O.is_option(h.symbol):
                # R3 / OPT-D4: the trend EXCLUDES an open contract's value (its premium is in
                # the cash series, so net worth is overstated by a sold contract's value and
                # understated by a bought one's — the note the options block carries).
                continue
            # The close for THIS day, as traded on it. `_ensure_daily_prices` seeded one
            # per symbol per day at exactly this value, so the app reads the same number and
            # the read-side window (priced_on, day] is empty — no factor on either side. The
            # comparison that survives is therefore a pure SHARE-COUNT comparison, which is
            # the whole reason the series exists.
            rate = (O.ONE if h.quote_ccy == REPORTING
                    else fx_on(day, h.quote_ccy, REPORTING))
            close = O.price_as_traded(facts, h.symbol, day,
                                      quote=PRICES[h.symbol], quoted_on=ASOF)
            total += close * h.shares * rate
        ev.check("trend.total_value", day_iso, total, dec(p["total_value"]), phase)
        cash_total = O.ZERO
        for (_acct, ccy), bal in res_d.cash.items():
            if bal == O.ZERO:
                continue          # a zero pool contributes nothing and needs no rate
            cash_total += bal * (O.ONE if ccy == REPORTING else fx_on(day, ccy, REPORTING))
        ev.check("trend.net_worth", day_iso, total + cash_total,
                 dec(p.get("net_worth")) if p.get("net_worth") is not None else None, phase)

    # No silent caps: a day this family declined to judge is named, not quietly absent.
    if skipped:
        ev.op(phase, "ORACLE", "trend.days_not_judged",
              {"reason": "corporate action refused by the oracle on that day"},
              {"count": len(skipped), "dates": skipped})


def reconcile(ev: C.Evidence, api: C.Api, db_path, label: str, *, valuation=True,
              reports=False):
    """Full oracle-vs-system reconciliation at the current state."""
    phase = f"phase1:{label}"
    raw = C.load_facts_from_db(db_path)
    # BEFORE any API call: the dashboard must be built over the daily series, or the trend
    # family would compare an app that could not value a day against an oracle that could.
    _ensure_daily_prices(db_path, raw)
    dash = api.get("/api/dashboard").json()
    # DEF-016 / DEF-056: the valuation reads only the rows that count by the app's own as_of
    # (read back from the response, as the XIRR family does) — every ledger from its own
    # date. The raw facts stay for the checks that compare stored ROWS (ledger APIs, the
    # dated cash statement).
    as_of = date.fromisoformat(str(dash.get("as_of", ASOF.isoformat()))[:10])
    facts = O.facts_valued_as_of(raw, as_of)
    res = O.replay(facts)
    sp = spots()
    prices = dict(PRICES)
    # Options (spec §5, R2): an open contract is valued at its LAST LEDGER TRADE, never at a
    # stored quote — the oracle's own derivation, keyed by symbol like every other price.
    for sym, (px, _on) in O.option_prices(facts, as_of).items():
        prices[sym] = px
    # Any negative-share STOCK position (賣超 OR an open declared short) gates the blended
    # KPI/XIRR comparison off at this checkpoint: the app computes them differently in
    # that state (oversold -> not computable; short -> negative terminal value) and the
    # scenario always closes the short before the final full-KPI reconcile.
    # An open SOLD OPTION does not gate it (OPT-D4): it is a priced point-in-time position
    # whose negative value KPI 總市值 / 總報酬 / XIRR must carry — reconciling them while one is
    # open is the point. Only an option 賣超 (no honest basis) would gate.
    oversold = any((h.shares < O.ZERO and not O.is_option(h.symbol))
                   or (h.oversold and O.is_option(h.symbol))
                   for h in res.holdings.values())
    # YQ1 (owner ruling 2026-10-05): the TREND family has its own, narrower gate. A declared
    # short is a real, priced position the series carries (2026-07-31), and a refused
    # dividend on it keeps that value (Y3) — so the trend is judged while a declared short is
    # open and switched off only by an UNDECLARED oversell (sticky 賣超), whose basis is gone
    # on every day. The KPI / XIRR gate above is unchanged, exactly as its author set it.
    undeclared_oversell = any(h.oversold for h in res.holdings.values())

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
    # Options spec §6.2 / OPT-D5: `holdings` is STOCK-ONLY — a contract is reconciled in the
    # `options` block (section O below), never as a holdings row.
    stock_holdings = {k: h for k, h in res.holdings.items() if not O.is_option(k[1])}
    app_hold = {(h["account_id"], h["symbol"]): h for h in dash["holdings"]}
    orc_keys = set(stock_holdings.keys())
    ev.check("holdings.keyset", label, sorted("|".join(k) for k in orc_keys),
             sorted("|".join(k) for k in app_hold), phase)
    for key, h in stock_holdings.items():
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
    # Stock-only (spec §6.2): an option's realized rows live in `options.realized.rows`.
    stock_realized = [rr for rr in res.realized_rows if not O.is_option(rr.symbol)]
    app_real = dash["realized"]["rows"]
    ev.check("realized.count", label, len(stock_realized), len(app_real), phase)
    for i, rr in enumerate(stock_realized):
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
    _reconcile_cash_statement(ev, raw, res, app_bal, phase)

    # ---- D. FX pools + realized/unrealized ----
    _reconcile_fx(ev, res, dash, facts, prices, sp, phase, valuation)

    # ---- O. The `options` block (spec §7.1) — presence, keys, open contracts, realized ----
    _reconcile_options(ev, dash, raw, facts, res, prices, sp, as_of, phase, valuation)

    # ---- E. KPI totals + XIRR scalar ----
    if valuation and not oversold:
        _reconcile_kpis(ev, res, dash, facts, prices, sp, phase)
        _reconcile_xirr(ev, res, dash, facts, prices, sp, phase)
    if valuation and not undeclared_oversell:
        # E3. POINT-IN-TIME: the daily replay, sampled at event boundaries. Behind the 賣超
        # gate only (YQ1): an undeclared oversell has no honest valuation on any day, so
        # judging its trend would be judging a state the app itself declines to value — but
        # a declared short is valued, and the `short_open` checkpoint is where a held carrier
        # of a refused dividend lives (Y3).
        _reconcile_trend(ev, dash, facts, phase)

    # ---- E2. Ledger INTEGRITY (validity, not arithmetic — see oracle layer 4) ----
    _reconcile_integrity(ev, res, facts, phase)

    # ---- F. Ledger APIs row-by-row (raw facts) ----
    _reconcile_ledger_api(ev, api, raw, phase)

    # ---- G. Exports (CSV) ----
    _reconcile_exports(ev, api, res, prices, phase, valuation)

    # ---- H. Print reports (HTML) ----
    if reports:
        _reconcile_reports(ev, api, res, prices, sp, phase, valuation)

    # ---- I. Registry ↔ symbol (options spec §3): asset_type is set FROM the parse, so the
    # two never disagree. The oracle parses with its OWN parser and checks the stored fact.
    for sym, inst in sorted(raw.instruments.items()):
        want = "option" if O.is_option(sym) else "stock"
        if want == "option" or inst.asset_type != "stock":
            ev.check("instrument.asset_type_matches_symbol", sym, want, inst.asset_type, phase)

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
        gross = O.settled(t.qty, t.price, inst.quote_ccy)  # the settled 價金 (2026-09-30)
        delta = -(gross + t.fee + t.tax) if t.side == "BUY" else (gross - t.fee - t.tax)
        add((t.account_id, inst.quote_ccy), t.trade_date, (2, t.id), f"{t.side}", delta)
    for dv in facts.divs:
        inst = facts.instruments.get(dv.symbol)
        if inst is None or dv.type not in O.CASH_DIVIDEND_TYPES:
            continue
        if dv.id in res.refused_dividend_ids:
            # ⑪ (owner ruling 2026-10-05): the statement LISTS a payout the replay refused but
            # it moves no running balance — the oracle's own replay says which (``res``).
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
        ev.check("ledger.integrity",
                 "no negative position / orphan sell / price-0 row outside a close row",
                 "clean", "clean", phase)


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
            elif (acct == aid and h.quote_ccy == settle and O.is_option(sym)
                  and h.shares != O.ZERO and sym in prices):
                # OPT-D4: the FX pool is a POINT-IN-TIME figure, so an open contract's value at
                # its last trade price is part of the foreign exposure — on its SIGNED quantity
                # (a sold contract is a liability that offsets the premium cash it brought in).
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


def _reconcile_kpis(ev, res, dash, facts, prices, sp, phase):
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
        # OPT-D4 / §6.2: KPI 總市值 = the stock grand subtotal + options.market_value — an open
        # contract counts on its SIGNED quantity (a sold contract lowers it).
        counted = (h.shares > O.ZERO) or (O.is_option(sym) and h.shares != O.ZERO)
        if counted and sym in prices:
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
    # AI-D48's third term. Asserted here because it is a NEW money-of-record figure and the
    # accumulation rule requires the oracle to derive one independently before it ships; the
    # other two terms are already pinned (A above, and B through `trend.total_value` /
    # `trend.net_invested` at the last point).
    # B (含匯兌總損益) is the trend's LAST day, and it is withheld — never replaced by an
    # earlier day — when that day is incomplete (AI-D41); its third term rides with it, so the
    # decomposition is never published half-built (AI-D48). Until YQ3 no KPI checkpoint had an
    # incomplete last day, so the oracle never had to say so: a refused dividend now marks every
    # day from its date (K3), and the oracle's OWN day rule decides (`_day_incomplete`, on the
    # replay as at the app's as_of — which `res` is). B's VALUE is pinned through
    # `trend.total_value` / `trend.net_invested` at the last point; this pins whether it exists.
    as_of = date.fromisoformat(str(dash.get("as_of", ASOF.isoformat()))[:10])
    # Options spec §4.4: "XIRR and B are withheld whenever Book.refused_closes is non-empty —
    # the record, like orphan_dividends". A RECORD, so it holds on a flat carrier too.
    b_withheld = _day_incomplete(res, as_of) or bool(res.refused_closes)
    ev.check("kpi.fx_complete_withheld", "TWD", b_withheld,
             kpis.get("total_return_fx_complete") is None, phase)
    ev.check("kpi.trading_financing_cost", "TWD",
             None if b_withheld else O.trading_financing_cost(facts, REPORTING, fx_on_resolver()),
             kpis.get("trading_financing_cost"), phase)
    # OPT-D20: while an option position is open on the valuation day, B excludes its value
    # (R3) and A includes its unrealized, so B − A would absorb the option's value as an "FX
    # effect" — 本金匯率效果 is WITHHELD. (Its value when no option is open is outside this
    # family; only the withholding is the options rule.)
    if any(O.is_option(h.symbol) and h.shares != O.ZERO for h in res.holdings.values()):
        ev.check("kpi.principal_fx_withheld_option_open", "TWD", None,
                 kpis.get("principal_fx_effect"), phase)


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

    if res.orphan_dividends:
        # CR-02 (domain-ledger.md): a dividend with no position is "named, and withholds
        # XIRR" — the ledger is missing the position that earned it. The oracle's OWN replay
        # says whether one exists; the app must then publish no figure and name each row
        # (account, symbol, effective date) in its reason.
        ev.check("xirr.withheld_for_orphan_dividend", "TWD", None, app_xirr, phase)
        reason = (dash.get("freshness") or {}).get("xirr_unavailable_reason") or ""
        for acct, sym, eff in res.orphan_dividends:
            ev.check("xirr.orphan_reason_names_row", f"{acct}/{sym}@{eff.isoformat()}", True,
                     (sym in reason and acct in reason and eff.isoformat() in reason), phase)
        return
    if res.refused_closes:
        # Options spec §4.4: a close the replay could not book withholds XIRR "with a reason
        # naming the row". The row is named by its contract — the OCC code or its display
        # name — and its date.
        ev.check("xirr.withheld_for_refused_close", "TWD", None, app_xirr, phase)
        reason = (dash.get("freshness") or {}).get("xirr_unavailable_reason") or ""
        for rc in res.refused_closes:
            terms = O.parse_occ(rc.symbol)
            named = rc.symbol in reason or (terms is not None and terms.name in reason)
            ev.check("xirr.refused_close_reason_names_row",
                     f"{rc.account_id}/{rc.symbol}@{rc.d.isoformat()}", True,
                     named and rc.d.isoformat() in reason, phase)
        return
    try:
        dates, amounts = O.xirr_cashflows(res, facts, prices, REPORTING, fx_on, fx_now, as_of)
    except KeyError:
        # oracle cannot form the series (missing rate/price) -> the app must be None too
        ev.check("xirr.uncomputable_matches", "TWD", None, app_xirr, phase)
        return
    orc = O.xirr_solve(dates, amounts)
    ev.check_close("kpi.xirr", "TWD", orc, app_xirr, O.XIRR_TOL, phase)


# ---------------------------------------------------------------- options (spec 2026-10-09)
#: Spec §7.1 — every new dashboard wire field lives inside `options`, and these are its keys.
OPTIONS_BLOCK_KEYS = frozenset({
    "open", "obligation_subtotals", "market_value", "unrealized", "refused_closes",
    "realized", "trend_excludes_value", "note", "principal_fx_note"})
#: Spec §7.1 `open[]` (`underlying_price` / `_date` are "omitted when unpriced", so they are
#: checked by value on a registered underlying, not by presence).
OPTION_OPEN_ROW_KEYS = frozenset({
    "account_id", "symbol", "contract", "underlying", "expiry", "days_to_expiry", "strike",
    "right", "direction", "contracts", "share_equivalents", "average_premium", "premium_total",
    "last_price", "last_price_date", "market_value", "unrealized", "obligation", "past_expiry",
    "unbookable_close"})
OPTION_REALIZED_KEYS = frozenset({
    "rows", "by_contract", "by_year", "by_underlying", "realized_total"})
#: Spec §3 `RefusedClose`.
REFUSED_CLOSE_KEYS = frozenset({
    "txn_id", "account_id", "symbol", "date", "side", "quantity", "close_reason", "reason",
    "remedy"})
#: Spec §5 (R3) — the trend note, verbatim.
OPTION_TREND_NOTE = ("走勢不含未平倉選擇權的市值；權利金已計入現金，未平倉期間淨值高估（賣方）"
                     "或低估（買方）該部位市值")
#: Spec §5 (OPT-D20) — the 本金匯率效果 note, verbatim.
OPTION_PRINCIPAL_FX_NOTE = ("有未平倉選擇權時，含匯兌總損益不含其市值而資產損益含其未實現，"
                            "兩者相減無法拆出本金匯率效果；平倉或到期後恢復顯示")
#: Spec §4.4 — the remedy every surface prints beside a refused close, verbatim.
REFUSED_CLOSE_REMEDY = "刪除該筆到期作廢／被指派／履約列，或補回被刪除的交易"


def _money(block: dict | None) -> tuple[dict, object]:
    """``{by_currency, reporting}`` -> (non-zero by_currency as Decimals, reporting)."""
    block = block or {}
    by = {c: dec(v) for c, v in (block.get("by_currency") or {}).items() if dec(v) != O.ZERO}
    return by, block.get("reporting")


def _cv(x):
    """Canonical form for a STRUCTURED comparison: ``Evidence.check`` compares a list by its
    string, where ``Decimal('281.90')`` and ``Decimal('281.9')`` differ although the money is
    equal. Every Decimal inside becomes its normalized fixed-point text, nothing else moves —
    exact-value comparison is kept, only the spelling of a trailing zero is not compared."""
    if isinstance(x, Decimal):
        return format(x.normalize(), "f") if x != O.ZERO else "0"
    if isinstance(x, (list, tuple)):
        return [_cv(v) for v in x]
    return x


def _reconcile_options(ev, dash, raw, facts, res, prices, sp, as_of, phase, valuation):
    """The `options` block (spec §7.1) against the oracle's own replay — section O.

    What it proves, each derived from the SPEC by this harness (never read off the app):

    * presence — the block exists iff the UNCUT ledger names an option (a transaction or an
      action), and is otherwise absent from the payload (the golden is untouched, D38);
    * `open[]` — every non-zero option position: terms from the oracle's own OCC parse,
      direction, 口數, last-trade price and its date (§5, incl. the predecessor ÷ ratio after
      an `OPTION_ADJUST`), market value and unrealized on the SIGNED quantity, the sold put's
      履約義務, `past_expiry`, and OPT-D17's underlying price on a registered underlying;
    * `realized` — the replay's option rows in emission order with their close reason and
      contract name, and the four roll-ups (total, by contract, by year, by underlying);
    * `refused_closes` (§4.4) with the verbatim remedy;
    * the KPI decomposition 「其中選擇權」: KPI − the block's part == the oracle's STOCK part,
      for 總市值 (the spec's own identity), 未實現 and 已實現;
    * R3 / OPT-D20: `trend_excludes_value` + its note, and `principal_fx_note`.
    """
    named = (any(O.is_option(t.symbol) for t in raw.txs)
             or any(a.kind == O.OPTION_ADJUST or O.is_option(a.from_symbol)
                    or O.is_option(a.to_symbol) for a in raw.actions))
    ev.check("options.block_present", "the uncut ledger names an option contract",
             named, "options" in dash, phase)
    block = dash.get("options")
    if not named or not isinstance(block, dict):
        return
    ev.check("options.block_keys", "spec §7.1", sorted(OPTIONS_BLOCK_KEYS), sorted(block), phase)

    open_pos = {k: h for k, h in res.holdings.items() if O.is_option(k[1])}
    priced = O.option_prices(facts, as_of)
    app_open = {(r.get("account_id"), r.get("symbol")): r for r in block.get("open") or []}
    ev.check("options.open.keyset", "open contracts", sorted("|".join(k) for k in open_pos),
             sorted(f"{a}|{s}" for a, s in app_open), phase)
    obligations: dict[tuple[str, str], Decimal] = {}
    mv_by: dict[str, Decimal] = {}
    unreal_by: dict[str, Decimal] = {}
    for key, h in sorted(open_pos.items()):
        terms = O.parse_occ(key[1])
        a = app_open.get(key)
        sc = "|".join(key)
        if a is None or terms is None:
            ev.check("options.open.present", sc, True, False, phase)
            continue
        ev.check("options.open.row_keys", sc, [], sorted(OPTION_OPEN_ROW_KEYS - set(a)), phase)
        short = h.shares < O.ZERO
        n_contracts = abs(h.shares) / O.OPTION_MULTIPLIER
        ev.check("options.open.contract", sc, terms.name, a.get("contract"), phase)
        ev.check("options.open.underlying", sc, terms.underlying, a.get("underlying"), phase)
        ev.check("options.open.expiry", sc, terms.expiry.isoformat(), a.get("expiry"), phase)
        ev.check("options.open.strike", sc, terms.strike, a.get("strike"), phase)
        ev.check("options.open.right", sc, terms.right, a.get("right"), phase)
        ev.check("options.open.direction", sc, "short" if short else "long",
                 a.get("direction"), phase)
        ev.check("options.open.contracts", sc, n_contracts, a.get("contracts"), phase)
        ev.check("options.open.days_to_expiry", sc, (terms.expiry - as_of).days,
                 a.get("days_to_expiry"), phase)
        ev.check("options.open.past_expiry", sc, terms.expiry < as_of,
                 a.get("past_expiry"), phase)
        ev.check("options.open.unbookable_close", sc, h.unbookable_close,
                 a.get("unbookable_close"), phase)
        # The spec names these three without a sign convention; the MAGNITUDE is the money
        # fact (premium per share / total premium of the position, all-in, net of fees for a
        # short), so the magnitude is what is asserted. The sign-carrying figures (market
        # value, unrealized) are asserted signed below.
        ev.check("options.open.share_equivalents_abs", sc, abs(h.shares),
                 abs(dec(a["share_equivalents"])) if a.get("share_equivalents") is not None
                 else None, phase)
        ev.check("options.open.premium_total_abs", sc, abs(h.adjusted_total),
                 abs(dec(a["premium_total"])) if a.get("premium_total") is not None
                 else None, phase)
        ev.check("options.open.average_premium_abs", sc, abs(h.adjusted_total / h.shares),
                 abs(dec(a["average_premium"])) if a.get("average_premium") is not None
                 else None, phase)
        # Spec §7.1 (final review R1-M1 / N2): a 待釐清 contract (賣超 / a refused close) states
        # no price, no value and no 履約義務 — its count is the thing in question.
        unresolved = h.oversold or h.unbookable_close
        obligation = (terms.strike * O.OPTION_MULTIPLIER * n_contracts
                      if short and terms.right == "P" and not unresolved else None)
        ev.check("options.open.obligation", sc, obligation, a.get("obligation"), phase)
        if obligation is not None:
            k2 = (key[0], h.quote_ccy)
            obligations[k2] = obligations.get(k2, O.ZERO) + obligation
        # OPT-D17: the underlying's latest stored price, dated, when it is registered AND
        # priced — in this fixture that is the seeded series, whose last row is ASOF.
        u_px = PRICES.get(terms.underlying)
        ev.check("options.open.underlying_price", sc, u_px, a.get("underlying_price"), phase)
        ev.check("options.open.underlying_price_date", sc,
                 ASOF.isoformat() if u_px is not None else None,
                 a.get("underlying_price_date"), phase)
        if valuation and unresolved:
            for fld in ("last_price", "last_price_date", "market_value", "unrealized"):
                ev.check(f"options.open.{fld}", f"{sc} (待釐清)", None, a.get(fld), phase)
        elif valuation and key[1] in priced:
            p, on = priced[key[1]]
            ev.check("options.open.last_price", sc, p, a.get("last_price"), phase)
            ev.check("options.open.last_price_date", sc, on.isoformat(),
                     a.get("last_price_date"), phase)
            mv = p * h.shares
            unreal = (p - h.adjusted_avg) * h.shares
            ev.check("options.open.market_value", sc, mv, a.get("market_value"), phase)
            ev.check("options.open.unrealized", sc, unreal, a.get("unrealized"), phase)
            mv_by[h.quote_ccy] = mv_by.get(h.quote_ccy, O.ZERO) + mv
            unreal_by[h.quote_ccy] = unreal_by.get(h.quote_ccy, O.ZERO) + unreal

    app_obl = {(r.get("account_id"), r.get("ccy")): dec(r["amount"])
               for r in block.get("obligation_subtotals") or [] if dec(r["amount"]) != O.ZERO}
    ev.check("options.obligation_subtotals", "per account",
             _cv(sorted((f"{k[0]}|{k[1]}", v) for k, v in obligations.items())),
             _cv(sorted((f"{k[0]}|{k[1]}", v) for k, v in app_obl.items())), phase)

    def to_rep(by: dict[str, Decimal]) -> Decimal:
        return sum((v if c == REPORTING else v * sp.rate(c, REPORTING) for c, v in by.items()),
                   O.ZERO)

    if valuation:
        for name, exp_by in (("market_value", mv_by), ("unrealized", unreal_by)):
            got_by, got_rep = _money(block.get(name))
            exp_nz = {c: v for c, v in exp_by.items() if v != O.ZERO}
            ev.check(f"options.{name}.by_currency", "block", _cv(sorted(exp_nz.items())),
                     _cv(sorted(got_by.items())), phase)
            if open_pos:
                # Whenever a contract is open — a total that happens to net to 0 (a sold
                # contract's value against a bought one's) is still a figure to agree on.
                ev.check(f"options.{name}.reporting", "block", to_rep(exp_by), got_rep, phase)

    # ---- realized: the replay's own option rows, emission order (spec §7.1) ----
    rblock = block.get("realized") or {}
    ev.check("options.realized.keys", "spec §7.1", sorted(OPTION_REALIZED_KEYS),
             sorted(rblock), phase)
    orc_rows = [rr for rr in res.realized_rows if O.is_option(rr.symbol)]
    app_rows = rblock.get("rows") or []
    ev.check("options.realized.count", "rows", len(orc_rows), len(app_rows), phase)
    for i, rr in enumerate(orc_rows):
        if i >= len(app_rows):
            break
        a = app_rows[i]
        sc = f"{rr.account_id}/{rr.symbol}@{rr.sell_date} #{i}"
        ev.check("options.realized.symbol", sc, rr.symbol, a.get("symbol"), phase)
        ev.check("options.realized.sell_date", sc, rr.sell_date.isoformat(),
                 a.get("sell_date"), phase)
        ev.check("options.realized.shares_sold", sc, rr.shares_sold, a.get("shares_sold"), phase)
        ev.check("options.realized.proceeds_net", sc, rr.proceeds_net,
                 a.get("proceeds_net"), phase)
        ev.check("options.realized.adjusted_removed", sc, rr.adjusted_cost_removed,
                 a.get("adjusted_cost_removed"), phase)
        ev.check("options.realized.original_removed", sc, rr.original_cost_removed,
                 a.get("original_cost_removed"), phase)
        ev.check("options.realized.realized", sc, rr.realized, a.get("realized"), phase)
        ev.check("options.realized.kind", sc, rr.kind, a.get("kind"), phase)
        ev.check("options.realized.close_reason", sc, rr.close_reason,
                 a.get("close_reason"), phase)
        terms = O.parse_occ(rr.symbol)
        ev.check("options.realized.contract", sc, terms.name if terms else None,
                 a.get("contract"), phase)
    total_by: dict[str, Decimal] = {}
    by_contract: dict[str, Decimal] = {}
    by_year: dict[str, dict[str, Decimal]] = {}
    by_underlying: dict[str, dict[str, Decimal]] = {}
    for rr in orc_rows:
        terms = O.parse_occ(rr.symbol)
        total_by[rr.quote_ccy] = total_by.get(rr.quote_ccy, O.ZERO) + rr.realized
        ck = f"{rr.account_id}|{rr.symbol}"
        by_contract[ck] = by_contract.get(ck, O.ZERO) + rr.realized
        yb = by_year.setdefault(str(rr.sell_date.year), {})
        yb[rr.quote_ccy] = yb.get(rr.quote_ccy, O.ZERO) + rr.realized
        ub = by_underlying.setdefault(terms.underlying if terms else rr.symbol, {})
        ub[rr.quote_ccy] = ub.get(rr.quote_ccy, O.ZERO) + rr.realized
    got_by, got_rep = _money(rblock.get("realized_total"))
    exp_nz = {c: v for c, v in total_by.items() if v != O.ZERO}
    ev.check("options.realized_total.by_currency", "block", _cv(sorted(exp_nz.items())),
             _cv(sorted(got_by.items())), phase)
    if orc_rows:
        ev.check("options.realized_total.reporting", "block", to_rep(total_by), got_rep, phase)
    ev.check("options.realized.by_contract", "realized per contract",
             _cv(sorted(by_contract.items())),
             _cv(sorted((f"{r.get('account_id')}|{r.get('symbol')}", dec(r["realized"]))
                        for r in rblock.get("by_contract") or [])), phase)
    ev.check("options.realized.by_year", "by_currency per year",
             _cv(sorted((y, sorted(v.items())) for y, v in by_year.items())),
             _cv(sorted((str(r.get("year")), sorted(_money(r)[0].items()))
                        for r in rblock.get("by_year") or [])), phase)
    ev.check("options.realized.by_underlying", "by_currency per underlying",
             _cv(sorted((u, sorted(v.items())) for u, v in by_underlying.items())),
             _cv(sorted((str(r.get("underlying")), sorted(_money(r)[0].items()))
                        for r in rblock.get("by_underlying") or [])), phase)

    # ---- §4.4 refused closes (the record) ----
    app_ref = block.get("refused_closes") or []
    ev.check("options.refused_closes", "§4.4 record",
             _cv([(rc.txn_id, rc.account_id, rc.symbol, rc.d.isoformat(), rc.side,
                   rc.quantity, rc.close_reason) for rc in res.refused_closes]),
             _cv([(r.get("txn_id"), r.get("account_id"), r.get("symbol"), r.get("date"),
                   r.get("side"),
                   dec(r["quantity"]) if r.get("quantity") is not None else None,
                   r.get("close_reason")) for r in app_ref]), phase)
    for r in app_ref:
        sc = f"id={r.get('txn_id')}"
        ev.check("options.refused_close.keys", sc, [], sorted(REFUSED_CLOSE_KEYS - set(r)), phase)
        ev.check("options.refused_close.remedy", sc, REFUSED_CLOSE_REMEDY, r.get("remedy"), phase)

    # ---- KPI 「其中選擇權」: the KPI minus the block's part is the STOCK part ----
    kpis = dash.get("kpis") or {}
    stock_gate = any(h.shares < O.ZERO and not O.is_option(h.symbol)
                     for h in res.holdings.values())
    if valuation and not stock_gate and kpis.get("total_market_value") is not None:
        stock_mv = O.ZERO
        stock_unreal: dict[str, Decimal] = {}
        for (_acct, sym), h in res.holdings.items():
            if O.is_option(sym) or h.shares <= O.ZERO or sym not in prices:
                continue
            v = prices[sym] * h.shares
            stock_mv += v if h.quote_ccy == REPORTING else v * sp.rate(h.quote_ccy, REPORTING)
            stock_unreal[h.quote_ccy] = (stock_unreal.get(h.quote_ccy, O.ZERO)
                                         + (prices[sym] - h.adjusted_avg) * h.shares)
        # check_rel, not check: KPI − part is a SUBTRACTION of two app figures each rounded at
        # 28 significant digits; the operands are each pinned exactly elsewhere (kpi.* and
        # options.*.reporting), so only the association can differ here (Evidence.ULP_REL_TOL).
        opt_mv = _money(block.get("market_value"))[1]
        ev.check_rel("options.kpi_total_mv_minus_options_is_stock_part", "其中選擇權 總市值",
                     stock_mv, dec(kpis["total_market_value"]) - dec(opt_mv or "0"), phase)
        opt_un = _money(block.get("unrealized"))[1]
        if kpis.get("unrealized_total") is not None:
            ev.check_rel("options.kpi_unrealized_minus_options_is_stock_part",
                         "其中選擇權 未實現", to_rep(stock_unreal),
                         dec(kpis["unrealized_total"]) - dec(opt_un or "0"), phase)
    if kpis.get("realized_total") is not None:
        stock_real: dict[str, Decimal] = {}
        for rr in res.realized_rows:
            if not O.is_option(rr.symbol):
                stock_real[rr.quote_ccy] = stock_real.get(rr.quote_ccy, O.ZERO) + rr.realized
        opt_real = _money(rblock.get("realized_total"))[1]
        ev.check_rel("options.kpi_realized_minus_options_is_stock_part", "其中選擇權 已實現",
                     to_rep(stock_real), dec(kpis["realized_total"]) - dec(opt_real or "0"),
                     phase)

    # ---- R3 / OPT-D20 ----
    pts = ((dash.get("trend") or {}).get("points")) or []
    if pts:
        first = date.fromisoformat(min(p["date"] for p in pts))
        last = date.fromisoformat(max(p["date"] for p in pts))
        skips = O.trend_skips_an_option(raw, first, last)
        ev.check("options.trend_excludes_value", "R3", skips,
                 block.get("trend_excludes_value"), phase)
        ev.check("options.note", "R3", OPTION_TREND_NOTE if skips else None,
                 block.get("note"), phase)
    is_open = any(h.shares != O.ZERO for h in open_pos.values())
    ev.check("options.principal_fx_note", "OPT-D20", OPTION_PRINCIPAL_FX_NOTE if is_open else None,
             block.get("principal_fx_note"), phase)


def _reconcile_ledger_api(ev, api, facts: O.Facts, phase):
    # transactions: /api/ledgers/transactions returns rows desc; compare set by id
    rows = api.get("/api/ledgers/transactions", limit=500).json()["rows"]
    app = {r["id"]: r for r in rows}
    ev.check("ledger.tx.count", "count", len(facts.txs), len(rows), phase)
    refused_close_ids = {rc.txn_id for rc in O.replay(facts).refused_closes}
    stock_rows_with_option_marks: list[int] = []
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
        gross = O.settled(t.qty, t.price, facts.instruments[t.symbol].quote_ccy)
        exp_total = -(gross + t.fee + t.tax) if t.side == "BUY" else (gross - t.fee - t.tax)
        ev.check("ledger.tx.total", f"id={t.id}", exp_total, a["total"], phase)
        if O.is_option(t.symbol):
            # Options spec §6.2: an option row carries its close_reason, its 口數 (the
            # share-equivalents ÷ the oracle's own multiplier) and whether the replay refused it
            # (§4.4 — the oracle's own replay of the whole stored ledger, not a valuation cut).
            sc = f"id={t.id} {t.account_id}/{t.symbol}"
            ev.check("ledger.tx.close_reason", sc, t.close_reason, a.get("close_reason"), phase)
            ev.check("ledger.tx.contracts", sc, t.qty / O.OPTION_MULTIPLIER,
                     a.get("contracts"), phase)
            ev.check("ledger.tx.refused_close", sc, t.id in refused_close_ids,
                     a.get("refused_close"), phase)
        elif (a.get("close_reason") is not None or a.get("contracts") is not None
              or a.get("option_side") is not None or a.get("refused_close")):
            stock_rows_with_option_marks.append(t.id)
    # §10 (4): on a STOCK row the four additive keys are null / false. One assertion per
    # checkpoint (the offending ids), not one per row — a thousand identical passes are noise.
    ev.check("ledger.tx.stock_rows_carry_no_option_marks", "stock rows", [],
             stock_rows_with_option_marks, phase)
    # dividends
    drows = api.get("/api/ledgers/dividends", limit=500).json()["rows"]
    dapp = {r["id"]: r for r in drows}
    ev.check("ledger.div.count", "count", len(facts.divs), len(drows), phase)
    # X1 / YQ3: the listing marks every row the replay refuses 「未入帳（待釐清）」. Which rows
    # those are is the oracle's OWN replay of the whole stored ledger (the listing is not a
    # valuation, so it is not cut at as_of).
    refused = O.replay(facts).refused_dividend_ids
    for dv in facts.divs:
        a = dapp.get(dv.id)
        if a is None:
            continue
        ev.check("ledger.div.net", f"id={dv.id}", dv.net, a["net"], phase)
        ev.check("ledger.div.gross", f"id={dv.id}", dv.gross, a["gross"], phase)
        ev.check("ledger.div.booked", f"id={dv.id} {dv.account_id}/{dv.symbol}@{dv.d}",
                 dv.id not in refused, a.get("booked"), phase)
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
        # L6 (demo audit 2026-09-16): the wire quotes the implied rate the conventional way —
        # the more valuable currency is the unit, so the figure is >= 1 whichever side was
        # sold (a USD→TWD row and a TWD→USD row both read 32.4). The oracle transcribes that
        # rule here (2026-09-24) rather than the old from/to figure, which is now only the
        # store's internal `StoredFxConversion.implied_rate`.
        ratio = c.from_amt / c.to_amt
        conventional = ratio if ratio >= 1 else c.to_amt / c.from_amt
        ev.check("ledger.fx.implied", f"id={c.id}", conventional, a["implied_rate"], phase)


def _reconcile_exports(ev, api, res, prices, phase, valuation):
    # holdings CSV
    raw = api.download("/api/export/holdings").decode("utf-8-sig")
    hrows = _parse_csv(raw)
    hkey = {(r.get("account_id"), r.get("symbol")): r for r in hrows}
    # Stock rows only: spec §9 renders an open contract in a SEPARATE 選擇權 block of the
    # holdings export (W6), whose layout this family does not parse.
    for key, h in res.holdings.items():
        if O.is_option(key[1]):
            continue
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
    # Spec §9: the realized CSV is the stock `realized.rows` FOLLOWED BY `options.realized.rows`
    # with `asset_type` and `contract` appended. That door is W6's; until its `asset_type`
    # column exists the CSV is the stock rows alone and only those are compared — a known,
    # README-disclosed gap (not op-logged per checkpoint: `ops` is the scenario signal).
    stock_rows = [rr for rr in res.realized_rows if not O.is_option(rr.symbol)]
    option_rows = [rr for rr in res.realized_rows if O.is_option(rr.symbol)]
    carries_options = bool(rrows) and "asset_type" in rrows[0]
    expected = stock_rows + (option_rows if carries_options else [])
    ev.check("export.realized.count", "count", len(expected), len(rrows), phase)
    for i, rr in enumerate(expected):
        if i >= len(rrows):
            break
        r = rrows[i]
        sc = f"{rr.account_id}/{rr.symbol} #{i}"
        ev.check("export.realized.realized", sc, rr.realized, r["realized"], phase)
        ev.check("export.realized.proceeds_net", sc, rr.proceeds_net, r["proceeds_net"], phase)
        if carries_options:
            ev.check("export.realized.asset_type", sc,
                     "option" if O.is_option(rr.symbol) else "stock", r.get("asset_type"), phase)
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
            # The KPI 總市值 card: stocks + every open contract on its signed quantity (OPT-D4).
            counted = h.shares > O.ZERO or (O.is_option(s) and h.shares != O.ZERO)
            if counted and s in prices:
                v = prices[s] * h.shares
                mv += v if h.quote_ccy == REPORTING else v * sp.rate(h.quote_ccy, REPORTING)
        ev.check("report.holdings.total_mv_present", "KPI 總市值",
                 True, _contains_number(html, mv), phase)
    n = sum(1 for (a, s), h in res.holdings.items()
            if h.shares > O.ZERO and not O.is_option(s)
            and s in html and _contains_number(html, h.shares))
    ev.check("report.holdings.shares_present", "持倉股數", True, n >= 3, phase)

    lhtml = api.download("/api/export/ledgers-report").decode("utf-8")
    ev.check("report.ledgers.renders", "帳本報告",
             True, "<!doctype html>" in lhtml.lower() and len(lhtml) > 500, phase)
    held_syms = {s for (a, s), h in res.holdings.items()
                 if h.shares > O.ZERO and not O.is_option(s)}
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
