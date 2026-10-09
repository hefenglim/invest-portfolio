"""Seed the DEMO database with synthetic (fictional) portfolio data.

Ops script for the public demo instance: it populates a realistic multi-currency,
multi-account portfolio so visitors can explore a full dashboard WITHOUT any real
data. Point ``DB_PATH`` at the demo folder and run once:

    DB_PATH=/home/<user>/data-demo/portfolio.db .venv/bin/python scripts/seed_demo.py

Everything here is FICTIONAL. It guards on an existing transaction ledger and refuses
to double-seed, so it is safe to re-run — but never point it at a real ledger.

**Two independently-guarded halves (spec 7.7 / D25, 2026-08-09).** The base ledger is
seeded once and never again; the corporate-action corpus is a SEPARATE top-up with its
own guard, because the live demo instance was already seeded long before corporate
actions existed and demo data **accumulates and is never reset** (owner ruling
2026-07-31). A corpus that only appeared on a fresh seed would never reach the site the
staged deploy is verified on — which is exactly the shipping defect 7.7 was written to
close: `engineering-process.md` verifies the release tag on the demo site FIRST, and a
demo ledger with no corporate actions returns green having exercised none of the feature.
Re-running this script on an already-seeded database therefore skips the base ledger and
still installs (or repairs) the corporate-action corpus.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_cash_movement,
    insert_corporate_action,
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.data_ingestion.validate import (
    CorporateActionInput,
    validate_corporate_action,
)
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.scheduler.jobs import split_factor_fn
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side

_NOW = datetime.now(ZoneInfo("Asia/Taipei"))
_TODAY = _NOW.date()


def _already_seeded(conn: sqlite3.Connection) -> bool:
    try:
        count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    except sqlite3.OperationalError:
        return False
    return bool(count)


def _month_starts(first: date, last: date) -> list[date]:
    """The 1st of every month from *first*'s month through *last*, plus *last* itself."""
    out: list[date] = []
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        out.append(date(y, m, 1))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    if out and out[-1] != last:
        out.append(last)
    return out


# Monthly close path per symbol: (market, currency, [closes aligned to _month_starts]).
# Fictional but monotone-ish, ending at the current price seeded below.
_HISTORY: dict[str, tuple[Market, Currency, list[str]]] = {
    "2330": (Market.TW, Currency.TWD,
             ["590", "610", "700", "820", "1100", "1650", "2100", "2500"]),
    "0056": (Market.TW, Currency.TWD,
             ["37.6", "38.0", "38.4", "39.1", "39.8", "40.4", "41.0", "41.5"]),
    "AAPL": (Market.US, Currency.USD,
             ["220", "228", "236", "245", "258", "270", "284", "294"]),
    "NVDA": (Market.US, Currency.USD,
             ["132", "136", "140", "146", "151", "157", "161", "165"]),
    "1155": (Market.MY, Currency.MYR,
             ["9.80", "9.95", "10.10", "10.25", "10.40", "10.55", "10.68", "10.78"]),
}

# Monthly FX path per pair, same alignment; the last point equals the spot seeded below.
_FX_HISTORY: dict[tuple[Currency, Currency], list[str]] = {
    (Currency.USD, Currency.TWD): ["32.0", "32.1", "32.2", "32.3", "32.4", "32.5",
                                   "32.5", "32.5"],
    (Currency.USD, Currency.MYR): ["4.60", "4.58", "4.56", "4.53", "4.50", "4.48",
                                   "4.46", "4.45"],
    (Currency.MYR, Currency.TWD): ["6.96", "7.01", "7.06", "7.13", "7.20", "7.26",
                                   "7.29", "7.30"],
}


