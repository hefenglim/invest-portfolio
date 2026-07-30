"""Independent Decimal-only accounting oracle for portfolio-dash stress verification.

INDEPENDENCE STATEMENT (non-negotiable design property #1 — do not weaken)
--------------------------------------------------------------------------
This module imports NOTHING from ``portfolio_dash``. Every accounting formula here is
derived solely from the project rule documents:
  - .claude/rules/domain-ledger.md    (cost basis, dividends, realized P&L, FX pool, XIRR)
  - .claude/rules/markets-and-fees.md  (fee/tax skeletons per account)
  - .claude/rules/data-and-pricing.md  (Decimal precision, per-currency minor units)

Numeric PARAMETERS (fee rates, min fees, minor units) are transcribed from the app's
seeded config (portfolio_dash/data_ingestion/config_seed.py FEE_RULES + DEFAULT_ACCOUNTS)
as constants below — parameters-from-config is explicitly allowed; the LOGIC is ours.
Because the logic is re-derived from the rules (not imported), a bug in the app's
calculation cannot hide behind a shared code path: the two implementations only agree
when both are right.

TWO LAYERS (non-negotiable design property #2 — keep them independent)
---------------------------------------------------------------------
1. FEE ENGINE oracle (``fee_tax``): recomputes expected fee/tax from the rules, to be
   compared against the app's stored fee/tax. This is the ONLY layer that depends on
   the (rule-skeleton) fee formula; documented assumptions are flagged.
2. BOOKKEEPING oracle (``replay``): replays the raw ledger FACTS (rows the harness
   submitted / read back) to derive holdings, realized P&L, cash pools and FX P&L.
   It takes each trade's fee/tax as a GIVEN ledger fact (exactly like price/qty), so
   bookkeeping correctness is verified INDEPENDENTLY of whether the fee engine is right.

All money is Decimal; no float anywhere — EXCEPT the XIRR scalar solver at the bottom,
which is an inherently numeric root-find (no closed form). That single figure is the
ONE documented-tolerance comparison in the whole suite (see ``XIRR_TOL``); every other
assertion is exact-Decimal with no tolerance.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal

D = Decimal
ZERO = D("0")
ONE = D("1")
CENT = D("0.01")

# --- per-currency minor units (data-and-pricing.md) --------------------------------
MINOR_UNITS = {"TWD": 0, "USD": 2, "MYR": 2}

# --- account -> (fee_rule, settlement_ccy, funding_ccy) (config_seed DEFAULT_ACCOUNTS) ---
# Batch B (2026-07-21 merge): the two legacy Moomoo accounts (moomoo_my_us + moomoo_my_my) are
# MERGED into ONE dual-market account ``moomoo_my`` — a single brokerage account that settles US
# trades in USD (funded via MYR->USD) AND holds MY-market MYR stocks, so settlement_ccy=USD /
# funding_ccy=MYR (mirrors config_seed.DEFAULT_ACCOUNTS). The fee_rule slot is None because a
# dual-market account has NO single fee rule: routing is per-market via ACCOUNT_MARKET_RULE /
# fee_rule_for(). settlement/funding still define the account's ONE FX-exposed pool (USD vs MYR).
ACCOUNTS: dict[str, tuple[str | None, str, str]] = {
    "tw_broker": ("tw", "TWD", "TWD"),
    "schwab": ("schwab", "USD", "TWD"),
    "moomoo_my": (None, "USD", "MYR"),   # dual-market; fee rule resolved per market
}

# --- (account, market) -> fee_rule_set (config_seed account_market_rules bindings) ----------
# The app resolves a trade's fee rule by (account, instrument.market) via fee_rule_for() over the
# account_market_rules table (data_ingestion/rules_binding.py). We re-derive the same mapping here
# (independent of the app) so a moomoo_my US trade books moomoo_us fees and a moomoo_my MY trade
# books moomoo_my fees — the merged-account fee proof. Single-market accounts have one binding row.
ACCOUNT_MARKET_RULE: dict[tuple[str, str], str] = {
    ("tw_broker", "TW"): "tw",
    ("schwab", "US"): "schwab",
    ("moomoo_my", "US"): "moomoo_us",
    ("moomoo_my", "MY"): "moomoo_my",
}

# --- fee-rule parameters (fee-engine v2, transcribed from config_seed.FEE_RULES) ---
# Parameters-from-config is allowed (independence rule); the LOGIC in fee_tax is re-derived
# from the mini-spec + reference doc, so a bug in fees.py cannot hide behind a shared path.
FEE_RULES = {
    "tw": dict(brokerage=D("0.001425"), discount=D("1"), min_fee=D("20"),
               tax_normal=D("0.003"), tax_etf=D("0.001"), tax_daytrade=D("0.0015"),
               rounding="floor", ccy="TWD"),
    "schwab": dict(sec_rate=D("0.0000206"), sec_min=D("0.01"), taf_per_share=D("0.000195"),
                   taf_min=D("0.01"), taf_cap=D("9.79"), ccy="USD"),
    "moomoo_us": dict(commission_rate=D("0.0003"), commission_min=D("0.01"),
                      platform=D("0.99"), settlement_per_share=D("0.003"),
                      settlement_cap_rate=D("0.01"), cat_per_share=D("0.000003"),
                      sec_rate=D("0.0000206"), sec_min=D("0.01"), taf_per_share=D("0.000195"),
                      taf_min=D("0.01"), taf_cap=D("9.79"), stamp_unit=D("1000"),
                      stamp_per_unit=D("1"), stamp_cap_stock=D("1000"), stamp_cap_etf=D("200"),
                      ccy="USD"),
    "moomoo_my": dict(commission_rate=D("0.0003"), commission_min=D("0.01"),
                      platform=D("3.00"), clearing_rate=D("0.0003"), clearing_cap=D("1000"),
                      sst_rate=D("0.08"), stamp_unit=D("1000"), stamp_per_unit=D("1"),
                      stamp_cap_stock=D("1000"), stamp_cap_etf=D("0"), ccy="MYR"),
}

# Map an /api/fee-rules field key onto this table's parameter name. Only keys listed here
# are overlayable; anything else in the payload is ignored on purpose.
_EFFECTIVE_KEYS = {
    "brokerage": "brokerage", "discount": "discount", "min_fee": "min_fee",
    "tax_normal": "tax_normal", "tax_etf": "tax_etf", "tax_daytrade": "tax_daytrade",
    "commission_rate": "commission_rate", "commission_min": "commission_min",
    "platform_fee": "platform", "settlement_per_share": "settlement_per_share",
    "settlement_cap_rate": "settlement_cap_rate", "cat_per_share": "cat_per_share",
    "sec_rate": "sec_rate", "sec_min": "sec_min", "taf_per_share": "taf_per_share",
    "taf_min": "taf_min", "taf_cap": "taf_cap", "clearing_rate": "clearing_rate",
    "clearing_cap": "clearing_cap", "sst_rate": "sst_rate",
    "stamp_unit": "stamp_unit", "stamp_per_unit": "stamp_per_unit",
    "stamp_cap_stock": "stamp_cap_stock", "stamp_cap_etf": "stamp_cap_etf",
}


def apply_effective_rules(effective: dict[str, dict[str, str]]) -> list[str]:
    """Overlay the instance's EFFECTIVE fee-rule RATES onto this table; return the diffs.

    Independence is preserved: only numeric PARAMETERS move, never the formulas in
    :func:`fee_tax` (`markets-and-fees.md`: rates live in config, logic does not). Without
    this the oracle asserts the seed defaults, so a live instance carrying a settings
    override — the demo has ``discount`` overridden to 0.23 — fails on every TW trade for
    a reason that is configuration, not a defect.
    """
    diffs: list[str] = []
    for name, fields in effective.items():
        target = FEE_RULES.get(name)
        if target is None:
            continue
        for key, val in fields.items():
            param = _EFFECTIVE_KEYS.get(key)
            if param is None or param not in target or val is None:
                continue
            new = D(str(val))
            if target[param] != new:
                diffs.append(f"{name}.{param}: seed {target[param]} -> effective {new}")
                target[param] = new
    return diffs

CASH_DIVIDEND_TYPES = {"CASH", "NET"}  # domain-ledger.md: TW cash + MY single-tier net


def _round(value: Decimal, places: int) -> Decimal:
    q = D(1).scaleb(-places)
    return value.quantize(q, rounding=ROUND_HALF_UP)


def _cent(value: Decimal) -> Decimal:
    """US/MY per-component minor unit, ROUND_HALF_UP."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _floor_int(value: Decimal) -> Decimal:
    """TW integer NT$, ROUND_DOWN (FE-D3, 角以下免收)."""
    return value.quantize(ONE, rounding=ROUND_DOWN)


