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

CORPORATE ACTIONS — WHY THIS FILE DUPLICATES THE RATIO ALGEBRA ON PURPOSE
------------------------------------------------------------------------
The corporate-action spec (``docs/spec/2026-08-06-corporate-actions.md``) §6.0 states
"ONE owner per concept" and the app owns it in ``shared/corporate_actions.py`` +
``shared/ledger_events.py``. **This module is the single deliberate suspension of that
rule** (spec §7.4), and the reason is written here so a future cleanup pass does not
"de-duplicate" the oracle back into uselessness:

  * §10.4 trap #11 — "let the oracle import ``shared/corporate_actions.py`` → the oracle
    checks the implementation against itself and proves nothing."
  * §10.4 trap #10 — "update two of the three event-priority literals → a silently
    mis-ordered replay." An oracle that imports ``EventPriority`` cannot see that class
    of defect at all, because it would inherit the mistake.

So this file carries its OWN two-term rational arithmetic (:func:`ratio_shares`), its OWN
``EventPriority`` (re-declared from spec §4.4, not imported), and its OWN transcription of
the §4.4 field-transfer table and the §5 refusal matrix (:func:`_apply_action`). Every
formula below is derived from the SPEC — §4.1/§4.2/§4.3 for the three kinds, §4.4 for the
complete field table, §5 for the rejection rows — never from ``portfolio/cost_basis.py``.
Duplication here is the *mechanism of the check*, not an oversight.

US EQUITY OPTIONS — TRANSCRIBED FROM ``docs/spec/2026-10-09-options-ledger.md``
-----------------------------------------------------------------------------
The same suspension, for the same reason. ``shared/options.py`` owns the OCC parse, the
multiplier and the §6.1 classifier inside the app; this file carries its OWN parser
(:func:`parse_occ` — letters-only root, ``YYMMDD``, ``C``/``P``, strike × 1000 as 8 digits),
its OWN multiplier, its own §4.1 price-0 close rows, the §4.4 refusal (a close that would
open or extend a lot books nothing), the §4.3 ``OPTION_ADJUST`` field table with
``ratio_from == 1`` (OPT-D11 / D16), the OPT-D6 fee branch and the §5 last-trade valuation.
An oracle that imported the app's parser would agree with a mis-parsed strike by
construction.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import IntEnum

D = Decimal
ZERO = D("0")
ONE = D("1")
CENT = D("0.01")

# --- per-currency minor units (data-and-pricing.md) --------------------------------
MINOR_UNITS = {"TWD": 0, "USD": 2, "MYR": 2}

#: A fee rule's market -> its settlement currency (transcribed; see ``settled``).
_RULE_CCY = {"TW": "TWD", "US": "USD", "MY": "MYR"}


def settled(qty: Decimal, price: Decimal, ccy: str) -> Decimal:
    """The settled 價金 of a trade — the oracle's OWN transcription of the rule (owner
    2026-09-30), deliberately not an import of ``shared.money.settled_notional``: TWD drops
    everything below the dollar (TWSE's odd-lot 交割價金 元以下捨去), USD / MYR round the cent /
    sen half up; a product already in the minor unit is kept exactly as it is."""
    raw = qty * price
    unit = ONE if str(ccy) == "TWD" else CENT
    rounding = ROUND_DOWN if str(ccy) == "TWD" else ROUND_HALF_UP
    q = raw.quantize(unit, rounding=rounding)
    return raw if q == raw else q

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
    # OPT-D6 (options spec §2): ``option_per_contract`` is per RULE SET — schwab 0.65, moomoo_us
    # UNSET (None, never 0: an unset rate makes the fee required, the engine must not guess);
    # ``option_reg_per_contract`` defaults to 0.
    "schwab": dict(sec_rate=D("0.0000206"), sec_min=D("0.01"), taf_per_share=D("0.000195"),
                   taf_min=D("0.01"), taf_cap=D("9.79"), ccy="USD",
                   option_per_contract=D("0.65"), option_reg_per_contract=D("0")),
    "moomoo_us": dict(commission_rate=D("0.0003"), commission_min=D("0.01"),
                      platform=D("0.99"), settlement_per_share=D("0.003"),
                      settlement_cap_rate=D("0.01"), cat_per_share=D("0.000003"),
                      sec_rate=D("0.0000206"), sec_min=D("0.01"), taf_per_share=D("0.000195"),
                      taf_min=D("0.01"), taf_cap=D("9.79"), stamp_unit=D("1000"),
                      stamp_per_unit=D("1"), stamp_cap_stock=D("1000"), stamp_cap_etf=D("200"),
                      ccy="USD", option_per_contract=None, option_reg_per_contract=D("0")),
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
    "option_per_contract": "option_per_contract",
    "option_reg_per_contract": "option_reg_per_contract",
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


# ===================================================================================
# US EQUITY OPTIONS — the oracle's OWN contract terms (options spec §3, OPT-D1 / D22)
# ===================================================================================
#: OPT-D1: one contract = 100 share-equivalents; the ledger's quantity IS share-equivalents
#: and its price is the premium PER SHARE, so every ``qty × price`` stays a money figure.
OPTION_MULTIPLIER = D("100")

#: Spec §3: "OCC without root padding: letters-only root, YYMMDD, C/P, strike × 1000 as 8
#: digits — ^[A-Z]{1,6}\d{6}[CP]\d{8}$". Written here from that sentence, not imported.
_OCC = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")

#: The three §4.1 price-0 closing rows (OPT-D2).
CLOSE_REASONS = frozenset({"expired", "assigned", "exercised"})


@dataclass(frozen=True)
class OptionTerms:
    underlying: str
    expiry: date
    right: str          # "C" | "P"
    strike: Decimal

    @property
    def name(self) -> str:
        """The display name 「XYZ 2026-12-18 50 Put」 (spec §3) — the oracle's own spelling.
        A whole-number strike prints without decimals; a fractional one as written."""
        strike = format(self.strike.normalize(), "f")
        return (f"{self.underlying} {self.expiry.isoformat()} {strike} "
                f"{'Put' if self.right == 'P' else 'Call'}")


def parse_occ(symbol: str) -> OptionTerms | None:
    """The contract terms of an OCC symbol, or ``None`` when *symbol* is not one.

    OPT-D22: there is no contract table — the symbol carries every term, so the oracle reads
    them from the symbol with its OWN parser. A malformed calendar date is not a contract.
    """
    m = _OCC.match(symbol or "")
    if m is None:
        return None
    root, yy, mm, dd, right, strike = m.groups()
    try:
        expiry = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None
    return OptionTerms(root, expiry, right, D(int(strike)) / D(1000))