def _seed_history(conn: sqlite3.Connection) -> None:
    """Monthly price + FX series from the first ledger flow to today (audit L6)."""
    days = _month_starts(date(2026, 1, 1), _TODAY)
    price_rows = []
    for symbol, (market, _ccy, closes) in _HISTORY.items():
        for i, day in enumerate(days):
            close = closes[min(i, len(closes) - 1)]
            price_rows.append(PriceRow(instrument=symbol, market=market, as_of=day,
                                       close=Decimal(close), source="demo"))
    upsert_prices(conn, price_rows, fetched_at=_NOW)

    fx_rows = []
    for (base, quote), rates in _FX_HISTORY.items():
        for i, day in enumerate(days):
            fx_rows.append(FxRow(base=base, quote=quote, as_of=day,
                                 rate=Decimal(rates[min(i, len(rates) - 1)]), source="demo"))
    upsert_fx(conn, fx_rows, fetched_at=_NOW)


def _seed_base(conn: sqlite3.Connection) -> None:
    """The original demo ledger: 5 instruments, 6 transactions, 1 dividend, 2 FX legs."""
    # --- instruments (fictional but plausible; names tagged DEMO) ---
    # Sectors use the canonical GICS vocabulary (R6, 2026-07-19): Semiconductors + Tech both
    # fold into Information Technology; Banking → Financials; ETF stays its own bucket.
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Information Technology", name="台積電 (DEMO)",
                                       board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="0056", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="ETF", name="元大高股息 (DEMO)", board="TWSE",
                                       is_etf=True))  # M10-01: the tax answer, not the label
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Information Technology", name="Apple (DEMO)"))
    upsert_instrument(conn, Instrument(symbol="NVDA", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Information Technology", name="NVIDIA (DEMO)"))
    upsert_instrument(conn, Instrument(symbol="1155", market=Market.MY, quote_ccy=Currency.MYR,
                                       sector="Financials", name="Maybank (DEMO)"))

    # --- funding deposits (audit L6, 2026-07-26) ---
    # Without these every pool on the demo read NEGATIVE: the seed booked trades and FX legs
    # but never the money that paid for them, so 資金管理 showed five overdrawn accounts AND
    # the FX card marked a negative USD cash balance to spot (換匯損益 is computed on
    # stock value + cash — see forex/pools.py). Each deposit lands before the first flow it
    # funds and is sized to leave a small positive residue, as a real account would.
    insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 1, 2),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("1600000"),
                         note="期初匯入 (DEMO)")
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 2),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("350000"),
                         note="期初匯入 (DEMO)")
    insert_cash_movement(conn, account_id="moomoo_my", move_date=date(2026, 1, 2),
                         kind="DEPOSIT", ccy=Currency.MYR, amount=Decimal("50000"),
                         note="期初匯入 (DEMO)")

    # --- transactions across all four accounts ---
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("2000"), price=Decimal("600"), fees=Decimal("171"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 6))
    insert_transaction(conn, account_id="tw_broker", symbol="0056", side=Side.BUY,
                       quantity=Decimal("5000"), price=Decimal("38"), fees=Decimal("27"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 10))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("30"), price=Decimal("225"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 15))
    insert_transaction(conn, account_id="schwab", symbol="NVDA", side=Side.BUY,
                       quantity=Decimal("20"), price=Decimal("140"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 3, 3))
    insert_transaction(conn, account_id="moomoo_my", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("240"), fees=Decimal("1"),
                       tax=Decimal("0"), trade_date=date(2026, 4, 1))
    insert_transaction(conn, account_id="moomoo_my", symbol="1155", side=Side.BUY,
                       quantity=Decimal("3000"), price=Decimal("10"), fees=Decimal("15"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 20))

    # --- a TW cash dividend (folds into adjusted cost) ---
    insert_dividend(conn, account_id="tw_broker", symbol="0056", div_date=date(2026, 4, 20),
                    div_type="CASH", gross=Decimal("6000"), withholding=Decimal("0"),
                    net=Decimal("6000"))

    # --- funding FX conversions ---
    # Sized so each USD pool stays POSITIVE after the USD buys it funds (audit L6): schwab
    # buys 9,550 USD of stock, moomoo_my 2,401. The implied rates are unchanged for schwab
    # (320,000/10,000 = 32.0, as before) so the FX attribution keeps the same cost basis.
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 12),
                         from_ccy=Currency.TWD, from_amount=Decimal("320000"),
                         to_ccy=Currency.USD, to_amount=Decimal("10000"))
    insert_fx_conversion(conn, account_id="moomoo_my", date=date(2026, 3, 28),
                         from_ccy=Currency.MYR, from_amount=Decimal("11500"),
                         to_ccy=Currency.USD, to_amount=Decimal("2500"))

    # --- price + FX HISTORY (audit L6, 2026-07-26) ---
    # The seed used to store ONE price row and ONE FX row, both dated today. Two flagship
    # surfaces died on that: XIRR needs a trade-date rate for every flow
    # (`no FX rate stored on or before 2026-01-15 for USD/TWD`) and the 總市值 trend needs a
    # dated series (`missing FX history for a ledger flow date`) — so the public demo showed
    # an empty XIRR card and an empty chart, which are the two things it exists to show.
    # A monthly series from the first flow to today fixes both; the app carries values
    # forward between points, so monthly granularity renders a smooth line.
    _seed_history(conn)

    # --- current prices ---
    upsert_prices(conn, [
        PriceRow(instrument="2330", market=Market.TW, as_of=_TODAY,
                 close=Decimal("2500"), source="demo"),
        PriceRow(instrument="0056", market=Market.TW, as_of=_TODAY,
                 close=Decimal("41.5"), source="demo"),
        PriceRow(instrument="AAPL", market=Market.US, as_of=_TODAY,
                 close=Decimal("294"), source="demo"),
        PriceRow(instrument="NVDA", market=Market.US, as_of=_TODAY,
                 close=Decimal("165"), source="demo"),
        PriceRow(instrument="1155", market=Market.MY, as_of=_TODAY,
                 close=Decimal("10.78"), source="demo"),
    ], fetched_at=_NOW)

    # --- FX rates (reporting blend) ---
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=_TODAY,
              rate=Decimal("32.5"), source="demo"),
        FxRow(base=Currency.USD, quote=Currency.MYR, as_of=_TODAY,
              rate=Decimal("4.45"), source="demo"),
        FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=_TODAY,
              rate=Decimal("7.3"), source="demo"),
    ], fetched_at=_NOW)

    conn.commit()
    print("demo seed complete: 5 instruments, 6 transactions, 1 dividend, 2 FX conversions.")


