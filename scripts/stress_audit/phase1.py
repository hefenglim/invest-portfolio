"""Phase 1 — clean-room stress + reconciliation on a local isolated server.

Fresh DB, scheduler disabled. A deterministic, dated (Jan-Jun 2026) scenario of 70+
operations across the three money flows, reconciled against the app's computed outputs
by the independent oracle (absolute assertions from zero).

Trade-date FX is seeded at the scenario start (2026-01-01) AND at the valuation as-of
(2026-07-14), so the app resolves each flow at its on-or-before rate and the terminal
value at spot — exercising the FX-aware reporting XIRR for real (the one tolerance check).

The settlement store is ON (PERF spec 2026-10-18 §18 "Stress audit"): every checkpoint first
asserts what the store's status says about the writes since the last settlement (the dirty
day, the stale range, the live window — BEFORE a run), then starts a settlement and waits for
it, so every family below reads a settled store. The month-end closes (``/api/settlement/
periods`` and the past-month ``/api/snapshots`` rows) are reconciled against the oracle's own
"as at D" values (``oracle.period_end``, spec §11.2).
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

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
    # ---- Month-end closes (PERF spec 2026-10-18 §11.2 / §18). DEDICATED symbols whose closes
    # are a FIXED, DRIFTING weekday series (``MONTH_END_SERIES``), never re-derived from the
    # actions the way ``_ensure_daily_prices`` derives the PRICES symbols: a split must be
    # entered AGAINST a price history that already carries it, or "entering the split leaves
    # the month-end value unchanged" would be the fixture agreeing with itself. The series
    # drift every day, so a close read off the wrong date moves a money figure.
    #   SPA — 2-for-1 dated Mon 2026-06-01, the first trading day after month end 2026-05-31;
    #   SPB — 2-for-1 dated Sun 2026-05-31 — ON the month end, which is a weekend D, so the
    #         close is Friday's and the READ must re-express it (the edge §18 names);
    #   MPX — held across month end 2026-02-28 with no close stored until 2026-03-02: the
    #         "held stock with no such price" row (missing price, XIRR withheld).
    ("SPA", "TW", "TWD", "Month-end Split After D", "Industrials", False),
    ("SPB", "TW", "TWD", "Month-end Split On D", "Industrials", False),
    ("MPX", "TW", "TWD", "Month-end Missing Price", "Industrials", False),
    #   STL — held across two month ends with a gap in its closes before each: 6 days old at
    #         2026-04-30 (stale) and EXACTLY 4 days old at 2026-05-31 (not stale — the live
    #         rule is "more than 4 days"), so the staleness boundary itself is pinned.
    ("STL", "TW", "TWD", "Month-end Stale Price", "Industrials", False),
    #   SPD — 2-for-1 dated Tue 2026-03-31, a TRADING-DAY month end with its own (post-split)
    #         close, so nothing is carried and nothing re-expressed;
    #   SPE — held in TWO accounts (schwab, moomoo_my), its 2-for-1 dated Sat 2026-02-28 (a
    #         weekend month end) recorded once per account — 2:1 and 4:2. One market event:
    #         Friday's close is re-expressed ONCE (the reduced-ratio dedupe), never twice.
    ("SPD", "US", "USD", "Month-end Split On Trading D", "Technology", False),
    ("SPE", "US", "USD", "Month-end Split Two Accounts", "Technology", False),
    # ---- Prior year (PERF §18 "Period closes": a CLOSED year — Y 2025 ≡ December 2025 and
    # Σ months ≡ year ≡ the 2025 tax package). TWD only: 2025 has no FX row on purpose.
    ("YRA", "TW", "TWD", "Prior Year Holding", "Industrials", False),
    # ---- A 賣超 netted back to EXACTLY zero (CR-11): flat, but the book records it, so XIRR
    # stays withheld with the row named until the oversell is resolved.
    ("OVF", "TW", "TWD", "Flat Oversell", "Industrials", False),
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
    # The flat-oversell symbol (CR-11). Held again after the oversell is resolved.
    "OVF": D("33.30"),
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

#: Where every fixture close series begins — fixed, never derived from the ledger, so the
#: harness always knows exactly which rows it has written (``fixture_closes``). The prior-year
#: block's first row is on this day.
PRICE_SERIES_START = date(2025, 11, 3)

#: The daily drift of a :data:`MONTH_END_SERIES` close, per calendar day.
SERIES_DRIFT = D("0.005")

#: The month-end symbols' FIXED close series (see INSTRUMENTS):
#: ``(market, first close, price on the first day, split date, split ratio (to, from), gaps)``.
#: Weekdays only, through ASOF, minus the ``gaps`` (inclusive date ranges with no close — a
#: suspension, a data hole). The underlying value DRIFTS by :data:`SERIES_DRIFT` a calendar day;
#: from the split date on, the close is AS TRADED in post-split terms (the value ÷ the ratio),
#: so a position's value never jumps across the split. Every value is exact at 4 dp — the
#: storage precision (data-and-pricing.md) — so the stored close IS the fixture's value, and a
#: cap at fewer places (the M6 mutation) shows. Each row is stamped ``fetched_at`` = its own
#: date (``C.seed_prices_daily``), so neither the write window nor a reconcile re-expresses it.
MONTH_END_SERIES: dict[str, tuple[str, date, Decimal, date | None, tuple[int, int],
                                   tuple[tuple[date, date], ...]]] = {
    # May close (Fri 05-29, 144 days in): 100.72. 06-01 (147 days): 100.735 / 2 = 50.3675.
    "SPA": ("TW", date(2026, 1, 5), D("100"), date(2026, 6, 1), (2, 1), ()),
    # Fri 05-29: 80.72 — at D = Sun 05-31 (the split's own day) read as 80.72 / 2 = 40.36.
    "SPB": ("TW", date(2026, 1, 5), D("80"), date(2026, 5, 31), (2, 1), ()),
    "MPX": ("TW", date(2026, 3, 2), D("52"), None, (1, 1), ()),
    # Last close before Thu 2026-04-30 is Fri 04-24 (6 days: stale); before Sun 2026-05-31 it
    # is Wed 05-27 (4 days: NOT stale).
    "STL": ("TW", date(2026, 3, 2), D("60"), None, (1, 1),
            ((date(2026, 4, 27), date(2026, 4, 30)), (date(2026, 5, 28), date(2026, 5, 29)))),
    # Tue 03-31 (29 days in): 60.145 / 2 = 30.0725 — the close of D itself, post-split.
    "SPD": ("US", date(2026, 3, 2), D("60"), date(2026, 3, 31), (2, 1), ()),
    # Fri 02-27 (53 days in): 40.265 — at D = Sat 02-28 read once as 40.265 / 2 = 20.1325.
    "SPE": ("US", date(2026, 1, 5), D("40"), date(2026, 2, 28), (2, 1), ()),
    # Prior year: Fri 2025-11-28 (25 days in) 120.125; Wed 2025-12-31 (58 days in) 120.29.
    "YRA": ("TW", date(2025, 11, 3), D("120"), None, (1, 1), ()),
}


def month_end_series(symbol: str) -> dict[date, Decimal]:
    """The weekday closes of a :data:`MONTH_END_SERIES` symbol, ``{date: close}``."""
    _market, first, p0, split_on, (to, frm), gaps = MONTH_END_SERIES[symbol]
    out: dict[date, Decimal] = {}
    day = first
    while day <= ASOF:
        if day.weekday() < 5 and not any(lo <= day <= hi for lo, hi in gaps):
            value = p0 + SERIES_DRIFT * (day - first).days
            if split_on is not None and day >= split_on:
                value = value * frm / to
            out[day] = value.quantize(D("0.0001"), rounding=ROUND_HALF_UP)
        day += timedelta(days=1)
    return out


#: The :data:`MONTH_END_SERIES` symbols whose series the scenario has written so far — the
#: fixture's own record of what it put in the prices table.
SEEDED_SERIES: set[str] = set()


def fixture_closes(facts) -> O.Closes:
    """The closes the FIXTURE wrote, ``{symbol: [(day, close), ...]}`` — the oracle's price
    input (README §9): every PRICES symbol on every day from :data:`PRICE_SERIES_START` through
    ASOF as traded on that day (``oracle.price_as_traded`` over the CURRENT corporate actions,
    exactly what :func:`_ensure_daily_prices` writes), and every seeded month-end series.

    Never the stored ``close`` column: the app DERIVES that column (``cap_4dp(close_raw ×
    split_basis)``), so an oracle reading it would agree with a wrong cap or a wrong basis by
    construction — the review's M6 mutation (a 2-dp cap) passed exactly that way.
    ``prices.stored_close`` compares the stored column against this instead.
    """
    out: O.Closes = {}
    for sym, quote in PRICES.items():
        series: list[tuple[date, Decimal]] = []
        day = PRICE_SERIES_START
        while day <= ASOF:
            series.append((day, O.price_as_traded(facts, sym, day, quote=quote,
                                                  quoted_on=ASOF)))
            day += timedelta(days=1)
        out[sym] = series
    for sym in sorted(SEEDED_SERIES):
        out[sym] = sorted(month_end_series(sym).items())
    return out


def fixture_fx_rows() -> O.FxRows:
    """The FX rows the fixture wrote (``seed_all``): the trade-date schedule at EARLY_ASOF and
    spot at ASOF — the oracle's FX input, for the same reason as :func:`fixture_closes`."""
    out: O.FxRows = {}
    for (b, q), rate in EARLY_FX_RATES.items():
        out.setdefault((b, q), []).append((EARLY_ASOF, rate))
    for (b, q), rate in FX_RATES.items():
        out.setdefault((b, q), []).append((ASOF, rate))
    for pair in out:
        out[pair].sort()
    return out


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


def _ensure_daily_prices(db_path, facts) -> int:
    """Seed a DAILY close for every PRICES symbol over the whole fixture span.

    This is what closes the last half of the point-in-time gap. ``seed_all`` seeds exactly
    ONE close per symbol, dated :data:`ASOF`, so every earlier day was unpriced by
    construction and ``trend.total_value`` could not be asserted before ASOF — which is
    precisely where a share-count defect confined to a window (R6's ex-date) lives. With a
    price on every day, ``total_value`` at a KNOWN price pins the SHARE COUNT.

    Two properties make this safe rather than a fixture that quietly lies:

    * **The closes are AS TRADED on their own date** — :func:`oracle.price_as_traded`
      multiplies the ASOF quote back out by the splits between that day and ASOF, which is
      the invariant ``data-and-pricing.md`` states for the column.
    * **Each row is stamped ``fetched_at`` = its own date**, so both the write window
      ``(as_of, fetched_at]`` and the read window ``(priced_on, day]`` are empty for it.

    The span is FIXED (:data:`PRICE_SERIES_START` .. ASOF), never derived from the ledger, so
    the rows the fixture has written are always exactly :func:`fixture_closes` — the oracle's
    price input — and re-seeding after a split changes precisely the rows it must. Called from
    :func:`reconcile` BEFORE the settlement, so the price writes are changes the run takes.
    """
    if not (facts.txs or facts.divs or facts.openings or facts.actions):
        return 0
    mkt_of = {i[0]: i[1] for i in INSTRUMENTS}
    closes = fixture_closes(facts)
    rows_by_date: dict[date, list] = {}
    for sym in PRICES:
        for day, close in closes[sym]:
            rows_by_date.setdefault(day, []).append((sym, mkt_of[sym], close))
    return C.seed_prices_daily(db_path, rows_by_date)


def _reconcile_stored_prices(ev: C.Evidence, db_path, closes: O.Closes, phase: str) -> None:
    """``prices.stored_close`` / ``fx.stored_rate``: the STORED columns equal what the fixture
    wrote, on every day of every fixture symbol and pair. The stored ``close`` is derived by
    the app's write seam (``cap_4dp(close_raw × split_basis)``, data-and-pricing.md), so this
    is where a wrong cap, a wrong basis or a lost row shows — the oracle itself never reads it
    (README §9). One assertion per symbol, naming the first wrong days."""
    stored = C.load_closes(db_path)
    for sym, series in sorted(closes.items()):
        have = dict(stored.get(sym, []))
        wrong = [f"{d.isoformat()}: stored {have.get(d)} != {v}" for d, v in series
                 if have.get(d) is None or have[d] != v]
        ev.check("prices.stored_close", f"{sym} ({len(series)} days)", [], wrong[:5], phase)
    stored_fx = C.load_fx_rows(db_path)
    for pair, rows in sorted(fixture_fx_rows().items()):
        have = dict(stored_fx.get(pair, []))
        wrong = [f"{d.isoformat()}: stored {have.get(d)} != {v}" for d, v in rows
                 if have.get(d) is None or have[d] != v]
        ev.check("fx.stored_rate", f"{pair[0]}/{pair[1]}", [], wrong, phase)


