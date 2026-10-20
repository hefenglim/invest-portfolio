"""The synthetic corpora every settlement package is checked against (plan W0.3).

Each corpus is a seed function, a fixed clock (``now``, an aware Asia/Taipei datetime) and a
reporting currency. Templates are FILE-backed SQLite databases (never ``:memory:``), built
once and copied per use, so ``PRAGMA data_version``, WAL and the shared-build key behave as
they do on prod.

=================  =========================================================================
``golden``         ``tests/conftest.py::_seed_golden`` over ``init_golden_base``, at
                   ``GOLDEN_NOW`` (2026-06-11 14:30), reporting TWD.
``demo``           ``scripts/seed_demo.py::seed`` (base ledger + the corporate-action corpus)
                   over ``init_golden_base``, at ``DEMO_NOW`` (2026-08-11 14:30), reporting TWD.
``doubled_demo``   ``demo`` plus a +1-day copy of every transaction and dividend dated
                   before ``DEMO_NOW``, provenance cleared, reporting TWD.
``usd_reporting``  ``golden`` reporting in USD, plus ONE USD/TWD rate (31.9 on 2026-01-02).
``unquoted_ccy_cash``  ``golden`` plus a MYR rate and one MYR REBATE (an XIRR cash kind in a
                   currency no ledger instrument is quoted in): PERF-F2's shape.
``rich``           ``seed_rich`` at ``RICH_NOW`` (2026-06-10 14:30), reporting TWD: a declared
                   short opened and covered, options (sell-to-open, OPTION_ADJUST, buy-to-close,
                   an expiry close row), DRIP and 配股, SPLIT + EXCHANGE + SPINOFF, rows dated
                   after the clock, an FX gap, a stale price — and no refusal.
``rich_refusals``  ``rich`` reporting USD, plus 賣超, an orphan dividend, a dividend on the open
                   short, a refused close row, a refused and an unreadable action, an unpriced
                   holding (XIRR and B withheld).
``epoch1990``      ``seed_epoch1990`` at ``EPOCH_NOW`` (1990-03-15 14:30), reporting TWD: an
                   opening built 1988, ``^GSPC`` closes from 1987, one 1987 USD/TWD rate.
=================  =========================================================================

Why three rich corpora, not one: the refusals withhold XIRR and B ledger-wide, so on one
ledger they would hide every other shape from those figures; and a 1988 opening under a 2026
clock would stretch the trend to 38 years and the month-end sweep to ~460 builds.
``tests/reference/payloads/README.md`` lists every shape.

**Synthetic only** — the repo is public. **Runs on commit 473fe5a's code too**:
``scripts/perf/make_reference_payloads.py`` copies this file into a worktree of that commit,
so it may use only APIs that exist there (no ``shared/db.connect_path``, no settle tables).

``usd_reporting`` — why the extra rate: golden's first flow (the 2330 buy, 2026-01-05) is in
TWD and its first USD/TWD row is dated 2026-01-08, so golden reporting in USD has NO trend, no
B and no XIRR (``查無 2026-01-05（含）之前的 TWD/USD 匯率``) — the 28-digit inverse rates would
then reach the KPIs only, never the trend's ``net_invested`` sums that spec §4's exactness
guard is about. One earlier row (dated before every flow, after nothing) makes every USD
figure computable; 1 / 31.9 does not terminate, so the 2330 buy converts to a 28-digit amount
and the partial sums of ``net_invested`` round.

``DEMO_NOW`` — why 2026-08-11: ``seed_demo``'s price paths are lists aligned to the first of
each month from January 2026 plus "today" (``_SPLIT_CLOSES`` and ``_SPIN_PARENT_CLOSES`` hold
nine points = January..August + today; ``_XCHG_TO_CLOSES`` / ``_SPIN_CHILD_CLOSES`` three
points = July 1, August 1, today), i.e. the seed was written for a "today" in August 2026 —
it landed on 2026-08-11 (commit cca111e, the W9 demo corpus). That day gives every path its
designed last point and seven complete month ends (January..July). The seed reads its clock
from the module globals ``_NOW`` / ``_TODAY``, which are set before ``seed`` runs.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import NamedTuple
from zoneinfo import ZoneInfo

import portfolio_dash
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import (
    insert_cash_movement,
    insert_corporate_action,
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    list_dividends,
    list_transactions,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, _seed_golden, init_golden_base

TAIPEI = ZoneInfo("Asia/Taipei")

#: The directory name, under pytest's session base temp, that holds the corpus templates —
#: shared by ``tests/reference/conftest.py`` and ``tests/settlement/conftest.py``, so one
#: session seeds each corpus once.
TEMPLATE_DIR_NAME = "pd-corpus-templates"

#: The demo corpus's fixed clock (see the module docstring for why this day).
DEMO_NOW = datetime(2026, 8, 11, 14, 30, tzinfo=TAIPEI)

#: ``unquoted_ccy_cash``: the MYR/TWD rate stored ON OR BEFORE the rebate, so XIRR (which
#: reads every stored pair) finds it, while v0.1.31's trend (which loaded only the ledger's
#: quote currencies, ``dashboard.py`` step 9) did not — PERF-F2, fixed after v0.1.32: the
#: trend now loads the rebate's currency too (``tests/reference/perf_f2.py``).
#: ``usd_reporting``: the one USD/TWD row added to golden (see the module docstring).
USD_EXTRA_RATE_DATE = date(2026, 1, 2)
USD_EXTRA_RATE = Decimal("31.9")

UNQUOTED_FX_DATE = date(2026, 3, 2)
UNQUOTED_FX_RATE = Decimal("7.05")
UNQUOTED_MOVE_DATE = date(2026, 3, 16)
UNQUOTED_MOVE_ACCOUNT = "moomoo_my"
UNQUOTED_MOVE_KIND = "REBATE"
UNQUOTED_MOVE_AMOUNT = Decimal("50")

#: The ledger / price tables a corpus fingerprint covers, with the columns they had at
#: 473fe5a. A column added later is deliberately NOT hashed, so a schema change alone never
#: reads as "the corpus changed".
FINGERPRINT_COLUMNS: dict[str, tuple[str, ...]] = {
    "accounts": ("account_id", "name", "broker", "settlement_ccy", "funding_ccy",
                 "fee_rule_set", "dividend_model"),
    "account_market_rules": ("account_id", "market", "fee_rule_set", "dividend_model"),
    "instruments": ("symbol", "market", "quote_ccy", "sector", "name", "board", "target_low",
                    "target_high", "is_etf", "etf_flag_unknown", "archived", "industry",
                    "aliases", "asset_type"),
    "transactions": ("id", "account_id", "symbol", "side", "quantity", "price", "fees", "tax",
                     "trade_date", "fee_rule_snapshot", "note", "daytrade", "short_sale",
                     "close_reason", "import_batch_id", "source_row_hash"),
    "dividends": ("id", "account_id", "symbol", "date", "type", "gross", "withholding", "net",
                  "reinvest_shares", "reinvest_price", "ex_date", "import_batch_id",
                  "source_row_hash"),
    "fx_conversions": ("id", "account_id", "date", "from_ccy", "from_amount", "to_ccy",
                       "to_amount", "import_batch_id", "source_row_hash"),
    "opening_inventory": ("account_id", "symbol", "shares", "original_cost_total",
                          "build_date"),
    "cash_movements": ("id", "account_id", "date", "kind", "ccy", "amount", "note",
                       "acq_home_amount", "corporate_action_id", "rebate_period",
                       "import_batch_id", "source_row_hash"),
    "corporate_actions": ("id", "account_id", "date", "kind", "from_symbol", "to_symbol",
                          "ratio_to", "ratio_from", "cost_carry", "note", "band_move_json",
                          "weight_move_json", "child_seed_json", "import_batch_id",
                          "source_row_hash"),
    "prices": ("instrument", "market", "as_of_date", "close", "open", "high", "low", "volume",
               "source", "fetched_at", "close_raw", "split_basis", "open_raw", "high_raw",
               "low_raw"),
    "fx_rates": ("base", "quote", "as_of_date", "rate", "source", "fetched_at"),
}


@dataclass(frozen=True)
class Corpus:
    """One corpus: its seed, its fixed clock and its reporting currency."""

    name: str
    now: datetime
    reporting: Currency
    seed: Callable[[sqlite3.Connection], None]
    summary: str


class CorpusDB(NamedTuple):
    """A private copy of a corpus template: ``(path, now, reporting)``."""

    path: Path
    now: datetime
    reporting: Currency


# ---------------------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------------------


def _load_seed_demo() -> ModuleType:
    """``scripts/seed_demo.py`` of the checkout ``portfolio_dash`` was imported from."""
    path = Path(portfolio_dash.__file__).resolve().parents[1] / "scripts" / "seed_demo.py"
    spec = importlib.util.spec_from_file_location("_reference_seed_demo", path)
    if spec is None or spec.loader is None:  # pragma: no cover - the file is in the repo
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_golden(conn: sqlite3.Connection) -> None:
    init_golden_base(conn)
    _seed_golden(conn)


def seed_demo(conn: sqlite3.Connection) -> None:
    init_golden_base(conn)
    module = _load_seed_demo()
    # The seed reads its clock from these two globals at call time (``_seed_history``,
    # ``_seed_base``, ``_seed_ca_prices`` …); pinning them is what makes the corpus fixed.
    vars(module).update(_NOW=DEMO_NOW, _TODAY=DEMO_NOW.date())
    with contextlib.redirect_stdout(io.StringIO()):  # the seed narrates; keep it quiet
        module.seed(conn)
    conn.commit()


def double_before(conn: sqlite3.Connection, now_day: date) -> tuple[int, int]:
    """Repeat every transaction and dividend dated before *now_day* at +1 day.

    The copies are written through the ordinary store inserts with every business field
    kept (fees, tax, snapshot, note, flags, DRIP terms, ex-date — an ex-date moves with its
    row) and NO provenance (``import_batch_id`` stays NULL). They are inserted after every
    original, in ``(date, id)`` order, so on a shared day a copy replays after the originals
    (same-day trades replay in id order, DEF-012).

    A copy that would land ON or AFTER *now_day* is refused rather than moved or dropped:
    the corpus is fixed, and a silent rule here would be a second definition of it. The demo
    seed at ``DEMO_NOW`` has none (its latest copied row is 2026-05-08 → 2026-05-09).
    Returns ``(transactions copied, dividends copied)``.
    """
    txs = [t for t in list_transactions(conn) if t.trade_date < now_day]
    divs = [d for d in list_dividends(conn) if d.date < now_day]
    one = timedelta(days=1)
    late = ([f"transaction {t.id} {t.trade_date}" for t in txs if t.trade_date + one >= now_day]
            + [f"dividend {d.id} {d.date}" for d in divs if d.date + one >= now_day])
    if late:
        raise ValueError(f"a +1-day copy would land on or after {now_day}: {late}")
    for t in txs:
        insert_transaction(
            conn, account_id=t.account_id, symbol=t.symbol, side=t.side,
            quantity=t.quantity, price=t.price, fees=t.fees, tax=t.tax,
            trade_date=t.trade_date + one, fee_rule_snapshot=t.fee_rule_snapshot or None,
            note=t.note, daytrade=t.daytrade, short_sale=t.short_sale,
            close_reason=t.close_reason, commit=False,
        )
    for d in divs:
        insert_dividend(
            conn, account_id=d.account_id, symbol=d.symbol, div_date=d.date + one,
            div_type=d.type, gross=d.gross, withholding=d.withholding, net=d.net,
            reinvest_shares=d.reinvest_shares, reinvest_price=d.reinvest_price,
            ex_date=None if d.ex_date is None else d.ex_date + one, commit=False,
        )
    conn.commit()
    return len(txs), len(divs)


def seed_doubled_demo(conn: sqlite3.Connection) -> None:
    seed_demo(conn)
    double_before(conn, DEMO_NOW.date())


def seed_usd_reporting(conn: sqlite3.Connection) -> None:
    seed_golden(conn)
    upsert_fx(conn, [FxRow(base=Currency.USD, quote=Currency.TWD, as_of=USD_EXTRA_RATE_DATE,
                           rate=USD_EXTRA_RATE, source="test")], fetched_at=GOLDEN_NOW)
    conn.commit()


def seed_unquoted_ccy_cash(conn: sqlite3.Connection) -> None:
    seed_golden(conn)
    upsert_fx(conn, [FxRow(base=Currency.MYR, quote=Currency.TWD, as_of=UNQUOTED_FX_DATE,
                           rate=UNQUOTED_FX_RATE, source="test")], fetched_at=GOLDEN_NOW)
    insert_cash_movement(conn, account_id=UNQUOTED_MOVE_ACCOUNT, move_date=UNQUOTED_MOVE_DATE,
                         kind=UNQUOTED_MOVE_KIND, ccy=Currency.MYR, amount=UNQUOTED_MOVE_AMOUNT,
                         note="synthetic rebate (reference corpus)", commit=False)
    conn.commit()


# --- the rich corpora (wave-1 review of package A): the shapes the five above lack ----------

#: The clock of ``rich`` / ``rich_refusals``.
RICH_NOW = datetime(2026, 6, 10, 14, 30, tzinfo=TAIPEI)
#: The clock of ``epoch1990`` — a ledger whose first row is in 1988 (see the module docstring).
EPOCH_NOW = datetime(1990, 3, 15, 14, 30, tzinfo=TAIPEI)
#: A sold put adjusted 1 -> 2 contracts (strike x contracts conserved), and a bought put that
#: expires worthless. OCC symbols: underlying, YYMMDD expiry, P, strike x 1000.
RICH_PUT = "XYZ261218P00050000"
RICH_PUT_ADJ = "XYZ261218P00025000"
RICH_OLD_PUT = "XYZ260116P00045000"
_RICH_FETCH = datetime(2026, 6, 10, 12, 0)


def _weekdays(first: date, last: date, *, skip: tuple[date, date] | None = None) -> list[date]:
    out: list[date] = []
    day = first
    while day <= last:
        if day.weekday() < 5 and not (skip is not None and skip[0] <= day <= skip[1]):
            out.append(day)
        day += timedelta(days=1)
    return out


def _closes(conn: sqlite3.Connection, symbol: str, market: Market, days: list[date],
            value: Callable[[int, date], Decimal], *, fetched_at: datetime = _RICH_FETCH
            ) -> None:
    upsert_prices(conn, [PriceRow(instrument=symbol, market=market, as_of=d, close=value(n, d),
                                  source="test") for n, d in enumerate(days)],
                  fetched_at=fetched_at)


def _instrument(conn: sqlite3.Connection, symbol: str, market: Market, ccy: Currency, *,
                etf: bool = False, board: str = "") -> None:
    upsert_instrument(conn, Instrument(symbol=symbol, market=market, quote_ccy=ccy,
                                       sector="Tech", name=f"{symbol} Corp", board=board,
                                       is_etf=etf))


def _trade(conn: sqlite3.Connection, account: str, symbol: str, side: Side, qty: str,
           price: str, day: date, *, fees: str = "1", short: bool = False,
           close: str | None = None) -> None:
    insert_transaction(conn, account_id=account, symbol=symbol, side=side,
                       quantity=Decimal(qty), price=Decimal(price),
                       fees=Decimal(fees) if close is None else Decimal("0"),
                       tax=Decimal("0"), trade_date=day, short_sale=short,
                       close_reason=close,  # type: ignore[arg-type]
                       commit=False)


def seed_rich(conn: sqlite3.Connection) -> None:
    """Every risky shape a ledger can hold WITHOUT a refusal that withholds XIRR or B, so the
    references exercise XIRR, B, B − A and the benchmark over them (see the docstring)."""
    usd, twd, myr = Currency.USD, Currency.TWD, Currency.MYR
    init_golden_base(conn)
    seed_accounts(conn)
    for sym, mkt, ccy, board in (("2330", Market.TW, twd, "TWSE"), ("AAPL", Market.US, usd, ""),
                                 ("XYZ", Market.US, usd, ""), ("OLDCO", Market.US, usd, ""),
                                 ("NEWCO", Market.US, usd, ""), ("XYZS", Market.US, usd, ""),
                                 ("1155", Market.MY, myr, ".KL")):
        _instrument(conn, sym, mkt, ccy, board=board)
    _instrument(conn, "0056", Market.TW, twd, etf=True, board="TWSE")
    for contract in (RICH_PUT, RICH_PUT_ADJ, RICH_OLD_PUT):
        register_option_contract(conn, contract, commit=False)

    # Prices: weekdays from 2025-10-01 to the clock's day. AAPL has a five-day gap and splits
    # 2-for-1 on 2026-03-16 (stored as traded: halved from that day); OLDCO stops when it is
    # exchanged; XYZS starts at its spinoff; 1155 stops 2026-06-01 (stale at the clock).
    last = RICH_NOW.date()
    split_day = date(2026, 3, 16)
    every = _weekdays(date(2025, 10, 1), last)
    _closes(conn, "2330", Market.TW, every, lambda n, d: Decimal(500) + Decimal(n) / 2)
    _closes(conn, "AAPL", Market.US,
            _weekdays(date(2025, 10, 1), last, skip=(date(2026, 1, 19), date(2026, 1, 23))),
            lambda n, d: (Decimal("200.13") + Decimal(n) / 10) / (2 if d >= split_day else 1))
    _closes(conn, "XYZ", Market.US, every, lambda n, d: Decimal("51.2") + Decimal(n % 7) / 10)
    _closes(conn, "OLDCO", Market.US, _weekdays(date(2025, 10, 1), date(2026, 2, 27)),
            lambda n, d: Decimal("30.05"))
    _closes(conn, "NEWCO", Market.US, _weekdays(date(2026, 3, 2), last),
            lambda n, d: Decimal("15.10") + Decimal(n) / 100)
    _closes(conn, "XYZS", Market.US, _weekdays(date(2026, 4, 15), last),
            lambda n, d: Decimal("4.05") + Decimal(n % 5) / 100)
    _closes(conn, "1155", Market.MY, _weekdays(date(2025, 10, 1), date(2026, 6, 1)),
            lambda n, d: Decimal("9.985") + Decimal(n % 3) / 1000)
    _closes(conn, "0056", Market.TW, every, lambda n, d: Decimal("35.15"))
    for key, mkt, value in (("0050", Market.TW, lambda n, d: Decimal(150) + Decimal(n) / 3),
                            ("^GSPC", Market.US, lambda n, d: Decimal(5000) + Decimal(n)),
                            ("^KLSE", Market.MY, lambda n, d: Decimal(1600) + Decimal(n) / 7)):
        _closes(conn, key, mkt, every, value)
    # FX, with a gap from 2026-02-02 to 2026-02-20 on every pair; MYR/TWD derived through USD.
    fx_days = _weekdays(date(2025, 10, 1), last, skip=(date(2026, 2, 2), date(2026, 2, 20)))
    upsert_fx(conn, [FxRow(base=usd, quote=twd, as_of=d, source="test",
                           rate=Decimal("31.9") + Decimal(n % 11) / 100)
                     for n, d in enumerate(fx_days)], fetched_at=_RICH_FETCH)
    upsert_fx(conn, [FxRow(base=usd, quote=myr, as_of=d, source="test",
                           rate=Decimal("4.41") + Decimal(n % 5) / 1000)
                     for n, d in enumerate(fx_days)], fetched_at=_RICH_FETCH)
    upsert_fx(conn, [FxRow(base=myr, quote=twd, as_of=d, source="derived:USD",
                           rate=Decimal("7.2345") + Decimal(n % 4) / 10000)
                     for n, d in enumerate(fx_days)], fetched_at=_RICH_FETCH)

    # TW broker: an opening, a buy and a sell, a cash dividend, a 配股 counted from its
    # ex-date, an ETF, a rebate, and a buy and a dividend dated after the clock.
    upsert_opening(conn, account_id="tw_broker", symbol="2330", shares=Decimal("2000"),
                   original_cost_total=Decimal("900000"), build_date=date(2025, 10, 1),
                   commit=False)
    _trade(conn, "tw_broker", "2330", Side.BUY, "1000", "505", date(2025, 11, 3), fees="719")
    _trade(conn, "tw_broker", "2330", Side.SELL, "300", "560", date(2026, 2, 10), fees="239")
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2026, 1, 15),
                    div_type="CASH", gross=Decimal("13500"), withholding=Decimal("0"),
                    net=Decimal("13500"), commit=False)
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2026, 4, 20),
                    ex_date=date(2026, 3, 31), div_type="STOCK", gross=Decimal("0"),
                    withholding=Decimal("0"), net=Decimal("0"), reinvest_shares=Decimal("27"),
                    commit=False)
    _trade(conn, "tw_broker", "0056", Side.BUY, "3333", "35.15", date(2026, 1, 6), fees="166")
    _trade(conn, "tw_broker", "2330", Side.BUY, "1000", "580", date(2026, 6, 24), fees="826")
    insert_dividend(conn, account_id="tw_broker", symbol="0056", div_date=date(2026, 6, 25),
                    div_type="CASH", gross=Decimal("3333"), withholding=Decimal("0"),
                    net=Decimal("3333"), commit=False)

    # Schwab: two same-day buys, a SPLIT, a DRIP, an EXCHANGE, a declared short opened and
    # covered, a long put that expires, and a sold put adjusted 1 -> 2 contracts and then
    # partly bought back (one contract stays open at the clock).
    _trade(conn, "schwab", "AAPL", Side.BUY, "10", "200.50", date(2025, 11, 5))
    _trade(conn, "schwab", "AAPL", Side.BUY, "3", "201.11", date(2025, 11, 5))
    insert_corporate_action(conn, account_id="schwab", action_date=split_day,
                            kind=CorporateActionKind.SPLIT, from_symbol="AAPL",
                            to_symbol="AAPL", ratio_to=Decimal("2"), ratio_from=Decimal("1"),
                            commit=False)
    insert_dividend(conn, account_id="schwab", symbol="AAPL", div_date=date(2026, 5, 15),
                    div_type="DRIP", gross=Decimal("6.50"), withholding=Decimal("1.95"),
                    net=Decimal("4.55"), reinvest_shares=Decimal("0.0412"),
                    reinvest_price=Decimal("110.4"), commit=False)
    _trade(conn, "schwab", "OLDCO", Side.BUY, "100", "30.05", date(2025, 12, 1))
    insert_corporate_action(conn, account_id="schwab", action_date=date(2026, 3, 2),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="OLDCO",
                            to_symbol="NEWCO", ratio_to=Decimal("2"), ratio_from=Decimal("1"),
                            commit=False)
    _trade(conn, "schwab", "XYZ", Side.SELL, "20", "52", date(2026, 2, 23), short=True)
    _trade(conn, "schwab", "XYZ", Side.BUY, "20", "51", date(2026, 3, 20))
    _trade(conn, "schwab", RICH_OLD_PUT, Side.BUY, "100", "1.05", date(2025, 12, 10))
    _trade(conn, "schwab", RICH_OLD_PUT, Side.SELL, "100", "0", date(2026, 1, 16),
           close="expired")
    _trade(conn, "schwab", RICH_PUT, Side.SELL, "100", "2.00", date(2026, 1, 20), fees="0.65",
           short=True)
    insert_corporate_action(conn, account_id="schwab", action_date=date(2026, 4, 1),
                            kind=CorporateActionKind.OPTION_ADJUST, from_symbol=RICH_PUT,
                            to_symbol=RICH_PUT_ADJ, ratio_to=Decimal("2"),
                            ratio_from=Decimal("1"), commit=False)
    _trade(conn, "schwab", RICH_PUT_ADJ, Side.BUY, "100", "0.40", date(2026, 5, 11),
           fees="0.65")

    # Moomoo MY: a MY stock with a NET dividend (its price stale at the clock), and a US stock
    # that spins off a child.
    _trade(conn, "moomoo_my", "1155", Side.BUY, "300", "9.985", date(2025, 12, 2), fees="3.85")
    insert_dividend(conn, account_id="moomoo_my", symbol="1155", div_date=date(2026, 3, 9),
                    div_type="NET", gross=Decimal("45"), withholding=Decimal("0"),
                    net=Decimal("45"), commit=False)
    _trade(conn, "moomoo_my", "XYZ", Side.BUY, "100", "50.11", date(2025, 12, 3))
    insert_corporate_action(conn, account_id="moomoo_my", action_date=date(2026, 4, 15),
                            kind=CorporateActionKind.SPINOFF, from_symbol="XYZ",
                            to_symbol="XYZS", ratio_to=Decimal("1"), ratio_from=Decimal("2"),
                            cost_carry=Decimal("0.1"), commit=False)

    # Cash: deposits, a foreign deposit with its cost, two conversions, and the three XIRR
    # kinds — each in a currency an instrument is quoted in (PERF-F2 is unquoted_ccy_cash's).
    insert_cash_movement(conn, account_id="tw_broker", move_date=date(2025, 10, 1),
                         kind="DEPOSIT", ccy=twd, amount=Decimal("2000000"), commit=False)
    insert_cash_movement(conn, account_id="schwab", move_date=date(2025, 10, 2), kind="DEPOSIT",
                         ccy=usd, amount=Decimal("5000"), acq_home_amount=Decimal("159500"),
                         commit=False)
    insert_fx_conversion(conn, account_id="schwab", date=date(2025, 11, 4), from_ccy=twd,
                         from_amount=Decimal("64000"), to_ccy=usd, to_amount=Decimal("2000"),
                         commit=False)
    insert_cash_movement(conn, account_id="moomoo_my", move_date=date(2025, 11, 20),
                         kind="DEPOSIT", ccy=myr, amount=Decimal("30000"), commit=False)
    insert_fx_conversion(conn, account_id="moomoo_my", date=date(2025, 11, 21), from_ccy=myr,
                         from_amount=Decimal("22050"), to_ccy=usd, to_amount=Decimal("5000"),
                         commit=False)
    insert_cash_movement(conn, account_id="tw_broker", move_date=date(2026, 3, 10),
                         kind="REBATE", ccy=twd, amount=Decimal("553"), rebate_period="2026-02",
                         commit=False)
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 4, 2),
                         kind="BROKER_FEE", ccy=usd, amount=Decimal("2.37"), commit=False)
    insert_cash_movement(conn, account_id="moomoo_my", move_date=date(2026, 5, 4),
                         kind="INTEREST_EXPENSE", ccy=usd, amount=Decimal("0.83"),
                         commit=False)
    conn.commit()


def seed_rich_refusals(conn: sqlite3.Connection) -> None:
    """``rich`` plus every refusal the replay records: XIRR and B are withheld here."""
    seed_rich(conn)
    _instrument(conn, "0050", Market.TW, Currency.TWD, etf=True, board="TWSE")
    _instrument(conn, "NOPX", Market.US, Currency.USD)
    _instrument(conn, "GHOST", Market.US, Currency.USD)
    # 賣超: a sell in an account that never held the symbol (acked, so it is stored).
    _trade(conn, "moomoo_my", "AAPL", Side.SELL, "5", "205", date(2026, 1, 12))
    # A dividend with no position (an orphan), and one paid on the open declared short.
    insert_dividend(conn, account_id="tw_broker", symbol="0050", div_date=date(2026, 2, 26),
                    div_type="CASH", gross=Decimal("100"), withholding=Decimal("0"),
                    net=Decimal("100"), commit=False)
    insert_dividend(conn, account_id="schwab", symbol="XYZ", div_date=date(2026, 3, 3),
                    div_type="CASH", gross=Decimal("4"), withholding=Decimal("1.2"),
                    net=Decimal("2.8"), commit=False)
    # A close row with no position to close.
    _trade(conn, "moomoo_my", RICH_OLD_PUT, Side.BUY, "100", "0", date(2026, 1, 16),
           close="expired")
    # A readable action the replay refuses (no position), and an unreadable stored row.
    insert_corporate_action(conn, account_id="schwab", action_date=date(2026, 4, 10),
                            kind=CorporateActionKind.SPLIT, from_symbol="GHOST",
                            to_symbol="GHOST", ratio_to=Decimal("2"), ratio_from=Decimal("1"),
                            commit=False)
    bad = insert_corporate_action(conn, account_id="schwab", action_date=date(2026, 4, 8),
                                  kind=CorporateActionKind.SPLIT, from_symbol="XYZ",
                                  to_symbol="XYZ", ratio_to=Decimal("3"),
                                  ratio_from=Decimal("1"), commit=False)
    conn.execute("UPDATE corporate_actions SET ratio_to = '0' WHERE id = ?", (bad,))
    # A holding no price was ever stored for.
    _trade(conn, "schwab", "NOPX", Side.BUY, "7", "12.34", date(2026, 1, 8))
    conn.commit()


def seed_epoch1990(conn: sqlite3.Connection) -> None:
    """An opening built in 1988 under an index history stored from 1987 and ONE 1987 USD/TWD
    rate: v0.1.31 reads benchmark closes from 1990-01-01 only, so the 1988 flow is uncovered
    while the 1990 buy is placed (the review's reproduction of the lost floor)."""
    init_golden_base(conn)
    seed_accounts(conn)
    fetched = datetime(1990, 3, 14, 12, 0)
    _instrument(conn, "AAPL", Market.US, Currency.USD)
    upsert_opening(conn, account_id="schwab", symbol="AAPL", shares=Decimal("40"),
                   original_cost_total=Decimal("1400"), build_date=date(1988, 3, 1),
                   commit=False)
    _trade(conn, "schwab", "AAPL", Side.BUY, "5", "40", date(1990, 2, 5), fees="0")
    days = _weekdays(date(1987, 1, 2), EPOCH_NOW.date() - timedelta(days=1))
    _closes(conn, "AAPL", Market.US, days, lambda n, d: Decimal(35) + Decimal(n % 13),
            fetched_at=fetched)
    _closes(conn, "^GSPC", Market.US, days, lambda n, d: Decimal(250) + Decimal(n) / 10,
            fetched_at=fetched)
    upsert_fx(conn, [FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(1987, 12, 31),
                           rate=Decimal("28.5"), source="test")], fetched_at=fetched)
    conn.commit()