# ---------------------------------------------------------------------------
# Corporate-action corpus (spec §7.7, decision D25)
# ---------------------------------------------------------------------------
# Seeded so the mandatory demo-first staged deploy actually EXERCISES the feature.
# One of each kind, chosen for the decisions that only appear at runtime:
#
#   ORBT   forward SPLIT 3-for-1, held in TWO accounts -> D13's all-or-nothing rule
#          (two ledger rows, one price event) and §5.1 detail 3's price dedup. The
#          9-for-1 / 27-for-1 trap is raising the factor to the power of the number
#          of holding accounts, so it EXISTS ONLY for a multi-account holder whose
#          price series spans the action date — which is why §7.7's first two table
#          rows are one symbol here and not two. Split apart, neither half can see it.
#   VRTB   EXCHANGE (ticker rename, ratio 1-for-1) -> VRTA: §6.3's `＋公司行動` drawer
#          term and its reconciliation footer, on the kind that empties its source.
#   KEMB   SPINOFF 1-for-5 with cost_carry -> KEMG: D21's provenance label on the
#          child's 回本進度. The parent is a MY (MYR) position with a cash dividend on
#          purpose: `dividend_portion` is what D21 labels, and only a CASH/NET dividend
#          reduces `adjusted_total` today. A US parent would carve a zero portion and
#          leave D21 with nothing to label until D35 lands (P1b, not this feature).
#
# Dedicated fictional symbols rather than the existing demo tickers, for three reasons:
#   1. **The price basis would be wrong on an already-seeded database.** Rows written
#      before their symbol's split was known carry `split_basis='1'` and a `close_raw`
#      equal to the as-traded close. Adding a split afterwards makes `target != 1` for
#      every pre-split row, and W6b's reconcile — correctly, for a row that came from a
#      real post-split provider — rebuilds `close := close_raw × target` and multiplies
#      eight months of AAPL closes by 3. A symbol whose whole series this script writes
#      (in provider basis, through `factor_of`) satisfies the invariant on a fresh AND
#      an already-seeded DB, identically.
#   2. A SPLIT on an existing symbol would silently re-denominate every accumulated
#      figure on a corpus the owner ruled must be preserved, not restated.
#   3. D13 counts the accounts holding the symbol ON THE ACTION DATE. Owning the whole
#      holder set makes the all-accounts rule satisfiable by construction; on a shared
#      ticker a later back-dated buy in a third account would quietly turn the seeded
#      action into the partial application E13 exists to forbid.

