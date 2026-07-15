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
from decimal import ROUND_HALF_UP, Decimal

D = Decimal
ZERO = D("0")
ONE = D("1")

# --- per-currency minor units (data-and-pricing.md) --------------------------------
MINOR_UNITS = {"TWD": 0, "USD": 2, "MYR": 2}

# --- account -> (fee_rule, settlement_ccy, funding_ccy) (config_seed DEFAULT_ACCOUNTS)
ACCOUNTS = {
    "tw_broker": ("tw", "TWD", "TWD"),
    "schwab": ("schwab", "USD", "TWD"),
    "moomoo_my_us": ("moomoo_us", "USD", "MYR"),
    "moomoo_my_my": ("moomoo_my", "MYR", "MYR"),
}

# --- fee-rule parameters (transcribed from config_seed.FEE_RULES) ------------------
FEE_RULES = {
    "tw": dict(brokerage=D("0.001425"), discount=D("1"), min_fee=D("20"),
               tax_normal=D("0.003"), tax_etf=D("0.001"), tax_daytrade=D("0.0015"),
               round_integer=True, ccy="TWD"),
    "schwab": dict(sec_fee=D("0.0000278"), ccy="USD"),
    "moomoo_us": dict(flat_fee=D("0.99"), sec_fee=D("0.0000278"), ccy="USD"),
    "moomoo_my": dict(brokerage=D("0.0008"), min_fee=D("3"), clearing=D("0.0003"),
                      clearing_cap=D("1000"), stamp_duty_rate=D("0.001"), ccy="MYR"),
}

CASH_DIVIDEND_TYPES = {"CASH", "NET"}  # domain-ledger.md: TW cash + MY single-tier net


def _round(value: Decimal, places: int) -> Decimal:
    q = D(1).scaleb(-places)
    return value.quantize(q, rounding=ROUND_HALF_UP)


# ===================================================================================
# LAYER 1 — FEE ENGINE ORACLE (rule-derived; compared vs app-stored fee/tax)
# ===================================================================================
def fee_tax(account_id: str, side: str, qty: Decimal, price: Decimal,
            is_etf: bool, daytrade: bool = False) -> tuple[Decimal, Decimal, list[str]]:
    """Return (fee, tax, assumptions[]) expected from markets-and-fees.md.

    ``side`` in {"BUY","SELL"}. ``assumptions`` records rule-silent mechanical choices
    so a mismatch on one of them can be classified as an oracle assumption rather than
    an app bug.

    TW sell-side tax rate precedence (mirrors fees.compute_fees): daytrade (0.15%) wins,
    else ETF (0.1%), else 現股 normal (0.3%). ``is_etf`` is the instrument-REGISTRY flag,
    NOT a per-input flag (found-bug 2026-07-15: entry paths defaulted is_etf=False and
    taxed ETF sells at 0.3%). ``daytrade`` is the per-transaction flag (manual body or
    CSV ``daytrade`` column) persisted so a recompute reproduces the sell tax (MED-1).
    """
    rule_name = ACCOUNTS[account_id][0]
    r = FEE_RULES[rule_name]
    notional = qty * price
    fee = ZERO
    tax = ZERO
    notes: list[str] = []

    if rule_name == "tw":
        raw = notional * r["brokerage"] * r["discount"]
        brok = _round(raw, 0)                       # integer NT$ (四捨五入)
        fee = max(brok, r["min_fee"])               # min NT$20
        notes.append("tw: fee=max(round_int(notional*0.001425*1), 20)")
        if side == "SELL":
            if daytrade:
                rate = r["tax_daytrade"]
            elif is_etf:
                rate = r["tax_etf"]
            else:
                rate = r["tax_normal"]
            tax = _round(notional * rate, 0)
            notes.append(f"tw: sell tax=round_int(notional*{rate}) "
                         f"is_etf={is_etf} daytrade={daytrade}")
    elif rule_name == "schwab":
        if side == "SELL":
            fee = _round(notional * r["sec_fee"], 2)  # SEC reg fee, sell-side, 2dp
            notes.append("schwab: sell fee=round2(notional*sec_fee); buy fee=0; ROUND_HALF_UP")
        else:
            notes.append("schwab: buy fee=0")
    elif rule_name == "moomoo_us":
        base = r["flat_fee"]                          # ASSUMPTION: flat fee both sides
        if side == "SELL":
            fee = _round(base + notional * r["sec_fee"], 2)
        else:
            fee = _round(base, 2)
        notes.append("moomoo_us: flat_fee $0.99 assumed BOTH sides; sell adds sec_fee; round2")
    elif rule_name == "moomoo_my":
        brok = notional * r["brokerage"]
        if brok < r["min_fee"]:
            brok = r["min_fee"]
        clr = notional * r["clearing"]
        if clr > r["clearing_cap"]:
            clr = r["clearing_cap"]
        # App models stamp duty in the TAX field (a tax-like contract-note charge),
        # keeping fee = brokerage + clearing. The rule lists stamp duty separately from
        # brokerage/clearing, so this split is a defensible modeling choice; the ALL-IN
        # cost (fee+tax) is identical either way. (Confirmed against app fees engine.)
        fee = _round(brok + clr, 2)
        tax = _round(notional * r["stamp_duty_rate"], 2)
        notes.append("moomoo_my: fee=round2(max(notional*0.0008,3)+min(notional*0.0003,1000)); "
                     "tax=round2(notional*0.001 stamp); SST=0; both sides")
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
    kind: str          # DEPOSIT | WITHDRAW
    ccy: str
    amount: Decimal


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
    fx_avg, fx_real, fx_fcash = _fx_pools(facts)

    return OracleResult(holdings, realized_rows, dict(realized_by_ccy), cash,
                        fx_avg, fx_real, fx_fcash)


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

    avg_rate = sum(home from_amt) / sum(foreign to_amt) over home->foreign conversions.
    realized_fx = sum over foreign->home reconversions of (home_received - foreign_sold*avg_rate).
    foreign_cash = conversions +/- ; +sale net ; -buy allin ; +CASH dividend net (foreign).
    Only accounts with settlement_ccy != funding_ccy are FX-exposed.
    """
    avg: dict[str, Decimal | None] = {}
    realized: dict[str, Decimal | None] = {}
    fcash: dict[str, Decimal] = {}
    for aid, (_rule, settle, funding) in ACCOUNTS.items():
        if settle == funding:
            continue
        home, foreign = funding, settle
        convs = [c for c in facts.fxs if c.account_id == aid]
        tot_home = ZERO
        tot_foreign = ZERO
        for c in convs:
            if c.from_ccy == home and c.to_ccy == foreign:
                tot_home += c.from_amt
                tot_foreign += c.to_amt
        a = (tot_home / tot_foreign) if tot_foreign != ZERO else None
        avg[aid] = a
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
        fcash[aid] = cash
    return avg, realized, fcash


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