def _ceil_int(value: Decimal) -> Decimal:
    """Stamp-duty lot count, ceil to integer."""
    return value.quantize(ONE, rounding=ROUND_CEILING)


def fee_rule_for(account_id: str, market: str | None) -> str:
    """Resolve the fee-rule-set name for a trade, per-market (Batch B dual-market merge).

    Mirrors the app's rules_binding.fee_rule_for over account_market_rules: a trade's rule binds
    to (account, instrument.market). For a SINGLE-market account ``market`` may be omitted (its
    sole binding is used); the merged dual-market ``moomoo_my`` REQUIRES a market so a US trade
    routes to ``moomoo_us`` and an MY trade to ``moomoo_my``. Raises KeyError when the account is
    multi-market and no matching market is supplied — the harness never guesses a fee regime.
    """
    if market is not None and (account_id, market) in ACCOUNT_MARKET_RULE:
        return ACCOUNT_MARKET_RULE[(account_id, market)]
    rules = {m: r for (a, m), r in ACCOUNT_MARKET_RULE.items() if a == account_id}
    if len(rules) == 1:
        return next(iter(rules.values()))
    raise KeyError(
        f"account {account_id!r} is multi-market; a valid market is required (got {market!r})")


# ===================================================================================
# LAYER 1 — FEE ENGINE ORACLE (rule-derived; compared vs app-stored fee/tax)
# ===================================================================================
def fee_tax(account_id: str, side: str, qty: Decimal, price: Decimal,
            is_etf: bool, daytrade: bool = False,
            stamp_fx: Decimal | None = None,
            market: str | None = None) -> tuple[Decimal, Decimal, list[str]]:
    """Return (fee, tax, notes[]) expected from the fee-engine v2 spec (independent derive).

    ``side`` in {"BUY","SELL"}. Logic is re-derived from the mini-spec + reference doc
    (NOT imported from fees.py); parameters are transcribed above. ``notes`` records the
    rule-silent mechanical choices so a mismatch can be triaged.

    Rounding: TW floors to integer NT$ (FE-D3); US/MY quantize each component to the cent
    (ROUND_HALF_UP) then sum. ``is_etf`` is the instrument-REGISTRY flag (TW sell tax rate;
    MY/US stamp cap). ``daytrade`` is the per-transaction TW flag. ``stamp_fx`` is the
    trade-date USD/MYR rate for the Moomoo US MY stamp (FE-D2); None -> stamp 0. ``market`` is
    the instrument's market (US/TW/MY): it routes the per-market fee rule for the merged
    dual-market ``moomoo_my`` (Batch B) and may be omitted for single-market accounts.
    """
    rule_name = fee_rule_for(account_id, market)
    r = FEE_RULES[rule_name]
    notional = qty * price
    fee = ZERO
    tax = ZERO
    notes: list[str] = []
    if notional <= ZERO:
        return fee, tax, ["zero/negative notional -> no fee/tax"]

    if rule_name == "tw":
        floored = _floor_int(notional * r["brokerage"] * r["discount"])
        fee = max(floored, r["min_fee"])            # min applies AFTER the floor (FE-D3)
        notes.append("tw v2: fee=max(floor(notional*0.001425*1), 20) [FE-D3 floor]")
        if side == "SELL":
            if daytrade:
                rate = r["tax_daytrade"]
            elif is_etf:
                rate = r["tax_etf"]
            else:
                rate = r["tax_normal"]
            tax = _floor_int(notional * rate)
            notes.append(f"tw v2: tax=floor(notional*{rate}) is_etf={is_etf} daytrade={daytrade}")
    elif rule_name in ("schwab", "moomoo_us"):
        if "commission_rate" in r:
            fee += _cent(max(notional * r["commission_rate"], r["commission_min"]))
        if "platform" in r:
            fee += _cent(r["platform"])
        if "settlement_per_share" in r:
            cap = r["settlement_cap_rate"] * notional
            fee += _cent(min(r["settlement_per_share"] * qty, cap))
        if "cat_per_share" in r:
            fee += _cent(r["cat_per_share"] * qty)
        if side == "SELL":
            fee += _cent(max(notional * r["sec_rate"], r["sec_min"]))
            taf = max(qty * r["taf_per_share"], r["taf_min"])
            if taf > r["taf_cap"]:
                taf = r["taf_cap"]
            fee += _cent(taf)
        notes.append(f"{rule_name} v2: Σ per-component cent-quantized; SELL adds SEC+TAF")
        if "stamp_unit" in r:                        # moomoo_us MY stamp (FE-D2)
            if stamp_fx is None or stamp_fx <= ZERO:
                notes.append("moomoo_us v2: no USD/MYR rate -> stamp 0")
            else:
                amt_myr = notional * stamp_fx
                stamp_myr = _ceil_int(amt_myr / r["stamp_unit"]) * r["stamp_per_unit"]
                cap = r["stamp_cap_etf"] if is_etf else r["stamp_cap_stock"]
                if stamp_myr > cap:
                    stamp_myr = cap
                tax = _cent(stamp_myr / stamp_fx)
                notes.append("moomoo_us v2: MY stamp computed in MYR, booked USD (FE-D2)")
    elif rule_name == "moomoo_my":
        commission = _cent(max(notional * r["commission_rate"], r["commission_min"]))
        platform = _cent(r["platform"])
        clr = notional * r["clearing_rate"]
        if clr > r["clearing_cap"]:
            clr = r["clearing_cap"]
        clearing = _cent(clr)
        sst = _cent(r["sst_rate"] * (commission + platform + clearing))
        fee = commission + platform + clearing + sst
        stamp_myr = _ceil_int(notional / r["stamp_unit"]) * r["stamp_per_unit"]
        cap = r["stamp_cap_etf"] if is_etf else r["stamp_cap_stock"]
        if stamp_myr > cap:
            stamp_myr = cap
        tax = _cent(stamp_myr)
        notes.append("moomoo_my v2: comm+platform+clearing+SST(8%); stamp step ceil(n/1000)"
                     "×RM1; ETF cap 0 => exempt")
    return fee, tax, notes