_SPLIT_SYM = "ORBT"
_SPLIT_DATE = date(2026, 5, 15)
_SPLIT_TO, _SPLIT_FROM = Decimal("3"), Decimal("1")  # 3-for-1 forward split

_XCHG_FROM_SYM, _XCHG_TO_SYM = "VRTB", "VRTA"
_XCHG_DATE = date(2026, 6, 16)
_XCHG_LAST_TRADE = date(2026, 6, 15)  # the old ticker's final price row

_SPIN_PARENT, _SPIN_CHILD = "KEMB", "KEMG"
_SPIN_DATE = date(2026, 6, 30)
_SPIN_TO, _SPIN_FROM = Decimal("1"), Decimal("5")  # one child share per five parent
_SPIN_CARRY = Decimal("0.18")  # 8-K allocation: 18% of the parent's basis moves

_CA_SOURCES = (_SPLIT_SYM, _XCHG_FROM_SYM, _SPIN_PARENT)

# Provider-basis closes — i.e. what a data vendor would deliver TODAY, with every split
# it knows about already folded in. `upsert_prices` multiplies each row by `factor_of`
# and stores the AS-TRADED close (§5.1(a)); pre-split ORBT rows are therefore written at
# one third of the price that actually traded, and come back out at 174/180/192/201/213.
# Chosen divisible by 3 so `close_raw × 3` is exact and the 4-dp cap never fires.
_SPLIT_CLOSES = ["58.00", "60.00", "64.00", "67.00", "71.00",
                 "74.40", "78.90", "81.60", "82.50"]
_XCHG_FROM_CLOSES = ["24.50", "25.10", "26.40", "27.80", "29.60", "30.90"]
_XCHG_TO_CLOSES = ["31.80", "33.10", "33.60"]
_SPIN_PARENT_CLOSES = ["2.30", "2.38", "2.40", "2.46", "2.52", "2.58",
                       "2.14", "2.20", "2.22"]
_SPIN_CHILD_CLOSES = ["2.18", "2.24", "2.26"]


def _ca_positions_present(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE symbol IN (?,?,?)", _CA_SOURCES
    ).fetchone()
    return bool(row[0])


def _ca_actions_present(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM corporate_actions WHERE from_symbol IN (?,?,?)", _CA_SOURCES
    ).fetchone()
    return bool(row[0])