def _priced(closes, actions, symbol: str, day: date) -> bool:
    """Whether a stored close on or before *day* values *symbol* (``oracle.price_at``)."""
    return O.price_at(closes, actions, symbol, day) is not None


def _day_incomplete(res_d, day: date, closes, actions) -> bool:
    """Whether the app must publish this trend day as ``incomplete`` - derived, not read.

    Three causes, from ``domain-ledger.md`` plus this fixture's own price schedule:

    * a held position that is 賣超 / 待釐清 (``oversold`` / ``unbookable_dividend`` /
      ``unbookable_action``). The first and last have no honest value that day; a refused
      dividend leaves the value honest and only the payout 待釐清 (Y3, owner ruling
      2026-10-05) — the day is marked all the same, and :func:`_day_value_judgeable` decides
      the value separately;
    * a held position with NO close on-or-before ``day`` cannot be valued — read from the
      fixture's closes (:func:`fixture_closes`): since PERF §11.2 the scenario carries series
      that start late on purpose (``MPX``). A day AFTER a series ends is NOT incomplete: the
      read carries the last close forward, which is a valid valuation and what the app does;
    * a day holding nothing is COMPLETE with a zero total - the app's loop simply adds nothing.

    K3 (owner ruling 2026-10-05) adds a fourth, independent of what is held: a dividend
    refused by ``day`` because it landed on an open short. The 2609 lifecycle covers that short
    back to exactly zero, so after the cover no holding carries ``unbookable_dividend`` — and
    the ruling is that the 待釐清 must not die with a flat carrier. The days stay incomplete.
    """
    if res_d.refused_dividends:
        return True
    if res_d.oversold_keys:
        # CR-11: a 賣超 is recorded on the BOOK, flat or held, and every reader of "is this
        # position 賣超" reads that record — so, like a refused dividend (K3), every day from it
        # stays incomplete after the position nets back to zero; its basis is gone for good.
        return True
    if res_d.refused_closes:
        # Options spec §4.4: a refused close is a RECORD "like orphan_dividends" — every day
        # from the close's own date is incomplete, whether or not its position is still held.
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
            # marking the day incomplete".
            continue
        if not _priced(closes, actions, h.symbol, day):
            return True
    return False


def _day_value_judgeable(res_d, day: date, closes, actions) -> bool:
    """Whether ``trend.total_value`` is assertable on a day - every held position has an
    honest value. A held 賣超 (basis discarded) or ``unbookable_action`` (pre-action shares
    against a post-action price) has no honest value: the app leaves it out, so the total is
    not asserted; an unpriced symbol cannot be valued either. A refused dividend leaves every
    SHARE right (Y3), so its days are judged although incomplete."""
    for h in res_d.holdings.values():
        if h.shares == O.ZERO or O.is_option(h.symbol):
            continue      # R3: an open contract's value is not in the trend at all
        if (h.oversold or h.unbookable_action
                or not _priced(closes, actions, h.symbol, day)):
            return False
    return True


def _event_days(facts: O.Facts) -> set[date]:
    days = {t.trade_date for t in facts.txs} | {o.build_date for o in facts.openings}
    days |= {dv.effective for dv in facts.divs} | {dv.d for dv in facts.divs}
    days |= {a.d for a in facts.actions} | {c.d for c in facts.cash} | {f.d for f in facts.fxs}
    return days


class _TrendOracle:
    """The oracle's WHOLE daily trend, from the first trade / dividend / opening to *end*.

    Every point is ``{"net_invested"}`` (price-free, exact) plus, unless a corporate action was
    refused by that day (``"refused_action"``: the family does not re-derive that path's
    marking), ``"incomplete"`` (:func:`_day_incomplete`) and — on a day whose every holding has
    an honest value (:func:`_day_value_judgeable`) — ``"total_value"`` and ``"net_worth"``.

    ``available`` is ``False`` when some flow has no on-or-before rate — the app then publishes
    NO trend at all (``daily_value_series``' honest bail), and nothing is compared.

    One replay per EVENT day (the cut, and so the book, only changes there), one valuation per
    calendar day: the close of a day is the latest fixture close on or before it re-expressed
    for the splits since (``oracle.price_at``) — the as-traded close of that very day for a
    PRICES symbol, Friday's carried across a weekend for a month-end series.
    """

    def __init__(self, facts: O.Facts, closes: O.Closes, fx_rows: O.FxRows, end: date) -> None:
        self.facts = facts
        self.closes = closes
        self.end = end
        self.points: dict[date, dict] = {}
        starts = ([t.trade_date for t in facts.txs] + [dv.effective for dv in facts.divs]
                  + [o.build_date for o in facts.openings])
        self.first = min(starts) if starts else None
        fx_on = O.fx_resolver(fx_rows)
        refused = O.replay(facts).refused_dividend_ids
        #: The first day some flow counting by it has no rate on or before its own date: from
        #: it on no point exists (a period close says ``trend_unavailable`` — "the series is
        #: unavailable ON D", W2-D), and the live series as a whole is unavailable.
        self.first_unavailable: date | None = None
        self.available = self.first is not None
        if self.first is None:
            return
        events = _event_days(facts)
        res_d = None
        net_invested = O.ZERO
        day = self.first
        while day <= end:
            if res_d is None or day in events:
                try:
                    net_invested = O.net_invested_through(facts, day, REPORTING, fx_on,
                                                          refused=refused)
                except KeyError:
                    self.first_unavailable = day
                    self.available = False
                    return
                res_d = O.replay_through(facts, day)
            point: dict = {"net_invested": net_invested}
            if res_d.action_refusals:
                point["refused_action"] = True
            else:
                point["incomplete"] = _day_incomplete(res_d, day, closes, facts.actions)
                if _day_value_judgeable(res_d, day, closes, facts.actions):
                    total = O.ZERO
                    for h in res_d.holdings.values():
                        if h.shares == O.ZERO or O.is_option(h.symbol):
                            continue   # R3 / OPT-D4: the trend excludes an open contract
                        rate = (O.ONE if h.quote_ccy == REPORTING
                                else fx_on(day, h.quote_ccy, REPORTING))
                        close = O.price_at(closes, facts.actions, h.symbol, day)
                        assert close is not None   # _day_value_judgeable proved it
                        total += close[0] * h.shares * rate
                    cash_total = O.ZERO
                    for (_acct, ccy), bal in res_d.cash.items():
                        if bal == O.ZERO:
                            continue
                        cash_total += bal * (O.ONE if ccy == REPORTING
                                             else fx_on(day, ccy, REPORTING))
                    point["total_value"] = total
                    point["net_worth"] = total + cash_total
            self.points[day] = point
            day += timedelta(days=1)

    def at(self, day: date) -> dict | None:
        return self.points.get(day)

    def twr(self) -> tuple[dict[date, Decimal], date | None, dict[date, bool], date | None]:
        """The TWR index over the points (``oracle.twr_index``), its anchor, each day's
        ``incomplete``, and the first day the chain cannot be built past (a refused action's
        day — its marking is not re-derived), ``None`` when it runs to the end."""
        pts: list[tuple[date, Decimal | None, Decimal, bool]] = []
        incomplete: dict[date, bool] = {}
        broken: date | None = None
        for day in sorted(self.points):
            p = self.points[day]
            if p.get("refused_action"):
                broken = day
                break
            incomplete[day] = p["incomplete"]
            pts.append((day, p.get("total_value"), p["net_invested"], p["incomplete"]))
        index, anchor = O.twr_index(pts)
        return index, anchor, incomplete, broken


def _shown_pnl(total_value: Decimal, net_invested: Decimal) -> Decimal:
    """A trend point's ``pnl`` as the app must serve it (owner rulings 2026-10-22) — the
    harness's OWN rounding, not an import: ``total_value − net_invested`` at the reporting
    currency's minor unit (``oracle.MINOR_UNITS``), ROUND_HALF_UP, a loss rounded away to zero
    as ``0`` (never ``-0``). An incomplete day serves none (the caller's rule)."""
    shown = (total_value - net_invested).quantize(D(1).scaleb(-O.MINOR_UNITS[REPORTING]),
                                                  rounding=ROUND_HALF_UP)
    return shown.copy_abs() if shown.is_zero() else shown


def _check_trend_point(ev: C.Evidence, prefix: str, day_iso: str, want: dict, point: dict,
                       phase: str) -> None:
    """Compare one published trend point with the oracle's."""
    ev.check(f"{prefix}.net_invested", day_iso, want["net_invested"],
             dec(point["net_invested"]), phase)
    if want.get("refused_action"):
        return
    ev.check(f"{prefix}.incomplete", day_iso, want["incomplete"], bool(point["incomplete"]),
             phase)
    served_pnl = point.get("pnl")
    if want["incomplete"]:
        # owner ruling 2026-10-22: an incomplete day's 累計總損益 is not shown (null, 「—」)
        ev.check(f"{prefix}.pnl", day_iso, None, served_pnl, phase)
    if "total_value" not in want:
        return
    ev.check(f"{prefix}.total_value", day_iso, want["total_value"],
             dec(point["total_value"]), phase)
    ev.check(f"{prefix}.net_worth", day_iso, want["net_worth"],
             dec(point.get("net_worth")) if point.get("net_worth") is not None else None,
             phase)
    if not want["incomplete"]:
        shown = _shown_pnl(want["total_value"], want["net_invested"])
        ev.check(f"{prefix}.pnl", day_iso, shown,
                 dec(served_pnl) if served_pnl is not None else None, phase)
        # the TEXT too: the minor unit's places and no "-0" (the value check above compares
        # numerically, where "13" == "13.0" and "-0" == "0")
        ev.check(f"{prefix}.pnl_text", day_iso, repr(format(shown, "f")), repr(served_pnl),
                 phase)


def _reconcile_trend(ev: C.Evidence, dash, oracle_trend: _TrendOracle, phase: str) -> None:
    """POINT-IN-TIME reconciliation of the daily replay (the ``trend.*`` family).

    Why it exists: every other family in this harness compares the app and the oracle at the
    CURRENT state, and a whole class of defect is invisible there - anything whose effect is
    confined to a window between two dates ends at the same place either way. R6's ex-date is
    exactly that shape, which is why ``fail=0`` was silent about it (README section 6).

    What is compared: ``trend.available`` (derived: every flow has a rate), ``trend.start`` /
    ``trend.contiguous_days``, and on the sampled days (every ledger date and the day before,
    ASOF, the last point) ``trend.net_invested`` (exact, price-free — the subtrahend of B),
    ``trend.incomplete``, and on every day whose holdings all have an honest value
    ``trend.total_value`` / ``trend.net_worth``. Since PERF (2026-10-18) the series is SETTLED:
    the harness settles before it reads, so on a clean store the whole series must equal the
    oracle — byte identity is the spec's own guarantee (§10, §18).
    """
    facts = oracle_trend.facts
    trend = (dash.get("trend") or {})
    pts = trend.get("points") or []
    ev.check("trend.available", "series", oracle_trend.available, bool(trend.get("available")),
             phase)
    if not pts or not oracle_trend.available:
        return
    by_date = {p["date"]: p for p in pts}
    first_iso, last_iso = min(by_date), max(by_date)
    span = date.fromisoformat(last_iso).toordinal() - date.fromisoformat(first_iso).toordinal()
    ev.check("trend.contiguous_days", "series", span + 1, len(pts), phase)
    if oracle_trend.first is not None:
        ev.check("trend.start", "series", oracle_trend.first.isoformat(), first_iso, phase)

    skipped: list[str] = []
    for day_iso in _trend_sample_dates(facts, by_date, last_iso):
        want = oracle_trend.at(date.fromisoformat(day_iso))
        if want is None:
            continue
        if want.get("refused_action"):
            # WHICH rule refused is scenario knowledge, and the app's refusal path marks the
            # day for reasons this family does not re-derive — logged ONCE below, never per
            # day (`ops` is a signal).
            ev.check("trend.net_invested", day_iso, want["net_invested"],
                     dec(by_date[day_iso]["net_invested"]), phase)
            skipped.append(day_iso)
            continue
        _check_trend_point(ev, "trend", day_iso, want, by_date[day_iso], phase)
    if skipped:
        ev.op(phase, "ORACLE", "trend.days_not_judged",
              {"reason": "corporate action refused by the oracle on that day"},
              {"count": len(skipped), "dates": skipped})