# ===================================================================================
# LAYER 2 — BOOKKEEPING ORACLE (replays raw ledger facts)
# ===================================================================================
@dataclass
class TxFact:
    id: int
    account_id: str
    symbol: str
    side: str          # BUY | SELL
    qty: Decimal
    price: Decimal
    fee: Decimal
    tax: Decimal
    trade_date: date


@dataclass
class DivFact:
    id: int
    account_id: str
    symbol: str
    d: date
    type: str          # CASH | NET | DRIP | STOCK
    gross: Decimal
    withholding: Decimal
    net: Decimal
    reinvest_shares: Decimal | None
    reinvest_price: Decimal | None


@dataclass
class FxFact:
    id: int
    account_id: str
    d: date
    from_ccy: str
    from_amt: Decimal
    to_ccy: str
    to_amt: Decimal


@dataclass
class OpenFact:
    account_id: str
    symbol: str
    shares: Decimal
    orig_avg: Decimal
    orig_total: Decimal
    build_date: date


@dataclass
class CashFact:
    id: int
    account_id: str
    d: date
    kind: str          # DEPOSIT | WITHDRAW | OPENING | REBATE
    ccy: str
    amount: Decimal
    # Home-ccy cost of a FOREIGN credit (spec 2026-07-30). None = cost unknown.
    acq_home_amount: Decimal | None = None