def _seed_ca_positions(conn: sqlite3.Connection) -> None:
    """Instruments, funding and the buys the three actions act on.

    Funding is sized so every currency pool stays POSITIVE at every date (audit L6): the
    demo's existing USD pools hold a few hundred dollars of residue, which these buys
    would overdraw. The two new conversions use the SAME implied rates as the ones
    already seeded (32.0 TWD/USD, 4.56 MYR/USD) so the FX cost basis — and therefore the
    demo's 換匯損益 attribution — is left exactly where it was.
    """
    upsert_instrument(conn, Instrument(symbol=_SPLIT_SYM, market=Market.US,
                                       quote_ccy=Currency.USD, sector="Information Technology",
                                       name="Orbital Dynamics (DEMO)"))
    upsert_instrument(conn, Instrument(symbol=_XCHG_FROM_SYM, market=Market.US,
                                       quote_ccy=Currency.USD, sector="Health Care",
                                       name="Verta Bio (DEMO)"))
    upsert_instrument(conn, Instrument(symbol=_XCHG_TO_SYM, market=Market.US,
                                       quote_ccy=Currency.USD, sector="Health Care",
                                       name="Verta Bio (DEMO,換股後新代號)"))
    upsert_instrument(conn, Instrument(symbol=_SPIN_PARENT, market=Market.MY,
                                       quote_ccy=Currency.MYR, sector="Consumer Staples",
                                       name="Kembun Plantations (DEMO)"))
    upsert_instrument(conn, Instrument(symbol=_SPIN_CHILD, market=Market.MY,
                                       quote_ccy=Currency.MYR, sector="Utilities",
                                       name="Kembun Green Energy (DEMO)"))

    # --- funding: one home-currency deposit + one conversion per account ---
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 28),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("220000"),
                         note="公司行動示範資金 (DEMO)")
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 30),
                         from_ccy=Currency.TWD, from_amount=Decimal("208000"),
                         to_ccy=Currency.USD, to_amount=Decimal("6500"))
    insert_cash_movement(conn, account_id="moomoo_my", move_date=date(2026, 3, 1),
                         kind="DEPOSIT", ccy=Currency.MYR, amount=Decimal("30000"),
                         note="公司行動示範資金 (DEMO)")
    insert_fx_conversion(conn, account_id="moomoo_my", date=date(2026, 3, 5),
                         from_ccy=Currency.MYR, from_amount=Decimal("17784"),
                         to_ccy=Currency.USD, to_amount=Decimal("3900"))

    # --- the SPLIT symbol, held in BOTH US accounts (D13's N = 2) ---
    insert_transaction(conn, account_id="schwab", symbol=_SPLIT_SYM, side=Side.BUY,
                       quantity=Decimal("30"), price=Decimal("180"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 5))
    insert_transaction(conn, account_id="moomoo_my", symbol=_SPLIT_SYM, side=Side.BUY,
                       quantity=Decimal("20"), price=Decimal("192"), fees=Decimal("2"),
                       tax=Decimal("0"), trade_date=date(2026, 3, 10))

    # --- the EXCHANGE source (one account: a rename moves the whole position) ---
    insert_transaction(conn, account_id="schwab", symbol=_XCHG_FROM_SYM, side=Side.BUY,
                       quantity=Decimal("40"), price=Decimal("26"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 18))

    # --- the SPINOFF parent + the cash dividend that gives D21 something to label ---
    insert_transaction(conn, account_id="moomoo_my", symbol=_SPIN_PARENT, side=Side.BUY,
                       quantity=Decimal("5000"), price=Decimal("2.40"), fees=Decimal("11"),
                       tax=Decimal("12"), trade_date=date(2026, 2, 25))
    insert_dividend(conn, account_id="moomoo_my", symbol=_SPIN_PARENT,
                    div_date=date(2026, 5, 8), div_type=DividendType.NET.value,
                    gross=Decimal("1500"), withholding=Decimal("0"), net=Decimal("1500"))
    conn.commit()


def _ca_batch() -> list[CorporateActionInput]:
    """The four rows, as the entry surface would submit them — ONE batch.

    D13/D28: a multi-account action is written as an N-row batch, not N separate
    submissions. E13 and E12 are batch-level rules and a per-row check cannot see its
    siblings, so the two ORBT rows must arrive together or the second one is rejected as
    an incomplete application of the first.
    """
    def split_row(account_id: str) -> CorporateActionInput:
        return CorporateActionInput(
            account_id=account_id, date=_SPLIT_DATE, kind=CorporateActionKind.SPLIT.value,
            from_symbol=_SPLIT_SYM, to_symbol=_SPLIT_SYM,
            ratio_to=_SPLIT_TO, ratio_from=_SPLIT_FROM, note="3 股換 1 股拆股 (DEMO)",
        )

    return [
        split_row("schwab"),
        split_row("moomoo_my"),
        CorporateActionInput(
            account_id="schwab", date=_XCHG_DATE, kind=CorporateActionKind.EXCHANGE.value,
            from_symbol=_XCHG_FROM_SYM, to_symbol=_XCHG_TO_SYM,
            ratio_to=Decimal("1"), ratio_from=Decimal("1"),
            note="代號變更，股數不變 (DEMO)",
        ),
        CorporateActionInput(
            account_id="moomoo_my", date=_SPIN_DATE, kind=CorporateActionKind.SPINOFF.value,
            from_symbol=_SPIN_PARENT, to_symbol=_SPIN_CHILD,
            ratio_to=_SPIN_TO, ratio_from=_SPIN_FROM, cost_carry=_SPIN_CARRY,
            note="每 5 股配 1 股子公司，成本移轉 18% (DEMO)",
        ),
    ]