def reconcile(ev: C.Evidence, api: C.Api, db_path, label: str, *, valuation=True,
              reports=False):
    """Full oracle-vs-system reconciliation at the current state.

    Order matters: the fixture's price writes first (they are writes, so they belong to the
    changes a settlement must take), then :func:`settle` — the status asserted BEFORE the run,
    the run started and waited for, the clean state asserted after — and only then any read of
    a figure, so every family below reads a settled store.
    """
    phase = f"phase1:{label}"
    raw = C.load_facts_from_db(db_path)
    # BEFORE any API call: the dashboard must be built over the daily series, or the trend
    # family would compare an app that could not value a day against an oracle that could.
    CHECKPOINT.today = server_today()
    _ensure_daily_prices(db_path, raw)
    settle(ev, api, db_path, label, raw)
    # The oracle's price and FX inputs are what the FIXTURE wrote — never the stored columns,
    # which the app derives (README §9); `prices.stored_close` / `fx.stored_rate` hold the two
    # together.
    closes = fixture_closes(raw)
    fx_rows = fixture_fx_rows()
    _reconcile_stored_prices(ev, db_path, closes, phase)
    dash = api.get("/api/dashboard").json()
    # DEF-016 / DEF-056: the valuation reads only the rows that count by the app's own as_of
    # (read back from the response, as the XIRR family does) — every ledger from its own
    # date. The raw facts stay for the checks that compare stored ROWS (ledger APIs, the
    # dated cash statement).
    as_of = date.fromisoformat(str(dash.get("as_of", ASOF.isoformat()))[:10])
    facts = O.facts_valued_as_of(raw, as_of)
    res = O.replay(facts)
    sp = spots()
    trend_oracle = _TrendOracle(facts, closes, fx_rows, as_of)
    # The valuation price of every symbol with a stored close: the latest on or before as_of,
    # re-expressed for any later split (oracle.price_at). For a PRICES symbol that is exactly
    # its ASOF quote, as before; for a MONTH_END_SERIES symbol its last weekday close.
    prices: dict[str, Decimal] = {}
    for sym in closes:
        got = O.price_at(closes, facts.actions, sym, as_of)
        if got is not None:
            prices[sym] = got[0]
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
        _reconcile_kpis(ev, res, dash, facts, prices, sp, phase, closes, trend_oracle)
        _reconcile_xirr(ev, res, dash, facts, prices, sp, phase)
    if valuation and not undeclared_oversell:
        # E3. POINT-IN-TIME: the daily replay, sampled at event boundaries. Behind the 賣超
        # gate only (YQ1): an undeclared oversell has no honest valuation on any day, so
        # judging its trend would be judging a state the app itself declines to value — but
        # a declared short is valued, and the `short_open` checkpoint is where a held carrier
        # of a refused dividend lives (Y3).
        _reconcile_trend(ev, dash, trend_oracle, phase)

    # ---- E4. Month-end values (PERF spec §11.2). First the oracle's "as at D" block at
    # D = today against the LIVE KPI block — runnable on any build, and the proof that the
    # transcription is right before a single period route is read. Then (package G's routes)
    # every past month's close and the past-month 月度成績 rows against the same oracle.
    _reconcile_asof_identity(ev, dash, raw, closes, fx_rows, as_of, phase)
    ctx = _Periods(raw, closes, fx_rows, as_of, trend_oracle)
    _reconcile_store_periods(ev, db_path, ctx, phase)
    if READ_PATH:
        _reconcile_periods(ev, api, ctx, phase)
    _reconcile_tax_tieout(ev, api, db_path, ctx, phase)

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


# ---------------------------------------------------------------- settlement (PERF spec §18)
#: Package G's read path and period routes (wave 3) are merged. ``run_phase1.py --wave2-only``
#: clears it: every check that reads the STORE through the dashboard, ``/api/settlement/
#: periods*`` or the past-month rows of ``/api/snapshots`` is skipped; the status / run checks
#: and the month-end oracle against the LIVE KPI block still run. For a run before the wave-3
#: merge ONLY — W4 and every later run keep it True.
READ_PATH = True

#: The app's business-day anchor (``shared/clock.py``: Asia/Taipei).
TAIPEI = ZoneInfo("Asia/Taipei")
#: How long one settlement may take before the harness stops waiting (child start included).
SETTLE_TIMEOUT_S = 600.0
_ACTIVE = ("preparing", "running")


class SettleAbort(RuntimeError):
    """A settlement did not end in time: the phase stops, with ONE clear failure, rather than
    reconcile every later family against an unsettled store."""


def server_today() -> date:
    """The app's business day — the day ``live_from`` and the live window are anchored on."""
    return datetime.now(TAIPEI).date()


class _Checkpoint:
    """The business day of the checkpoint being reconciled, read ONCE at its start: every W,
    every "last month" and every current period of the checkpoint derives from it, so a
    checkpoint that straddles Taipei midnight on the 1st never mixes two windows."""

    today: date = date.min


CHECKPOINT = _Checkpoint()


def window_start(today: date) -> date:
    """``W`` (spec §10 step 3): the 1st of the previous month. Transcribed, not imported."""
    return date(today.year - 1, 12, 1) if today.month == 1 else date(
        today.year, today.month - 1, 1)


def _d(value) -> date | None:
    return None if value in (None, "") else date.fromisoformat(str(value)[:10])


def _iso(day: date | None) -> str | None:
    return None if day is None else day.isoformat()


@dataclass
class _Settled:
    """The INPUTS of the generation the harness's last settlement published — so a stale day
    or a stale close can be checked against the PREVIOUS generation's values (PERF-D3)."""

    facts: O.Facts
    closes: O.Closes
    fx_rows: O.FxRows
    generation: int


class _SettleState:
    last: _Settled | None = None


SETTLE = _SettleState()


def reset_settlement() -> None:
    """A fresh database: no generation has been seen yet, no series written."""
    SETTLE.last = None
    SEEDED_SERIES.clear()


def _status(api: C.Api) -> dict:
    r = api.get("/api/settlement/status")
    if r.status_code != 200:
        return {"_http": r.status_code}
    body = r.json()
    return body if isinstance(body, dict) else {"_http": r.status_code}


def _ledger_symbols(facts: O.Facts) -> set[str]:
    """The symbols the price trigger's gate admits (a ledger symbol: a trade, a dividend, an
    opening, either side of a corporate action)."""
    out = {t.symbol for t in facts.txs} | {dv.symbol for dv in facts.divs}
    out |= {o.symbol for o in facts.openings}
    out |= {a.from_symbol for a in facts.actions} | {a.to_symbol for a in facts.actions}
    return out


def _change_bound(prev: _Settled, facts: O.Facts, closes: O.Closes,
                  fx_rows: O.FxRows) -> tuple[date | None, list[str]]:
    """The earliest day whose value a write since the last settlement can have changed — and
    what changed (for the evidence), derived from the fixture's rows before and after.

    Each changed row counts from the EARLIER of its old and new day (a row moved to a later
    date changes the old date's value too): a trade's date, a dividend's EFFECTIVE date (a
    配股 with an ex-date from it), a conversion's / movement's / action's date, an opening's
    build date; a price or rate from its own date. The prices of a symbol that became a
    ledger symbol in between are left out: the trigger's symbol gate saw them while the
    symbol was not one, and its own ledger rows bound the change anyway. A corporate action's
    ``note`` is an exempt column (spec §7) and is not compared.
    """
    days: list[date] = []
    what: list[str] = []

    def diff(label: str, old: dict, new: dict, day_of) -> None:
        for key in old.keys() | new.keys():
            a, b = old.get(key), new.get(key)
            if a == b:
                continue
            first = min(day_of(x) for x in (a, b) if x is not None)
            days.append(first)
            what.append(f"{label}:{key}@{first.isoformat()}")

    diff("transactions", {t.id: t for t in prev.facts.txs}, {t.id: t for t in facts.txs},
         lambda t: t.trade_date)
    diff("dividends", {dv.id: dv for dv in prev.facts.divs}, {dv.id: dv for dv in facts.divs},
         lambda dv: dv.effective)
    diff("fx_conversions", {f.id: f for f in prev.facts.fxs}, {f.id: f for f in facts.fxs},
         lambda f: f.d)
    diff("cash_movements", {c.id: c for c in prev.facts.cash}, {c.id: c for c in facts.cash},
         lambda c: c.d)
    diff("opening_inventory", {(o.account_id, o.symbol): o for o in prev.facts.openings},
         {(o.account_id, o.symbol): o for o in facts.openings}, lambda o: o.build_date)

    def no_note(a: O.CorpFact) -> tuple:
        return (a.id, a.account_id, a.d, a.kind, a.from_symbol, a.to_symbol, a.ratio_to,
                a.ratio_from, a.cost_carry)

    diff("corporate_actions", {a.id: (no_note(a), a.d) for a in prev.facts.actions},
         {a.id: (no_note(a), a.d) for a in facts.actions}, lambda pair: pair[1])
    for sym in sorted(_ledger_symbols(prev.facts) & _ledger_symbols(facts)):
        old_c, new_c = dict(prev.closes.get(sym, [])), dict(closes.get(sym, []))
        changed = [d for d in old_c.keys() | new_c.keys() if old_c.get(d) != new_c.get(d)]
        if changed:
            days.append(min(changed))
            what.append(f"prices:{sym}@{min(changed).isoformat()}")
    for pair in sorted(prev.fx_rows.keys() | fx_rows.keys()):
        old_r, new_r = dict(prev.fx_rows.get(pair, [])), dict(fx_rows.get(pair, []))
        changed = [d for d in old_r.keys() | new_r.keys() if old_r.get(d) != new_r.get(d)]
        if changed:
            days.append(min(changed))
            what.append(f"fx_rates:{pair[0]}/{pair[1]}@{min(changed).isoformat()}")
    return (min(days) if days else None), sorted(what)


def settle(ev: C.Evidence, api: C.Api, db_path, label: str, raw: O.Facts | None = None):
    """Assert the store's state for the writes since the last settlement, run a settlement,
    wait for it, and assert the clean state it leaves (PERF spec §18 "Stress audit": "the
    harness runs a settlement after its writes and also asserts the stale state before it").

    BEFORE the run, from ``GET /api/settlement/status`` alone:

    * the store is unusable before the first generation: nothing is stale, nothing is live
      from a day (``live_from`` / ``stale_*`` null — every day is computed live, §10 step 2);
    * a write since the last run marked the store dirty, and ``dirty_from`` is NOT LATER than
      the earliest day the written rows can change (:func:`_change_bound`) — the prefix
      property's safety half (§4, §18): over-firing is allowed, a day after the change is a
      settled day that silently keeps a wrong value;
    * ``live_from = min(S + 1, max(dirty_from, W))`` and the stale range is
      ``[dirty_from, live_from − 1]`` when ``dirty_from`` lies before it (§10 step 3), the
      prompt is due exactly when ``dirty_from < W`` (§14.3), and the chip reads 待結算.

    With the read path (:data:`READ_PATH`), :func:`_reconcile_stale_serving` then proves the
    stale days ARE the previous generation's values and the live days the new ones.

    AFTER the run: the report ended 完成 (``published``), the store is clean (no dirty day,
    no stale range), ``live_from = settled_through + 1`` and ``settled_through`` reached
    ``final_through``. A run that does not end within :data:`SETTLE_TIMEOUT_S` raises
    :class:`SettleAbort` after ONE failing assertion — nothing is reconciled against an
    unsettled store.
    """
    phase = f"phase1:{label}"
    if CHECKPOINT.today == date.min:
        CHECKPOINT.today = server_today()
    raw = raw if raw is not None else C.load_facts_from_db(db_path)
    closes = fixture_closes(raw)
    fx_rows = fixture_fx_rows()
    st0 = _status(api)
    ev.check("settle.status_route", label, 200, st0.get("_http", 200), phase)
    if "_http" in st0:
        return None
    _check_pending_state(ev, api, label, st0, raw, closes, fx_rows, phase)

    r = api.post("/api/settlement/run", {"scope": "dirty"})
    if r.status_code == 409:
        # A run is already active (a chained follow-up) — let it finish, then ask once more.
        _wait_idle(api)
        r = api.post("/api/settlement/run", {"scope": "dirty"})
    body = _json(r)
    ev.op(phase, "API", "settlement.run", {"scope": "dirty"},
          {"status": r.status_code, "json": body})
    ev.check("settle.run_accepted", label, 202, r.status_code, phase)
    if r.status_code != 202 or not isinstance(body, dict) or "run_id" not in body:
        return None
    run_id = int(body["run_id"])
    report, st1, ended = _wait_run(api, run_id)
    if not ended:
        ev.check("settle.run_finished_in_time", f"{label} #{run_id} within "
                 f"{SETTLE_TIMEOUT_S:.0f}s", True, False, phase)
        raise SettleAbort(f"settlement #{run_id} ({label}) did not end within "
                          f"{SETTLE_TIMEOUT_S:.0f}s — phase 1 stops here")
    summary = (report or {}).get("summary") or {}
    ev.check("settle.run_published", f"{label} #{run_id}", "published", summary.get("status"),
             phase)
    if summary.get("status") != "published":
        ev.op(phase, "API", "settlement.run_not_published", {"run_id": run_id},
              {"summary": summary, "failure": (report or {}).get("failure")})
    ev.check("settle.clean_after_run", label, ["clean", None, None, None],
             [st1.get("state"), st1.get("dirty_from"), st1.get("stale_from"),
              st1.get("stale_to")], phase)
    settled = _d(st1.get("settled_through"))
    final = _d(st1.get("final_through"))
    ev.check("settle.live_from_after_run", label,
             _iso(settled + timedelta(days=1)) if settled else None,
             _iso(_d(st1.get("live_from"))), phase)
    ev.check("settle.settled_through_reaches_final", f"{label}: {settled} >= {final}", True,
             settled is not None and final is not None and settled >= final, phase)
    gen0, gen1 = int(st0.get("generation") or 0), int(st1.get("generation") or 0)
    ev.check("settle.generation_after_run", f"{label}: {gen0} -> {gen1}", True,
             gen1 >= max(1, gen0), phase)
    SETTLE.last = _Settled(raw, closes, fx_rows, gen1)
    return report