@dataclass
class Instrument:
    symbol: str
    market: str
    quote_ccy: str
    is_etf: bool
    sector: str = ""


@dataclass
class Facts:
    txs: list[TxFact] = field(default_factory=list)
    divs: list[DivFact] = field(default_factory=list)
    fxs: list[FxFact] = field(default_factory=list)
    openings: list[OpenFact] = field(default_factory=list)
    cash: list[CashFact] = field(default_factory=list)
    instruments: dict[str, Instrument] = field(default_factory=dict)


@dataclass
class Holding:
    account_id: str
    symbol: str
    quote_ccy: str
    shares: Decimal
    original_total: Decimal
    adjusted_total: Decimal

    @property
    def original_avg(self) -> Decimal:
        return self.original_total / self.shares

    @property
    def adjusted_avg(self) -> Decimal:
        return self.adjusted_total / self.shares

    @property
    def dividend_portion(self) -> Decimal:
        return self.original_total - self.adjusted_total


@dataclass
class RealizedRow:
    account_id: str
    symbol: str
    quote_ccy: str
    sell_date: date
    shares_sold: Decimal
    proceeds_net: Decimal
    original_cost_removed: Decimal
    adjusted_cost_removed: Decimal
    realized: Decimal
    # "sale" | "dividend". A CASH-family dividend whose payment date falls after the position
    # already reached zero shares is realized INCOME: there is no cost left to reduce, so
    # (domain-ledger.md, 2026-07-26) it is booked as a realized row of net = proceeds rather
    # than being absorbed by — and then discarded with — the closed position.
    kind: str = "sale"


@dataclass
class OracleResult:
    holdings: dict[tuple[str, str], Holding]
    realized_rows: list[RealizedRow]
    realized_by_ccy: dict[str, Decimal]
    cash: dict[tuple[str, str], Decimal]
    # FX per account
    fx_avg_rate: dict[str, Decimal | None]
    fx_realized: dict[str, Decimal | None]
    fx_foreign_cash: dict[str, Decimal]
    # Basis-known share of the pool (spec F2); 1 when every foreign funding flow has a cost.
    fx_covered_ratio: dict[str, Decimal]