def _assert_validator_clean(conn: sqlite3.Connection, batch: list[CorporateActionInput]) -> None:
    """Refuse to seed a corpus the app's own entry path would reject.

    A demo whose data could not have been entered through the UI is worse than no demo:
    it teaches the wrong lesson at exactly the moment someone is verifying a release.
    Every issue counts, soft ones included — a `needs_confirm` row is one the owner would
    have had to click through, which is not a state to bake into a fixture.
    """
    for inp in batch:
        issues = validate_corporate_action(conn, inp, batch=batch)
        if issues:
            detail = "; ".join(f"[{i.kind}] {i.message}" for i in issues)
            raise SystemExit(
                f"REFUSING TO SEED — {inp.account_id}/{inp.from_symbol} "
                f"{inp.date.isoformat()} failed validation: {detail}"
            )


def _seed_ca_actions(conn: sqlite3.Connection, batch: list[CorporateActionInput]) -> None:
    for inp in batch:
        insert_corporate_action(
            conn, account_id=inp.account_id, action_date=inp.date,
            kind=CorporateActionKind(inp.kind), from_symbol=inp.from_symbol,
            to_symbol=inp.to_symbol, ratio_to=inp.ratio_to, ratio_from=inp.ratio_from,
            cost_carry=inp.cost_carry, note=inp.note, commit=False,
        )
    conn.commit()


def _monthly(symbol: str, market: Market, start: date, end: date,
             closes: list[str]) -> list[PriceRow]:
    """Month-start rows (plus *end* itself), carrying the last close forward."""
    return [
        PriceRow(instrument=symbol, market=market, as_of=day,
                 close=Decimal(closes[min(i, len(closes) - 1)]), source="demo")
        for i, day in enumerate(_month_starts(start, end))
    ]


def _seed_ca_prices(conn: sqlite3.Connection) -> None:
    """Price series for the EXCHANGE and SPINOFF symbols — written BEFORE their actions.

    Order is a requirement, not a preference. `validate.py`'s N3-price check raises a
    `needs_confirm` issue when an EXCHANGE / SPINOFF destination has no price at all,
    because ONE unpriced holding returns `rate=None` for the WHOLE portfolio's XIRR. A
    fixture that needs an acknowledgement to commit is a fixture that could not have been
    entered cleanly, so these rows land first. (A SPLIT is excluded from that check — its
    destination is its own source — which is what lets the split series be written later,
    once the factor it is expressed through exists.)

    Neither symbol group has a SPLIT, so `factor_of` returns the identity for every row
    here and the stored close is the provider value untouched. It is still threaded
    through, so there is one write path and not two.
    """
    factor_of = split_factor_fn(conn)
    rows: list[PriceRow] = []

    # The renamed ticker's series ENDS at the action and the new one BEGINS there, at the
    # same level (ratio 1-for-1): 40 x 31.20 on either side, so net worth is continuous
    # across the rename and any discontinuity on the chart is a defect, not the fixture.
    rows += _monthly(_XCHG_FROM_SYM, Market.US, date(2026, 1, 1), date(2026, 6, 1),
                     _XCHG_FROM_CLOSES)
    rows.append(PriceRow(instrument=_XCHG_FROM_SYM, market=Market.US,
                         as_of=_XCHG_LAST_TRADE, close=Decimal("31.20"), source="demo"))
    rows.append(PriceRow(instrument=_XCHG_TO_SYM, market=Market.US, as_of=_XCHG_DATE,
                         close=Decimal("31.20"), source="demo"))
    rows += _monthly(_XCHG_TO_SYM, Market.US, date(2026, 7, 1), _TODAY, _XCHG_TO_CLOSES)

    # The parent re-prices on the ex-date and the child starts there: 5,000 x 2.58 before
    # equals 5,000 x 2.15 + 1,000 x 2.15 after, so the carve-out is value-neutral on the
    # day it happens (§2.1's continuity clause, made visible on the chart).
    rows += _monthly(_SPIN_PARENT, Market.MY, date(2026, 1, 1), _TODAY, _SPIN_PARENT_CLOSES)
    for sym in (_SPIN_PARENT, _SPIN_CHILD):
        rows.append(PriceRow(instrument=sym, market=Market.MY, as_of=_SPIN_DATE,
                             close=Decimal("2.15"), source="demo"))
    rows += _monthly(_SPIN_CHILD, Market.MY, date(2026, 7, 1), _TODAY, _SPIN_CHILD_CLOSES)

    upsert_prices(conn, rows, fetched_at=_NOW, factor_of=factor_of)