def _check_pending_state(ev, api, label, st0, raw, closes, fx_rows, phase) -> None:
    """The BEFORE-a-run half of :func:`settle` (its docstring states the rules)."""
    prev = SETTLE.last
    live_from = _d(st0.get("live_from"))
    if prev is None or live_from is None:
        # No generation yet (or the store is unusable): every day is live, nothing is stale.
        ev.check("settle.unusable_nothing_stale", label, [None, None],
                 [st0.get("stale_from"), st0.get("stale_to")], phase)
        if prev is None:
            ev.check("settle.unusable_no_live_from", label, None, st0.get("live_from"), phase)
        return
    today = CHECKPOINT.today
    w = window_start(today)
    settled = _d(st0.get("settled_through"))
    dirty = _d(st0.get("dirty_from"))
    bound, what = _change_bound(prev, raw, closes, fx_rows)
    ev.op(phase, "API", "settlement.status_before_run", {"label": label},
          {k: st0.get(k) for k in ("state", "generation", "settled_through", "dirty_from",
                                   "live_from", "stale_from", "stale_to", "needs_prompt")}
          | {"earliest_change": _iso(bound), "changes": what[:40],
             "changes_total": len(what)})
    if bound is not None:
        ev.check("settle.change_marks_dirty", f"{label}: earliest change {bound}", True,
                 dirty is not None, phase)
        if dirty is not None:
            ev.check("settle.dirty_not_after_change",
                     f"{label}: dirty_from {dirty} <= earliest change {bound}", True,
                     dirty <= bound, phase)
    if settled is None:
        return
    if server_today() != today:
        # Taipei midnight passed since the checkpoint began: the server's W may already be
        # the next month's. Nothing W-dependent is judged rather than judged against two days.
        ev.op(phase, "ORACLE", "settle.midnight_straddle",
              {"label": label, "checkpoint_day": today.isoformat()}, {})
        return
    if dirty is None:
        ev.check("settle.clean_nothing_stale", label, [None, None],
                 [st0.get("stale_from"), st0.get("stale_to")], phase)
        ev.check("settle.live_from", label, _iso(settled + timedelta(days=1)), _iso(live_from),
                 phase)
        return
    want_live = min(settled + timedelta(days=1), max(dirty, w))
    ev.check("settle.live_from", f"{label}: min(S+1, max({dirty}, W={w}))", _iso(want_live),
             _iso(live_from), phase)
    stale = [_iso(dirty), _iso(want_live - timedelta(days=1))] if dirty < want_live else \
        [None, None]
    ev.check("settle.stale_range", label, stale, [st0.get("stale_from"), st0.get("stale_to")],
             phase)
    ev.check("settle.needs_prompt", f"{label}: dirty_from {dirty} < W {w}", dirty < w,
             bool(st0.get("needs_prompt")), phase)
    if dirty < w:
        ev.check("settle.prompt_from", label, _iso(dirty),
                 _iso(_d((st0.get("prompt") or {}).get("from"))), phase)
    ev.check("settle.state_pending", label, "pending", st0.get("state"), phase)
    if READ_PATH and dirty < want_live:
        _reconcile_stale_serving(ev, api, prev, raw, closes, fx_rows, dirty, want_live, today,
                                 phase)


def _wait_idle(api: C.Api, timeout: float = SETTLE_TIMEOUT_S) -> dict:
    deadline = time.monotonic() + timeout
    st = _status(api)
    while time.monotonic() < deadline:
        if st.get("state") not in _ACTIVE and st.get("run") is None:
            return st
        time.sleep(0.5)
        st = _status(api)
    return st


def _wait_run(api: C.Api, run_id: int,
              timeout: float = SETTLE_TIMEOUT_S) -> tuple[dict | None, dict, bool]:
    """Poll run *run_id*'s REPORT until it has ended, THEN the status until it reads idle
    in a state at or after that end: no run active and, when the run published, a generation
    at least the report's ``generation_after``. The last element says whether it ended in
    time.

    Report first, status after (2026-10-22): the status used to be read FIRST and the report
    after it, so a status read in the instant before the run ended was accepted whenever the
    report, read later, had ended — the harness then judged a pre-swap status as the run's
    result (a status torn across the swap, since fixed in the service, read generation 3 with
    the claimed day stale while run #4 had published generation 4). Idle alone is still not
    enough: the status can read idle in the instant before a chained follow-up starts, and
    the generation floor keeps a status from before this run's swap from passing."""
    deadline = time.monotonic() + timeout
    report: dict | None = None
    summary: dict = {}
    while time.monotonic() < deadline:
        rr = api.get(f"/api/settlement/runs/{run_id}")
        if rr.status_code == 200:
            report = rr.json()
            summary = (report or {}).get("summary") or {}
            if summary.get("status") not in _ACTIVE:
                break
        time.sleep(0.5)
    else:
        return report, _status(api), False
    floor = (int(summary.get("generation_after") or 0)
             if summary.get("status") in ("published", "partial") else 0)
    while time.monotonic() < deadline:
        st = _status(api)
        if (st.get("state") not in _ACTIVE and st.get("run") is None
                and int(st.get("generation") or 0) >= floor):
            return report, st, True
        time.sleep(0.5)
    return report, _status(api), False


def _twr_pending(start: date, day: date, stale: tuple[date, date] | None,
                 live_from: date | None) -> bool:
    """Spec §11.2: a period's TWR is 待結算 while history is stale, for any period whose
    ``(previous end, D]`` — i.e. ``[start, D]`` — contains ``live_from`` or overlaps the stale
    range (the join would book a back-dated position's whole gain as one day's return)."""
    if stale is None:
        return False
    if start <= stale[1] and stale[0] <= day:
        return True
    return live_from is not None and start <= live_from <= day


def _served_prefix(old_trend: _TrendOracle, new_first: date | None, status_live_from: date,
                   stale: tuple[date, date], today: date
                   ) -> tuple[date | None, tuple[date, date] | None]:
    """Which days a reader is SERVED from the previous generation (spec §10 step 4: "points
    before ``live_from`` from ``settled_day``"), and the stale part of them — ``(served
    live_from, served stale range)``.

    The stored rows serve CONTIGUOUSLY from the series' own first day, never a day the
    generation does not hold and never a placeholder (W2-D: a placeholder carries no figure,
    so from it on the reader values live). So the served prefix stops at the earliest of the
    status's ``live_from`` (and tomorrow), the previous generation's first PLACEHOLDER day —
    the first day its trend was unavailable on — and the generation's first day when the
    series now starts EARLIER than it did (the first day is then a day nobody holds: nothing
    is served from the store at all). The stale range the reader is served is the status's,
    cut at that prefix — empty when the prefix ends before it begins.
    """
    if new_first is None:
        return None, None
    stop = min(status_live_from, today + timedelta(days=1))
    if old_trend.first_unavailable is not None:
        stop = min(stop, old_trend.first_unavailable)
    if old_trend.first is None or new_first < old_trend.first:
        served_live = new_first
    else:
        served_live = max(new_first, stop)
    if stale[0] < served_live:
        return served_live, (stale[0], min(stale[1], served_live - timedelta(days=1)))
    return served_live, None


def _reconcile_stale_serving(ev, api, prev: _Settled, raw, closes, fx_rows, stale_from: date,
                             live_from: date, today: date, phase: str) -> None:
    """PERF-D3 (spec §10 steps 3–4, §11.4), BEFORE the run: the days a reader is served from
    the PREVIOUS generation (:func:`_served_prefix`) carry the oracle's values over the inputs
    the last settlement saw (:class:`_Settled`), every later day the oracle's over today's.
    The period rows served from the store and ending in the served stale range say so
    (``stale: true``) with the previous generation's KPIs and no receivable (§11.2); every
    period whose span meets that range or the served ``live_from`` shows its TWR as 待結算
    (§11.2, fold verification #2). A close the store does not serve is computed live: not
    stale, and today's values."""
    old_trend = _TrendOracle(O.facts_valued_as_of(prev.facts, today), prev.closes,
                             prev.fx_rows, today)
    new_trend = _TrendOracle(O.facts_valued_as_of(raw, today), closes, fx_rows, today)
    served_live, served_stale = _served_prefix(
        old_trend, new_trend.first, live_from, (stale_from, live_from - timedelta(days=1)),
        today)
    ev.op(phase, "ORACLE", "stale.served_prefix",
          {"status_stale": [_iso(stale_from), _iso(live_from - timedelta(days=1))]},
          {"served_live_from": _iso(served_live),
           "served_stale": None if served_stale is None else [_iso(d) for d in served_stale]})
    dash = api.get("/api/dashboard").json()
    pts = {p["date"]: p for p in ((dash.get("trend") or {}).get("points") or [])}
    if served_stale is not None:
        lo, hi = served_stale
        sample = {lo, lo + timedelta(days=1), hi}
        sample |= {e for e in _event_days(prev.facts) | _event_days(raw) if lo <= e <= hi}
        for day in sorted(sample):
            point = pts.get(day.isoformat())
            want = old_trend.at(day) if point is not None and day <= hi else None
            if want is not None:
                _check_trend_point(ev, "stale.trend", day.isoformat(), want, point, phase)
    if pts and served_live is not None:
        for day in sorted({max(served_live, date.fromisoformat(min(pts))),
                           date.fromisoformat(max(pts))}):
            point = pts.get(day.isoformat())
            want = new_trend.at(day) if point is not None else None
            if want is not None:
                _check_trend_point(ev, "stale.live_trend", day.isoformat(), want, point, phase)

    first = O.first_ledger_day(raw)
    code, rows, _total = _get_rows(api, "/api/settlement/periods", kind="M")
    ev.check("stale.periods_route", "M", 200, code, phase)
    for row in rows or []:
        end = _d(row.get("end_day"))
        if end is None or first is None:
            continue
        period = str(row.get("period"))
        start = max(first, date.fromisoformat(f"{period}-01"))
        scope = f"M {period}"
        kpi = row.get("kpi") or {}
        if _twr_pending(start, min(end, today), served_stale, served_live):
            _check_reason_word(ev, "stale.period_twr_pending", scope, "待結算",
                               kpi.get("period_twr_reason"), phase)
            ev.check("stale.period_twr_pending_value", scope, None, kpi.get("period_twr"),
                     phase)
        if end >= today.replace(day=1):
            continue
        from_store = served_live is not None and end < served_live
        in_range = (from_store and served_stale is not None
                    and served_stale[0] <= end <= served_stale[1])
        ev.check("stale.period_flag", scope, in_range, bool(row.get("stale")), phase)
        ev.check("stale.period_source", scope, "store" if from_store else "live",
                 row.get("source"), phase)
        pe = (O.period_end(prev.facts, end, prev.closes, prev.fx_rows, REPORTING) if from_store
              else O.period_end(raw, end, closes, fx_rows, REPORTING))
        _check_kpi_values(ev, "stale.period", scope, pe, {
            "total_market_value": kpi.get("total_market_value"),
            "total_return": kpi.get("total_return"),
            "total_return_rate": kpi.get("total_return_rate"),
            "xirr": kpi.get("xirr"), "xirr_window_days": kpi.get("xirr_window_days"),
        }, phase)
        if in_range:
            ev.check("stale.period_no_receivable", scope, None,
                     (kpi.get("notes") or {}).get("dividends_receivable"), phase)
    code, rows, _total = _get_rows(api, "/api/snapshots")
    ev.check("stale.snapshots_route", "rows", 200, code, phase)
    first_this = today.replace(day=1)
    for row in rows or []:
        end = O.month_end(date.fromisoformat(f"{row.get('month')}-01"))
        if end >= first_this:
            continue
        from_store = served_live is not None and end < served_live
        ev.check("stale.snapshot_flag", str(row.get("month")),
                 bool(from_store and served_stale is not None
                      and served_stale[0] <= end <= served_stale[1]),
                 bool(row.get("stale")), phase)