CORPORA: dict[str, Corpus] = {
    c.name: c
    for c in (
        Corpus("golden", GOLDEN_NOW, Currency.TWD, seed_golden,
               "tests/conftest.py::_seed_golden over init_golden_base"),
        Corpus("demo", DEMO_NOW, Currency.TWD, seed_demo,
               "scripts/seed_demo.py::seed over init_golden_base, clock pinned"),
        Corpus("doubled_demo", DEMO_NOW, Currency.TWD, seed_doubled_demo,
               "demo + every transaction and dividend before now repeated at +1 day"),
        Corpus("usd_reporting", GOLDEN_NOW, Currency.USD, seed_usd_reporting,
               "golden + one earlier USD/TWD rate, reporting currency USD"),
        Corpus("unquoted_ccy_cash", GOLDEN_NOW, Currency.TWD, seed_unquoted_ccy_cash,
               "golden + MYR/TWD rate + a MYR REBATE (PERF-F2)"),
        Corpus("rich", RICH_NOW, Currency.TWD, seed_rich,
               "short sale, options (sell-to-open, adjust, buy-to-close, expiry), DRIP, 配股, "
               "SPLIT + EXCHANGE + SPINOFF, future rows, an FX gap, a stale price"),
        Corpus("rich_refusals", RICH_NOW, Currency.USD, seed_rich_refusals,
               "rich + 賣超, orphan / short dividends, a refused close, refused and "
               "unreadable actions, an unpriced holding; reporting USD"),
        Corpus("epoch1990", EPOCH_NOW, Currency.TWD, seed_epoch1990,
               "an opening built 1988, index closes from 1987, one 1987 rate"),
    )
}
CORPUS_NAMES: tuple[str, ...] = tuple(CORPORA)