def _seed_ca_split_prices(conn: sqlite3.Connection) -> None:
    """The SPLIT symbol's series, spanning the action date — through the D17 factor seam.

    **`factor_of` is passed, and it is not optional here.** `fetched_at` is NOW, which is
    AFTER the action, so `target(row)` for a pre-split row is 3, not 1. Writing without the
    factor would store `split_basis='1'` against a `target` of 3 — a row that violates
    §5.1(b)'s stored invariant on the day it is written, and whose close the first reconcile
    (any insert / edit / delete of that SPLIT) would then multiply by 3 a second time.
    Passing it makes the seed reproduce EXACTLY what a real refresh against a real provider
    produces: `close_raw` in the vendor's post-split basis, `split_basis` = 3, and `close` =
    the price that actually traded that day.

    The series straddles 2026-05-15 with its nearest rows on 05-01 and 06-01, so the ~16
    days between them exercise §5.1(d)'s carry-forward re-expression: 30 x 213.00 on 05-01
    must equal 90 x (213.00 / 3) on 05-16, or the demo's net-worth chart shows a 3x cliff.
    That cliff is the observable this row of §7.7 exists to put on the demo site.

    Idempotent (`upsert_prices` is ON CONFLICT DO UPDATE keyed on instrument+date), so it
    runs unconditionally: a re-run restates every close from the same raw input and lands
    byte-identically, which also repairs a half-finished earlier run.
    """
    upsert_prices(
        conn,
        _monthly(_SPLIT_SYM, Market.US, date(2026, 1, 1), _TODAY, _SPLIT_CLOSES),
        fetched_at=_NOW,
        factor_of=split_factor_fn(conn),  # built ONCE for the whole write (D17)
    )


def seed_corporate_actions(conn: sqlite3.Connection) -> None:
    """Install the §7.7 corpus. Guarded independently of the base ledger — see module doc."""
    if not _ca_positions_present(conn):
        _seed_ca_positions(conn)
    _seed_ca_prices(conn)  # destinations must be priced before their action validates
    if not _ca_actions_present(conn):
        batch = _ca_batch()
        _assert_validator_clean(conn, batch)  # BEFORE the write: E15 sees stored rows
        _seed_ca_actions(conn, batch)
        print("corporate-action corpus seeded: 1 multi-account SPLIT (2 rows), "
              "1 EXCHANGE, 1 SPINOFF.")
    else:
        print("corporate-action corpus already present — restating its price basis only.")
    _seed_ca_split_prices(conn)  # the factor only exists once the action row does


def seed(conn: sqlite3.Connection) -> None:
    # Idempotent table setup (safe whether the app has booted this DB yet or not).
    bootstrap_db(conn)
    create_pricing_tables(conn)
    seed_accounts(conn)
    if _already_seeded(conn):
        print("demo DB already has a transaction ledger — skipping the base seed (idempotent).")
    else:
        _seed_base(conn)
    # NOT inside the guard above: the live demo was seeded before this feature existed
    # (D25, §7.7). Its own guard decides whether there is anything to do.
    seed_corporate_actions(conn)


def main() -> None:
    db_path = get_settings().db_path
    print(f"seeding demo DB at: {db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        seed(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