def _get_rows(api: C.Api, path: str, **params) -> tuple[int, list[dict] | None, int | None]:
    """``{rows, total_count}`` of a list route, asking for as many rows as it allows."""
    code = 0
    for limit in (120, 36, 12):
        r = api.get(path, limit=limit, **params)
        code = r.status_code
        if code == 200:
            body = r.json()
            return code, list(body.get("rows") or []), body.get("total_count")
        if code not in (400, 422):
            break
    return code, None, None


# ---------------------------------------------------------------- month-end values (§11.2)
def _dec_map(m) -> dict[str, Decimal] | None:
    if m is None:
        return None
    return {str(k): dec(v) for k, v in m.items() if v is not None}


#: When ``xirr_window_days`` is asserted (manual §7.2.1: "reported in any case, the rate None
#: included"): XIRR computed, withheld for the 30-day window, or uncomputable over a complete
#: series (a missing price; no sign change). DISCLOSED (README §9): when XIRR is withheld
#: because the LEDGER is 待釐清 — a 賣超, an unapplied action, a refused close, an orphan
#: dividend — or a rate is missing, the app builds no flow series and reports no window. The
#: manual's "any case" reads wider than that; the owner rules which is meant, and until then
#: those cases are not asserted either way.
_WINDOW_REPORTED = (None, "window", "missing_price", "unsolvable")


def _check_kpi_values(ev, prefix: str, scope: str, pe: O.PeriodEnd, got: dict,
                      phase: str) -> None:
    """The money KPIs of one "as at D" block against :func:`oracle.period_end`.

    *got* holds the app's values under the oracle's names; a key that is absent is not
    compared (a surface that does not carry the field). Exact Decimal everywhere except the
    two disclosed cases: XIRR (``check_close``, the solver) and the rate, a QUOTIENT of two
    28-digit sums the app may assemble in another order (``check_rel``, association only).
    A held 賣超 / unapplied action is not a reason to skip: its value is dropped and every
    other figure judged (domain-ledger.md), and XIRR is withheld naming the row.
    """
    if "total_market_value" in got:
        ev.check(f"{prefix}.total_market_value", scope, pe.total_market_value,
                 got["total_market_value"], phase)
    if "total_return" in got:
        ev.check(f"{prefix}.total_return", scope, pe.total_return, got["total_return"], phase)
    if "total_return_rate" in got:
        ev.check_rel(f"{prefix}.total_return_rate", scope, pe.total_return_rate,
                     got["total_return_rate"], phase)
    if "by_currency_value" in got:
        have = _dec_map(got["by_currency_value"])
        want = dict(pe.by_currency_value)
        ev.check(f"{prefix}.by_currency_value.keys", scope, sorted(want), sorted(have or {}),
                 phase)
        for ccy in sorted(want):
            ev.check(f"{prefix}.by_currency_value", f"{scope} {ccy}", want[ccy],
                     (have or {}).get(ccy), phase)
    if ("xirr_window_days" in got and pe.xirr_window_days is not None
            and pe.xirr_withheld in _WINDOW_REPORTED):
        ev.check(f"{prefix}.xirr_window_days", scope, pe.xirr_window_days,
                 got["xirr_window_days"], phase)
    if "xirr" not in got:
        return
    withheld = pe.xirr_withheld
    if withheld is None:
        ev.check_close(f"{prefix}.xirr", scope, pe.xirr, got["xirr"], O.XIRR_TOL, phase)
        return
    ev.check(f"{prefix}.xirr_withheld", f"{scope} [{withheld}]", None, got["xirr"], phase)
    if "xirr_reason" not in got:
        return
    reason = got["xirr_reason"] or ""
    # "a blank XIRR or TWR always shows its reason" (spec §11.4).
    ev.check(f"{prefix}.xirr_reason_present", f"{scope} [{withheld}]", True, bool(reason),
             phase)
    if withheld == "window":
        ev.check(f"{prefix}.xirr_reason_names_window", f"{scope} {pe.xirr_window_days} days",
                 True, str(pe.xirr_window_days) in reason, phase)
    for acct, sym, on in pe.xirr_named:
        if withheld == "refused_close":
            # Options §4.4: the row is named by its CONTRACT — the OCC code or its display
            # name — and its date (the live family's rule, xirr.refused_close_reason_names_row).
            terms = O.parse_occ(sym)
            named = ((sym in reason or (terms is not None and terms.name in reason))
                     and (on is None or on.isoformat() in reason))
        else:
            named = (sym in reason and acct in reason
                     and (on is None or on.isoformat() in reason))
        ev.check(f"{prefix}.xirr_reason_names_row", f"{scope} [{withheld}] {acct}/{sym}@{on}",
                 True, named, phase)


def _reconcile_asof_identity(ev, dash, raw, closes, fx_rows, as_of: date, phase: str) -> None:
    """The month-end oracle at D = TODAY against the LIVE KPI block (``month.asof.*``).

    Spec §11.2: ``kpis_as_of`` is ONE function, called by the live dashboard with "latest
    stored" sources and by the run with "on or before D" — and on or before today, the latest
    stored row IS the row. So :func:`oracle.period_end` at today must equal the live KPIs:
    總市值, 總報酬 (A), the blended rate, XIRR with its window and withholding reason, and the
    currency view's native values — while a declared stock short is open, and while a 賣超 or
    an unapplied action is held (its value dropped) too.
    """
    pe = O.period_end(raw, as_of, closes, fx_rows, REPORTING)
    kpis = dash.get("kpis") or {}
    cv = dash.get("currency_view") or {}
    _check_kpi_values(ev, "month.asof", f"as at {as_of}", pe, {
        "total_market_value": kpis.get("total_market_value"),
        "total_return": kpis.get("total_return"),
        "total_return_rate": kpis.get("total_return_rate"),
        "xirr": kpis.get("xirr"),
        "xirr_window_days": kpis.get("xirr_window_days"),
        "xirr_reason": (dash.get("freshness") or {}).get("xirr_unavailable_reason"),
        "by_currency_value": cv.get("by_currency_value"),
    }, phase)


def _pair_key(pair: str) -> frozenset[str]:
    return frozenset(str(pair).split("/"))


#: The zh reason words the app's ``period_twr_reason`` must carry for each oracle reason —
#: any one of them, matched as substrings, so a re-worded sentence that keeps the meaning
#: still passes. "No trend on D" may name the trend (走勢) or its cause, a missing rate (匯率).
_TWR_REASON_WORDS: dict[str, tuple[str, ...]] = {
    "unavailable": ("走勢", "匯率"), "no_anchor": ("尚無",), "end_incomplete": ("期末",),
    "start_unavailable": ("期初",), "start_incomplete": ("期初",),
}


def _check_reason_word(ev, check: str, scope: str, words, reason, phase: str) -> None:
    """A zh reason must carry one of *words* — the evidence keeps the actual sentence when it
    does not, so a re-worded reason is triaged from the line, not from a re-run."""
    words = (words,) if isinstance(words, str) else tuple(words)
    text = str(reason or "")
    want = "contains " + " or ".join(words)
    ev.check(check, scope, want, want if any(w in text for w in words) else text, phase)


class _Periods:
    """One checkpoint's oracle values for every period end, computed once and shared by the
    period list, the detail route, the store family, the year rows, the 月度成績 rows and the
    tax tie-out."""

    def __init__(self, raw: O.Facts, closes, fx_rows, today: date,
                 trend: _TrendOracle) -> None:
        self.raw = raw
        self.closes = closes
        self.fx_rows = fx_rows
        self.today = today
        self.trend_oracle = trend
        self.first = O.first_ledger_day(raw)
        self._pe: dict[date, O.PeriodEnd] = {}
        self._opt: dict[tuple[date, date], bool] = {}
        self._act: dict[tuple[date, date], O.Activity] = {}
        self.index, self.anchor, self.incomplete, self.broken = trend.twr()

    def at(self, day: date) -> O.PeriodEnd:
        if day not in self._pe:
            self._pe[day] = O.period_end(self.raw, day, self.closes, self.fx_rows, REPORTING)
        return self._pe[day]

    def trend(self, day: date) -> dict | None:
        return self.trend_oracle.at(day)

    def options_excluded(self, start: date, end: date) -> bool:
        """``notes.options_value_excluded``: an option open at the close of some day of the
        period ``(previous end, D]`` (spec §11.2, the OR of ``settled_day.options_open``)."""
        if (start, end) not in self._opt:
            self._opt[(start, end)] = O.trend_skips_an_option(self.trend_oracle.facts, start,
                                                              end)
        return self._opt[(start, end)]

    def activity(self, start: date, end: date) -> O.Activity:
        if (start, end) not in self._act:
            self._act[(start, end)] = O.period_activity(self.raw, start, end, self.fx_rows,
                                                        REPORTING)
        return self._act[(start, end)]

    def twr(self, start_end: date | None, end: date) -> tuple[Decimal | None, str | None]:
        """``period_twr`` (spec §11.2), or ``("not_judged", …)`` past a refused action."""
        if self.broken is not None and end >= self.broken:
            return None, "not_judged"
        return O.period_twr(self.index, self.anchor, self.incomplete, start_end=start_end,
                            end=end)


def _period_bounds(kind: str, period: str, ctx: _Periods) -> tuple[date, date, date | None]:
    """``(first day counted, the day valued, the previous period's end | None)`` of a period:
    a period starts at its first calendar day (or the ledger's first day), is valued as at its
    end — today for the current one — and the first period has no previous end."""
    if kind == "M":
        first_day = date.fromisoformat(f"{period}-01")
        end = O.month_end(first_day)
    else:
        first_day = date(int(period), 1, 1)
        end = date(int(period), 12, 31)
    prev_end = first_day - timedelta(days=1)
    first = ctx.first or first_day
    start = max(first_day, first)
    previous = None if prev_end < first else prev_end
    return start, min(end, ctx.today), previous