# ---------------------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------------------


def open_db(path: Path) -> sqlite3.Connection:
    """Open a corpus file the way ``shared/db.get_connection`` opens the configured one."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def build_template(name: str, path: Path) -> Path:
    """Seed corpus *name* into a NEW file at *path* (written aside, then moved into place)."""
    corpus = CORPORA[name]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".building-{os.getpid()}")
    for stale in _db_files(tmp):
        stale.unlink()
    conn = open_db(tmp)
    try:
        corpus.seed(conn)
        conn.commit()
    finally:
        conn.close()  # the last close checkpoints the WAL into the main file
    os.replace(tmp, path)
    for side in _db_files(tmp):  # pragma: no cover - only if the checkpoint left one
        side.unlink()
    return path


def ensure_template(name: str, base_dir: Path) -> Path:
    """The template of corpus *name* under *base_dir*, built on first use."""
    path = base_dir / f"{name}.db"
    if not path.exists():
        build_template(name, path)
    return path


def copy_db(src: Path, dest: Path) -> Path:
    """Copy a closed database file (and any WAL side files) to *dest*."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    for suffix in ("-wal", "-shm"):
        side = src.with_name(src.name + suffix)
        if side.exists():  # pragma: no cover - templates are closed, so normally absent
            shutil.copyfile(side, dest.with_name(dest.name + suffix))
    return dest


