"""The five synthetic corpora every settlement package is checked against (plan W0.3).

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
=================  =========================================================================

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
from portfolio_dash.data_ingestion.store import (
    insert_cash_movement,
    insert_dividend,
    insert_transaction,
    list_dividends,
    list_transactions,
)
from portfolio_dash.pricing.results import FxRow
from portfolio_dash.pricing.store import upsert_fx
from portfolio_dash.shared.enums import Currency
from tests.conftest import GOLDEN_NOW, _seed_golden, init_golden_base

TAIPEI = ZoneInfo("Asia/Taipei")

#: The directory name, under pytest's session base temp, that holds the corpus templates —
#: shared by ``tests/reference/conftest.py`` and ``tests/settlement/conftest.py``, so one
#: session seeds each corpus once.
TEMPLATE_DIR_NAME = "pd-corpus-templates"

#: The demo corpus's fixed clock (see the module docstring for why this day).
DEMO_NOW = datetime(2026, 8, 11, 14, 30, tzinfo=TAIPEI)

#: ``unquoted_ccy_cash``: the MYR/TWD rate stored ON OR BEFORE the rebate, so XIRR (which
#: reads every stored pair) finds it, while the trend (which loads only the ledger's quote
#: currencies, ``dashboard.py`` step 9) does not.
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