_PHASE = {"open": 0, "buy": 1, "sell": 2, "div": 3}


def replay(facts: Facts) -> OracleResult:
    """Replay the ledger facts -> holdings, realized, cash, FX pools.

    Same-day ordering derived from domain-ledger / build_book semantics:
      opening(0) -> buy(1) -> sell(2) -> dividend(3); ties broken by DB id
      (insertion order), reproducing the app's stable sort over (date, phase).
    """
    insts = facts.instruments

    def qccy(sym: str) -> str:
        return insts[sym].quote_ccy

    # ------- build the ordered event stream (mirrors build_book event list) -------
    events: list[tuple[date, int, int, str, object]] = []
    # openings first (phase 0); app orders them by (account, symbol)
    for i, o in enumerate(sorted(facts.openings, key=lambda x: (x.account_id, x.symbol))):
        events.append((o.build_date, 0, i, "open", o))
    for t in facts.txs:
        events.append((t.trade_date, _PHASE["buy"] if t.side == "BUY" else _PHASE["sell"],
                       t.id, "tx", t))
    for dv in facts.divs:
        events.append((dv.d, 3, dv.id, "div", dv))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    positions: dict[tuple[str, str], Holding] = {}
    realized_rows: list[RealizedRow] = []

    for _d, _p, _seq, kind, ev in events:
        if kind == "open":
            assert isinstance(ev, OpenFact)
            key = (ev.account_id, ev.symbol)
            pos = positions.setdefault(key, Holding(ev.account_id, ev.symbol,
                                                    qccy(ev.symbol), ZERO, ZERO, ZERO))
            pos.shares += ev.shares
            pos.original_total += ev.orig_total
            pos.adjusted_total += ev.orig_total
        elif kind == "tx":
            assert isinstance(ev, TxFact)
            key = (ev.account_id, ev.symbol)
            pos = positions.setdefault(key, Holding(ev.account_id, ev.symbol,
                                                    qccy(ev.symbol), ZERO, ZERO, ZERO))
            if ev.side == "BUY":
                cost = ev.qty * ev.price + ev.fee + ev.tax   # all-in buy cost
                pos.shares += ev.qty
                pos.original_total += cost
                pos.adjusted_total += cost
            else:
                if ev.qty > pos.shares:
                    # oversell (acked): net negative, drop cost basis, no realized row
                    pos.shares -= ev.qty
                    pos.original_total = ZERO
                    pos.adjusted_total = ZERO
                    continue
                frac = ev.qty / pos.shares
                orig_removed = pos.original_total * frac
                adj_removed = pos.adjusted_total * frac
                proceeds_net = ev.qty * ev.price - ev.fee - ev.tax
                realized_rows.append(RealizedRow(
                    ev.account_id, ev.symbol, qccy(ev.symbol), ev.trade_date, ev.qty,
                    proceeds_net, orig_removed, adj_removed, proceeds_net - adj_removed))
                pos.shares -= ev.qty
                pos.original_total -= orig_removed
                pos.adjusted_total -= adj_removed
        else:  # dividend
            assert isinstance(ev, DivFact)
            key = (ev.account_id, ev.symbol)
            pos = positions.get(key)
            if pos is None:
                raise ValueError(f"dividend for unknown position {key}")
            if ev.type in CASH_DIVIDEND_TYPES:
                if pos.shares == ZERO:
                    # Position already closed when the payout landed (TW/MY pay weeks after
                    # the ex-date). No cost basis remains to reduce, so the net is realized
                    # income — booked exactly once, never absorbed into a dropped position.
                    realized_rows.append(RealizedRow(
                        ev.account_id, ev.symbol, qccy(ev.symbol), ev.d, ZERO,
                        ev.net, ZERO, ZERO, ev.net, "dividend"))
                else:
                    pos.adjusted_total -= ev.net
            else:  # DRIP / STOCK -> add shares at zero cost
                if ev.reinvest_shares is None:
                    raise ValueError(f"{ev.type} needs reinvest_shares for {key}")
                pos.shares += ev.reinvest_shares

    holdings = {k: p for k, p in positions.items() if p.shares != ZERO}

    realized_by_ccy: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for rr in realized_rows:
        realized_by_ccy[rr.quote_ccy] += rr.realized

    cash = _cash_balances(facts)
    fx_avg, fx_real, fx_fcash, fx_cov = _fx_pools(facts)

    return OracleResult(holdings, realized_rows, dict(realized_by_ccy), cash,
                        fx_avg, fx_real, fx_fcash, fx_cov)