def copy_corpus(name: str, base_dir: Path, dest: Path) -> CorpusDB:
    """A private copy of corpus *name*'s template at *dest*: ``(path, now, reporting)``."""
    corpus = CORPORA[name]
    return CorpusDB(copy_db(ensure_template(name, base_dir), dest), corpus.now, corpus.reporting)


def _db_files(path: Path) -> list[Path]:
    return [p for p in (path, path.with_name(path.name + "-wal"),
                        path.with_name(path.name + "-shm")) if p.exists()]


def fingerprint(conn: sqlite3.Connection) -> str:
    """sha256 over the corpus's ledger, price and FX rows (``FINGERPRINT_COLUMNS``).

    It tells "the corpus changed" (a seed edited) apart from "the engine changed" before any
    payload is compared. Rows are read in rowid order, values as stored (TEXT / INTEGER /
    NULL), so two seeds that store the same rows in the same order agree.
    """
    digest = hashlib.sha256()
    for table, columns in FINGERPRINT_COLUMNS.items():
        present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        cols = [c for c in columns if c in present]
        digest.update(f"\x1e{table}\x1f{','.join(cols)}".encode())
        for row in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY rowid"):
            digest.update(("\x1d" + "\x1f".join(
                "\x00" if v is None else f"{type(v).__name__}:{v}" for v in row)).encode())
    return digest.hexdigest()