def _fold_activity(app: dict) -> dict[str, dict]:
    """The served activity lines folded onto the oracle's keys (``oracle.Activity``): the app
    may split a key into several lines, and its order is its own."""
    out: dict[str, dict] = {k: {} for k in ("realized", "dividends", "not_booked", "fees", "cash",
                                             "fx", "fx_realized", "openings")}

    def add(group: str, key: tuple, field_: str, money) -> None:
        if money is None:
            return
        line = out[group].setdefault(key, {})
        n, r = line.get(field_, (O.ZERO, O.ZERO))
        rv = money.get("reporting")
        line[field_] = (n + dec(money["native"]),
                        None if r is None or rv is None else r + dec(rv))

    def rows(group: str, key: tuple, count) -> None:
        line = out[group].setdefault(key, {})
        line["rows"] = int(line.get("rows", 0)) + int(count or 0)

    for x in app.get("realized") or []:
        key = (x["kind"], x["asset_type"], x["realized"]["ccy"])
        rows("realized", key, x.get("rows"))
        add("realized", key, "realized", x["realized"])
        add("realized", key, "capital_gain", x.get("capital_gain"))
    for x in app.get("dividends") or []:
        money = x.get("net") or x.get("gross")
        key = (str(x["type"]).upper(), x["account_id"], money["ccy"] if money else "-")
        rows("dividends", key, x.get("rows"))
        for f in ("gross", "withholding", "net"):
            add("dividends", key, f, x.get(f))
        for f in ("reinvested_shares", "stock_shares"):
            if x.get(f) is not None:
                line = out["dividends"][key]
                line[f] = line.get(f, O.ZERO) + dec(x[f])
    for x in app.get("dividends_not_booked") or []:
        key = (x["net"]["ccy"],)
        rows("not_booked", key, x.get("rows"))
        add("not_booked", key, "net", x["net"])
    for x in app.get("fees") or []:
        key = (x["account_id"], x["fees"]["ccy"])
        add("fees", key, "fees", x["fees"])
        add("fees", key, "tax", x["tax"])
    for x in app.get("cash_movements") or []:
        key = (x["account_id"], str(x["kind"]).upper(), x["amount"]["ccy"])
        rows("cash", key, x.get("rows"))
        add("cash", key, "amount", x["amount"])
    for x in app.get("fx_conversions") or []:
        key = (x["account_id"], x["from_amount"]["ccy"], x["to_amount"]["ccy"])
        rows("fx", key, x.get("rows"))
        add("fx", key, "from", x["from_amount"])
        add("fx", key, "to", x["to_amount"])
    for x in app.get("fx_realized") or []:
        key = (x["account_id"], x["realized"]["ccy"])
        add("fx_realized", key, "realized", x["realized"])
    for x in app.get("openings") or []:
        key = (x["account_id"], x["symbol"])
        line = out["openings"].setdefault(key, {})
        line["shares"] = line.get("shares", O.ZERO) + dec(x["shares"])
        add("openings", key, "original_cost_total", x["original_cost_total"])
    return out


def _oracle_activity(act: O.Activity) -> dict[str, dict]:
    """``oracle.Activity`` under the fold's group names (a 配股 line has no currency: ``-``)."""
    dividends = {}
    for (t, a, ccy), line in act.dividends.items():
        dividends[(t, a, "-" if t == "STOCK" else ccy)] = line
    return {"realized": act.realized, "dividends": dividends,
            "not_booked": {k: v for k, v in act.not_booked.items()}, "fees": act.fees,
            "cash": act.cash, "fx": act.fx, "fx_realized": act.fx_realized,
            "openings": act.openings}


#: Activity fields whose native sums carry 28-digit quotients (a realized amount removes cost
#: through an average): ``check_rel`` — association only. Every other native is exact.
_QUOTIENT_FIELDS = {("realized", "realized"), ("realized", "capital_gain"),
                    ("fx_realized", "realized")}


def _check_activity(ev, prefix: str, scope: str, want: dict[str, dict], have: dict[str, dict],
                    phase: str) -> None:
    """Every activity line (§11.3) of a period: the key sets, the row counts, every native sum
    and its reporting twin (each row at its own date's rate — ``check_rel``, a sum of
    products)."""
    for group in sorted(want):
        w, h = want[group], have.get(group, {})
        ev.check(f"{prefix}.activity.{group}.keys", scope, sorted(map(str, w)),
                 sorted(map(str, h)), phase)
        for key in sorted(w, key=str):
            line, got = w[key], h.get(key, {})
            for field_ in sorted(line):
                exp = line[field_]
                act = got.get(field_)
                sc = f"{scope} {key} {field_}"
                if isinstance(exp, tuple):
                    act_n, act_r = act if isinstance(act, tuple) else (None, None)
                    if (group, field_) in _QUOTIENT_FIELDS:
                        ev.check_rel(f"{prefix}.activity.{group}", sc, exp[0], act_n, phase)
                    else:
                        ev.check(f"{prefix}.activity.{group}", sc, exp[0], act_n, phase)
                    ev.check_rel(f"{prefix}.activity.{group}.reporting", sc, exp[1], act_r,
                                 phase)
                else:
                    ev.check(f"{prefix}.activity.{group}", sc, exp, act, phase)


def _check_period_row(ev, prefix: str, kind: str, row: dict, ctx: _Periods,
                      live_from: date | None, phase: str, *, served: bool = True) -> None:
    """One close against the oracle (spec §11.2–§11.4): its KPI block, the rates it used,
    every ``notes`` field, the settled day's trend figures, its TWR and its activity lines.

    *served* — the row came through ``/api/settlement/periods`` (package G): its ``source`` /
    ``stale`` flags and the ``dividends_receivable`` computed when the close is READ are
    asserted too. ``False`` for a row read straight from ``settled_period`` (the store family,
    ``store_period.*``): those three exist only on the served row.
    """
    period = str(row.get("period"))
    scope = f"{kind} {period}"
    end = _d(row.get("end_day"))
    if end is None:
        ev.check(f"{prefix}.end_day_present", scope, True, False, phase)
        return
    start, day, previous = _period_bounds(kind, period, ctx)
    today = ctx.today
    if end < today.replace(day=1) or (kind == "Y" and end < today):
        want_end = (O.month_end(date.fromisoformat(f"{period}-01")) if kind == "M"
                    else date(int(period), 12, 31))
        ev.check(f"{prefix}.end_day", scope, want_end.isoformat(), end.isoformat(), phase)
        if served:
            ev.check(f"{prefix}.source", scope,
                     "store" if live_from is not None and end < live_from else "live",
                     row.get("source"), phase)
    if served:
        ev.check(f"{prefix}.stale", scope, False, bool(row.get("stale")), phase)
    kpi = row.get("kpi") or {}
    ev.check(f"{prefix}.reporting_ccy", scope, REPORTING, kpi.get("reporting_ccy"), phase)
    pe = ctx.at(day)
    _check_kpi_values(ev, prefix, scope, pe, {
        "total_market_value": kpi.get("total_market_value"),
        "total_return": kpi.get("total_return"),
        "total_return_rate": kpi.get("total_return_rate"),
        "xirr": kpi.get("xirr"), "xirr_window_days": kpi.get("xirr_window_days"),
        "xirr_reason": kpi.get("xirr_reason"),
        "by_currency_value": kpi.get("by_currency_value"),
    }, phase)

    # The rates used and their dates (§11.2 "FX": the rates used and their dates are stored).
    used = kpi.get("fx_used") or []
    ev.check(f"{prefix}.fx_used.pairs", scope,
             sorted("/".join(sorted(_pair_key(f"{c}/{REPORTING}"))) for c in pe.rates),
             sorted("/".join(sorted(_pair_key(u.get("pair", "")))) for u in used), phase)
    for u in used:
        base, _sep, quote = str(u.get("pair", "")).partition("/")
        want_rate = O.fx_rate_at(ctx.fx_rows, base, quote, day)
        ev.check(f"{prefix}.fx_used.rate", f"{scope} {u.get('pair')}",
                 None if want_rate is None else want_rate[0], u.get("rate"), phase)
        ev.check(f"{prefix}.fx_used.as_of", f"{scope} {u.get('pair')}",
                 None if want_rate is None else want_rate[1].isoformat(), u.get("as_of"), phase)

    notes = kpi.get("notes") or {}
    ev.check(f"{prefix}.notes.missing_prices", scope, sorted(pe.missing_prices),
             sorted(notes.get("missing_prices") or []), phase)
    ev.check(f"{prefix}.notes.stale_prices", scope,
             sorted(f"{s}@{d.isoformat()}" for s, d in pe.stale_prices.items()),
             sorted(f"{p.get('symbol')}@{p.get('price_date')}"
                    for p in notes.get("stale_prices") or []), phase)
    ev.check(f"{prefix}.notes.missing_fx", scope, sorted(pe.missing_fx),
             sorted(notes.get("missing_fx") or []), phase)
    ev.check(f"{prefix}.notes.stale_fx", scope,
             sorted(f"{'/'.join(sorted(_pair_key(f'{c}/{REPORTING}')))}@{d.isoformat()}"
                    for c, (_r, d) in pe.rates.items()
                    if (day - d).days > O.STALE_AFTER_DAYS),
             sorted(f"{'/'.join(sorted(_pair_key(p.get('pair', ''))))}@{p.get('rate_date')}"
                    for p in notes.get("stale_fx") or []), phase)
    # The scheduler is off on this instance: no quote run, so no close is ever unconfirmed.
    ev.check(f"{prefix}.notes.unconfirmed_closes", scope, [],
             list(notes.get("unconfirmed_closes") or []), phase)
    ev.check(f"{prefix}.notes.pending_positions", scope,
             sorted("|".join(p) for p in pe.pending),
             sorted(f"{p.get('account_id')}|{p.get('symbol')}|{p.get('kind')}"
                    for p in notes.get("pending_positions") or []), phase)
    ev.check(f"{prefix}.notes.options_value_excluded", f"{scope} ({start}..{day})",
             ctx.options_excluded(start, day), bool(notes.get("options_value_excluded")),
             phase)
    ev.check(f"{prefix}.notes.options_past_expiry", scope, pe.options_past_expiry,
             sorted(notes.get("options_past_expiry") or []), phase)
    if served:
        got_recv = notes.get("dividends_receivable")
        ev.check(f"{prefix}.notes.dividends_receivable", scope,
                 sorted(f"{a}|{s}|{x.isoformat()}|{p.isoformat()}|{_cv(n)}"
                        for a, s, x, p, n in pe.receivable),
                 None if got_recv is None else sorted(
                     f"{r.get('account_id')}|{r.get('symbol')}|{r.get('ex_date')}|"
                     f"{r.get('pay_date')}|{_cv(dec(r.get('net')))}" for r in got_recv),
                 phase)

    # The period's TWR (§11.2): I(D) / I(previous end) − 1 on the oracle's own index.
    twr, why = ctx.twr(previous, day)
    if why != "not_judged":
        got_twr = kpi.get("period_twr")
        if twr is None or twr == O.ZERO:
            ev.check(f"{prefix}.period_twr", scope, twr, got_twr, phase)
        else:
            ev.check_rel(f"{prefix}.period_twr", scope, twr, got_twr, phase)
        if why is not None:
            _check_reason_word(ev, f"{prefix}.period_twr_reason", f"{scope} [{why}]",
                               _TWR_REASON_WORDS[why], kpi.get("period_twr_reason"), phase)

    # The period's activity lines (§11.3), keyed so the app's grouping folds onto them.
    _check_activity(ev, prefix, scope, _oracle_activity(ctx.activity(start, day)),
                    _fold_activity(row.get("activity") or {}), phase)

    # The settled day D's trend figures (stocks only; a partial sum on an incomplete day).
    want = ctx.trend(day)
    if want is None:
        ev.check(f"{prefix}.trend_unavailable", scope, True,
                 bool(notes.get("trend_unavailable")), phase)
        # With no trend point the flag states the KPI block's OWN completeness (W2-D): a
        # blank figure, a missing price or rate, or a 待釐清 position.
        ev.check(f"{prefix}.notes.incomplete", f"{scope} (no trend point)",
                 pe.total_market_value is None or pe.total_return is None
                 or bool(pe.missing_prices or pe.missing_fx or pe.pending),
                 bool(notes.get("incomplete")), phase)
        return
    ev.check(f"{prefix}.net_invested", scope, want["net_invested"], kpi.get("net_invested"),
             phase)
    if want.get("refused_action"):
        return
    ev.check(f"{prefix}.notes.incomplete", scope, want["incomplete"],
             bool(notes.get("incomplete")), phase)
    if "total_value" in want:
        ev.check(f"{prefix}.trend_total_value", scope, want["total_value"],
                 kpi.get("trend_total_value"), phase)
        ev.check(f"{prefix}.net_worth", scope, want["net_worth"], kpi.get("net_worth"), phase)