def _cash_balances(facts: Facts) -> dict[tuple[str, str], Decimal]:
    """Per (account, ccy) pool (portfolio/cash.py semantics, re-derived from rules):
    +deposit -withdraw; -fx.from +fx.to; -buy(qty*p+fee+tax) +sell(qty*p-fee-tax);
    +cash-family dividend net (CASH/NET). Opening + DRIP/STOCK do not touch cash.
    """
    bal: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for m in facts.cash:
        bal[(m.account_id, m.ccy)] += m.amount if m.kind == "DEPOSIT" else -m.amount
    for c in facts.fxs:
        bal[(c.account_id, c.from_ccy)] -= c.from_amt
        bal[(c.account_id, c.to_ccy)] += c.to_amt
    for t in facts.txs:
        inst = facts.instruments.get(t.symbol)
        if inst is None:
            continue
        if t.side == "BUY":
            bal[(t.account_id, inst.quote_ccy)] -= (t.qty * t.price + t.fee + t.tax)
        else:
            bal[(t.account_id, inst.quote_ccy)] += (t.qty * t.price - t.fee - t.tax)
    for dv in facts.divs:
        inst = facts.instruments.get(dv.symbol)
        if inst is None:
            continue
        if dv.type in CASH_DIVIDEND_TYPES:
            bal[(dv.account_id, inst.quote_ccy)] += dv.net
    return dict(bal)


def _fx_pools(facts: Facts):
    """Per-account FX pool (domain-ledger.md / forex module semantics).

    avg_rate = sum(home cost) / sum(foreign acquired) over home->foreign conversions AND
    foreign cash CREDITS that carry an acq_home_amount (spec 2026-07-30 F1).
    realized_fx = sum over foreign->home reconversions of (home_received - foreign_sold*avg_rate).
    foreign_cash = conversions +/- ; +sale net ; -buy allin ; +CASH dividend net (foreign)
                   ; +/- foreign cash movements (credit/WITHDRAW).
    covered_ratio = basis-known acquisitions / all acquisitions (F2); exactly 1 when nothing
                   is unbased. It scales BOTH unrealized legs (F3) — see phase1's reconcile.
    Only accounts with settlement_ccy != funding_ccy are FX-exposed.
    """
    avg: dict[str, Decimal | None] = {}
    realized: dict[str, Decimal | None] = {}
    fcash: dict[str, Decimal] = {}
    covered: dict[str, Decimal] = {}
    for aid, (_rule, settle, funding) in ACCOUNTS.items():
        if settle == funding:
            continue
        home, foreign = funding, settle
        convs = [c for c in facts.fxs if c.account_id == aid]
        moves = [m for m in facts.cash if m.account_id == aid and m.ccy == foreign]
        tot_home = ZERO
        tot_foreign = ZERO
        unbased = ZERO
        for c in convs:
            if c.from_ccy == home and c.to_ccy == foreign:
                tot_home += c.from_amt
                tot_foreign += c.to_amt
        for m in moves:
            if m.kind.upper() == "WITHDRAW":
                continue          # a disposal changes neither the average nor the coverage
            if m.acq_home_amount is None:
                unbased += m.amount
            else:
                tot_home += m.acq_home_amount
                tot_foreign += m.amount
        a = (tot_home / tot_foreign) if tot_foreign != ZERO else None
        avg[aid] = a
        covered[aid] = (ONE if unbased == ZERO
                        else (tot_foreign / (tot_foreign + unbased)
                              if tot_foreign + unbased > ZERO else ONE))
        if a is None:
            realized[aid] = None
        else:
            r = ZERO
            for c in convs:
                if c.from_ccy == foreign and c.to_ccy == home:
                    r += c.to_amt - c.from_amt * a
            realized[aid] = r
        # foreign cash reconstruction
        cash = ZERO
        for c in convs:
            if c.to_ccy == foreign:
                cash += c.to_amt
            if c.from_ccy == foreign:
                cash -= c.from_amt
        for t in facts.txs:
            if t.account_id != aid:
                continue
            if facts.instruments[t.symbol].quote_ccy != foreign:
                continue
            if t.side == "BUY":
                cash -= t.qty * t.price + t.fee + t.tax
            else:
                cash += t.qty * t.price - t.fee - t.tax
        for dv in facts.divs:
            if dv.account_id != aid:
                continue
            if dv.type == "CASH" and facts.instruments[dv.symbol].quote_ccy == foreign:
                cash += dv.net
        for m in moves:
            cash += -m.amount if m.kind.upper() == "WITHDRAW" else m.amount
        fcash[aid] = cash
    return avg, realized, fcash, covered