def first_ledger_date(conn: sqlite3.Connection) -> date | None:
    """The earliest date any ledger row carries (trades, dividends incl. ex-dates, openings,
    cash movements, FX conversions, corporate actions)."""
    rows = conn.execute(
        "SELECT MIN(d) FROM ("
        " SELECT trade_date AS d FROM transactions"
        " UNION ALL SELECT date FROM dividends"
        " UNION ALL SELECT ex_date FROM dividends WHERE ex_date IS NOT NULL"
        " UNION ALL SELECT build_date FROM opening_inventory"
        " UNION ALL SELECT date FROM cash_movements"
        " UNION ALL SELECT date FROM fx_conversions"
        " UNION ALL SELECT date FROM corporate_actions)"
    ).fetchone()
    return None if rows is None or rows[0] is None else date.fromisoformat(rows[0])


__all__ = [
    "CORPORA",
    "CORPUS_NAMES",
    "DEMO_NOW",
    "EPOCH_NOW",
    "RICH_NOW",
    "TAIPEI",
    "TEMPLATE_DIR_NAME",
    "Corpus",
    "CorpusDB",
    "build_template",
    "copy_corpus",
    "copy_db",
    "double_before",
    "ensure_template",
    "fingerprint",
    "first_ledger_date",
    "open_db",
]