def _check_year_against_months(ev, prefix: str, year_row: dict, month_rows: dict[str, dict],
                               ctx: _Periods, phase: str) -> None:
    """Spec §18 "Period closes" on the app's own rows: a CLOSED year's KPIs ≡ its December
    row's, and for every FLOW line the year's activity ≡ Σ its months (balances are not
    summed). Realized sums are quotients: ``check_rel`` (Σ in another grouping, association
    only)."""
    year = str(year_row.get("period"))
    dec_row = month_rows.get(f"{year}-12")
    scope = f"Y {year}"
    if dec_row is not None and date(int(year), 12, 31) < ctx.today:
        ykpi, mkpi = year_row.get("kpi") or {}, dec_row.get("kpi") or {}
        for field_ in ("total_market_value", "total_return", "total_return_rate", "xirr",
                       "xirr_window_days", "net_invested", "trend_total_value", "net_worth"):
            ev.check(f"{prefix}.year_equals_december", f"{scope} {field_}", mkpi.get(field_),
                     ykpi.get(field_), phase)
    months = [r for p, r in month_rows.items() if p.startswith(f"{year}-")]
    if not months:
        return
    summed: dict[str, dict] = {}
    for m in months:
        for group, lines in _fold_activity(m.get("activity") or {}).items():
            for key, line in lines.items():
                acc = summed.setdefault(group, {}).setdefault(key, {})
                for field_, value in line.items():
                    if isinstance(value, tuple):
                        n, r = acc.get(field_, (O.ZERO, O.ZERO))
                        acc[field_] = (n + value[0],
                                       None if r is None or value[1] is None else r + value[1])
                    else:
                        acc[field_] = acc.get(field_, 0) + value
    yearly = _fold_activity(year_row.get("activity") or {})
    for group in sorted(summed):
        ev.check(f"{prefix}.year_sum_of_months.keys", f"{scope} {group}",
                 sorted(map(str, summed[group])), sorted(map(str, yearly.get(group, {}))), phase)
        for key, line in summed[group].items():
            for field_, value in line.items():
                got = yearly.get(group, {}).get(key, {}).get(field_)
                sc = f"{scope} {group} {key} {field_}"
                if isinstance(value, tuple):
                    ev.check_rel(f"{prefix}.year_sum_of_months", sc, value[0],
                                 got[0] if isinstance(got, tuple) else None, phase)
                else:
                    ev.check(f"{prefix}.year_sum_of_months", sc, value, got, phase)


def _check_period_positions(ev, prefix: str, scope: str, ctx: _Periods, day: date,
                            holdings: list[dict], cash_rows: list[dict], phase: str) -> None:
    """Holdings and cash at D (spec §11.3 "Holdings at D" / "cash at D") against the oracle's
    book at D: every position (stocks AND options, tagged ``asset_type``), its costs, flags and
    — when it has an honest value — the price used, its date, market value and unrealized;
    every cash pool's end-of-day balance (booked dividends only, ⑪) and its reporting value."""
    pe = ctx.at(day)
    have = {(h.get("account_id"), h.get("symbol")): h for h in holdings}
    want = {k: h for k, h in pe.res.holdings.items() if h.shares != O.ZERO}
    ev.check(f"{prefix}.holdings.keyset", scope, sorted("|".join(k) for k in want),
             sorted(f"{a}|{s}" for a, s in have), phase)
    for key, h in sorted(want.items()):
        a = have.get(key)
        if a is None:
            continue
        sc = f"{scope} {key[0]}|{key[1]}"
        ev.check(f"{prefix}.asset_type", sc, "option" if O.is_option(key[1]) else "stock",
                 a.get("asset_type"), phase)
        ev.check(f"{prefix}.quote_ccy", sc, h.quote_ccy, a.get("quote_ccy"), phase)
        ev.check(f"{prefix}.shares", sc, h.shares, a.get("shares"), phase)
        ev.check(f"{prefix}.original_cost_total", sc, h.original_total,
                 a.get("original_cost_total"), phase)
        ev.check(f"{prefix}.adjusted_cost_total", sc, h.adjusted_total,
                 a.get("adjusted_cost_total"), phase)
        flags = a.get("flags") or {}
        if isinstance(flags, str):
            flags = json.loads(flags or "{}")
        for flag in ("oversold", "short_open", "unbookable_dividend", "unbookable_action"):
            ev.check(f"{prefix}.flags.{flag}", sc, getattr(h, flag), bool(flags.get(flag)),
                     phase)
        if key in pe.unvalued:
            continue
        px = pe.prices.get(key[1])
        if px is None:
            ev.check(f"{prefix}.unpriced", sc, [None, None],
                     [a.get("price"), a.get("market_value")], phase)
            continue
        ev.check(f"{prefix}.price", sc, px[0], a.get("price"), phase)
        ev.check(f"{prefix}.price_as_of", sc, px[1].isoformat(), a.get("price_as_of"), phase)
        ev.check(f"{prefix}.market_value", sc, px[0] * h.shares, a.get("market_value"), phase)
        ev.check_rel(f"{prefix}.unrealized", sc, (px[0] - h.adjusted_avg) * h.shares,
                     a.get("unrealized"), phase)
    cash = {(c.get("account_id"), c.get("ccy")): c for c in cash_rows}
    for pool in sorted(set(pe.res.cash) | set(cash)):
        balance = pe.res.cash.get(pool, O.ZERO)
        got = cash.get(pool)
        sc = f"{scope} {pool[0]}|{pool[1]}"
        ev.check(f"{prefix}.cash.balance", sc, balance,
                 dec(got.get("balance")) if got is not None else O.ZERO, phase)
        if got is None:
            continue
        rate = O.fx_rate_at(ctx.fx_rows, pool[1], REPORTING, day)
        # A zero pool is worth zero in any currency — it needs no rate.
        want_rep = (O.ZERO if balance == O.ZERO else None if rate is None
                    else balance * rate[0])
        ev.check(f"{prefix}.cash.reporting_value", sc, want_rep, got.get("reporting_value"),
                 phase)
        if pool[1] != REPORTING and rate is not None:
            ev.check(f"{prefix}.cash.fx", sc, [rate[0], rate[1].isoformat()],
                     [dec(got.get("fx_rate")) if got.get("fx_rate") is not None else None,
                      got.get("fx_as_of")], phase)


def _check_period_detail(ev, api, period: str, ctx: _Periods, day: date, phase: str) -> None:
    """``/api/settlement/periods/M/{period}``: holdings and cash at D against the oracle's
    book at D (the route's one caller — the 月度成績 page reads the list only)."""
    scope = f"M {period}"
    r = api.get(f"/api/settlement/periods/M/{period}")
    ev.check("period_detail.route", scope, 200, r.status_code, phase)
    if r.status_code != 200:
        return
    body = r.json()
    _check_period_positions(ev, "period_detail", scope, ctx, day, body.get("holdings") or [],
                            body.get("cash") or [], phase)


def _past_months(ctx: _Periods) -> list[date]:
    if ctx.first is None:
        return []
    return O.month_ends(ctx.first, ctx.today.replace(day=1) - timedelta(days=1))


def _reconcile_store_periods(ev, db_path, ctx: _Periods, phase: str) -> None:
    """The PUBLISHED generation's closes, read straight from ``settled_period`` /
    ``settled_period_holding`` / ``settled_period_cash`` (``store_period.*``), against the
    oracle — package D's computation pinned on its own, whatever serves it. Runs on every
    build (no route needed); the served rows (``period.*``, package G) are checked on top.
    Read-only, on the harness's own phase-1 database, after the settlement published."""
    if ctx.first is None:
        return
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        meta = conn.execute("SELECT generation FROM settle_meta WHERE id = 1").fetchone()
        if meta is None or not meta["generation"]:
            return
        gen = int(meta["generation"])
        rows = [dict(r) for r in conn.execute(
            "SELECT kind, period, end_day, kpi, activity FROM settled_period "
            "WHERE generation = ? ORDER BY kind, period", (gen,))]
        holdings = [dict(r) for r in conn.execute(
            "SELECT * FROM settled_period_holding WHERE generation = ?", (gen,))]
        cash = [dict(r) for r in conn.execute(
            "SELECT * FROM settled_period_cash WHERE generation = ?", (gen,))]
    finally:
        conn.close()
    past = _past_months(ctx)
    ev.check("store_period.months", f"generation {gen}", [f"{d:%Y-%m}" for d in past],
             [r["period"] for r in rows if r["kind"] == "M"], phase)
    month_rows: dict[str, dict] = {}
    for r in rows:
        r["kpi"] = json.loads(r["kpi"] or "{}")
        r["activity"] = json.loads(r["activity"] or "{}")
        if r["kind"] == "M":
            month_rows[r["period"]] = r
    for d in past:
        row = month_rows.get(f"{d:%Y-%m}")
        if row is None:
            continue
        _check_period_row(ev, "store_period", "M", row, ctx, None, phase, served=False)
        _check_period_positions(
            ev, "store_period.position", f"M {row['period']}", ctx, d,
            [h for h in holdings if h["kind"] == "M" and h["period"] == row["period"]],
            [c for c in cash if c["kind"] == "M" and c["period"] == row["period"]], phase)
    years = [r for r in rows if r["kind"] == "Y"]
    ev.check("store_period.years", f"generation {gen}",
             [str(y) for y in range(ctx.first.year, ctx.today.year)],
             [r["period"] for r in years], phase)
    for row in years:
        _check_period_row(ev, "store_period", "Y", row, ctx, None, phase, served=False)
        _check_year_against_months(ev, "store_period", row, month_rows, ctx, phase)


def _reconcile_periods(ev, api, ctx: _Periods, phase: str) -> None:
    """Every month and year close, and the 月度成績 rows, against the oracle (spec §11.2,
    §11.4, §15; package G's routes). Runs on a SETTLED store, so every past close is the
    store's (``source = store``), none is stale, and each equals the oracle as at its own end
    day — the "independent 'as at D' computation" §18 asks the stress audit for."""
    if ctx.first is None:
        return
    st = _status(api)
    live_from = _d(st.get("live_from"))
    today = ctx.today
    first_this = today.replace(day=1)
    past = _past_months(ctx)

    code, rows, total = _get_rows(api, "/api/settlement/periods", kind="M")
    ev.check("periods.route", "M", 200, code, phase)
    month_rows: dict[str, dict] = {}
    if rows is not None:
        periods = [str(r.get("period")) for r in rows]
        ev.check("periods.newest_first", "M", sorted(periods, reverse=True), periods, phase)
        ev.check("periods.months", "M past months",
                 [f"{d:%Y-%m}" for d in reversed(past)],
                 [p for r, p in zip(rows, periods, strict=True)
                  if (_d(r.get("end_day")) or today) < first_this], phase)
        ev.check("periods.total_count", "M", True, (total or 0) >= len(past), phase)
        month_rows = {str(r.get("period")): r for r in rows}
        for d in past:
            row = month_rows.get(f"{d:%Y-%m}")
            if row is None:
                continue
            _check_period_row(ev, "period", "M", row, ctx, live_from, phase)
            _check_period_detail(ev, api, f"{d:%Y-%m}", ctx, d, phase)
        current = month_rows.get(f"{today:%Y-%m}")
        if current is not None:
            _check_period_row(ev, "period", "M", current, ctx, live_from, phase)

    code, rows, _total = _get_rows(api, "/api/settlement/periods", kind="Y")
    ev.check("periods.route", "Y", 200, code, phase)
    ev.check("periods.years", "Y (newest first)",
             [str(y) for y in range(today.year, ctx.first.year - 1, -1)],
             [str(r.get("period")) for r in rows or []], phase)
    for row in rows or []:
        _check_period_row(ev, "period", "Y", row, ctx, live_from, phase)
        _check_year_against_months(ev, "period", row, month_rows, ctx, phase)

    _reconcile_snapshots(ev, api, ctx, past, phase)