def is_option(symbol: str) -> bool:
    return parse_occ(symbol) is not None


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
            market: str | None = None, *, is_option: bool = False,
            close_reason: str | None = None) -> tuple[Decimal | None, Decimal, list[str]]:
    """Return (fee, tax, notes[]) expected from the fee-engine v2 spec (independent derive).

    OPTIONS (options spec OPT-D6 / §4.2, transcribed): a ``close_reason`` row (expiry /
    assignment / exercise) carries NO fee and NO tax — every door sets 0 / 0 without the
    engine. An ordinary option trade is ``option_per_contract × contracts`` (+ the optional
    ``option_reg_per_contract × contracts``) + SEC on a SELL over the premium notional (the
    stock SEC formula), each component cent-quantized; no TAF, no transaction tax, no MY stamp
    in v1. On a rule set whose per-contract rate is UNSET the engine has no answer: ``fee`` is
    returned as ``None`` and the caller must supply the statement's fee (the door refuses the
    row otherwise — ``option_fee_unset``).

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
    rule_market = next(m for (_a, m), name in ACCOUNT_MARKET_RULE.items() if name == rule_name)
    notional = settled(qty, price, _RULE_CCY[rule_market])  # the fee base is the 價金
    fee = ZERO
    tax = ZERO
    notes: list[str] = []
    if close_reason is not None:
        return ZERO, ZERO, [f"option close row ({close_reason}): fee = tax = 0, no engine"]
    if notional <= ZERO:
        return fee, tax, ["zero/negative notional -> no fee/tax"]
    if is_option:
        per = r.get("option_per_contract")
        if per is None:
            return None, ZERO, [f"{rule_name}: option_per_contract unset -> fee must be "
                                "supplied (OPT-D6)"]
        contracts = qty / OPTION_MULTIPLIER
        fee = _cent(per * contracts)
        reg = r.get("option_reg_per_contract") or ZERO
        if reg > ZERO:
            fee += _cent(reg * contracts)
        if side == "SELL":
            fee += _cent(max(notional * r["sec_rate"], r["sec_min"]))
        notes.append(f"{rule_name} option: {per}/contract x {contracts} + reg {reg}"
                     " + SEC on a sell; tax 0, no stamp (OPT-D6)")
        return fee, ZERO, notes

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
    # DECLARED short sale (domain-ledger.md 2026-07-31, option C). A ledger FACT of the
    # row, exactly like qty/price — it changes which accounting branch the replay takes.
    # Never inferred: an oversell without this flag stays an oversell (sticky 賣超).
    short_sale: bool = False
    # Options spec §4.1 (OPT-D2): ``expired`` / ``assigned`` / ``exercised`` marks a price-0
    # CLOSING row of an option contract. A ledger fact, read back like ``short_sale``.
    close_reason: str | None = None


@dataclass
class DivFact:
    id: int
    account_id: str
    symbol: str
    d: date            # the PAYMENT date
    type: str          # CASH | NET | DRIP | STOCK
    gross: Decimal
    withholding: Decimal
    net: Decimal
    reinvest_shares: Decimal | None
    reinvest_price: Decimal | None
    ex_date: date | None = None

    @property
    def effective(self) -> date:
        """The date this event is replayed on — derived from domain-ledger semantics.

        Re-derived here rather than read off the app (the independence rule): a STOCK
        dividend (配股) is an ENTITLEMENT that attaches on the ex-date, and the quoted price
        adjusts on that same day, so holding the pre-dividend share count against the
        post-adjustment price until payment misstates the position for the whole gap.

        A **DRIP** does not move: its shares are BOUGHT when the cash lands, at the recorded
        reinvest price, so they do not exist earlier. **CASH / NET** does not move either:
        the price falls on the ex-date while the cost reduces on payment, and that dip is
        honest — nothing about what is OWNED changed on the ex-date.

        An absent ``ex_date`` falls back to the payment date, which is how every pre-R6 row
        replays and why adding the column moved no existing figure.
        """
        if self.type.upper() == "STOCK" and self.ex_date is not None:
            return self.ex_date
        return self.d


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


# The two cash-kind axes, written out INDEPENDENTLY of the app.
#
# ``portfolio_dash/shared/cash_kinds.py`` holds the app's table. This oracle deliberately
# does NOT import it: an oracle that shares the implementation's table cannot disagree with
# it, and disagreeing is the whole job. These sets are re-derived here from
# ``domain-ledger.md`` — debits reduce the pool; only FUNDING flows enter the coverage
# denominator, so interest earned (income arising inside the pool) is a credit that is not
# an acquisition, exactly as sale proceeds and foreign cash dividends already are.
DEBIT_KINDS = frozenset({"WITHDRAW", "INTEREST_EXPENSE", "BROKER_FEE"})
ACQUIRING_KINDS = frozenset({"DEPOSIT", "OPENING", "REBATE"})

# AI-D42 (owner ruling 2026-08-24, superseding D1=A of 2026-08-13). The kinds that are COSTS
# OF TRADING AND FINANCING are part of the return and enter the XIRR flow series; capital
# movements are not, and neither is interest earned on idle cash.
#
# Re-derived here, not imported, for the reason above — and the line is drawn on a different
# axis from either set above, which is why it needs its own name rather than an expression
# over them: ``REBATE`` is a credit AND an acquisition AND a return flow, while ``DEPOSIT``
# is a credit AND an acquisition AND NOT a return flow. Neither ``credit`` nor
# ``fx_acquisition`` separates them.
#
# * ``REBATE`` — a refund of commission that was capitalised into cost basis, so leaving it
#   out overstates the cost of every round trip (FE-D1 charge-first: 0.229% of capital).
# * ``INTEREST_EXPENSE`` — the cost of financing the positions.
# * ``BROKER_FEE`` — a cost of trading that no transaction row carries.
# * ``INTEREST`` is EXCLUDED although it is pool income: the principal earning it never
#   entered XIRR's denominator, so crediting its yield to the numerator is asymmetric.
# * ``DEPOSIT`` / ``WITHDRAW`` / ``OPENING`` are EXCLUDED: admitting them would quietly turn
#   XIRR from "the return on money put into securities" into an account return. D12's
#   重組費 is booked as a WITHDRAW, so that standing limitation is unchanged.
XIRR_CASH_KINDS = frozenset({"REBATE", "INTEREST_EXPENSE", "BROKER_FEE"})


@dataclass
class CashFact:
    id: int
    account_id: str
    d: date
    kind: str          # see DEBIT_KINDS / ACQUIRING_KINDS above
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
    # Options spec §3: 'stock' | 'option'. A registry FACT — the oracle derives the terms from
    # the symbol itself (parse_occ) and only CHECKS that the two agree.
    asset_type: str = "stock"


@dataclass
class CorpFact:
    """One ``corporate_actions`` ledger row, as stored (spec §3).

    ``ratio_to`` / ``ratio_from`` are the TWO TERMS of a rational, never a quotient: a
    decimal ratio is a rounded quotient and `data-and-pricing.md` forbids storing one as
    the authority (spec §3.1(ii)). They are read back exactly as written and are combined
    only inside :func:`ratio_shares`. ``cost_carry`` is a single Decimal because an 8-K
    publishes an allocation PERCENTAGE, which is already exact as a decimal; the parent's
    complement is never stored — it is ``total − carved`` on read (spec §4.3).
    """

    id: int
    account_id: str
    d: date
    kind: str          # SPLIT | EXCHANGE | SPINOFF
    from_symbol: str
    to_symbol: str
    ratio_to: Decimal
    ratio_from: Decimal
    cost_carry: Decimal | None = None
    note: str | None = None


@dataclass
class Facts:
    txs: list[TxFact] = field(default_factory=list)
    divs: list[DivFact] = field(default_factory=list)
    fxs: list[FxFact] = field(default_factory=list)
    openings: list[OpenFact] = field(default_factory=list)
    cash: list[CashFact] = field(default_factory=list)
    instruments: dict[str, Instrument] = field(default_factory=dict)
    actions: list[CorpFact] = field(default_factory=list)


@dataclass
class Holding:
    account_id: str
    symbol: str
    quote_ccy: str
    shares: Decimal
    original_total: Decimal
    adjusted_total: Decimal
    # Flags (domain-ledger.md): 賣超 is STICKY ("was ever negative", not the final sign);
    # an open declared short is a REAL position (negative shares, proceeds as basis);
    # a dividend that landed while short is NOT booked and leaves the position 待釐清.
    oversold: bool = False
    short_open: bool = False
    unbookable_dividend: bool = False
    # A corporate action could NOT be applied and was skipped (spec §5 dashboard path).
    # The shares are then in PRE-action terms while the price series is post-action, so
    # every valued figure on this position is wrong by the ratio — a different failure
    # from 賣超 (a discarded basis) and from an unbookable dividend (a missing event).
    unbookable_action: bool = False
    # Options spec §4.4: a close row on this position could not be booked (it would have
    # opened or extended a lot). STICKY, like 賣超; never on the stock holdings wire.
    unbookable_close: bool = False

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
    # "sale" | "dividend" | "short_cover". A CASH-family dividend whose payment date falls
    # after the position already reached zero shares is realized INCOME: there is no cost
    # left to reduce, so (domain-ledger.md, 2026-07-26) it is booked as a realized row of
    # net = proceeds rather than being absorbed by — and then discarded with — the closed
    # position. A "short_cover" row (2026-07-31) realizes a declared short's P&L on the
    # COVER date: proceeds_net is the short's weighted-average sale value for the covered
    # shares, *_cost_removed is the covering buy's all-in cost for them.
    kind: str = "sale"
    # Options spec §7.1: the close row's reason (expired / assigned / exercised) when the
    # realized row came from one; None for an ordinary trade or a dividend.
    close_reason: str | None = None


@dataclass
class RefusedClose:
    """A §4.4 close the replay could NOT book: it would have opened or extended a lot (a BUY
    beyond the short lot, a SELL beyond the long lot, or a close against the opposite
    direction). Booked nowhere — no realized row, no position created — and always recorded.
    """

    txn_id: int
    account_id: str
    symbol: str
    d: date
    side: str
    quantity: Decimal
    close_reason: str


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
    # (edge-code, scope) for every corporate action the replay REFUSED to apply — the
    # oracle's own §5 refusal matrix, surfaced so the evidence trail says WHICH rule fired.
    action_refusals: list[tuple[str, str]] = field(default_factory=list)
    # (account, symbol, pay date) for EVERY dividend the replay refused — all three classes
    # domain-ledger.md defines (YQ3, 2026-10-05): no position by its date (CR-02), an OPEN
    # declared short (the short pays in lieu), a ticker an EXCHANGE vacated (E24). Recorded on
    # the RESULT, not only as a position's `unbookable_dividend` (K3): an orphan has no
    # position at all, and a carrier can be FLAT — a short covered to 0, a successor sold out —
    # which the emit loop drops with its flag. A day by which any of them was refused stays
    # incomplete; the value of what IS held is still honest (Y3).
    refused_dividends: list[tuple[str, str, date]] = field(default_factory=list)
    # ⑪ / K2 (owner rulings 2026-10-05): the ids of those same dividends. A refused payout is
    # booked nowhere and moves no cash figure — the pools, the statement, the FX pool's foreign
    # cash, the trend's net_invested and the XIRR flows all leave it out.
    refused_dividend_ids: frozenset[int] = field(default_factory=frozenset)
    # (account, symbol, EFFECTIVE date) of the refusals that are ORPHANS — no opening, trade or
    # corporate action had created the position by then (CR-02). The one class that also
    # WITHHOLDS XIRR ("named, and withholds XIRR", domain-ledger.md): the ledger is missing the
    # position that earned it, so a money-weighted return over it is a different ledger's.
    orphan_dividends: list[tuple[str, str, date]] = field(default_factory=list)
    # Options spec §4.4: every close row the replay refused, in replay order. Like an orphan
    # dividend it is a RECORD, not a flag: XIRR and B are withheld while it is non-empty.
    refused_closes: list[RefusedClose] = field(default_factory=list)
    # The fact ids of every corporate action the replay refused (``action_refusals`` names the
    # rule). Options spec §5 (clarified 2026-10-10, final review R1-M2): a refused
    # ``OPTION_ADJUST`` moved nothing, so it propagates no price — :func:`option_price_on`.
    refused_action_ids: frozenset[int] = field(default_factory=frozenset)


class EventPriority(IntEnum):
    """Same-day replay order — the oracle's OWN copy (spec §4.4, trap #10/#11).

    Deliberately NOT imported from ``portfolio_dash.shared.ledger_events``: an oracle that
    inherits the implementation's ordering cannot detect an error in it, and a mis-ordered
    replay is the defect this enum exists to prevent (a same-day buy or sell trades in
    POST-action terms, so the action must apply first). Spaced by 10, transcribed from the
    spec's normative block.

    **Rule change transcribed 2026-09-23 (DEF-012, domain-ledger.md "same-day trades").**
    ``BUY = 20 / SELL = 30`` — buys before sells within a day — was retired: a day's trades
    are ONE priority (``TRADE``) and tie-break on the ledger id, i.e. the order they were
    entered. The oracle keeps its own transcription of that rule, as of every other.
    """

    OPENING = 0
    CORPORATE_ACTION = 10
    TRADE = 20
    DIVIDEND = 40


@dataclass
class _Pos:
    """Replay-time position: a LONG lot and a declared-SHORT lot, mutually exclusive
    BY CONSTRUCTION (domain-ledger.md, declared short sale): a declared sell exhausts the
    long lot before opening a short, and a buy covers the short before adding to the long.
    ``short_proceeds`` is the NET proceeds received for the owed shares — the short lot's
    (negative) basis when emitted.
    """

    account_id: str
    symbol: str
    quote_ccy: str
    shares: Decimal = ZERO             # long lot
    original_total: Decimal = ZERO
    adjusted_total: Decimal = ZERO
    short_shares: Decimal = ZERO       # declared-short lot (shares owed)
    short_proceeds: Decimal = ZERO     # net proceeds received for them
    ever_oversold: bool = False        # sticky 賣超 ("was ever negative")
    unbookable_dividend: bool = False  # a dividend was refused on it (short / vacated)
    unbookable_action: bool = False    # a corporate action was refused — skipped, flagged
    # E24 (D32) + CR-06, transcribed from domain-ledger.md: the symbol an EXCHANGE moved this
    # position to, while it stays moved. "A trade, an opening, or an EXCHANGE / SPINOFF
    # landing shares on a vacated ticker clears it, so a reused ticker's dividends book;
    # while it stays vacated, a dividend on it is still refused (E24) and flagged on the live
    # successor." Set by an APPLIED exchange only — a ticker sold down to zero is not vacated,
    # and its payout books as post-close income (audit H2).
    vacated_to: str | None = None
    # Options spec §4.4 + §4.3's field table: a close on this position was refused. Sticky;
    # OR-ed into the destination by EXCHANGE / SPINOFF / OPTION_ADJUST like unbookable_action.
    unbookable_close: bool = False


def _reopen(pos: _Pos) -> None:
    """Shares are landing on *pos*, so it is no longer an EXCHANGE-vacated ticker (CR-06)."""
    pos.vacated_to = None


def _live_successor(positions: dict[tuple[str, str], _Pos], account_id: str,
                    start: _Pos) -> _Pos:
    """Where E24's flag goes: follow ``vacated_to`` to the position the owner holds now.

    Transitive (a de-SPAC later renamed is an exchange of an exchange) and bounded by the
    symbols already visited, so it ends even on a ledger nobody should have. A chain that
    dead-ends flags the last position reached.
    """
    seen: set[str] = set()
    node = start
    while node.vacated_to is not None and node.vacated_to not in seen:
        seen.add(node.vacated_to)
        nxt = positions.get((account_id, node.vacated_to))
        if nxt is None:
            break
        node = nxt
    return node


SPLIT, EXCHANGE, SPINOFF = "SPLIT", "EXCHANGE", "SPINOFF"
#: Options spec §4.3 (OPT-D3): a standard contract adjustment, option -> option, carrying the
#: long AND the short lot. A fourth kind, so the three equity kinds stay byte-identical (D38).
OPTION_ADJUST = "OPTION_ADJUST"
_KINDS = {SPLIT, EXCHANGE, SPINOFF, OPTION_ADJUST}


def _close_refused(pos: _Pos | None, t: TxFact) -> bool:
    """Options spec §4.4, transcribed: does this ``close_reason`` row OPEN OR EXTEND a lot?

    A close can only take shares out of the lot it closes: a BUY close (an expired / assigned
    short) covers the SHORT lot, a SELL close (an expired / exercised long) sells the LONG lot.
    Anything beyond that lot — including a close against the opposite direction, where the
    lot it would close is empty — would open or extend a position, so the replay books
    nothing for it. A partial assignment / exercise within the lot books normally. Evaluated
    BEFORE the position is created or reopened, so a refused close never creates a position.
    """
    if t.close_reason is None:
        return False
    if t.side.upper() == "BUY":
        lot = pos.short_shares if pos is not None else ZERO
    else:
        lot = (pos.shares if pos is not None and pos.shares > ZERO else ZERO)
    return t.qty > lot


def _standard_adjustment(act: CorpFact) -> bool:
    """OPT-D11 / OPT-D16, transcribed: both contracts parse (multiplier 100, letters-only
    root), same right, same expiry, ``ratio_from == 1`` and the strike is conserved —
    ``strike_from × ratio_from == strike_to × ratio_to``. Anything else has no deliverable
    model in v1 and is refused (the doors reject it first; the replay re-checks)."""
    a, b = parse_occ(act.from_symbol), parse_occ(act.to_symbol)
    if a is None or b is None:
        return False
    return (a.right == b.right and a.expiry == b.expiry and act.ratio_from == ONE
            and a.strike * act.ratio_from == b.strike * act.ratio_to)


def ratio_shares(qty: Decimal, ratio_to: Decimal, ratio_from: Decimal) -> Decimal:
    """Apply a two-term rational to a share count — MULTIPLY FIRST, DIVIDE LAST.

    Spec §3.1(ii)(a): ``qty * to / from`` in ONE expression, so the division happens last
    against a value large enough to absorb it. NOT ``qty * (to / from)`` — the
    parenthesised quotient rounds to the Decimal context precision before it ever touches
    the share count, and a 400,000-pair sweep found 3,530 combinations that cross an
    integer boundary (``3 × 1/3`` is exactly ``1`` here and ``0.999…9`` parenthesised).
    A share count that lands a hair under an integer trips ``validate.py``'s bare ``>``
    comparison on the next sell, which is the 賣超 cascade the whole feature exists to
    prevent. Also NOT a ``split_factor``-style product of quotients (trap #2a): that is
    the same defect with an unbounded error term.

    This is the oracle's OWN arithmetic. It must never delegate to
    ``shared/corporate_actions.apply_ratio`` (trap #11).
    """
    return qty * ratio_to / ratio_from


def price_as_traded(facts: Facts, symbol: str, day: date, *,
                    quote: Decimal, quoted_on: date) -> Decimal:
    """The price AS TRADED on ``day``, derived from a quote dated ``quoted_on``.

    A quote taken after a split is in post-split terms; the price that actually traded
    BEFORE that split was larger by exactly the split's ratio — the mirror image of what
    the ratio does to the share count. So the expression is the same one
    :func:`apply_ratio` uses, applied to a price:

        as_traded := quote * ratio_to / ratio_from,   for each SPLIT in ``(day, quoted_on]``

    ONE ACTION AT A TIME, with the division last, and NEVER as a product of quotients
    (trap #2a) — a product's error term is unbounded, which is the whole reason this file
    refuses a ``split_factor``-style helper for share counts too.

    Only ``SPLIT`` moves a price basis. ``EXCHANGE`` and ``SPINOFF`` change WHICH instrument
    is held, not the terms the old one was quoted in, so they are skipped: the surviving
    symbol's own quote already is its own basis.

    This is the oracle's own arithmetic and must never delegate to ``pricing/`` (trap #11).

    **Quantized to 4 dp at the end, and that is deliberate.** A ratio like ``1 / 3`` makes the
    as-traded price a repeating decimal, and ``data-and-pricing.md`` caps a stored close at
    4 dp — so an unquantized value is one the price column CANNOT HOLD. This function is the
    single source of the fixture's daily closes: the harness seeds what it returns and values
    with what it returns, so both sides meet the number that is actually stored. Quantizing
    only HERE (measured: 91 ``trend.total_value`` failures off by 0.0070 without it) keeps
    that agreement a property of one expression rather than of two roundings staying in step.

    ⚠ This is the fixture's storage precision, not an accounting rule. It must never spread to
    the share/cost arithmetic, where this file's whole discipline is to divide last and never
    round in the middle.
    """
    out = quote
    for act in sorted((a for a in facts.actions
                       if a.kind == SPLIT and a.from_symbol == symbol
                       and day < a.d <= quoted_on),
                      key=lambda a: (a.d, a.id)):
        out = out * act.ratio_to / act.ratio_from
    return out.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _positive_integer(term: Decimal) -> bool:
    """D14 / E6+E6a: both ratio terms are positive INTEGERS.

    "Decimal > 0" was not enough — ``ratio_to = 0.2857`` satisfies it, passes the CSV
    importer and the API, and reproduces the cascade at ANY share count (700 × 0.2857 =
    199.9900). Validation rejects such a row before the replay; the oracle re-checks it so
    a row that got behind validation is REFUSED here rather than silently applied.
    """
    return term > ZERO and term == term.to_integral_value()


def _apply_action(positions: dict[tuple[str, str], _Pos], act: CorpFact,
                  insts: dict[str, Instrument]) -> str | None:
    """Apply ONE corporate action to the replay state, or REFUSE it.

    Returns ``None`` when applied, else the spec's edge code for the refusal. This is the
    dashboard path (``allow_oversell=True``): a refusal SKIPS the event and flags the
    source position ``unbookable_action`` — it never raises, because
    ``portfolio/dashboard.py`` calls the replay with no ``try``/``except`` and the
    standing never-500 rule applies at every call site (spec E1a).

    The refusals are refusals. An oracle that silently applied E2/E3/E5/E18/E22 would
    disagree with the app and invite "fixing" the app to match, so the matrix comes first
    and the arithmetic second.

    Field transfer follows spec §4.4's NORMATIVE table, every field explicitly:

    ============ ================= =========================== ==========================
    field        SPLIT (P)         EXCHANGE  P -> Q             SPINOFF  P -> Q
    ============ ================= =========================== ==========================
    quote_ccy    unchanged         Q keeps its own (E11)       Q keeps its own (E11)
    shares       * to / from       P := 0; Q += P.sh*to/from   P unchanged; Q += ...
    original_tot unchanged         P := 0; Q += P.orig         Q += P.orig*c; P -= same
    adjusted_tot unchanged         P := 0; Q += P.adj          Q += P.adj*c;  P -= same
    short_shares * to / from (E4)  **P := 0** (explicit)       unchanged (E5 => 0)
    short_procee unchanged (E4)    **P := 0** (explicit)       unchanged (E5 => 0)
    ever_oversld unchanged         nothing transferred (E3/E22 keep both sides False)
    unbook_divid unchanged         Q |= P (E19)                Q |= P (E19); P keeps its
    unbook_actio unchanged         Q |= P (F-37)               Q |= P (F-37); P keeps its
    vacated_to   unchanged         P := Q; Q := None (CR-06)   Q := None (CR-06)
    ============ ================= =========================== ==========================

    Why EXCHANGE zeroes ``short_proceeds``/``short_shares`` explicitly even though E5
    guarantees they are already zero (§4.4, trap #7): they are *nearly* zero. A full cover
    computes ``P − (P/S)×S`` and Decimal division is inexact whenever ``S`` does not divide
    ``P``, so a residue ``ε`` survives. It is invisible today only because the emitted
    share count is ``0 − 0`` and the holdings loop drops the position — but EXCHANGE leaves
    the source in the map with a live meaning, and the emitted
    ``original_total − short_proceeds`` would then subtract ``ε`` from a position a later
    buy on the old ticker can reopen. Zeroing all three removes the whole class.
    """
    src_key = (act.account_id, act.from_symbol)
    dst_key = (act.account_id, act.to_symbol)
    src = positions.get(src_key)

    def refuse(code: str) -> str:
        # Flag the SOURCE: it is the position left holding PRE-action shares against a
        # post-action price series, which is exactly what the flag announces. The
        # destination of a refused EXCHANGE/SPINOFF is untouched and self-consistent.
        if src is not None:
            src.unbookable_action = True
        return code

    # -- E21/E10 mirror: an action referencing an unregistered symbol is dropped with the
    # rest of that symbol's rows (the dashboard's unregistered skip-set), not flagged.
    if act.from_symbol not in insts or act.to_symbol not in insts:
        return "E21"
    # -- validation-tier shape checks (E6/E6a/E8/E11/E20). These are rejected at entry and
    # must never reach a replay; re-checked here so a row that got behind validation is
    # refused rather than applied. `ratio_from == 0` would otherwise be a ZeroDivisionError
    # inside the replay, i.e. a 500 on the dashboard path.
    if act.kind not in _KINDS:
        return refuse("E-kind")
    if not (_positive_integer(act.ratio_to) and _positive_integer(act.ratio_from)):
        return refuse("E6")
    if act.kind == SPLIT and act.to_symbol != act.from_symbol:
        return refuse("E20")
    if act.kind != SPLIT and act.to_symbol == act.from_symbol:
        return refuse("E20")
    if act.kind == SPINOFF and (act.cost_carry is None
                                or not (ZERO <= act.cost_carry <= ONE)):
        return refuse("E8")
    if act.kind != SPLIT and insts[act.from_symbol].quote_ccy != insts[act.to_symbol].quote_ccy:
        return refuse("E11")
    if act.kind == OPTION_ADJUST and not _standard_adjustment(act):
        return refuse("E-option-nonstandard")     # OPT-D11 / D16

    # -- E1: no prior position. Fabricating one would invent a $0-cost ghost, so nothing
    # is created and nothing can be flagged (there is no position to carry the flag).
    if src is None:
        return "E1"
    # -- E3 before E2: 賣超 is sticky, so an oversold position that a later buy netted back
    # to exactly zero is an E3, not a "closed" position. Scaling an undefined basis
    # produces an undefined result either way; only the reported reason differs.
    if src.ever_oversold:
        return refuse("E3")
    # -- E2: closed (0 shares, 0 basis) — includes an EXCHANGE-vacated source, so a second
    # action on a moved-away ticker is refused rather than re-animating it.
    if (src.shares == ZERO and src.short_shares == ZERO
            and src.original_total == ZERO and src.adjusted_total == ZERO):
        return refuse("E2")

    if act.kind == OPTION_ADJUST:
        # Options spec §4.3 — placed after SPLIT's checks and BEFORE the E5 block, because
        # an option adjustment carries the SHORT lot too: E5 (short source) and E18 (short
        # destination) are bypassed and replaced by the same-direction rule. Its own
        # destination lookup: E22 (Q ever oversold), the opposite-direction refusal, then the
        # §4.3 field transfer, then Q reopened.
        dst = positions.get(dst_key)
        if dst is not None and dst.ever_oversold:
            return refuse("E22")
        if dst is not None and ((src.short_shares > ZERO and dst.shares > ZERO)
                                or (src.shares > ZERO and dst.short_shares > ZERO)):
            return refuse("E-option-direction")
        if dst is None:
            dst = positions.setdefault(
                dst_key, _Pos(act.account_id, act.to_symbol, insts[act.to_symbol].quote_ccy))
        # §4.3 table, P = source, Q = destination, every field explicitly:
        dst.shares += ratio_shares(src.shares, act.ratio_to, act.ratio_from)
        dst.original_total += src.original_total
        dst.adjusted_total += src.adjusted_total
        dst.short_shares += ratio_shares(src.short_shares, act.ratio_to, act.ratio_from)
        dst.short_proceeds += src.short_proceeds       # residue included
        dst.unbookable_dividend |= src.unbookable_dividend
        dst.unbookable_action |= src.unbookable_action
        dst.unbookable_close |= src.unbookable_close
        src.shares = ZERO
        src.original_total = ZERO
        src.adjusted_total = ZERO
        src.short_shares = ZERO
        src.short_proceeds = ZERO
        src.vacated_to = act.to_symbol
        _reopen(dst)
        return None

    # -- E5: EXCHANGE/SPINOFF on an OPEN declared short has no honest booking (precedent:
    # dividend-on-short). A SPLIT is supported (E4) and is handled below.
    if act.kind != SPLIT and src.short_shares > ZERO:
        return refuse("E5")

    if act.kind == SPLIT:
        # §4.1 — one position; to_symbol == from_symbol is enforced above.
        src.shares = ratio_shares(src.shares, act.ratio_to, act.ratio_from)
        src.short_shares = ratio_shares(src.short_shares, act.ratio_to, act.ratio_from)
        # original_total / adjusted_total / short_proceeds: UNCHANGED. Both averages
        # divide by the new share count on read, so they scale by 1/ratio automatically;
        # dividend_portion and payback_ratio are unchanged, which is correct — a split
        # changes nothing about how much cost has been returned as dividends.
        return None

    dst = positions.get(dst_key)
    # -- E18 / E22: the RECEIVING side. E18 keeps long/short mutually exclusive by
    # construction; E22 stops a discarded cost basis being restored onto a position whose
    # basis the STICKY guard deliberately threw away, where it renders as an ordinary
    # average over shares that have no basis at all.
    if dst is not None:
        if dst.short_shares > ZERO:
            return refuse("E18")
        if dst.ever_oversold:
            return refuse("E22")
    if dst is None:
        dst = positions.setdefault(
            dst_key, _Pos(act.account_id, act.to_symbol, insts[act.to_symbol].quote_ccy))
    _reopen(dst)   # CR-06: shares land on the destination, so it is live from here on

    if act.kind == EXCHANGE:
        # §4.2 — the whole position moves. If Q already holds, the two merge by weighted
        # average, which is simply the sum of the totals over the sum of the shares: no
        # special case, exactly what the weighted-average method prescribes.
        carried = ratio_shares(src.shares, act.ratio_to, act.ratio_from)
        dst.shares += carried
        dst.original_total += src.original_total
        dst.adjusted_total += src.adjusted_total
        dst.unbookable_dividend |= src.unbookable_dividend   # E19
        dst.unbookable_action |= src.unbookable_action       # F-37
        dst.unbookable_close |= src.unbookable_close         # options spec §4.3
        src.shares = ZERO
        src.original_total = ZERO
        src.adjusted_total = ZERO
        src.short_shares = ZERO      # explicit (trap #7) — E5 says 0, but only NEARLY
        src.short_proceeds = ZERO    # explicit (trap #7)
        src.vacated_to = act.to_symbol   # E24: moved away, not sold — see _Pos.vacated_to
        return None

    # §4.3 SPINOFF — the parent keeps its shares, a child is created.
    carry = act.cost_carry
    assert carry is not None      # E8 checked above
    carved_orig = src.original_total * carry
    carved_adj = src.adjusted_total * carry
    dst.shares += ratio_shares(src.shares, act.ratio_to, act.ratio_from)
    dst.original_total += carved_orig
    dst.adjusted_total += carved_adj
    dst.unbookable_dividend |= src.unbookable_dividend       # E19 — the child inherits
    dst.unbookable_action |= src.unbookable_action           # F-37
    dst.unbookable_close |= src.unbookable_close             # options spec §4.3
    # The parent is `total − carved`, NOT `total × (1 − c)`: algebraically identical,
    # numerically not (1−c rounds once and ×(1−c) rounds again), so subtracting the exact
    # amount added to the child conserves Σ original_total BY CONSTRUCTION (§2.1).
    src.original_total -= carved_orig
    src.adjusted_total -= carved_adj
    # src.shares / short_shares / short_proceeds: unchanged (E5 guarantees the shorts are 0)
    return None


def replay(facts: Facts) -> OracleResult:
    """Replay the ledger facts -> holdings, realized, cash, FX pools.

    Same-day ordering derived from domain-ledger semantics + spec §4's normative
    ``EventPriority`` (this module's OWN copy, see :class:`EventPriority`):
      opening(0) -> CORPORATE ACTION(10) -> trades(20, buys and sells in DB-id order,
      DEF-012 2026-09-23) -> dividend(40); ties broken by DB id (insertion order),
      reproducing a sort over (date, priority, id). An action is effective at the START of
      its date: a same-day buy or sell trades in post-action terms (post-split price, new
      ticker), so the action must apply first. Opening inventory dated ON an action date is
      PRE-action — it describes the position as it stood before — which is why OPENING
      sorts ahead of it.

    Declared-short model — derived INDEPENDENTLY from domain-ledger.md ("Declared short
    sale", owner ruling 2026-07-31), not from the app:
      * a declared sell first sells the LONG lot (ordinary realized P&L), then opens or
        extends a SHORT lot holding the net proceeds received. Sell-side costs attach
        pro rata per share (the doc prescribes "net proceeds"; per-share allocation is the
        only mechanical reading once one sell row can split across the two lots).
      * a buy first COVERS the short at THIS buy's all-in per-share cost, realizing
        ``(short weighted-avg sale price − cover cost) × covered`` dated the COVER date
        (kind="short_cover"); only the remainder becomes long, starting at that same
        per-share cost. An ordinary buy (nothing covered) keeps its exact all-in total.
      * the emitted position is ONE signed quantity: long stays positive, an open short
        reports negative shares with the proceeds as its (negative) basis, so
        avg = total/shares is the average sale price and every formula works unchanged.
      * a dividend landing while the short lot is open is NOT representable (a short pays
        the dividend in lieu): it is skipped and the position flagged
        ``unbookable_dividend`` — never booked as income or shares.

    Options (spec 2026-10-09 §4, transcribed): a contract is a position in share-
    equivalents and goes through the SAME branches — sell-to-open IS the declared short,
    buy-to-close covers it. The §4.1 price-0 close rows need no branch of their own: a BUY
    at 0 covering the short realizes the whole remaining premium, a SELL at 0 of the long
    realizes ``−basis``. What IS new is the §4.4 refusal (:func:`_close_refused`), tested
    before the position is created, and ``OPTION_ADJUST`` in :func:`_apply_action`.
    """
    insts = facts.instruments

    def qccy(sym: str) -> str:
        return insts[sym].quote_ccy

    # ------- build the ordered event stream (mirrors build_book event list) -------
    events: list[tuple[date, int, int, str, object]] = []
    # openings first (priority 0); app orders them by (account, symbol)
    for i, o in enumerate(sorted(facts.openings, key=lambda x: (x.account_id, x.symbol))):
        events.append((o.build_date, EventPriority.OPENING, i, "open", o))
    for act in facts.actions:
        events.append((act.d, EventPriority.CORPORATE_ACTION, act.id, "corp", act))
    for t in facts.txs:
        # One rank for both sides; the id (third key) is the intraday order (DEF-012).
        events.append((t.trade_date, EventPriority.TRADE, t.id, "tx", t))
    for dv in facts.divs:
        events.append((dv.effective, EventPriority.DIVIDEND, dv.id, "div", dv))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    positions: dict[tuple[str, str], _Pos] = {}
    realized_rows: list[RealizedRow] = []
    refusals: list[tuple[str, str]] = []
    refused_dividends: list[tuple[str, str, date]] = []   # K3 / YQ3 — see OracleResult
    refused_divs: set[int] = set()   # ⑪: the payouts this replay refused, by fact id
    orphan_dividends: list[tuple[str, str, date]] = []    # CR-02 — see OracleResult
    refused_closes: list[RefusedClose] = []               # options spec §4.4
    refused_actions: set[int] = set()                     # options spec §5 (R1-M2)

    def refuse_dividend(dv: DivFact) -> None:
        refused_dividends.append((dv.account_id, dv.symbol, dv.d))
        refused_divs.add(dv.id)

    for _d, _p, _seq, kind, ev in events:
        if kind == "corp":
            assert isinstance(ev, CorpFact)
            code = _apply_action(positions, ev, insts)
            if code is not None:
                refused_actions.add(ev.id)
                refusals.append(
                    (code, f"{ev.account_id}/{ev.from_symbol}->{ev.to_symbol}"
                           f" {ev.kind} {ev.ratio_to}-for-{ev.ratio_from} @{ev.d}"))
        elif kind == "open":
            assert isinstance(ev, OpenFact)
            key = (ev.account_id, ev.symbol)
            pos = positions.setdefault(key, _Pos(ev.account_id, ev.symbol, qccy(ev.symbol)))
            _reopen(pos)                                     # CR-06
            pos.shares += ev.shares
            pos.original_total += ev.orig_total
            pos.adjusted_total += ev.orig_total
        elif kind == "tx":
            assert isinstance(ev, TxFact)
            key = (ev.account_id, ev.symbol)
            if _close_refused(positions.get(key), ev):
                # Options spec §4.4: a close that would open or extend a lot books NOTHING —
                # no realized row, no lot, no position created, ``vacated_to`` untouched —
                # and is always recorded; an existing position carries the sticky flag.
                refused_closes.append(RefusedClose(
                    ev.id, ev.account_id, ev.symbol, ev.trade_date, ev.side.upper(), ev.qty,
                    str(ev.close_reason)))
                existing = positions.get(key)
                if existing is not None:
                    existing.unbookable_close = True
                continue
            pos = positions.setdefault(key, _Pos(ev.account_id, ev.symbol, qccy(ev.symbol)))
            if ev.qty != ZERO:
                _reopen(pos)       # CR-06: a trade that moves shares puts it back in play
            if ev.side == "BUY":
                # Cover an open declared short FIRST, at this buy's all-in per-share cost;
                # the gain/loss realizes on the COVER date (owner's rule).
                cover = min(ev.qty, pos.short_shares)
                per_share = ZERO
                if cover > ZERO:
                    per_share = (settled(ev.qty, ev.price, qccy(ev.symbol))
                                 + ev.fee + ev.tax) / ev.qty
                    short_avg = pos.short_proceeds / pos.short_shares
                    realized_rows.append(RealizedRow(
                        ev.account_id, ev.symbol, qccy(ev.symbol), ev.trade_date, cover,
                        short_avg * cover, per_share * cover, per_share * cover,
                        (short_avg - per_share) * cover, "short_cover",
                        close_reason=ev.close_reason))
                    pos.short_proceeds -= short_avg * cover
                    pos.short_shares -= cover
                to_long = ev.qty - cover
                if to_long > ZERO:
                    # Exact all-in total when nothing was covered (an ordinary buy must not
                    # round-trip through a per-share division); leftover shares of a covering
                    # buy start their long life at that same per-share cost (the rule).
                    cost = (settled(ev.qty, ev.price, qccy(ev.symbol)) + ev.fee + ev.tax
                            if cover == ZERO
                            else per_share * to_long)
                    pos.shares += to_long
                    pos.original_total += cost
                    pos.adjusted_total += cost
            elif ev.short_sale:
                # DECLARED short sale: long lot first (ordinary realized P&L), remainder
                # opens/extends the short lot holding its net proceeds. Costs pro rata.
                from_long = min(ev.qty, pos.shares if pos.shares > ZERO else ZERO)
                per_share_net = (settled(ev.qty, ev.price, qccy(ev.symbol))
                                 - ev.fee - ev.tax) / ev.qty
                if from_long > ZERO:
                    frac = from_long / pos.shares
                    orig_removed = pos.original_total * frac
                    adj_removed = pos.adjusted_total * frac
                    proceeds = per_share_net * from_long
                    realized_rows.append(RealizedRow(
                        ev.account_id, ev.symbol, qccy(ev.symbol), ev.trade_date,
                        from_long, proceeds, orig_removed, adj_removed,
                        proceeds - adj_removed))
                    pos.shares -= from_long
                    pos.original_total -= orig_removed
                    pos.adjusted_total -= adj_removed
                to_short = ev.qty - from_long
                if to_short > ZERO:
                    pos.short_shares += to_short
                    pos.short_proceeds += per_share_net * to_short
            else:
                if ev.qty > pos.shares:
                    # UNDECLARED oversell (acked): net negative, drop cost basis, no
                    # realized row. STICKY — a later buy does not restore the basis, so it
                    # must not clear the flag either (domain-ledger.md 2026-07-31).
                    pos.ever_oversold = True
                    pos.shares -= ev.qty
                    pos.original_total = ZERO
                    pos.adjusted_total = ZERO
                    continue
                frac = ev.qty / pos.shares
                orig_removed = pos.original_total * frac
                adj_removed = pos.adjusted_total * frac
                proceeds_net = settled(ev.qty, ev.price, qccy(ev.symbol)) - ev.fee - ev.tax
                realized_rows.append(RealizedRow(
                    ev.account_id, ev.symbol, qccy(ev.symbol), ev.trade_date, ev.qty,
                    proceeds_net, orig_removed, adj_removed, proceeds_net - adj_removed,
                    close_reason=ev.close_reason))
                pos.shares -= ev.qty
                pos.original_total -= orig_removed
                pos.adjusted_total -= adj_removed
        else:  # dividend
            assert isinstance(ev, DivFact)
            key = (ev.account_id, ev.symbol)
            pos2 = positions.get(key)
            if pos2 is None:
                # CR-02 (domain-ledger.md): "A dividend needs a position that exists by its
                # effective date" — no opening, no trade, no EXCHANGE / SPINOFF had created
                # (account, symbol) by now. Refused: never income, never a ghost holding, and
                # the ledger is missing the position that earned it. This raised until YQ3 —
                # an oracle that crashes on a row the app degrades on cannot reconcile it.
                orphan_dividends.append((ev.account_id, ev.symbol, ev.effective))
                refuse_dividend(ev)
                continue
            if pos2.short_shares > ZERO:
                # A dividend on an OPEN SHORT is not representable: the short seller PAYS
                # the dividend in lieu, and there is no debit row for that. Booking the
                # positive net (income) or DRIP shares (breaks long/short exclusivity)
                # would be money-of-record errors — skip the event, flag the position.
                pos2.unbookable_dividend = True
                refuse_dividend(ev)       # K3 + ⑪: on the result, and no cash either
                continue
            if pos2.vacated_to is not None:
                # E24 (D32): the ticker was moved away by an EXCHANGE and nothing has put it
                # back in play. A CASH payout here would be income on a ticker that no longer
                # exists; a DRIP / 配股 would resurrect it at zero cost with no price. Refused,
                # and flagged on the LIVE successor — the position the owner still holds (it
                # may be flat; the result records the refusal either way, K3).
                _live_successor(positions, ev.account_id, pos2).unbookable_dividend = True
                refuse_dividend(ev)
                continue
            if ev.type in CASH_DIVIDEND_TYPES:
                if pos2.shares == ZERO:
                    # Position already closed when the payout landed (TW/MY pay weeks after
                    # the ex-date). No cost basis remains to reduce, so the net is realized
                    # income — booked exactly once, never absorbed into a dropped position.
                    realized_rows.append(RealizedRow(
                        ev.account_id, ev.symbol, qccy(ev.symbol), ev.d, ZERO,
                        ev.net, ZERO, ZERO, ev.net, "dividend"))
                else:
                    pos2.adjusted_total -= ev.net
            else:  # DRIP / STOCK -> add shares at zero cost
                if ev.reinvest_shares is None:
                    raise ValueError(f"{ev.type} needs reinvest_shares for {key}")
                pos2.shares += ev.reinvest_shares

    # ------- emit: ONE signed quantity per position (long positive, short negative) -------
    holdings: dict[tuple[str, str], Holding] = {}
    for k, p in positions.items():
        signed = p.shares - p.short_shares
        if signed == ZERO:
            continue
        holdings[k] = Holding(
            p.account_id, p.symbol, p.quote_ccy, signed,
            p.original_total - p.short_proceeds,
            p.adjusted_total - p.short_proceeds,
            oversold=p.ever_oversold or p.shares < ZERO,
            short_open=p.short_shares > ZERO,
            unbookable_dividend=p.unbookable_dividend,
            unbookable_action=p.unbookable_action,
            unbookable_close=p.unbookable_close)

    realized_by_ccy: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for rr in realized_rows:
        realized_by_ccy[rr.quote_ccy] += rr.realized

    cash = _cash_balances(facts, refused_divs)
    fx_avg, fx_real, fx_fcash, fx_cov = _fx_pools(facts, refused_divs)

    return OracleResult(holdings, realized_rows, dict(realized_by_ccy), cash,
                        fx_avg, fx_real, fx_fcash, fx_cov, refusals,
                        refused_dividends=refused_dividends,
                        refused_dividend_ids=frozenset(refused_divs),
                        orphan_dividends=orphan_dividends,
                        refused_closes=refused_closes,
                        refused_action_ids=frozenset(refused_actions))


def facts_through(facts: Facts, day: date) -> Facts:
    """Every fact dated on-or-before *day* — the ledger AS IT STOOD at that close.

    Re-derived from ``domain-ledger.md``, never read off the app (the independence rule).
    Each ledger is cut on its OWN date field, and dividends are cut on
    :attr:`DivFact.effective`: a 配股 attaches on the ex-date, so between ex and payment it
    must be IN the ledger at all — not merely sorted earlier. Cutting on the payment date is
    the exact defect R6 fixed, and an oracle that cut the same way could not have seen it.

    ``instruments`` is deliberately NOT cut. A registry row is not an event; dropping it
    would make an early replay raise on ``quote_ccy`` rather than give an early answer.
    """
    return Facts(
        txs=[t for t in facts.txs if t.trade_date <= day],
        divs=[d for d in facts.divs if d.effective <= day],
        fxs=[f for f in facts.fxs if f.d <= day],
        openings=[o for o in facts.openings if o.build_date <= day],
        cash=[c for c in facts.cash if c.d <= day],
        instruments=facts.instruments,
        actions=[a for a in facts.actions if a.d <= day],
    )


def facts_valued_as_of(facts: Facts, day: date) -> Facts:
    """The ledger a VALUATION as at *day* reads: every row counts from its OWN date (DEF-016
    for dividends, widened to trades, openings, FX conversions, cash movements and corporate
    actions by DEF-056 — owner rulings 2026-09-24).

    Re-derived from the rulings, never read off the app (the independence rule): a row dated
    after *day* has not happened yet — a confirmed dividend stored on its future PAYMENT
    date, a trade / opening / conversion entered ahead of its date — so it is in neither the
    holdings, the adjusted cost (總報酬), the cash pools, the FX pools nor the XIRR flows. That
    is exactly :func:`facts_through`: the valuation as at *day* IS the world at the close of
    *day*, so the oracle keeps ONE cut and this name says which question it answers.
    Dividends cut on :attr:`DivFact.effective` (a 配股 with a known ex-date is owned from
    the ex-date, R6; cash / DRIP / NET from the payment date).
    """
    return facts_through(facts, day)


# ---- options: last-trade valuation (spec §5, R2) ------------------------------------
def option_price_on(facts: Facts, symbol: str, day: date,
                    refused: Collection[int] = frozenset(), *,
                    before: CorpFact | None = None) -> tuple[Decimal, date] | None:
    """The price an open contract is valued at on *day*, and the date it is dated.

    Transcribed from spec §5 (R2, clarified 2026-10-10 — final review R1-M2): the price of the
    contract's LAST ORDINARY TRADE — ``close_reason`` rows excluded (a price-0 expiry is not a
    market price) — per symbol ACROSS accounts, ``trade_date <= day``, dated that trade's date.
    After an ``OPTION_ADJUST`` into it, the destination's price is the LATER — by date; on a
    tie, the destination's own trade — of its own last ordinary trade and the predecessor's
    last price BEFORE the adjustment ÷ ``ratio_to`` (× ``ratio_from``, which OPT-D16 fixes at
    1), dated the predecessor's trade; an adjustment the replay refused (*refused*, the ids in
    :attr:`OracleResult.refused_action_ids`) propagates no price.

    "Before the adjustment" is the replay's order (spec §4.4's EventPriority, this file's own
    copy): an action precedes the day's trades and same-day actions run in id order, so the
    predecessor is priced by its trades dated strictly before the adjustment and by the
    adjustments into it that precede this one (*before*). Two derived prices of one date (two
    adjustments into one contract) keep the earlier adjustment's.

    Recursive over a chain of adjustments; ``None`` when nothing priced the contract.
    Never a provider quote: an option is never quoted (spec §5), so this is the ONLY price.
    """
    if before is None:
        trades = [t for t in facts.txs if t.symbol == symbol and t.close_reason is None
                  and t.trade_date <= day]
        adjs = [a for a in facts.actions if a.kind == OPTION_ADJUST and a.to_symbol == symbol
                and a.d <= day and a.id not in refused]
    else:
        trades = [t for t in facts.txs if t.symbol == symbol and t.close_reason is None
                  and t.trade_date < before.d]
        adjs = [a for a in facts.actions if a.kind == OPTION_ADJUST and a.to_symbol == symbol
                and (a.d, a.id) < (before.d, before.id) and a.id not in refused]
    best: tuple[Decimal, date] | None = None
    if trades:
        last_t = max(trades, key=lambda t: (t.trade_date, t.id))
        best = (last_t.price, last_t.trade_date)
    # Own trade first, then the adjustments in replay order: a candidate replaces the best
    # only when STRICTLY later, so a tie keeps the own trade (then the earlier adjustment).
    for a in sorted(adjs, key=lambda a: (a.d, a.id)):
        prev = option_price_on(facts, a.from_symbol, day, refused, before=a)
        if prev is not None and (best is None or prev[1] > best[1]):
            best = (prev[0] * a.ratio_from / a.ratio_to, prev[1])
    return best


def option_prices(facts: Facts, day: date) -> dict[str, tuple[Decimal, date]]:
    """:func:`option_price_on` for every option symbol the ledger names, with the refusals of
    the oracle's OWN replay of the same ledger through *day*."""
    refused = replay(facts_through(facts, day)).refused_action_ids
    syms = {t.symbol for t in facts.txs if is_option(t.symbol)}
    syms |= {a.to_symbol for a in facts.actions if a.kind == OPTION_ADJUST}
    out: dict[str, tuple[Decimal, date]] = {}
    for s in syms:
        p = option_price_on(facts, s, day, refused)
        if p is not None:
            out[s] = p
    return out


def trend_skips_an_option(facts: Facts, first: date, last: date) -> bool:
    """``options.trend_excludes_value`` (spec §5), derived: was an option position open at the
    CLOSE of some trend day in ``[first, last]``?

    The trend excludes an open option's value (R3); the flag says it did so on at least one
    day. Positions only change on event dates, so the day-end states after each option
    event (and at ``first``) are every state the series passed through.
    """
    days = {first}
    days |= {t.trade_date for t in facts.txs if is_option(t.symbol)}
    days |= {a.d for a in facts.actions if a.kind == OPTION_ADJUST}
    for d in sorted(days):
        if d < first or d > last:
            continue
        res = replay_through(facts, d)
        if any(is_option(h.symbol) and h.shares != ZERO for h in res.holdings.values()):
            return True
    return False


def trading_financing_cost(facts: Facts, reporting: str, fx_on) -> Decimal:
    """The reporting-currency P&L effect of the three AI-D48 cash kinds.

    The THIRD term of the decomposition ``B = A + 本金匯率效果 + 交易與融資成本``. It exists
    as its own figure because AI-D48 briefly reproduced the defect it was raised to fix: B
    started counting these costs while A never did, so ``B − A`` — labelled 「本金匯率效果」 on
    the KPI band — was silently carrying broker fees under an exchange-rate name.

    ⚠ Sign is the P&L one and is therefore the NEGATION of the same movement's contribution
    to :func:`net_invested_through`: a broker fee is negative here and positive there. Two
    signs for one row is exactly the kind of thing an oracle should state rather than infer,
    so both are written out from ``DEBIT_KINDS`` at their own call site.
    """
    total = ZERO
    for mv in facts.cash:
        if mv.kind.upper() not in XIRR_CASH_KINDS:
            continue
        sign = -ONE if mv.kind.upper() in DEBIT_KINDS else ONE
        rate = ONE if mv.ccy == reporting else fx_on(mv.d, mv.ccy, reporting)
        total += sign * mv.amount * rate
    return total


def replay_through(facts: Facts, day: date) -> OracleResult:
    """The oracle's answer as at the CLOSE of *day*.

    Exists because every reconciliation in this harness compared the app and the oracle at
    the CURRENT state, where a whole class of defect is invisible: anything whose effect is
    confined to a window between two dates produces the same final answer either way.
    R6's ex-date is precisely that shape — a 配股's shares arrive a month early or a month
    late, and the position at ``as_of`` is identical either way — so ``fail=0`` was silent
    about it (disclosed in this directory's README §6, 2026-08-26, and closed here).
    """
    return replay(facts_through(facts, day))


def net_invested_through(facts: Facts, day: date, reporting: str, fx_on) -> Decimal:
    """Reporting-currency money put into SECURITIES through *day* (the trend's B leg).

    The app publishes this per day as ``trend.points[].net_invested``, and it is the
    subtrahend of **B** (``total_value − net_invested``) — the FX-complete lifetime result
    of AI-D41's A · B · B−A decomposition. It is worth an independent derivation for the
    reason B exists at all: A applies FX to each currency's GAIN, while B converts every
    FLOW at its own trade date, so the two disagree by the principal's FX effect and only
    one of them can be checked against a flow stream.

    Signs are the XIRR conventions NEGATED (money in is positive here):
    opening ``+orig_total`` · buy ``+(qty·price + fee + tax)`` · sell ``−(qty·price − fee −
    tax)`` · CASH/NET dividend ``−net`` · DRIP/STOCK neutral. Each flow converted at its OWN
    date via ``fx_on`` (on-or-before), never at spot.

    **Cash movements: the same three kinds as XIRR** (AI-D48, owner ruling 2026-08-27) —
    ``XIRR_CASH_KINDS``, signed by ``DEBIT_KINDS`` and then NEGATED like every other flow
    here, so a fee raises the figure exactly as a buy-side fee does and a rebate lowers it.
    ``INTEREST`` and the capital movements stay out for AI-D42's reasons, unchanged.

    ⚠ This function was written one commit earlier WITHOUT them, deliberately, so it was a
    faithful oracle of the pre-ruling app — and the ``trend.*`` family then went red on
    exactly 27 assertions (all of them ``trend.net_invested``, on exactly the dates carrying
    a cash movement, with nothing else in the run disturbed) when the app moved and the
    oracle had not yet. That red run IS this family's detection-power evidence; writing the
    helper ahead of the change would have hidden the transition it exists to catch.

    Raises ``KeyError`` when a required rate is missing — the app returns an unavailable
    trend in that case, so a silent zero here would be a false agreement.
    """
    insts = facts.instruments
    total = ZERO
    # K2 (owner ruling 2026-10-05): a payout the replay refused is no flow. This module's OWN
    # replay decides which; a refusal is fixed by the events before the payout, so the whole
    # ledger's replay answers for every day.
    refused = replay(facts).refused_dividend_ids

    def add(d: date, ccy: str, native: Decimal) -> None:
        nonlocal total
        rate = ONE if ccy == reporting else fx_on(d, ccy, reporting)
        total += native * rate

    for o in facts.openings:
        if o.build_date <= day:
            add(o.build_date, insts[o.symbol].quote_ccy, o.orig_total)
    for t in facts.txs:
        if t.trade_date > day:
            continue
        ccy = insts[t.symbol].quote_ccy
        gross = settled(t.qty, t.price, ccy)
        if t.side.upper() == "BUY":
            add(t.trade_date, ccy, gross + t.fee + t.tax)
        else:
            add(t.trade_date, ccy, -(gross - t.fee - t.tax))
    for dv in facts.divs:
        # The PAYMENT date, not `effective`: this is when the cash actually arrives. Only a
        # STOCK dividend moves under R6, and a STOCK dividend is not a cash flow at all.
        if dv.d <= day and dv.type.upper() in ("CASH", "NET") and dv.id not in refused:
            add(dv.d, insts[dv.symbol].quote_ccy, -dv.net)
    for mv in facts.cash:
        # A movement carries its OWN currency (no symbol to look one up from). The sign is
        # this module's own DEBIT_KINDS, re-derived from domain-ledger.md, then negated into
        # the money-in-is-positive convention this series uses.
        if mv.d <= day and mv.kind.upper() in XIRR_CASH_KINDS:
            sign = -ONE if mv.kind.upper() in DEBIT_KINDS else ONE
            add(mv.d, mv.ccy, -sign * mv.amount)
    return total


def _cash_balances(
    facts: Facts, refused: Collection[int]
) -> dict[tuple[str, str], Decimal]:
    """Per (account, ccy) pool (portfolio/cash.py semantics, re-derived from rules):
    debits (WITHDRAW / INTEREST_EXPENSE / BROKER_FEE) reduce the pool, every other kind
    credits it (audit C4); -fx.from +fx.to; -buy(qty*p+fee+tax) +sell(qty*p-fee-tax);
    +cash-family dividend net (CASH/NET). Opening inventory + DRIP/STOCK do not touch cash.

    ⑪ (owner ruling 2026-10-05): a dividend the replay REFUSED (*refused*, by fact id — this
    module's own replay decides it) moves no pool. Transcribed when the app moved: until then
    the pool counted it, and this function said so.
    """
    bal: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for m in facts.cash:
        bal[(m.account_id, m.ccy)] += (
            -m.amount if m.kind.upper() in DEBIT_KINDS else m.amount)
    for c in facts.fxs:
        bal[(c.account_id, c.from_ccy)] -= c.from_amt
        bal[(c.account_id, c.to_ccy)] += c.to_amt
    for t in facts.txs:
        inst = facts.instruments.get(t.symbol)
        if inst is None:
            continue
        gross = settled(t.qty, t.price, inst.quote_ccy)
        if t.side == "BUY":
            bal[(t.account_id, inst.quote_ccy)] -= (gross + t.fee + t.tax)
        else:
            bal[(t.account_id, inst.quote_ccy)] += (gross - t.fee - t.tax)
    for dv in facts.divs:
        inst = facts.instruments.get(dv.symbol)
        if inst is None or dv.id in refused:
            continue
        if dv.type in CASH_DIVIDEND_TYPES:
            bal[(dv.account_id, inst.quote_ccy)] += dv.net
    return dict(bal)


def _fx_avg_as_of(
    convs: list[FxFact], moves: list[CashFact], home: str, foreign: str, on: date,
) -> Decimal | None:
    """Weighted-average home-per-foreign rate over acquisitions dated ``<= on``.

    The cost side of a DISPOSAL is the average as it stood on the disposal's own date
    (manual §8.2 「回換前 ... avg_rate」). Re-derived here from the rule text, deliberately
    duplicating the un-bounded sum in :func:`_fx_pools` rather than sharing it: the bound
    is the property under test, and a helper shared with the full-history path would make
    the two agree by construction.

    ``moves`` is already scoped to the pool's currency by the caller. The bound is ``<=``
    because the ledger carries dates, not timestamps.
    """
    h = ZERO
    f = ZERO
    for c in convs:
        if c.d <= on and c.from_ccy == home and c.to_ccy == foreign:
            h += c.from_amt
            f += c.to_amt
    for m in moves:
        if m.d <= on and m.kind.upper() in ACQUIRING_KINDS and m.acq_home_amount is not None:
            h += m.acq_home_amount
            f += m.amount
    return (h / f) if f != ZERO else None


def _fx_pools(facts: Facts, refused: Collection[int]):
    """Per-account FX pool (domain-ledger.md / forex module semantics).

    avg_rate = sum(home cost) / sum(foreign acquired) over home->foreign conversions AND
    foreign cash CREDITS that carry an acq_home_amount (spec 2026-07-30 F1). It is
    FULL-HISTORY: it prices the exposure still held, so it is asked as of today.
    realized_fx = sum over foreign->home reconversions of
    (home_received - foreign_sold * the average AS AT THAT RECONVERSION'S DATE) — a
    disposal's cost is fixed by the acquisitions that funded it, so entering a later
    conversion must not restate it (manual §8.2; see :func:`_fx_avg_as_of`).
    foreign_cash = conversions +/- ; +sale net ; -buy allin ; +cash-family dividend net
                   ; +/- foreign cash movements (credit/WITHDRAW).
    covered_ratio = basis-known acquisitions / all acquisitions (F2); exactly 1 when nothing
                   is unbased. It scales BOTH unrealized legs (F3) — see phase1's reconcile.
    Only accounts with settlement_ccy != funding_ccy are FX-exposed.
    A dividend the replay refused (*refused*) is no foreign cash (⑪) — the funds view's rule.
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
            if m.kind.upper() not in ACQUIRING_KINDS:
                # A disposal changes neither the average nor the coverage (N1); income
                # arising inside the pool (INTEREST) inherits the average rather than
                # acquiring at an unknown rate.
                continue
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
                    # Each reconversion is priced at the average 回換前 — the acquisitions
                    # dated on or before it (manual §8.2). Applying ONE all-time average
                    # let a later conversion restate an already-reported figure, and at a
                    # high enough later rate flip its sign (QA-02). A reconversion with no
                    # acquisition yet contributes nothing rather than being priced by a
                    # rate that did not exist on its date.
                    rate = _fx_avg_as_of(convs, moves, home, foreign, c.d)
                    if rate is None:
                        continue
                    r += c.to_amt - c.from_amt * rate
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
            gross = settled(t.qty, t.price, foreign)
            if t.side == "BUY":
                cash -= gross + t.fee + t.tax
            else:
                cash += gross - t.fee - t.tax
        for dv in facts.divs:
            if dv.account_id != aid or dv.id in refused:
                continue
            # CASH *and* NET (domain-ledger.md: TW cash and MY single-tier net are both
            # money that lands in the pool). This read ``== "CASH"`` while ``_cash_balances``
            # above already read ``CASH_DIVIDEND_TYPES``, so the oracle carried the SAME
            # narrowing the app did (QA-03) and could not have caught it: the two views it
            # is supposed to prove equal were both wrong in the same direction. An oracle
            # that shares the app's bug is not an oracle.
            if dv.type in CASH_DIVIDEND_TYPES and facts.instruments[dv.symbol].quote_ccy == foreign:
                cash += dv.net
        for m in moves:
            cash += -m.amount if m.kind.upper() in DEBIT_KINDS else m.amount
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
    DRIP/STOCK neutral; the three trading/financing cash kinds signed by DEBIT_KINDS
    (AI-D42, see XIRR_CASH_KINDS); terminal +sum(price*shares) @ as_of. Each non-terminal flow is
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
        gross = settled(t.qty, t.price, ccy)
        if t.side == "BUY":
            add(t.trade_date, ccy, -(gross + t.fee + t.tax))
        else:
            add(t.trade_date, ccy, gross - t.fee - t.tax)
    for dv in facts.divs:
        # A payout the replay refused is no flow (K2 / review R2 ⑤): it is booked nowhere, so
        # counting it here would give one payment two answers. The ORPHAN class goes further
        # and withholds the whole figure — decided by the caller from ``res.orphan_dividends``.
        if dv.type in CASH_DIVIDEND_TYPES and dv.id not in res.refused_dividend_ids:
            add(dv.d, insts[dv.symbol].quote_ccy, dv.net)
    # AI-D42: the three trading/financing costs. A cash movement carries its OWN currency
    # (there is no symbol to look one up from), and the sign comes from DEBIT_KINDS — the
    # same table the pool balance uses, so a movement can never be a credit here and a debit
    # there.
    for m in facts.cash:
        kind = m.kind.upper()
        if kind in XIRR_CASH_KINDS:
            add(m.d, m.ccy, -m.amount if kind in DEBIT_KINDS else m.amount)

    final = ZERO
    for h in res.holdings.values():
        # OPT-D4: "XIRR's final value must carry the open liability" — an open option enters
        # the terminal value at its last trade price on its SIGNED quantity (a sold contract
        # is a negative value). A stock short never reaches here: the caller gates the KPI
        # family off while one is open.
        if h.shares == ZERO or (h.shares < ZERO and not is_option(h.symbol)):
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

    DECLARED shorts (2026-07-31): a sell flagged ``short_sale`` may legitimately drive the
    signed position negative — the LONG lot alone must never go negative. The check
    replays the two lots with the same exclusivity rule as the oracle (buy covers short
    first; declared sell exhausts long first) and flags only an undeclared sell exceeding
    the long lot. A DRIP/STOCK share credit while the short is open is skipped, mirroring
    the unbookable-dividend rule. Declared-short sells are also excluded from
    ``sell.has_realized_row``: a pure short-open realizes nothing until covered (the cover
    P&L is verified by the reconciliation's realized-row comparison instead).

    CORPORATE ACTIONS (2026-08-10, spec W8): the walk is action-aware, and it has to be —
    a 3-for-1 split followed by a sell of the post-split count would otherwise read as a
    position going negative, which is the FALSE POSITIVE version of the exact defect this
    check exists to catch. The walk is therefore a single ordered pass over ALL keys (an
    EXCHANGE moves shares BETWEEN symbols, so a per-symbol walk cannot express it) and it
    reuses :func:`_apply_action`, so the refusal matrix is stated once. Basis fields stay
    zero here — this layer tracks share validity only — which is sound because a position
    with zero shares always has zero basis, so ``_apply_action``'s E2 predicate reaches
    the same verdict it reaches in the replay.

    OPTIONS (2026-10-09 spec): ``OPTION_ADJUST`` moves the long AND the short count through
    the same :func:`_apply_action`; a §4.4-refused close moves nothing, exactly as the replay
    books nothing for it, and is not a "sell without a realized row".

    Returns ``[(check, scope)]`` for each violation; empty means clean.
    """
    ok = acked or set()
    out: list[tuple[str, str]] = []
    insts = facts.instruments

    events: list[tuple[date, int, int, str, object]] = []
    for i, o in enumerate(sorted(facts.openings, key=lambda x: (x.account_id, x.symbol))):
        events.append((o.build_date, EventPriority.OPENING, i, "open", o))
    for act in facts.actions:
        events.append((act.d, EventPriority.CORPORATE_ACTION, act.id, "corp", act))
    for t in facts.txs:
        # One rank for both sides; the id (third key) is the intraday order (DEF-012).
        events.append((t.trade_date, EventPriority.TRADE, t.id, "tx", t))
    for dv in facts.divs:
        if dv.reinvest_shares:
            events.append((dv.effective, EventPriority.DIVIDEND, dv.id, "div", dv))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    positions: dict[tuple[str, str], _Pos] = {}
    reported: set[tuple[str, str]] = set()

    def pos_for(aid: str, sym: str) -> _Pos:
        inst = insts.get(sym)
        return positions.setdefault(
            (aid, sym), _Pos(aid, sym, inst.quote_ccy if inst is not None else ""))

    refused_close_ids: set[int] = set()
    for d, _prio, _seq, ekind, ev in events:
        if ekind == "corp":
            assert isinstance(ev, CorpFact)
            _apply_action(positions, ev, insts)
        elif ekind == "open":
            assert isinstance(ev, OpenFact)
            p = pos_for(ev.account_id, ev.symbol)
            _reopen(p)                                       # CR-06, as the replay
            p.shares += ev.shares
        elif ekind == "tx":
            assert isinstance(ev, TxFact)
            if _close_refused(positions.get((ev.account_id, ev.symbol)), ev):
                # Options §4.4, as the replay: a refused close moves no share and creates no
                # position. It is also no "sell without a realized row" — it was never booked.
                refused_close_ids.add(ev.id)
                continue
            p = pos_for(ev.account_id, ev.symbol)
            if ev.qty != ZERO:
                _reopen(p)                                   # CR-06, as the replay
            if ev.side.upper() == "BUY":
                cover = min(ev.qty, p.short_shares)
                p.short_shares -= cover
                p.shares += ev.qty - cover
            elif ev.short_sale:
                from_long = min(ev.qty, p.shares if p.shares > ZERO else ZERO)
                p.shares -= from_long
                p.short_shares += ev.qty - from_long
            else:
                p.shares -= ev.qty
                if p.shares < ZERO:
                    p.ever_oversold = True     # sticky, exactly as the replay marks it
                    key = (ev.account_id, ev.symbol)
                    if key not in ok and key not in reported:
                        reported.add(key)
                        out.append(("position.never_negative",
                                    f"{key[0]}/{key[1]} nets {p.shares} on {d.isoformat()}"))
        else:  # reinvest shares — skipped wherever the replay refuses the dividend
            assert isinstance(ev, DivFact)
            # The three refusal classes (YQ3): no position at all (CR-02 — checked BEFORE
            # pos_for, which would create one), an open short, a vacated ticker (E24).
            if (ev.account_id, ev.symbol) not in positions:
                continue
            p = pos_for(ev.account_id, ev.symbol)
            if (p.short_shares > ZERO or p.vacated_to is not None
                    or ev.reinvest_shares is None):
                continue
            p.shares += ev.reinvest_shares

    # Options spec §4.2 (structural, every door): a price-0 row exists ONLY as a close row, and
    # a close row is an option, one of the three reasons, never a declared short, and carries
    # no fee and no tax. A stored row breaking either rule got past an input control.
    for t in facts.txs:
        if t.close_reason is None:
            if t.price <= ZERO:
                out.append(("trade.price_positive", f"id={t.id} {t.account_id}/{t.symbol}"))
        elif (t.close_reason not in CLOSE_REASONS or not is_option(t.symbol)
              or t.price != ZERO or t.fee != ZERO or t.tax != ZERO or t.short_sale):
            out.append(("close_row.well_formed", f"id={t.id} {t.account_id}/{t.symbol}"))

    sold = {(t.account_id, t.symbol, t.trade_date)
            for t in facts.txs if t.side.upper() == "SELL" and not t.short_sale
            and t.id not in refused_close_ids}
    booked = {(r.account_id, r.symbol, r.sell_date) for r in realized_rows
              if getattr(r, "kind", "sale") == "sale"}
    for key in sorted(sold - booked):
        if (key[0], key[1]) in ok:
            continue      # an acked oversell legitimately emits no realized row
        out.append(("sell.has_realized_row", f"{key[0]}/{key[1]} sold {key[2].isoformat()}"))
    return out