# ---- convenience roll-ups for KPI reconciliation ----------------------------------
def convert(amount: Decimal, rate: Decimal) -> Decimal:
    return amount * rate


def reporting_realized(res: OracleResult, spot: dict[tuple[str, str], Decimal],
                       reporting: str) -> Decimal:
    total = ZERO
    for ccy, amt in res.realized_by_ccy.items():
        total += amt if ccy == reporting else amt * spot[(ccy, reporting)]
    return total


def unrealized_by_ccy(res: OracleResult, prices: dict[str, Decimal]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for h in res.holdings.values():
        p = prices.get(h.symbol)
        if p is None:
            continue
        out[h.quote_ccy] += (p - h.adjusted_avg) * h.shares
    return dict(out)


# ===================================================================================
# XIRR SCALAR ORACLE — the ONE documented-tolerance comparison in the suite
# ===================================================================================
# Independent, money-weighted, FX-aware reporting-currency XIRR. Derived from
# domain-ledger.md ("XIRR cashflow signs") — it does NOT import the app's pyxirr path.
# XIRR has no closed form, so this is solved numerically (float); the resulting SCALAR
# is compared to /api/dashboard kpis.xirr with an explicit, disclosed tolerance:
XIRR_TOL = Decimal("0.000001")  # |oracle_rate - app_rate| <= 1e-6  (everything else exact)
_DAYCOUNT = 365.0                # ACT/365F, same as pyxirr default -> same root


def xirr_cashflows(res: OracleResult, facts: Facts, prices: dict[str, Decimal],
                   reporting: str, fx_on, fx_now, as_of: date):
    """Build the reporting-currency (dates, amounts:float) cashflow series.

    Signs (domain-ledger.md): opening -original_cost_total @ build_date; buy
    -(qty*price+fee+tax); sell +(qty*price-fee-tax); cash dividend +net (CASH/NET);
    DRIP/STOCK neutral; terminal +sum(price*shares) @ as_of. Each non-terminal flow is
    converted at its TRADE-DATE FX via ``fx_on(d, base, quote)`` (on-or-before, exactly
    like the app's get_fx_on); the terminal value at current spot via ``fx_now(base,
    quote)`` (latest, like the app's resolver). Raises KeyError if any required rate or
    price is missing (mirrors the app returning None in that case).
    """
    insts = facts.instruments
    dates: list[date] = []
    amounts: list[float] = []

    def add(d: date, ccy: str, native: Decimal) -> None:
        rate = ONE if ccy == reporting else fx_on(d, ccy, reporting)
        dates.append(d)
        amounts.append(float(native * rate))

    for o in facts.openings:
        add(o.build_date, insts[o.symbol].quote_ccy, -o.orig_total)
    for t in facts.txs:
        ccy = insts[t.symbol].quote_ccy
        if t.side == "BUY":
            add(t.trade_date, ccy, -(t.qty * t.price + t.fee + t.tax))
        else:
            add(t.trade_date, ccy, t.qty * t.price - t.fee - t.tax)
    for dv in facts.divs:
        if dv.type in CASH_DIVIDEND_TYPES:
            add(dv.d, insts[dv.symbol].quote_ccy, dv.net)

    final = ZERO
    for h in res.holdings.values():
        if h.shares <= ZERO:
            continue
        p = prices.get(h.symbol)
        if p is None:
            raise KeyError(f"no current price for {h.symbol}")
        rate = ONE if h.quote_ccy == reporting else fx_now(h.quote_ccy, reporting)
        final += p * h.shares * rate
    if final != ZERO:
        dates.append(as_of)
        amounts.append(float(final))
    return dates, amounts


def _npv(rate: float, t0: date, dates: list[date], amounts: list[float]) -> float:
    acc = 0.0
    for d, a in zip(dates, amounts, strict=True):
        acc += a / (1.0 + rate) ** ((d - t0).days / _DAYCOUNT)
    return acc


def xirr_solve(dates: list[date], amounts: list[float]) -> Decimal | None:
    """Independent XIRR: Newton step, verified by a guaranteed bisection fallback.

    Returns the annualized rate as a Decimal, or None when not computable (fewer than
    two flows, no sign change, or no bracketable root). The root is invariant to the
    day-count base date, so t0 = min(dates) is used purely for numeric conditioning.
    """
    if len(dates) < 2:
        return None
    if not (any(a > 0 for a in amounts) and any(a < 0 for a in amounts)):
        return None
    t0 = min(dates)

    # --- Newton (fast path) ---
    rate = 0.1
    for _ in range(80):
        try:
            f = _npv(rate, t0, dates, amounts)
            df = 0.0
            for d, a in zip(dates, amounts, strict=True):
                t = (d - t0).days / _DAYCOUNT
                df += -t * a / (1.0 + rate) ** (t + 1.0)
        except (OverflowError, ZeroDivisionError, ValueError):
            rate = None
            break
        if df == 0.0:
            break
        step = f / df
        new = rate - step
        if new <= -1.0:
            new = (rate - 1.0) / 2.0  # keep the iterate in (-1, inf)
        if abs(new - rate) < 1e-13:
            rate = new
            break
        rate = new
    if rate is not None and rate > -1.0:
        try:
            if abs(_npv(rate, t0, dates, amounts)) < 1e-7:
                return Decimal(repr(rate))
        except (OverflowError, ValueError):
            pass

    # --- bisection (guaranteed within a bracket) ---
    lo, hi = -0.999999, 10.0
    try:
        flo = _npv(lo, t0, dates, amounts)
        fhi = _npv(hi, t0, dates, amounts)
    except (OverflowError, ValueError):
        return None
    tries = 0
    while flo * fhi > 0 and hi < 1e9 and tries < 80:
        hi *= 2.0
        try:
            fhi = _npv(hi, t0, dates, amounts)
        except (OverflowError, ValueError):
            return None
        tries += 1
    if flo * fhi > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2.0
        fm = _npv(mid, t0, dates, amounts)
        if abs(fm) < 1e-13 or (hi - lo) < 1e-15:
            return Decimal(repr(mid))
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return Decimal(repr((lo + hi) / 2.0))


# ===================================================================================
# LAYER 4 — LEDGER INTEGRITY (validity, not arithmetic)
# ===================================================================================
def integrity_findings(
    facts: Facts, realized_rows: list, *, acked: set[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Assertions a RECONCILIATION cannot make (lesson 2026-07-30).

    Replaying the same rows two ways proves the app and this oracle agree; it says nothing
    about whether an impossible row got in. These checks are the ones a defective INPUT
    CONTROL cannot satisfy, so they stay even when every figure reconciles:

    * ``position.never_negative`` — a position must not be negative at ANY date, not merely
      at the end. ``build_book`` drops the cost basis of an oversold position and emits no
      realized row; a LATER buy restores a positive net position and clears the ``oversold``
      flag, so the damage becomes permanent AND invisible. Measured live: a back-dated sell
      left an average cost 10-16x the market price with no flag anywhere.
    * ``sell.has_realized_row`` — every SELL must produce a realized row. Cash receives the
      proceeds either way, so a missing row is an unbalanced entry.

    ``acked`` lists the (account, symbol) pairs the SCENARIO deliberately oversold with a
    user acknowledgement. domain-ledger.md permits an acked oversell (it is a recorded
    user decision, and the resulting holding stays flagged), so those are excluded — the
    detector targets a negative position that NOBODY confirmed, which is the case a
    non-date-aware guard lets through.

    Returns ``[(check, scope)]`` for each violation; empty means clean.
    """
    ok = acked or set()
    out: list[tuple[str, str]] = []

    events: dict[tuple[str, str], list[tuple[date, int, Decimal]]] = defaultdict(list)
    for o in facts.openings:
        events[(o.account_id, o.symbol)].append((o.build_date, 0, o.shares))
    for t in facts.txs:
        q = t.qty if t.side.upper() == "BUY" else -t.qty
        events[(t.account_id, t.symbol)].append(
            (t.trade_date, 1 if t.side.upper() == "BUY" else 2, q))
    for dv in facts.divs:
        if dv.reinvest_shares:
            events[(dv.account_id, dv.symbol)].append((dv.d, 3, dv.reinvest_shares))
    for (aid, sym), evs in events.items():
        if (aid, sym) in ok:
            continue
        run = ZERO
        for d, _phase_i, delta in sorted(evs, key=lambda x: (x[0], x[1])):
            run += delta
            if run < ZERO:
                out.append(("position.never_negative",
                            f"{aid}/{sym} nets {run} on {d.isoformat()}"))
                break

    sold = {(t.account_id, t.symbol, t.trade_date)
            for t in facts.txs if t.side.upper() == "SELL"}
    booked = {(r.account_id, r.symbol, r.sell_date) for r in realized_rows
              if getattr(r, "kind", "sale") == "sale"}
    for key in sorted(sold - booked):
        if (key[0], key[1]) in ok:
            continue      # an acked oversell legitimately emits no realized row
        out.append(("sell.has_realized_row", f"{key[0]}/{key[1]} sold {key[2].isoformat()}"))
    return out