def _reconcile_snapshots(ev, api, ctx: _Periods, past: list[date], phase: str) -> None:
    """``GET /api/snapshots`` (月度成績, PERF-D7, spec §11.4): its row fields unchanged, the
    current month from the live KPIs (``as_of`` = now), every past month the close at its end
    day D (``as_of`` = D) — so each row equals the oracle as at D, its TWR included."""
    today = ctx.today
    code, rows, _total = _get_rows(api, "/api/snapshots")
    ev.check("snapshots.route", "rows", 200, code, phase)
    if rows is None:
        return
    months = [str(r.get("month")) for r in rows]
    want = [f"{today:%Y-%m}"] + [f"{d:%Y-%m}" for d in reversed(past)]
    ev.check("snapshots.months", "rows (newest first)", want, months[:len(want)], phase)
    first_this = today.replace(day=1)
    for row in rows:
        month = str(row.get("month"))
        end = O.month_end(date.fromisoformat(f"{month}-01"))
        if ctx.first is None or end < ctx.first:
            continue
        past_month = end < first_this
        _start, day, previous = _period_bounds("M", month, ctx)
        ev.check("snapshot.as_of", month, day.isoformat(), str(row.get("as_of"))[:10], phase)
        ev.check("snapshot.reporting_ccy", month, REPORTING, row.get("reporting_ccy"), phase)
        _check_kpi_values(ev, "snapshot", month, ctx.at(day), {
            "total_market_value": row.get("total_value"),
            "total_return": row.get("total_return"),
            "total_return_rate": row.get("total_return_rate"),
            "xirr": row.get("xirr"),
            "xirr_reason": row.get("xirr_reason"),
            "by_currency_value": row.get("by_currency"),
        }, phase)
        twr, why = ctx.twr(previous, day)
        if why != "not_judged":
            got_twr = row.get("period_twr")
            if twr is None or twr == O.ZERO:
                ev.check("snapshot.period_twr", month, twr, got_twr, phase)
            else:
                ev.check_rel("snapshot.period_twr", month, twr, got_twr, phase)
            if why is not None:
                ev.check("snapshot.period_twr_reason", f"{month} [{why}]", True,
                         bool(row.get("period_twr_reason")), phase)
        if past_month:
            ev.check("snapshot.stale", month, False, bool(row.get("stale")), phase)
            # 暫定 only when D is after final_through — every day qualifies with the
            # scheduler off, so no past month of this instance is provisional (§11.4).
            ev.check("snapshot.provisional", month, False, bool(row.get("provisional")), phase)


def _strict_refusal(res: O.OracleResult) -> bool:
    """Whether the tax package's STRICT replay refuses this ledger (§11.3: it raises on every
    refusal and answers 422 — so there is no package to tie to): a refused dividend, a refused
    close, an action it cannot apply, or a 賣超."""
    return bool(res.refused_dividends or res.refused_closes or res.action_refusals
                or res.oversold_keys)


def _reconcile_tax_tieout(ev, api, db_path, ctx: _Periods, phase: str) -> None:
    """Spec §11.3 / §18: the sheet-by-sheet tie-out with the tax package, for every year of
    the ledger — ``realized_gains_<year>.csv`` ≡ the realized TRADE lines (the adjusted and the
    original column), ``dividends_<year>.csv`` ≡ the booked CASH / NET / DRIP dividends,
    ``fx_realized_<year>.csv`` ≡ realized FX — each against the oracle's own year activity;
    on a ledger the strict replay refuses the package answers 422 instead (no package exists
    to tie to)."""
    if ctx.first is None:
        return
    strict = O.replay(O.facts_valued_as_of(ctx.raw, ctx.today))
    for year in range(ctx.first.year, ctx.today.year + 1):
        r = api.post("/api/export/tax-package", {"year": year})
        scope = str(year)
        if _strict_refusal(strict):
            ev.check("tax.strict_replay_refuses", scope, 422, r.status_code, phase)
            continue
        ev.check("tax.package", scope, 200, r.status_code, phase)
        if r.status_code != 200:
            continue
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            sheets = {n: _parse_csv(z.read(n).decode("utf-8-sig")) for n in z.namelist()
                      if n.endswith(".csv")}
        start = max(ctx.first, date(year, 1, 1))
        act = ctx.activity(start, min(date(year, 12, 31), ctx.today))
        want_real: dict[str, list[Decimal]] = {}
        for (kind, _atype, ccy), line in act.realized.items():
            if kind != "trade":
                continue
            acc = want_real.setdefault(ccy, [O.ZERO, O.ZERO, 0])
            acc[0] += line["realized"][0]
            acc[1] += line["capital_gain"][0]
            acc[2] += int(line["rows"])
        have_real: dict[str, list[Decimal]] = {}
        for row in sheets.get(f"realized_gains_{year}.csv", []):
            acc = have_real.setdefault(row["quote_ccy"], [O.ZERO, O.ZERO, 0])
            acc[0] += dec(row["realized_adjusted"])
            acc[1] += dec(row["realized_original"])
            acc[2] += 1
        ev.check("tax.realized.currencies", scope, sorted(want_real), sorted(have_real), phase)
        for ccy, (adj, orig, n) in sorted(want_real.items()):
            got = have_real.get(ccy, [None, None, None])
            ev.check_rel("tax.realized_adjusted", f"{scope} {ccy}", adj, got[0], phase)
            ev.check_rel("tax.realized_original", f"{scope} {ccy}", orig, got[1], phase)
            ev.check("tax.realized_rows", f"{scope} {ccy}", n, got[2], phase)
        want_div: dict[str, list[Decimal]] = {}
        for (dtype, _acct, ccy), line in act.dividends.items():
            if dtype == "STOCK":
                continue
            acc = want_div.setdefault(ccy, [O.ZERO, O.ZERO, O.ZERO])
            for i, f in enumerate(("gross", "withholding", "net")):
                acc[i] += line[f][0]
        have_div: dict[str, list[Decimal]] = {}
        for row in sheets.get(f"dividends_{year}.csv", []):
            if str(row["type"]).upper() == "STOCK":
                continue
            acc = have_div.setdefault(row["ccy"], [O.ZERO, O.ZERO, O.ZERO])
            for i, f in enumerate(("gross", "withholding", "net")):
                acc[i] += dec(row[f] or "0")
        ev.check("tax.dividends.currencies", scope, sorted(want_div), sorted(have_div), phase)
        for ccy, sums in sorted(want_div.items()):
            ev.check("tax.dividends", f"{scope} {ccy} gross/withholding/net", sums,
                     have_div.get(ccy), phase)
        want_fx: dict[str, Decimal] = {}
        for (_acct, home), line in act.fx_realized.items():
            want_fx[home] = want_fx.get(home, O.ZERO) + line["realized"][0]
        have_fx: dict[str, Decimal] = {}
        for row in sheets.get(f"fx_realized_{year}.csv", []):
            have_fx[row["home_ccy"]] = have_fx.get(row["home_ccy"], O.ZERO) + dec(row["realized"])
        ev.check("tax.fx_realized.currencies", scope, sorted(want_fx), sorted(have_fx), phase)
        for ccy, amount in sorted(want_fx.items()):
            ev.check_rel("tax.fx_realized", f"{scope} {ccy}", amount, have_fx.get(ccy), phase)


def check_live_window_row(ev: C.Evidence, api: C.Api, db_path, label: str,
                          day: date) -> None:
    """A change dated inside the live window (on or after ``W``) — WITHOUT a run (spec §10
    step 3 "dirty inside the live window", §11.4 "a correction dated last month changes last
    month's row at once (a test pins it)"):

    * the status: ``dirty_from`` = the change's day, NO stale range, ``live_from`` =
      ``min(S + 1, dirty_from)``, no prompt;
    * (read path) last month's close is served LIVE (``source = live``, not stale) and already
      equals the oracle over the CHANGED ledger, as does its 月度成績 row.
    """
    phase = f"phase1:{label}"
    st = _status(api)
    settled = _d(st.get("settled_through"))
    ev.check("live_window.dirty_from", label, day.isoformat(), st.get("dirty_from"), phase)
    ev.check("live_window.nothing_stale", label, [None, None],
             [st.get("stale_from"), st.get("stale_to")], phase)
    if settled is not None:
        ev.check("live_window.live_from", label,
                 min(settled + timedelta(days=1), day).isoformat(), st.get("live_from"), phase)
    ev.check("live_window.no_prompt", label, False, bool(st.get("needs_prompt")), phase)
    if not READ_PATH:
        return
    raw = C.load_facts_from_db(db_path)
    today = CHECKPOINT.today if CHECKPOINT.today != date.min else server_today()
    closes, fx_rows = fixture_closes(raw), fixture_fx_rows()
    ctx = _Periods(raw, closes, fx_rows, today,
                   _TrendOracle(O.facts_valued_as_of(raw, today), closes, fx_rows, today))
    end = O.month_end(day)
    period = f"{end:%Y-%m}"
    code, rows, _total = _get_rows(api, "/api/settlement/periods", kind="M")
    ev.check("live_window.periods_route", label, 200, code, phase)
    row = next((r for r in rows or [] if str(r.get("period")) == period), None)
    ev.check("live_window.row_present", f"{label} M {period}", True, row is not None, phase)
    if row is not None:
        ev.check("live_window.row_source", f"{label} M {period}", "live", row.get("source"),
                 phase)
        ev.check("live_window.row_not_stale", f"{label} M {period}", False,
                 bool(row.get("stale")), phase)
        kpi = row.get("kpi") or {}
        _check_kpi_values(ev, "live_window.period", f"{label} M {period}", ctx.at(end), {
            "total_market_value": kpi.get("total_market_value"),
            "total_return": kpi.get("total_return"),
            "total_return_rate": kpi.get("total_return_rate"),
            "xirr": kpi.get("xirr"), "xirr_window_days": kpi.get("xirr_window_days"),
            "by_currency_value": kpi.get("by_currency_value"),
        }, phase)
    code, rows, _total = _get_rows(api, "/api/snapshots")
    snap = next((r for r in rows or [] if str(r.get("month")) == period), None)
    ev.check("live_window.snapshot_present", f"{label} {period}", True, snap is not None, phase)
    if snap is not None:
        _check_kpi_values(ev, "live_window.snapshot", f"{label} {period}", ctx.at(end), {
            "total_market_value": snap.get("total_value"),
            "total_return": snap.get("total_return"),
            "total_return_rate": snap.get("total_return_rate"),
            "xirr": snap.get("xirr"),
            "by_currency_value": snap.get("by_currency"),
        }, phase)


def period_kpi_of(api: C.Api, kind: str, period: str) -> dict | None:
    """One close's KPI block from the list route (``None`` when absent) — for the scenario's
    before / after literals."""
    _code, rows, _total = _get_rows(api, "/api/settlement/periods", kind=kind)
    row = next((r for r in rows or [] if str(r.get("period")) == period), None)
    return None if row is None else (row.get("kpi") or {})


def period_holding_of(api: C.Api, period: str, account_id: str, symbol: str) -> dict | None:
    """One position of a month close (the detail route)."""
    r = api.get(f"/api/settlement/periods/M/{period}")
    if r.status_code != 200:
        return None
    return next((h for h in r.json().get("holdings") or []
                 if h.get("account_id") == account_id and h.get("symbol") == symbol), None)


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
ACKED_OVERSELLS = {("tw_broker", "0050"), ("tw_broker", "OVF")}


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


def _reconcile_kpis(ev, res, dash, facts, prices, sp, phase, closes, trend_oracle):
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
    # check_rel, not check (2026-10-19, PERF wave 3): the TWD realized total sums rows that
    # carry 28-digit quotients (the 2609 cover, the 1/3 ratio sale), and a sum of those rounds
    # in the 28th digit by ADDITION ORDER. It agreed exactly until the month-end block added
    # three more TWD realized rows; from then the two orders landed 1 ULP apart (…396 vs
    # …397) while every row compared exactly in `realized.*`. Association, not money — the
    # rule `Evidence.check_rel` states.
    ev.check_rel("kpi.realized_total", "TWD", realized_total, kpis.get("realized_total"),
                 phase)
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
    # B also needs a trend at all (every flow converted at its own date's rate), and it is a
    # 待釐清 RECORD figure like XIRR: a 賣超 the book recorded — netted back to zero included
    # (CR-11) — or an action it could not apply withholds it as well.
    b_withheld = (not trend_oracle.available
                  or _day_incomplete(res, as_of, closes, facts.actions)
                  or bool(res.refused_closes) or bool(res.oversold_keys)
                  or bool(res.action_refusals))
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

    if res.oversold_keys:
        # CR-01 / CR-11: a 賣超 the book recorded — a FLAT one included (its basis is gone for
        # good, and the book keeps it) — withholds XIRR, and the reason names each position.
        ev.check("xirr.withheld_for_oversold", "TWD", None, app_xirr, phase)
        reason = (dash.get("freshness") or {}).get("xirr_unavailable_reason") or ""
        for acct, sym in sorted(res.oversold_keys):
            ev.check("xirr.oversold_reason_names_row", f"{acct}/{sym}", True,
                     sym in reason and acct in reason, phase)
        return
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
    # Stock rows only: the holdings CSV is the stock table, and an open contract leaves in its
    # own options CSV (spec §9, owner ruling 2026-10-10: B), which this family does not read.
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
