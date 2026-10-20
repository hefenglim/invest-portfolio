"""The settlement store: settled days, month / year closes and the run reports (PERF).

Spec ``docs/spec/2026-10-18-settlement-store.md`` §6, §7, §9.4, §9.6, §10 steps 2–3. Every table
here is DERIVED: the ledgers stay the only source of truth, so dropping the store loses nothing
and a run rebuilds it. This module owns:

- the DDL (§6) and the documented reset (:func:`drop_store`);
- the trigger set (§7): :func:`expected_triggers`, :func:`install_or_verify`,
  :func:`triggers_verified`, :func:`record_schema_version`;
- the change trail and the run's claim on it: :func:`claim`, :func:`put_back`,
  :func:`settle_claimed`, :func:`pending_changes`, :func:`run_changes`;
- the run-report store (§9.6): :func:`create_run` … :func:`restatements_for`;
- the generations and the swap (§6 "Generations", §9.4): :func:`next_generation`,
  :func:`write_days` …, :func:`copy_before`, :func:`swap`, :func:`end_without_publish`,
  :func:`cleanup_orphans`, :func:`prune`;
- the read helpers of §10: :func:`store_state`, :func:`live_from`, :func:`stale_range`,
  :func:`read_days` ….

Created at boot only (``api/app.py`` lifespan, after every borrowed table exists) — never by a
GET (DEF-065). The trigger set reads the borrowed ledgers (``transactions``, ``dividends``,
``opening_inventory``, ``cash_movements``, ``fx_conversions``, ``corporate_actions``, ``prices``,
``fx_rates``, ``instruments``, ``accounts``, ``account_market_rules``) by direct SQL, the
cross-layer convention of ``architecture.md``; :func:`first_ledger_day` reads the first three of
them the same way.

**Never drop a settle table while its triggers exist**: every ledger write would then fail with
"no such table". :func:`drop_store` drops the triggers first.

``settle_meta.run_id`` is the ACTIVE run's id (§9.4: the claim records it, the swap and every
end clear it, boot sees a non-NULL one); the DDL comment beside ``built_at`` that reads "the run
that produced this generation" describes ``built_at``. The run that produced a generation is the
``settle_run`` row whose ``generation_after`` it is.

Limits of the trigger set (the weekly self-check, spec §9.1 ``settlement_selfcheck``, is the
backstop for each — it recomputes in full and names the first day that differs):

- A ``REPLACE`` that names the hidden ``rowid`` explicitly can remove one key's row to insert
  ANOTHER key's (``instruments``, ``opening_inventory``, ``prices``). No DELETE trigger fires for
  the removed row, and :data:`REPLACE_TABLES`' premise — the removed row has the new row's key,
  so its day is the new row's — does not hold for it. The application never writes ``rowid``;
  only a hand-written script could.

Transactions: every function that writes does so in ONE short transaction of its own
(``BEGIN IMMEDIATE`` … ``COMMIT``); called while the connection already holds an open
transaction, it nests as a SAVEPOINT and the caller commits. The chunked writers
(:func:`write_days`, :func:`write_periods`) commit once per calendar month.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import portfolio_dash
from portfolio_dash.portfolio.cash import UNBOOKED_DIVIDEND_NOTE
from portfolio_dash.portfolio.returns import DividendRefusals
from portfolio_dash.portfolio.settlement_models import (
    ACTIVE_RUN_STATUSES,
    CHANGE_TABLE_ZH,
    ChangeCounter,
    ChangeRecord,
    ChangeTableCount,
    DayValue,
    DiffRow,
    HistoryResult,
    PeriodActivity,
    PeriodCash,
    PeriodHolding,
    PeriodKpi,
    ReceivableDividend,
    Restatement,
    RunReport,
    RunRequest,
    RunScope,
    RunStatus,
    RunSummary,
    RunVersions,
    is_run_reason,
    run_status_text,
)
from portfolio_dash.pricing.benchmarks import BENCHMARKS
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import Dividend, LedgerBundle, counts_by
from portfolio_dash.shared.money import from_db, to_db

#: Bumped whenever a settle table's shape or meaning changes; part of :func:`calc_version`, so
#: a release that changes it re-settles in full at boot.
STORE_SCHEMA_VERSION = 1

#: Every trigger the store installs is named with this prefix (§7), so :func:`drop_store` and
#: the integrity check can find exactly the store's own.
TRIGGER_PREFIX = "settle_trg_"

SETTLE_TABLES: tuple[str, ...] = (
    "settle_meta",
    "settle_dirty",
    "settle_change_log",
    "settle_change_agg",
    "settle_run",
    "settle_run_diff",
    "settled_day",
    "settled_period",
    "settled_period_holding",
    "settle_restatement",
    "settled_period_cash",
)

#: Spec §6, verbatim but for comment layout (the comments are the column documentation).
_DDL = """
CREATE TABLE IF NOT EXISTS settle_meta (  -- exactly one row
    id INTEGER PRIMARY KEY CHECK (id = 1),
    generation INTEGER NOT NULL DEFAULT 0,  -- 0 = never settled
    settled_through TEXT,  -- ISO date; NULL when generation = 0
    first_day TEXT,  -- the series start of this generation
    calc_version TEXT,  -- PERF-S5
    reporting_ccy TEXT,
    bench_state TEXT,  -- JSON: counterfactual state through settled_through
    built_at TEXT, run_id INTEGER,  -- the run that produced this generation
    run_from TEXT, run_through TEXT,  -- the ACTIVE run's range; NULL when idle (§9.4)
    run_dirty_from TEXT,  -- the dirty_from the ACTIVE run claimed (readers' Dd); NULL = none
    held_through TEXT,  -- last day this generation holds rows for (§9.4 partial)
    trigger_schema_version INTEGER  -- PRAGMA schema_version when the trigger set was verified (§7)
);
CREATE TABLE IF NOT EXISTS settle_dirty (  -- exactly one row; triggers raise it, a run claims it
    id INTEGER PRIMARY KEY CHECK (id = 1),
    dirty_from TEXT,  -- earliest affected day; NULL = clean
    seq INTEGER NOT NULL DEFAULT 0  -- +1 on every recorded change
);
CREATE TABLE IF NOT EXISTS settle_change_log (  -- one row per changed ledger / reference record
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,  -- settle_dirty.seq this change produced
    table_name TEXT NOT NULL,  -- not prices / fx_rates (settle_change_agg)
    op TEXT NOT NULL CHECK (op IN ('insert', 'update', 'delete')),
    row_key TEXT NOT NULL,  -- id; account|symbol for openings; symbol; account_id
    old_account_id TEXT, new_account_id TEXT,  -- kept apart, rendered as {account:<id>} tokens
    -- for a corporate action: from_symbol (old/new), with to_symbol in detail
    old_symbol TEXT, new_symbol TEXT,
    detail TEXT,  -- JSON: e.g. to_symbol, kind, for the panel line
    old_day TEXT, new_day TEXT,  -- the affected day before / after (§7)
    import_batch_id TEXT,  -- so an import or a batch undo groups as one line
    actor TEXT,  -- 'user:<name>' | 'job:<id>' | 'system:boot' | NULL = an external writer (§7)
    written_at TEXT NOT NULL,  -- the statement's time (UTC), shown as 寫入時間
    claimed_run_id INTEGER, settled_run_id INTEGER,
    attempted_run_ids TEXT NOT NULL DEFAULT ''  -- runs that claimed it and did not publish it
);
-- prices and fx_rates: one counter per symbol / pair
CREATE TABLE IF NOT EXISTS settle_change_agg (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL, key TEXT NOT NULL,  -- symbol, or 'USD/TWD'
    changes INTEGER NOT NULL, earliest TEXT NOT NULL, latest TEXT NOT NULL,
    claimed_run_id INTEGER, settled_run_id INTEGER, attempted_run_ids TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS settle_change_agg_pending  -- the trigger's upsert target
    ON settle_change_agg (table_name, key) WHERE claimed_run_id IS NULL AND settled_run_id IS NULL;
CREATE TABLE IF NOT EXISTS settle_run (  -- §9.6: one report per run, kept after it ends
    run_id INTEGER PRIMARY KEY,  -- = its job_runs id
    status TEXT NOT NULL CHECK (status IN ('preparing', 'running', 'published', 'partial',
                                           'superseded', 'cancelled', 'failed', 'interrupted')),
    resumed INTEGER NOT NULL DEFAULT 0,  -- interrupted AND a resume actually started
    reason TEXT NOT NULL,  -- prompt|button|boot|job:<id>|nightly|chained|resume|selfcheck
    started_by TEXT,  -- 'user:<name>' for prompt / button; else NULL
    cancelled_by TEXT,  -- 'user:<name>' when cancelled
    trigger_text TEXT NOT NULL,  -- rendered per viewer ('you' only for the viewer who started it)
    started_at TEXT NOT NULL, finished_at TEXT,
    range_from TEXT, range_to TEXT, days INTEGER,
    generation_before INTEGER, generation_after INTEGER,
    settled_through_before TEXT, settled_through_after TEXT,
    calc_version_before TEXT, calc_version_after TEXT,
    reporting_ccy_before TEXT, reporting_ccy_after TEXT,
    eta_initial_s REAL, duration_s REAL,
    previous_run_id INTEGER, next_run_id INTEGER,  -- the chain (resume / chained follow-up)
    report TEXT NOT NULL  -- JSON, §9.6 — Decimal values as canonical strings
);
-- §9.6: every changed day of a run, kept with its report
CREATE TABLE IF NOT EXISTS settle_run_diff (
    run_id INTEGER NOT NULL, day TEXT NOT NULL,
    field TEXT NOT NULL,  -- total_value | net_invested | net_worth | incomplete
    kind TEXT NOT NULL CHECK (kind IN ('changed', 'value_to_none', 'none_to_value',
                                       'flag_flip', 'new_day', 'dropped_day')),
    before TEXT, after TEXT,  -- canonical Decimal strings (NULL where none)
    flags_before TEXT, flags_after TEXT,  -- incomplete / cash_incomplete / options_open
    PRIMARY KEY (run_id, day, field)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS settled_day (
    generation INTEGER NOT NULL, day TEXT NOT NULL,
    total_value TEXT NOT NULL, net_invested TEXT NOT NULL,
    net_worth TEXT, cash_total TEXT, cash_incomplete INTEGER NOT NULL,
    incomplete INTEGER NOT NULL, options_open INTEGER NOT NULL,
    PRIMARY KEY (generation, day)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS settled_period (
    generation INTEGER NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('M','Y')),
    period TEXT NOT NULL,  -- 'YYYY-MM' or 'YYYY'
    end_day TEXT NOT NULL,  -- the period's last calendar day (PERF-S7)
    kpi TEXT NOT NULL,  -- JSON: §11.2 KPI block
    activity TEXT NOT NULL,  -- JSON: §11.3 period activity
    PRIMARY KEY (generation, kind, period)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS settled_period_holding (
    generation INTEGER NOT NULL, kind TEXT NOT NULL, period TEXT NOT NULL,
    account_id TEXT NOT NULL, symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL, quote_ccy TEXT NOT NULL,
    shares TEXT NOT NULL, original_cost_total TEXT NOT NULL, adjusted_cost_total TEXT NOT NULL,
    price TEXT, price_as_of TEXT, market_value TEXT, unrealized TEXT,
    flags TEXT NOT NULL DEFAULT '{}',  -- oversold / unbookable_* / short_open
    PRIMARY KEY (generation, kind, period, account_id, symbol)
) WITHOUT ROWID;
-- §14.6: a closed period that moved, and why — never pruned
CREATE TABLE IF NOT EXISTS settle_restatement (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, period TEXT NOT NULL,
    field TEXT NOT NULL, label TEXT NOT NULL,  -- e.g. total_market_value and its zh label
    unit TEXT NOT NULL,  -- 'TWD' | 'USD' | 'MYR' (reporting or native) | 'ratio'
    old_value TEXT, new_value TEXT,
    notes_before TEXT, notes_after TEXT,  -- the close's notes, so a jump explains itself
    run_id INTEGER NOT NULL, reason TEXT NOT NULL, source TEXT, at TEXT NOT NULL,
    cutover INTEGER NOT NULL DEFAULT 0  -- 1 = the one-time restatement at go-live (§11.4)
);
CREATE TABLE IF NOT EXISTS settled_period_cash (  -- §11.3: cash ties to the broker statement
    generation INTEGER NOT NULL, kind TEXT NOT NULL, period TEXT NOT NULL,
    account_id TEXT NOT NULL, ccy TEXT NOT NULL,
    balance TEXT NOT NULL, reporting_value TEXT, fx_rate TEXT, fx_as_of TEXT,
    PRIMARY KEY (generation, kind, period, account_id, ccy)
) WITHOUT ROWID;
"""

#: Lookup indexes on the trail (W1-B; not in the spec's DDL, which names no index but the
#: pending counter's). The import-batch annotation trigger finds a PENDING log row by its table
#: and key on every stamped import row; the run's queries find its rows by claim and settle.
_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS settle_change_log_pending ON settle_change_log (table_name, row_key)
    WHERE claimed_run_id IS NULL AND settled_run_id IS NULL;
CREATE INDEX IF NOT EXISTS settle_change_log_claimed ON settle_change_log (claimed_run_id);
CREATE INDEX IF NOT EXISTS settle_change_log_settled ON settle_change_log (settled_run_id);
CREATE INDEX IF NOT EXISTS settle_change_agg_claimed ON settle_change_agg (claimed_run_id);
CREATE INDEX IF NOT EXISTS settle_change_agg_settled ON settle_change_agg (settled_run_id);
CREATE INDEX IF NOT EXISTS settle_restatement_period ON settle_restatement (kind, period);
"""


def _statements(script: str) -> list[str]:
    """Split the DDL into statements; a ``;`` inside a ``--`` comment does not end one."""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise ValueError(f"incomplete settle DDL statement: {buffer.strip()[:60]}")
    return statements


def create_tables(conn: sqlite3.Connection) -> None:
    """Create every settle table and seed the two singleton rows; idempotent.

    A boot on an existing store changes nothing: ``CREATE … IF NOT EXISTS`` on an existing
    table takes no lock and moves no ``schema_version`` (``tests/contract/
    test_def065_get_never_writes.py``), and ``INSERT OR IGNORE`` keeps the existing rows.
    """
    for statement in _statements(_DDL) + _statements(_INDEX_DDL):
        conn.execute(statement)
    # a store created before the column existed (in-branch builds only — no released version
    # had the store): add it once, at boot, before the trigger set's schema_version is recorded
    if "run_dirty_from" not in {str(r[1]) for r in conn.execute(
            "PRAGMA table_info(settle_meta)")}:
        conn.execute("ALTER TABLE settle_meta ADD COLUMN run_dirty_from TEXT")
    conn.execute("INSERT OR IGNORE INTO settle_meta (id) VALUES (1)")
    conn.execute("INSERT OR IGNORE INTO settle_dirty (id) VALUES (1)")
    conn.commit()


def drop_store(conn: sqlite3.Connection) -> None:
    """The documented reset: drop the store's triggers FIRST, then its tables.

    The next boot recreates the tables and the triggers and settles in full; nothing is lost,
    because every row here is derived from the ledgers.
    """
    triggers = [
        str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND substr(name, 1, ?) = ?",
            (len(TRIGGER_PREFIX), TRIGGER_PREFIX),
        )
    ]
    for name in triggers:
        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    for table in reversed(SETTLE_TABLES):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


#: The packages whose code decides a settled figure (PERF-S5). A change to any file in them
#: changes the calc version, and a store built by other code is not served.
CALC_PACKAGES: tuple[str, ...] = ("portfolio", "forex", "pricing", "data_ingestion", "shared")


def code_digest(root: Path) -> str:
    """sha256 over the sorted relative paths and bytes of every ``.py`` in :data:`CALC_PACKAGES`
    under the package root *root*.

    Paths are POSIX-style and line endings are normalised to ``\\n``, so the same commit gives
    the same digest on every checkout (a Windows working copy stores CRLF).
    """
    digest = hashlib.sha256()
    files = sorted(
        path for package in CALC_PACKAGES for path in (root / package).rglob("*.py")
    )
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()


#: Computed ONCE, at import — never at swap time: a deploy while a run computes must not stamp
#: new-formula rows over old ones (spec §9.4).
CODE_DIGEST = code_digest(Path(portfolio_dash.__file__).resolve().parent)


def calc_version(reporting: Currency) -> str:
    """The version a settled row is valid for: code digest + reporting currency + store schema."""
    digest = hashlib.sha256(
        f"{CODE_DIGEST}|{reporting.value}|{STORE_SCHEMA_VERSION}".encode()
    )
    return digest.hexdigest()[:16]


# =============================================================================================
# Shared helpers
# =============================================================================================

_ONE_DAY = timedelta(days=1)


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    """One short write transaction (``BEGIN IMMEDIATE`` … ``COMMIT``, rolled back on error).

    Inside a transaction the caller already holds, it nests as a SAVEPOINT instead and the
    caller commits — a failure here then undoes only this block.
    """
    if conn.in_transaction:
        conn.execute("SAVEPOINT settle_store")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK TO settle_store")
            conn.execute("RELEASE settle_store")
            raise
        conn.execute("RELEASE settle_store")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _day(value: object) -> date | None:
    """A stored ISO day, or ``None`` for NULL and for an empty string (an external writer's
    ``''`` reads as "no date", as ``data_ingestion/store.py`` reads ``ex_date``)."""
    return None if value is None or value == "" else date.fromisoformat(str(value))


def _dec(value: object) -> Decimal | None:
    return None if value is None else from_db(str(value))


def _nmin(a: date | None, b: date | None) -> date | None:
    """The earlier of two optional days; ``None`` only when both are."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """The table's column names (``PRAGMA table_info``), or ``()`` when it does not exist."""
    return tuple(str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})"))


def _schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA schema_version").fetchone()[0])


def _attempts(text: str | None) -> list[int]:
    return [int(part) for part in (text or "").split(",") if part.strip()]


def _attempts_text(ids: Iterable[int]) -> str:
    return ",".join(str(i) for i in sorted(set(ids)))


#: The SQL twin of :attr:`shared.models.ledger.Dividend.effective_date` over the ``dividends``
#: columns of the row in scope (``tests/settlement/test_triggers.py`` asserts they agree for
#: every type): a 配股 with a known ex-date counts from it, everything else from the payment date.
#: An empty ``ex_date`` (an external writer's ``''``) is no ex-date, exactly as the Python reader
#: (``data_ingestion/store.py::_div_row``) treats it.
DIVIDEND_EFFECTIVE_DAY_SQL = (
    "CASE WHEN type = 'STOCK' AND NULLIF(ex_date, '') IS NOT NULL THEN ex_date ELSE date END"
)


def _dividend_day(row: str) -> str:
    return (f"CASE WHEN {row}.type = 'STOCK' AND NULLIF({row}.ex_date, '') IS NOT NULL "
            f"THEN {row}.ex_date ELSE {row}.date END")


#: The first day any ledger row counts on (trades, openings, dividends by effective date, cash
#: movements, FX conversions, corporate actions) — the start of a full re-settle (§7: accounts
#: and their market rules).
_FIRST_LEDGER_DAY_SQL = (
    "(SELECT MIN(d) FROM ("
    "SELECT MIN(trade_date) AS d FROM transactions"
    " UNION ALL SELECT MIN(build_date) FROM opening_inventory"
    f" UNION ALL SELECT MIN({DIVIDEND_EFFECTIVE_DAY_SQL}) FROM dividends"
    " UNION ALL SELECT MIN(date) FROM cash_movements"
    " UNION ALL SELECT MIN(date) FROM fx_conversions"
    " UNION ALL SELECT MIN(date) FROM corporate_actions))"
)


def _symbol_day(symbols: str) -> str:
    """The earliest ledger day of the symbol(s) *symbols* (an SQL ``IN`` list), corporate
    actions by ``from_symbol`` OR ``to_symbol`` (§7 ``instruments``); NULL when it has none."""
    return (
        "(SELECT MIN(d) FROM ("
        f"SELECT MIN(trade_date) AS d FROM transactions WHERE symbol IN ({symbols})"
        f" UNION ALL SELECT MIN(build_date) FROM opening_inventory WHERE symbol IN ({symbols})"
        f" UNION ALL SELECT MIN({DIVIDEND_EFFECTIVE_DAY_SQL}) FROM dividends"
        f" WHERE symbol IN ({symbols})"
        " UNION ALL SELECT MIN(date) FROM corporate_actions"
        f" WHERE from_symbol IN ({symbols}) OR to_symbol IN ({symbols})))"
    )


def first_ledger_day(conn: sqlite3.Connection) -> date | None:
    """The series start: the earliest trade, opening or dividend (by its effective date) of a
    REGISTERED symbol — what ``timeseries.daily_value_series`` starts its replay on after
    ``without_unregistered()``. ``None`` when the ledger has no such row.

    Reads the borrowed ``transactions``, ``dividends``, ``opening_inventory`` and
    ``instruments`` by direct SQL.
    """
    row = conn.execute(
        "SELECT MIN(d) FROM ("
        "SELECT MIN(trade_date) AS d FROM transactions"
        " WHERE symbol IN (SELECT symbol FROM instruments)"
        " UNION ALL SELECT MIN(build_date) FROM opening_inventory"
        " WHERE symbol IN (SELECT symbol FROM instruments)"
        f" UNION ALL SELECT MIN({DIVIDEND_EFFECTIVE_DAY_SQL}) FROM dividends"
        " WHERE symbol IN (SELECT symbol FROM instruments))"
    ).fetchone()
    return _day(row[0]) if row is not None else None


# =============================================================================================
# Triggers (§7)
# =============================================================================================

#: The latest day a recorded change can matter for: the later of ``settled_through``,
#: ``held_through`` and the active run's ``run_through``, NULL-safe (review C #4d). A change
#: dated after it is in the live tail anyway.
_BOUND = ("(SELECT MAX(COALESCE(settled_through, ''), COALESCE(held_through, ''), "
          "COALESCE(run_through, '')) FROM settle_meta WHERE id = 1)")

#: The trail's write time: the statement's UTC time with milliseconds (``'now'`` is fixed for
#: the whole statement).
_NOW_UTC = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"

#: The upsert target of the price / FX counter: the partial unique index's columns AND its
#: ``WHERE`` (measured on SQLite 3.49.1 and 3.53.1: without the repeated ``WHERE`` the trigger
#: body does not match the partial index).
_AGG_CONFLICT = (
    "ON CONFLICT (table_name, key) WHERE claimed_run_id IS NULL AND settled_run_id IS NULL "
    "DO UPDATE SET changes = changes + excluded.changes, "
    "earliest = min(earliest, excluded.earliest), latest = max(latest, excluded.latest)"
)


@dataclass(frozen=True)
class _Spec:
    """One trigger table (§7's table). Templates use ``{r}`` for ``NEW`` / ``OLD``."""

    table: str
    #: The affected day of one row version.
    day: str
    #: UPDATE fires when any column changed EXCEPT these (the exclusion list, §7 rule 2) …
    exempt: frozenset[str] = frozenset()
    #: … or, for ``prices`` / ``fx_rates``, only when one of THESE changed.
    inputs: tuple[str, ...] | None = None
    #: The UPDATE's affected day when it is not ``min(day(OLD), day(NEW))``.
    update_day: str | None = None
    #: The affected day may be NULL (no ledger rows): then nothing is recorded.
    day_nullable: bool = False
    row_key: str = "CAST({r}.id AS TEXT)"
    account: str | None = "{r}.account_id"
    symbol: str | None = "{r}.symbol"
    #: (json key, column) pairs for the trail's ``detail``.
    detail: tuple[tuple[str, str], ...] = ()
    #: ``prices`` / ``fx_rates``: the counter key, the columns that form it, the symbol gate.
    agg_key: str | None = None
    key_columns: tuple[str, ...] = ()
    price_gate: bool = False
    #: The key a ``REPLACE`` conflicts on, when the removed row's affected day can differ from
    #: the new row's (a date column outside the key): the BEFORE INSERT trigger marks it.
    replace_key: tuple[str, ...] = ()


_PROVENANCE = frozenset({"import_batch_id", "source_row_hash"})

_SPECS: tuple[_Spec, ...] = (
    _Spec("transactions", "{r}.trade_date",
          exempt=frozenset({"fee_rule_snapshot", "note"}) | _PROVENANCE,
          detail=(("side", "side"),), replace_key=("id",)),
    _Spec("dividends", _dividend_day("{r}"), exempt=_PROVENANCE,
          detail=(("type", "type"),), replace_key=("id",)),
    _Spec("opening_inventory", "{r}.build_date",
          row_key="{r}.account_id || '|' || {r}.symbol", replace_key=("account_id", "symbol")),
    _Spec("cash_movements", "{r}.date", exempt=frozenset({"note"}) | _PROVENANCE,
          symbol=None, detail=(("kind", "kind"), ("ccy", "ccy")), replace_key=("id",)),
    _Spec("fx_conversions", "{r}.date", exempt=_PROVENANCE, symbol=None,
          detail=(("from_ccy", "from_ccy"), ("to_ccy", "to_ccy")), replace_key=("id",)),
    _Spec("corporate_actions", "{r}.date",
          exempt=frozenset({"child_seed_json", "note"}) | _PROVENANCE,
          symbol="{r}.from_symbol",
          detail=(("kind", "kind"), ("to_symbol", "to_symbol")), replace_key=("id",)),
    _Spec("prices", "{r}.as_of_date", inputs=("as_of_date", "close", "instrument"),
          agg_key="{r}.instrument", key_columns=("instrument",), price_gate=True),
    _Spec("fx_rates", "{r}.as_of_date", inputs=("as_of_date", "base", "quote", "rate"),
          agg_key="{r}.base || '/' || {r}.quote", key_columns=("base", "quote")),
    _Spec("instruments", _symbol_day("{r}.symbol"),
          exempt=frozenset({
              "name", "sector", "industry", "aliases", "board", "board_status", "target_low",
              "target_high", "target_set_at", "archived", "etf_flag_unknown", "is_etf",
          }),
          update_day=_symbol_day("OLD.symbol, NEW.symbol"), day_nullable=True,
          row_key="{r}.symbol", account=None),
    # ``name`` and ``broker`` are the display labels (§7: "name / display labels"); every other
    # column is an input by default.
    _Spec("accounts", _FIRST_LEDGER_DAY_SQL, exempt=frozenset({"name", "broker"}),
          update_day=_FIRST_LEDGER_DAY_SQL, day_nullable=True,
          row_key="{r}.account_id", symbol=None),
    _Spec("account_market_rules", _FIRST_LEDGER_DAY_SQL,
          update_day=_FIRST_LEDGER_DAY_SQL, day_nullable=True,
          row_key="{r}.account_id || '|' || {r}.market", symbol=None),
)

#: The trigger tables, in the order the trail lists them.
TRIGGER_TABLES: tuple[str, ...] = tuple(spec.table for spec in _SPECS)

#: §7's exclusion lists: an UPDATE of any OTHER column of these tables is an input.
EXEMPT_COLUMNS: dict[str, frozenset[str]] = {
    spec.table: spec.exempt for spec in _SPECS if spec.inputs is None}
#: ``prices`` / ``fx_rates``: the only columns whose change is an input (§7).
INPUT_COLUMNS: dict[str, tuple[str, ...]] = {
    spec.table: spec.inputs for spec in _SPECS if spec.inputs is not None}

#: Why ``instruments``, ``accounts`` and ``account_market_rules`` carry no REPLACE trigger: a
#: REPLACE conflicts on their key alone, and their affected day depends on the key alone (the
#: symbol's first ledger day; the ledger's first day), so the AFTER INSERT of the new row
#: already marks the removed row's day. A BEFORE INSERT there would also misfire on
#: ``upsert_instrument``: an option contract's upsert proposes the model default
#: ``asset_type = 'stock'``, which its ``DO UPDATE`` deliberately does not write. (A REPLACE
#: that names ``rowid`` breaks the premise — see the module docstring's limits.)
REPLACE_TABLES: tuple[str, ...] = tuple(spec.table for spec in _SPECS if spec.replace_key)


def watched_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """The columns whose change an UPDATE (or a REPLACE) of *table* records, sorted: the
    table's ``PRAGMA table_info`` minus its exclusion list, or its input list."""
    spec = next(s for s in _SPECS if s.table == table)
    return _watched(spec, _columns(conn, table))

#: The trail line a recreated trigger writes (§7, §9.1 boot row): the table's zh label.
TRIGGER_REBUILT_KIND = "trigger_rebuilt"
TRIGGER_REBUILT_ZH = "觸發器重建：{label}"


def default_benchmark_keys() -> tuple[str, ...]:
    """The benchmark storage keys (``pricing/benchmarks.py`` code constants)."""
    return tuple(sorted({b.storage_key for b in BENCHMARKS}))


def _r(template: str, row: str) -> str:
    return template.replace("{r}", row)


def _price_gate(row: str, keys: Sequence[str]) -> str:
    """The ``prices`` row version's symbol is a ledger symbol or a benchmark storage key."""
    sym = f"{row}.instrument"
    parts = [f"{sym} IN ({', '.join(_sql_literal(k) for k in keys)})"] if keys else []
    parts += [
        f"EXISTS (SELECT 1 FROM transactions WHERE symbol = {sym})",
        f"EXISTS (SELECT 1 FROM dividends WHERE symbol = {sym})",
        f"EXISTS (SELECT 1 FROM opening_inventory WHERE symbol = {sym})",
        f"EXISTS (SELECT 1 FROM corporate_actions WHERE from_symbol = {sym})",
        f"EXISTS (SELECT 1 FROM corporate_actions WHERE to_symbol = {sym})",
    ]
    return "(" + " OR ".join(parts) + ")"


def _dirty_stmt(day: str) -> str:
    return (f"UPDATE settle_dirty SET dirty_from = CASE WHEN dirty_from IS NULL OR {day} < "
            f"dirty_from THEN {day} ELSE dirty_from END, seq = seq + 1 WHERE id = 1")


def _detail(spec: _Spec, op: str, *, old_row: str = "OLD", new_row: str = "NEW") -> str:
    if not spec.detail:
        return "NULL"

    def obj(row: str) -> str:
        pairs = ", ".join(f"'{key}', {row}.{col}" for key, col in spec.detail)
        return f"json_object({pairs})"

    parts: list[str] = []
    if op in ("update", "delete"):
        parts.append(f"'old', {obj(old_row)}")
    if op in ("insert", "update"):
        parts.append(f"'new', {obj(new_row)}")
    return f"json_object({', '.join(parts)})"


_LOG_INSERT = ("INSERT INTO settle_change_log (seq, table_name, op, row_key, old_account_id, "
               "new_account_id, old_symbol, new_symbol, detail, old_day, new_day, "
               "import_batch_id, written_at)")


def _log_values(spec: _Spec, op: str, columns: Sequence[str], *, old_row: str = "OLD",
                new_row: str = "NEW") -> str:
    """The trail row's values for *op*, the row versions named *old_row* / *new_row*."""
    old = op in ("update", "delete")
    new = op in ("insert", "update")
    key_row = old_row if op == "delete" else new_row

    def pick(template: str | None, row: str, present: bool) -> str:
        return _r(template, row) if template is not None and present else "NULL"

    batch = (f"{key_row}.import_batch_id" if "import_batch_id" in columns else "NULL")
    values = [
        "(SELECT seq FROM settle_dirty WHERE id = 1)",
        _sql_literal(spec.table),
        _sql_literal(op),
        _r(spec.row_key, key_row),
        pick(spec.account, old_row, old), pick(spec.account, new_row, new),
        pick(spec.symbol, old_row, old), pick(spec.symbol, new_row, new),
        _detail(spec, op, old_row=old_row, new_row=new_row),
        pick(spec.day, old_row, old), pick(spec.day, new_row, new),
        batch,
        _NOW_UTC,
    ]
    return ", ".join(values)


def _log_stmt(spec: _Spec, op: str, columns: Sequence[str]) -> str:
    return f"{_LOG_INSERT} VALUES ({_log_values(spec, op, columns)})"


def _agg_values(spec: _Spec, key: str, earliest: str, latest: str) -> str:
    return (f"INSERT INTO settle_change_agg (table_name, key, changes, earliest, latest) "
            f"VALUES ({_sql_literal(spec.table)}, {key}, 1, {earliest}, {latest}) "
            f"{_AGG_CONFLICT}")


def _trigger_sql(name: str, event: str, when: str, body: Sequence[str]) -> str:
    """A ``CREATE TRIGGER`` in the exact form ``sqlite_master.sql`` stores (P8): no
    ``IF NOT EXISTS``, no schema qualifier, no leading whitespace, no trailing ``;``."""
    lines = [f"CREATE TRIGGER {name} {event}", f"WHEN {when}", "BEGIN"]
    lines += [f"    {stmt};" for stmt in body]
    lines.append("END")
    return "\n".join(lines)


def _watched(spec: _Spec, columns: Sequence[str]) -> list[str]:
    if spec.inputs is not None:
        return sorted(c for c in spec.inputs if c in columns)
    return sorted(c for c in columns if c not in spec.exempt)


def _spec_triggers(spec: _Spec, columns: Sequence[str],
                   keys: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    base = f"{TRIGGER_PREFIX}{spec.table}"
    new_day, old_day = _r(spec.day, "NEW"), _r(spec.day, "OLD")

    # --- INSERT / DELETE ---
    for op, row, day, suffix, event in (
        ("insert", "NEW", new_day, "ins", "AFTER INSERT"),
        ("delete", "OLD", old_day, "del", "AFTER DELETE"),
    ):
        when = f"{day} <= {_BOUND}"
        if spec.price_gate:
            when += f" AND {_price_gate(row, keys)}"
        body = [_dirty_stmt(day)]
        if spec.agg_key is not None:
            body.append(_agg_values(spec, _r(spec.agg_key, row), day, day))
        else:
            body.append(_log_stmt(spec, op, columns))
        out[f"{base}_{suffix}"] = _trigger_sql(
            f"{base}_{suffix}", f"{event} ON {spec.table}", when, body)

    # --- UPDATE ---
    changed = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in _watched(spec, columns))
    day = spec.update_day if spec.update_day is not None else f"min({old_day}, {new_day})"
    when = f"({changed}) AND {day} <= {_BOUND}"
    if spec.price_gate:
        when += f" AND ({_price_gate('OLD', keys)} OR {_price_gate('NEW', keys)})"
    body = [_dirty_stmt(day)]
    if spec.agg_key is not None:
        same_key = " AND ".join(f"OLD.{c} IS NEW.{c}" for c in spec.key_columns)
        moved_key = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in spec.key_columns)
        earliest = f"CASE WHEN {same_key} THEN min({old_day}, {new_day}) ELSE {new_day} END"
        latest = f"CASE WHEN {same_key} THEN max({old_day}, {new_day}) ELSE {new_day} END"
        body.append(_agg_values(spec, _r(spec.agg_key, "NEW"), earliest, latest))
        body.append(
            f"INSERT INTO settle_change_agg (table_name, key, changes, earliest, latest) "
            f"SELECT {_sql_literal(spec.table)}, {_r(spec.agg_key, 'OLD')}, 1, {old_day}, "
            f"{old_day} WHERE {moved_key} {_AGG_CONFLICT}"
        )
    else:
        body.append(_log_stmt(spec, "update", columns))
    out[f"{base}_upd"] = _trigger_sql(
        f"{base}_upd", f"AFTER UPDATE ON {spec.table}", when, body)

    # --- REPLACE: a conflicting row a REPLACE removes fires no DELETE trigger
    # (``recursive_triggers`` is off), so the removed row's day is marked BEFORE the insert —
    # only when a row with the key exists, a watched column differs, and the REMOVED row's day
    # is inside the bound.
    #
    # The id-keyed ledgers (the application never upserts them): when the NEW row's day lies
    # past the bound, its AFTER INSERT records nothing, so this trigger also adds 1 to ``seq``
    # and writes the removed row as a ``delete`` trail row — the REPLACE then reads as the
    # delete + insert it is. When the new day is inside the bound, the AFTER INSERT records the
    # change and this trigger only marks the removed day.
    #
    # ``opening_inventory`` marks only: ``upsert_opening`` takes ``DO UPDATE``, which fires
    # BEFORE INSERT too and is recorded by the AFTER UPDATE — a trail row here would record it
    # twice (an unchanged upsert stays silent; a changed one marks a day its UPDATE marks).
    if spec.replace_key and all(c in columns for c in spec.replace_key):
        key = " AND ".join(f"o.{c} = NEW.{c}" for c in spec.replace_key)
        differs = " OR ".join(f"o.{c} IS NOT NEW.{c}" for c in _watched(spec, columns))
        removed = f"(SELECT {_r(spec.day, 'o')} FROM {spec.table} AS o WHERE {key})"
        logs = spec.replace_key == ("id",)
        past = f"{new_day} > {_BOUND}"
        seq = f", seq = seq + CASE WHEN {past} THEN 1 ELSE 0 END" if logs else ""
        rep_body = [f"UPDATE settle_dirty SET dirty_from = CASE WHEN dirty_from IS NULL OR "
                    f"{removed} < dirty_from THEN {removed} ELSE dirty_from END{seq} "
                    f"WHERE id = 1"]
        if logs:
            rep_body.append(
                f"{_LOG_INSERT} SELECT {_log_values(spec, 'delete', columns, old_row='o')} "
                f"FROM {spec.table} AS o WHERE {key} AND {past}")
        out[f"{base}_rep"] = _trigger_sql(
            f"{base}_rep", f"BEFORE INSERT ON {spec.table}",
            f"EXISTS (SELECT 1 FROM {spec.table} AS o WHERE {key} AND ({differs}) "
            f"AND {_r(spec.day, 'o')} <= {_BOUND})",
            rep_body,
        )

    # --- the import-batch annotation: an import stamps its batch AFTER the insert
    # (``provenance.stamp_row``), so the insert's trail row learns it here. It records no
    # change (the stamp's columns are exempt) and touches no dirt.
    if spec.agg_key is None and "import_batch_id" in columns and "id" in columns:
        out[f"{base}_batch"] = _trigger_sql(
            f"{base}_batch", f"AFTER UPDATE OF import_batch_id ON {spec.table}",
            "OLD.import_batch_id IS NULL AND NEW.import_batch_id IS NOT NULL",
            [f"UPDATE settle_change_log SET import_batch_id = NEW.import_batch_id "
             f"WHERE table_name = {_sql_literal(spec.table)} AND row_key = "
             f"CAST(NEW.id AS TEXT) AND import_batch_id IS NULL AND claimed_run_id IS NULL "
             f"AND settled_run_id IS NULL"],
        )
    return out


def _expected(conn: sqlite3.Connection,
              benchmark_keys: Iterable[str] | None) -> dict[str, tuple[str, str]]:
    """name → (table, CREATE text) of every trigger the store expects on this database."""
    keys = (default_benchmark_keys() if benchmark_keys is None
            else tuple(sorted(set(benchmark_keys))))
    out: dict[str, tuple[str, str]] = {}
    for spec in _SPECS:
        columns = _columns(conn, spec.table)
        if not columns:
            continue  # a table this database does not have yet: its triggers come with it
        for name, sql in _spec_triggers(spec, columns, keys).items():
            out[name] = (spec.table, sql)
    return out


def expected_triggers(conn: sqlite3.Connection,
                      benchmark_keys: Iterable[str] | None = None) -> dict[str, str]:
    """name → expected ``CREATE TRIGGER`` text, in the form ``sqlite_master.sql`` stores (P8).

    Each UPDATE trigger's ``WHEN`` names the table's columns from ``PRAGMA table_info`` minus
    §7's exclusion list, sorted — so a column a later migration adds is an input by default,
    and adding one changes the text, which makes the next boot recreate the trigger and mark
    everything dirty. *benchmark_keys* defaults to the benchmark storage keys.
    """
    return {name: sql for name, (_table, sql) in _expected(conn, benchmark_keys).items()}


def _installed(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    return {
        str(r[0]): (str(r[1]), str(r[2]))
        for r in conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND substr(name, 1, ?) = ?", (len(TRIGGER_PREFIX), TRIGGER_PREFIX))
    }


@dataclass(frozen=True)
class TriggerCheck:
    """What :func:`install_or_verify` found and did."""

    #: Missing triggers it created.
    created: tuple[str, ...] = ()
    #: Triggers whose stored text differed, dropped and created again.
    replaced: tuple[str, ...] = ()
    #: Store triggers no longer expected, dropped.
    dropped: tuple[str, ...] = ()
    #: The tables whose trigger set changed.
    tables: tuple[str, ...] = ()
    #: ``dirty_from`` was lowered to the first day and a trail line written per table (a
    #: settled generation existed and the calc version had not changed).
    marked_dirty: bool = False
    #: No store trigger existed before (a new or reset store).
    first_install: bool = False

    @property
    def recreated(self) -> bool:
        """The trigger set changed: the service starts a full run (§9.1 boot row)."""
        return bool(self.created or self.replaced or self.dropped)


def verify_triggers(conn: sqlite3.Connection,
                    benchmark_keys: Iterable[str] | None = None) -> bool:
    """The installed store triggers are exactly the expected set, text for text. Reads only."""
    expected = expected_triggers(conn, benchmark_keys)
    installed = {name: sql for name, (_table, sql) in _installed(conn).items()}
    return installed == expected


def install_or_verify(conn: sqlite3.Connection, *, calc_version_changed: bool,
                      benchmark_keys: Iterable[str] | None = None) -> TriggerCheck:
    """Compare each store trigger's text in ``sqlite_master`` with its expected definition and
    (re)create only a missing or different one, in ONE explicit transaction (§7).

    An identical trigger is left alone, so an ordinary boot writes nothing and moves no
    ``schema_version``. Outside a calc-version change, a recreated trigger on a store that
    holds a settled generation sets ``dirty_from = first_day`` and writes one trail line
    「觸發器重建：<table>」 per table — edits made while it was missing never reached the
    trail. ``schema_version`` is NOT recorded here: boot calls :func:`record_schema_version`
    last. Call after every migration (the ledgers' columns decide the text).
    """
    if not all(_table_exists(conn, t) for t in ("settle_meta", "settle_dirty",
                                                 "settle_change_log", "settle_change_agg")):
        # a trigger without its target tables would make every ledger write fail
        raise RuntimeError("create the settle tables before installing their triggers")
    expected = _expected(conn, benchmark_keys)
    installed = _installed(conn)
    created = tuple(sorted(n for n in expected if n not in installed))
    replaced = tuple(sorted(n for n in expected
                            if n in installed and installed[n][1] != expected[n][1]))
    dropped = tuple(sorted(n for n in installed if n not in expected))
    if not (created or replaced or dropped):
        return TriggerCheck()
    tables = tuple(sorted(
        {expected[n][0] for n in created + replaced} | {installed[n][0] for n in dropped},
        key=lambda t: (TRIGGER_TABLES.index(t) if t in TRIGGER_TABLES else len(TRIGGER_TABLES),
                       t),
    ))
    marked = False
    with _atomic(conn):
        for name in dropped + replaced:
            conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        for name in sorted(created + replaced):
            conn.execute(expected[name][1])
        if not calc_version_changed:
            meta = _meta_row(conn)
            if meta is not None and int(meta["generation"]) > 0:
                # the earlier of the generation's start and the ledger's: an edit made while
                # the trigger was missing may lie before either
                first = _nmin(_day(meta["first_day"]), first_ledger_day(conn))
                if first is not None:
                    for table in tables:
                        _write_rebuilt_line(conn, table, first)
                    marked = True
    return TriggerCheck(created=created, replaced=replaced, dropped=dropped, tables=tables,
                        marked_dirty=marked, first_install=not installed)


def _write_rebuilt_line(conn: sqlite3.Connection, table: str, first: date) -> None:
    day = first.isoformat()
    conn.execute(
        "UPDATE settle_dirty SET dirty_from = CASE WHEN dirty_from IS NULL OR ? < dirty_from "
        "THEN ? ELSE dirty_from END, seq = seq + 1 WHERE id = 1", (day, day))
    text = TRIGGER_REBUILT_ZH.format(label=CHANGE_TABLE_ZH.get(table, table))
    conn.execute(
        "INSERT INTO settle_change_log (seq, table_name, op, row_key, detail, new_day, "
        "written_at) VALUES ((SELECT seq FROM settle_dirty WHERE id = 1), ?, 'update', '*', "
        f"?, ?, {_NOW_UTC})",
        (table, _json({"kind": TRIGGER_REBUILT_KIND, "text": text}), day))


def triggers_verified(conn: sqlite3.Connection) -> bool:
    """For reads: the trigger set was verified at the current ``schema_version`` (one pragma
    and one row; §7 integrity, §10 step 2). Any schema change since — a migration, a manual
    ``DROP TRIGGER`` — reads as unverified until boot or a claim verifies the set again."""
    try:
        row = conn.execute(
            "SELECT trigger_schema_version FROM settle_meta WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return False
    if row is None or row[0] is None:
        return False
    return int(row[0]) == _schema_version(conn)


def record_schema_version(conn: sqlite3.Connection) -> int:
    """Record the current ``PRAGMA schema_version`` as the verified one; returns it.

    Boot calls it LAST (after every DDL of the lifespan), and :func:`claim` calls it whenever
    the installed set matches the expected one. Writing ``settle_meta`` does not move
    ``schema_version``.
    """
    with _atomic(conn):
        version = _schema_version(conn)
        conn.execute("UPDATE settle_meta SET trigger_schema_version = ? WHERE id = 1",
                     (version,))
    return version


# =============================================================================================
# State reads (§10 steps 2–3)
# =============================================================================================


def _meta_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    cur = conn.execute(
        "SELECT generation, settled_through, first_day, calc_version, reporting_ccy, "
        "bench_state, built_at, run_id, run_from, run_through, run_dirty_from, held_through, "
        "trigger_schema_version FROM settle_meta WHERE id = 1")
    cur.row_factory = sqlite3.Row
    row: sqlite3.Row | None = cur.fetchone()
    return row


@dataclass(frozen=True)
class StoreState:
    """``settle_meta`` + ``settle_dirty`` + the trigger check, read together (§10 step 2)."""

    generation: int = 0
    settled_through: date | None = None
    held_through: date | None = None
    first_day: date | None = None
    calc_version: str | None = None
    reporting_ccy: Currency | None = None
    bench_state: str | None = None
    built_at: str | None = None
    #: The ACTIVE run (§9.4).
    run_id: int | None = None
    run_from: date | None = None
    run_through: date | None = None
    #: The ``dirty_from`` the active run claimed (``None``: it claimed no change, or no run).
    run_dirty_from: date | None = None
    dirty_from: date | None = None
    seq: int = 0
    trigger_schema_version: int | None = None
    schema_version: int | None = None
    #: ``calc_version`` equals the expected one for the reporting currency.
    calc_ok: bool = False
    currency_ok: bool = False
    triggers_ok: bool = False

    @property
    def usable(self) -> bool:
        """generation > 0 ∧ calc version ∧ currency ∧ triggers verified (§10 step 2)."""
        return self.generation > 0 and self.calc_ok and self.currency_ok and self.triggers_ok

    @property
    def dirty_day(self) -> date | None:
        """``Dd``: the earlier of ``dirty_from`` and the ACTIVE run's claimed ``dirty_from``
        — the changes a run has claimed are still dirty until its swap publishes them (review
        A #1), but the rest of its range is not: a full run on a clean store claims no change
        and serves every settled day as current (final senior review, 2026-10-20 — it had
        labelled all history 更新前版本 for the whole run). ``None`` = ∞."""
        return _nmin(self.dirty_from, self.run_dirty_from)

    @property
    def claimed_day(self) -> date | None:
        """The earlier of ``dirty_from`` and the active run's ``run_from`` — "anything pending
        or claimed" (§10 step 5: the stored benchmark state is used only when this is ∞)."""
        return _nmin(self.dirty_from, self.run_from)


def store_state(conn: sqlite3.Connection, *, reporting: Currency,
                expected_calc_version: str | None = None) -> StoreState:
    """Read the store's state. Never writes. A database without the settle tables reads as
    an empty, unusable store (degrades, never ``OperationalError``)."""
    try:
        meta = _meta_row(conn)
        dirty = conn.execute(
            "SELECT dirty_from, seq FROM settle_dirty WHERE id = 1").fetchone()
    except sqlite3.OperationalError:
        return StoreState()
    if meta is None:
        return StoreState()
    schema = _schema_version(conn)
    expected = expected_calc_version if expected_calc_version is not None else calc_version(
        reporting)
    stored_ccy = meta["reporting_ccy"]
    tsv = meta["trigger_schema_version"]
    return StoreState(
        generation=int(meta["generation"]),
        settled_through=_day(meta["settled_through"]),
        held_through=_day(meta["held_through"]),
        first_day=_day(meta["first_day"]),
        calc_version=meta["calc_version"],
        reporting_ccy=Currency(stored_ccy) if stored_ccy is not None else None,
        bench_state=meta["bench_state"],
        built_at=meta["built_at"],
        run_id=None if meta["run_id"] is None else int(meta["run_id"]),
        run_from=_day(meta["run_from"]),
        run_through=_day(meta["run_through"]),
        run_dirty_from=_day(meta["run_dirty_from"]),
        dirty_from=None if dirty is None else _day(dirty[0]),
        seq=0 if dirty is None else int(dirty[1]),
        trigger_schema_version=None if tsv is None else int(tsv),
        schema_version=schema,
        calc_ok=meta["calc_version"] == expected,
        currency_ok=stored_ccy == reporting.value,
        triggers_ok=tsv is not None and int(tsv) == schema,
    )


def window_start(today: date) -> date:
    """``W``: the 1st of the previous month (§10 step 3)."""
    if today.month == 1:
        return date(today.year - 1, 12, 1)
    return date(today.year, today.month - 1, 1)


def live_from(state: StoreState, today: date) -> date:
    """The first day served live (§10 step 3): ``min(H + 1, max(Dd, W))`` with ``S`` =
    ``settled_through``, ``H`` = ``held_through``, ``Dd`` = the earlier of ``dirty_from`` and
    ``run_from`` (∞ when both are NULL) and ``W`` = the 1st of the previous month.

    An unusable store returns ``date.min``: every day is live.
    """
    if not state.usable or state.settled_through is None:
        return date.min
    held = state.held_through or state.settled_through
    dirty = state.dirty_day
    if dirty is None:
        return held + _ONE_DAY
    return min(held + _ONE_DAY, max(dirty, window_start(today)))


def stale_range(state: StoreState, today: date) -> tuple[date, date] | None:
    """The days served from the previous generation although they are dirty (PERF-D3):
    ``[Dd, live_from)`` when ``Dd`` lies before it, else ``None``."""
    if not state.usable:
        return None
    dirty = state.dirty_day
    if dirty is None:
        return None
    first_live = live_from(state, today)
    if dirty < first_live:
        return dirty, first_live - _ONE_DAY
    return None


# =============================================================================================
# The change trail and the run's claim (§9.4)
# =============================================================================================

#: The affected day of a trail row: the earlier of ``old_day`` and ``new_day``.
_AFFECTED_SQL = ("CASE WHEN old_day IS NULL THEN new_day WHEN new_day IS NULL OR "
                 "old_day <= new_day THEN old_day ELSE new_day END")

_APPEND_ATTEMPT_SQL = ("CASE WHEN attempted_run_ids = '' THEN ? "
                       "ELSE attempted_run_ids || ',' || ? END")


class ClaimRefused(RuntimeError):
    """:func:`claim` refused: another run is active, or this run's report is not preparing."""


@dataclass(frozen=True)
class Claim:
    """What :func:`claim` took, read in its transaction."""

    run_id: int
    #: The effective scope: every scope is ``full`` on generation 0.
    scope: RunScope
    #: The run's first day; ``None`` when the ledger has no row to settle.
    run_from: date | None
    run_through: date
    #: Days in ``[run_from, run_through]`` (0 when empty).
    days: int
    #: ``G``: the run writes ``G + 1``.
    generation: int
    settled_through: date | None
    held_through: date | None
    first_day: date | None
    #: ``dirty_from`` before the claim.
    dirty_from: date | None
    seq: int
    records: int
    counters: int
    triggers_verified: bool
    #: The ``run_through`` the caller asked for.
    requested_through: date | None = None
    #: The claim raised ``run_through`` to ``max(settled_through, held_through)``: a claiming
    #: run never publishes short of a day the store already holds (wave-1 review B #1).
    through_clamped: bool = False


def claim(conn: sqlite3.Connection, run_id: int, *, scope: RunScope,
          run_through: date) -> Claim:
    """The run's first write, ONE short transaction (§9.4 "Claiming the dirt"):

    - cleans orphan generations (:func:`cleanup_orphans`);
    - sets ``run_from`` by the range rule (plan §6.1): ``dirty`` / ``nightly`` →
      ``min(dirty_from, settled_through + 1)``; ``full`` → :func:`first_ledger_day`;
      ``selfcheck`` → the first ledger day through ``min(run_through, settled_through,
      Dd − 1)``; on generation 0 every scope is ``full``;
    - for every CLAIMING scope (``dirty``, ``full``, ``nightly``) raises ``run_through`` to
      ``max(run_through, settled_through, held_through)`` — nothing moves back and no held day
      is left behind; :attr:`Claim.through_clamped` says so;
    - records ``run_id`` / ``run_through`` and sets ``dirty_from = NULL``;
    - tags every PENDING trail row and counter with ``claimed_run_id`` (rows are tagged, never
      moved);
    - moves the report to ``running`` with its range and its "before" versions;
    - re-verifies the trigger set and records ``schema_version`` when it matches.

    A ``selfcheck`` claims nothing: it leaves ``dirty_from`` and the trail alone, and records no
    ``run_from`` (readers must not see the whole history as dirty while it checks).
    """
    with _atomic(conn):
        meta = _meta_row(conn)
        if meta is None:
            raise ClaimRefused("the settle store has no state row")
        active = meta["run_id"]
        if active is not None and int(active) != run_id:
            raise ClaimRefused(f"run {active} is active")
        run = conn.execute("SELECT status FROM settle_run WHERE run_id = ?",
                           (run_id,)).fetchone()
        if run is None:
            raise ClaimRefused(f"run {run_id} has no report")
        if run[0] != "preparing":
            raise ClaimRefused(f"run {run_id} is {run[0]}, not preparing")
        _cleanup_orphans(conn)
        dirty_row = conn.execute(
            "SELECT dirty_from, seq FROM settle_dirty WHERE id = 1").fetchone()
        dirty_from = _day(dirty_row[0])
        seq = int(dirty_row[1])
        generation = int(meta["generation"])
        settled = _day(meta["settled_through"])
        effective: RunScope = "full" if generation == 0 or settled is None else scope
        through = run_through
        clamped = False
        if effective != "selfcheck":
            # A claiming run publishes through at least every day the store holds: a publish
            # short of them would leave the held days (through, H] served as settled while
            # the changes claimed for them are marked settled (wave-1 review B #1).
            held = _day(meta["held_through"])
            floor = max((d for d in (settled, held) if d is not None), default=None)
            if floor is not None and floor > through:
                through, clamped = floor, True
        start: date | None
        if effective in ("dirty", "nightly"):
            assert settled is not None
            start = settled + _ONE_DAY
            if dirty_from is not None and dirty_from < start:
                start = dirty_from
        elif effective == "full":
            start = first_ledger_day(conn)
        else:  # selfcheck
            assert settled is not None
            start = first_ledger_day(conn)
            through = min(through, settled)
            if dirty_from is not None:
                through = min(through, dirty_from - _ONE_DAY)
        days = (through - start).days + 1 if start is not None and through >= start else 0
        records = counters = 0
        if effective == "selfcheck":
            conn.execute(
                "UPDATE settle_meta SET run_id = ?, run_from = NULL, run_through = ?, "
                "run_dirty_from = NULL WHERE id = 1", (run_id, through.isoformat()))
        else:
            # what the run claims as DIRTY: the trail's earliest day — or, before anything was
            # ever published, its whole range (nothing settled exists to serve)
            claimed_dirty = start if (generation == 0 or settled is None) else dirty_from
            conn.execute(
                "UPDATE settle_meta SET run_id = ?, run_from = ?, run_through = ?, "
                "run_dirty_from = ? WHERE id = 1",
                (run_id, _iso(start), through.isoformat(), _iso(claimed_dirty)))
            conn.execute("UPDATE settle_dirty SET dirty_from = NULL WHERE id = 1")
            records = conn.execute(
                "UPDATE settle_change_log SET claimed_run_id = ? "
                "WHERE claimed_run_id IS NULL AND settled_run_id IS NULL", (run_id,)).rowcount
            counters = conn.execute(
                "UPDATE settle_change_agg SET claimed_run_id = ? "
                "WHERE claimed_run_id IS NULL AND settled_run_id IS NULL", (run_id,)).rowcount
        kept = 0
        if generation > 0 and start is not None:
            kept = int(conn.execute(
                "SELECT COUNT(*) FROM settled_day WHERE generation = ? AND day < ?",
                (generation, start.isoformat())).fetchone()[0])
        conn.execute(
            "UPDATE settle_run SET status = 'running', range_from = ?, range_to = ?, days = ?, "
            "generation_before = ?, settled_through_before = ?, calc_version_before = ?, "
            "reporting_ccy_before = ? WHERE run_id = ?",
            (_iso(start), through.isoformat(), days, generation, meta["settled_through"],
             meta["calc_version"], meta["reporting_ccy"], run_id))
        report = _load_report(conn, run_id)
        if report is not None:
            ccy = meta["reporting_ccy"]
            report = report.model_copy(update={
                "range": report.range.model_copy(update={
                    "from_": start, "to": through, "days": days, "kept_before": kept,
                    "kind": "dirty" if effective in ("dirty", "nightly") else "full"}),
                "versions": report.versions.model_copy(update={
                    "generation_before": generation, "settled_through_before": settled,
                    "calc_version_before": meta["calc_version"],
                    "reporting_ccy_before": Currency(ccy) if ccy is not None else None}),
            })
            _store_report(conn, run_id, report)
        verified = verify_triggers(conn)
        if verified:
            conn.execute("UPDATE settle_meta SET trigger_schema_version = ? WHERE id = 1",
                         (_schema_version(conn),))
    return Claim(
        run_id=run_id, scope=effective, run_from=start, run_through=through, days=days,
        generation=generation, settled_through=settled,
        held_through=_day(meta["held_through"]), first_day=_day(meta["first_day"]),
        dirty_from=dirty_from, seq=seq, records=records, counters=counters,
        triggers_verified=verified, requested_through=run_through, through_clamped=clamped,
    )


def _put_back(conn: sqlite3.Connection, run_id: int, from_day: date | None) -> tuple[int, int]:
    """Put this run's CLAIMED rows back (§9.4): each log row loses its claim and records the
    attempt; each counter is MERGED into the pending counter of its table and key (count
    added, earliest = min, latest = max, attempts kept) and removed — a plain re-insert would
    collide with a counter written during the run. With *from_day*, only the rows whose
    affected day is ≥ it (a partial publish)."""
    run = str(run_id)
    cond = ""
    params: list[Any] = [run, run, run_id]
    if from_day is not None:
        cond = f" AND COALESCE({_AFFECTED_SQL}, '') >= ?"
        params.append(from_day.isoformat())
    records = conn.execute(
        f"UPDATE settle_change_log SET claimed_run_id = NULL, attempted_run_ids = "
        f"{_APPEND_ATTEMPT_SQL} WHERE claimed_run_id = ? AND settled_run_id IS NULL{cond}",
        params).rowcount
    agg_cond = "" if from_day is None else " AND earliest >= ?"
    agg_params: list[Any] = [run_id] + ([] if from_day is None else [from_day.isoformat()])
    claimed = conn.execute(
        "SELECT id, table_name, key, changes, earliest, latest, attempted_run_ids "
        f"FROM settle_change_agg WHERE claimed_run_id = ? AND settled_run_id IS NULL{agg_cond} "
        "ORDER BY id", agg_params).fetchall()
    for row in claimed:
        attempts = _attempts(row[6]) + [run_id]
        pending = conn.execute(
            "SELECT id, attempted_run_ids FROM settle_change_agg WHERE table_name = ? "
            "AND key = ? AND claimed_run_id IS NULL AND settled_run_id IS NULL",
            (row[1], row[2])).fetchone()
        if pending is None:
            conn.execute(
                "UPDATE settle_change_agg SET claimed_run_id = NULL, attempted_run_ids = ? "
                "WHERE id = ?", (_attempts_text(attempts), row[0]))
            continue
        conn.execute(
            "UPDATE settle_change_agg SET changes = changes + ?, earliest = min(earliest, ?), "
            "latest = max(latest, ?), attempted_run_ids = ? WHERE id = ?",
            (row[3], row[4], row[5],
             _attempts_text(_attempts(pending[1]) + attempts), pending[0]))
        conn.execute("DELETE FROM settle_change_agg WHERE id = ?", (row[0],))
    return records, len(claimed)


def put_back(conn: sqlite3.Connection, run_id: int) -> tuple[int, int]:
    """Put every row this run claimed back to pending; returns (records, counters)."""
    with _atomic(conn):
        return _put_back(conn, run_id, None)


def _settle_claimed(conn: sqlite3.Connection, run_id: int,
                    below: date | None) -> tuple[int, int]:
    cond = "" if below is None else f" AND COALESCE({_AFFECTED_SQL}, '') < ?"
    agg_cond = "" if below is None else " AND earliest < ?"
    extra = [] if below is None else [below.isoformat()]
    records = conn.execute(
        f"UPDATE settle_change_log SET settled_run_id = ? WHERE claimed_run_id = ? "
        f"AND settled_run_id IS NULL{cond}", [run_id, run_id, *extra]).rowcount
    counters = conn.execute(
        f"UPDATE settle_change_agg SET settled_run_id = ? WHERE claimed_run_id = ? "
        f"AND settled_run_id IS NULL{agg_cond}", [run_id, run_id, *extra]).rowcount
    return records, counters


def settle_claimed(conn: sqlite3.Connection, run_id: int, *,
                   below: date | None = None) -> tuple[int, int]:
    """Mark this run's claimed rows settled by it — every one, or with *below* only those
    whose affected day is before it. Returns (records, counters). :func:`swap` does this
    itself; it is public for the worker's tests."""
    with _atomic(conn):
        return _settle_claimed(conn, run_id, below)


def _change_state(claimed: object, settled: object) -> Literal["pending", "claimed", "settled"]:
    if settled is not None:
        return "settled"
    return "claimed" if claimed is not None else "pending"


@dataclass(frozen=True)
class ChangePage:
    """One page of trail records, every counter, and the per-table summary."""

    records: list[ChangeRecord]
    #: Every matching log record (``records`` is one page of them).
    total_count: int
    #: The price / FX counters (per symbol or pair — never paged).
    counters: list[ChangeCounter]
    tables: list[ChangeTableCount]


_LOG_COLUMNS = ("id, seq, table_name, op, row_key, old_account_id, new_account_id, old_symbol, "
                "new_symbol, detail, old_day, new_day, import_batch_id, actor, written_at, "
                "claimed_run_id, settled_run_id, attempted_run_ids")
_AGG_COLUMNS = ("id, table_name, key, changes, earliest, latest, claimed_run_id, "
                "settled_run_id, attempted_run_ids")


def _record(row: Sequence[Any]) -> ChangeRecord:
    return ChangeRecord(
        id=int(row[0]), seq=int(row[1]), table=str(row[2]), op=row[3], row_key=str(row[4]),
        old_account_id=row[5], new_account_id=row[6], old_symbol=row[7], new_symbol=row[8],
        detail=json.loads(row[9]) if row[9] else {}, old_day=_day(row[10]),
        new_day=_day(row[11]),
        import_batch_id=None if row[12] is None else str(row[12]), actor=row[13],
        written_at=datetime.fromisoformat(str(row[14])),
        state=_change_state(row[15], row[16]), claimed_run_id=row[15], settled_run_id=row[16],
        attempted_run_ids=_attempts(row[17]),
    )


def _counter(row: Sequence[Any]) -> ChangeCounter:
    return ChangeCounter(
        id=int(row[0]), table=row[1], key=str(row[2]), changes=int(row[3]),
        earliest=date.fromisoformat(str(row[4])), latest=date.fromisoformat(str(row[5])),
        state=_change_state(row[6], row[7]), claimed_run_id=row[6], settled_run_id=row[7],
        attempted_run_ids=_attempts(row[8]),
    )


def _table_order(table: str) -> tuple[int, str]:
    order = list(CHANGE_TABLE_ZH)
    return (order.index(table) if table in order else len(order), table)


def _changes(conn: sqlite3.Connection, where: str, params: Sequence[Any], *, limit: int,
             offset: int) -> ChangePage:
    total = int(conn.execute(
        f"SELECT COUNT(*) FROM settle_change_log WHERE {where}", params).fetchone()[0])
    records = [_record(r) for r in conn.execute(
        f"SELECT {_LOG_COLUMNS} FROM settle_change_log WHERE {where} ORDER BY id "
        "LIMIT ? OFFSET ?", [*params, limit, offset])]
    counters = [_counter(r) for r in conn.execute(
        f"SELECT {_AGG_COLUMNS} FROM settle_change_agg WHERE {where} ORDER BY table_name, key",
        params)]
    tables: list[ChangeTableCount] = []
    for r in conn.execute(
            f"SELECT table_name, COUNT(*), MIN({_AFFECTED_SQL}), MAX({_AFFECTED_SQL}) "
            f"FROM settle_change_log WHERE {where} GROUP BY table_name", params):
        tables.append(ChangeTableCount(
            table=str(r[0]), label=CHANGE_TABLE_ZH.get(str(r[0]), str(r[0])),
            changes=int(r[1]), earliest=_day(r[2]), latest=_day(r[3])))
    for r in conn.execute(
            f"SELECT table_name, SUM(changes), COUNT(*), MIN(earliest), MAX(latest) "
            f"FROM settle_change_agg WHERE {where} GROUP BY table_name", params):
        tables.append(ChangeTableCount(
            table=str(r[0]), label=CHANGE_TABLE_ZH.get(str(r[0]), str(r[0])),
            changes=int(r[1]), keys=int(r[2]), earliest=_day(r[3]), latest=_day(r[4])))
    tables.sort(key=lambda t: _table_order(t.table))
    return ChangePage(records=records, total_count=total, counters=counters, tables=tables)


def pending_changes(conn: sqlite3.Connection, *, limit: int = 100,
                    offset: int = 0) -> ChangePage:
    """The PENDING trail: changes no run has claimed (during a run: the queued ones)."""
    return _changes(conn, "claimed_run_id IS NULL AND settled_run_id IS NULL", (),
                    limit=limit, offset=offset)


def run_changes(conn: sqlite3.Connection, run_id: int, *, limit: int = 100,
                offset: int = 0) -> ChangePage:
    """A run's trail: what it claimed or settled, and what it attempted and put back
    (「本次未生效，已退回待結算」) — never truncated, paged."""
    where = ("(claimed_run_id = ? OR settled_run_id = ? "
             "OR (',' || attempted_run_ids || ',') LIKE ?)")
    return _changes(conn, where, (run_id, run_id, f"%,{run_id},%"), limit=limit,
                    offset=offset)


# =============================================================================================
# The run-report store (§9.6)
# =============================================================================================

#: Run statuses that end a run.
END_STATUSES: frozenset[str] = frozenset(
    {"published", "partial", "superseded", "cancelled", "failed", "interrupted"})
#: Ends that publish nothing (:func:`end_without_publish`).
UNPUBLISHED_ENDS: frozenset[str] = frozenset({"superseded", "cancelled", "failed",
                                              "interrupted"})
#: How many reports :func:`prune` keeps (§9.6 retention).
KEEP_RUNS = 400

_RUN_COLUMNS = (
    "run_id", "status", "resumed", "reason", "started_by", "cancelled_by", "trigger_text",
    "started_at", "finished_at", "range_from", "range_to", "days", "generation_before",
    "generation_after", "settled_through_before", "settled_through_after", "calc_version_before",
    "calc_version_after", "reporting_ccy_before", "reporting_ccy_after", "eta_initial_s",
    "duration_s", "previous_run_id", "next_run_id", "report",
)
#: Columns :func:`update_run` may set (``status`` moves only through the store's own ends).
_UPDATABLE = frozenset(_RUN_COLUMNS) - {"run_id", "status"}


def _store_report(conn: sqlite3.Connection, run_id: int, report: RunReport) -> None:
    conn.execute("UPDATE settle_run SET report = ? WHERE run_id = ?",
                 (_json(report.model_dump(mode="json", by_alias=True)), run_id))


def _load_report(conn: sqlite3.Connection, run_id: int) -> RunReport | None:
    row = conn.execute("SELECT report FROM settle_run WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    return RunReport.model_validate(json.loads(row[0]))


def create_run(conn: sqlite3.Connection, request: RunRequest, *, run_id: int,
               trigger_text: str, started_at: datetime, eta_initial_s: float | None = None,
               previous_run_id: int | None = None) -> int:
    """Create a run's ``preparing`` report (§9.4 "Starting") with its ``RunRequest`` in
    ``inputs``; returns *run_id* (= its ``job_runs`` id, created by the parent first, P7).

    The "before" versions are read now and refreshed by :func:`claim`; *previous_run_id*
    links the chain both ways.
    """
    if not is_run_reason(request.reason):
        raise ValueError(f"unknown run reason {request.reason!r}")
    with _atomic(conn):
        meta = _meta_row(conn)
        generation = 0 if meta is None else int(meta["generation"])
        settled = None if meta is None else meta["settled_through"]
        calc = None if meta is None else meta["calc_version"]
        ccy = None if meta is None else meta["reporting_ccy"]
        summary = RunSummary(
            run_id=run_id, status="preparing",
            status_text=run_status_text("preparing", resumed=False), reason=request.reason,
            started_by=request.started_by, trigger_text=trigger_text, started_at=started_at,
            eta_initial_s=eta_initial_s, generation_before=generation,
            settled_through_before=_day(settled), previous_run_id=previous_run_id)
        report = RunReport(summary=summary, inputs=request, versions=RunVersions(
            generation_before=generation, settled_through_before=_day(settled),
            calc_version_before=calc,
            reporting_ccy_before=Currency(ccy) if ccy is not None else None))
        conn.execute(
            "INSERT INTO settle_run (run_id, status, reason, started_by, trigger_text, "
            "started_at, eta_initial_s, previous_run_id, generation_before, "
            "settled_through_before, calc_version_before, reporting_ccy_before, report) "
            "VALUES (?, 'preparing', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, request.reason, request.started_by, trigger_text, started_at.isoformat(),
             eta_initial_s, previous_run_id, generation, settled, calc, ccy,
             _json(report.model_dump(mode="json", by_alias=True))))
        if previous_run_id is not None:
            conn.execute("UPDATE settle_run SET next_run_id = ? WHERE run_id = ?",
                         (run_id, previous_run_id))
    return run_id


def _column_value(value: Any) -> Any:
    if isinstance(value, RunReport):
        return _json(value.model_dump(mode="json", by_alias=True))
    if isinstance(value, Mapping):
        return _json(dict(value))
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Currency):
        return value.value
    if isinstance(value, Decimal):
        return to_db(value)
    if isinstance(value, bool):
        return int(value)
    return value


def update_run(conn: sqlite3.Connection, run_id: int, **fields: Any) -> None:
    """Set ``settle_run`` columns of one run (the worker at each phase end, the service's
    chain links). ``report`` takes a :class:`RunReport` (or its JSON dict). ``status`` is not
    settable here: it moves only through :func:`claim`, :func:`swap`,
    :func:`end_without_publish` and :func:`finalise_run`."""
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"not an updatable settle_run column: {sorted(unknown)}")
    if not fields:
        return
    if isinstance(fields.get("report"), Mapping):
        fields["report"] = RunReport.model_validate(fields["report"])  # never store a bad one
    names = sorted(fields)
    with _atomic(conn):
        conn.execute(
            f"UPDATE settle_run SET {', '.join(f'{n} = ?' for n in names)} WHERE run_id = ?",
            [*(_column_value(fields[n]) for n in names), run_id])


def finalise_run(conn: sqlite3.Connection, run_id: int, status: RunStatus,
                 report: RunReport) -> RunStatus:
    """Freeze a run's report at its end; returns the end status actually stored.

    A run's end is decided once. When the swap or :func:`end_without_publish` already ended
    it, that status stands and *status* is ignored (a cancel arriving after the swap leaves
    the published generation, §9.4). A run still active is ended here with *status* through
    :func:`end_without_publish`'s transaction — ``published`` / ``partial`` only ever come from
    :func:`swap`, so asking for them on an active run is an error. The summary, range and
    versions columns are the authority: the stored JSON is overlaid with them.
    """
    if status not in END_STATUSES:
        raise ValueError(f"finalise_run needs an end status, not {status!r}")
    with _atomic(conn):
        row = conn.execute("SELECT status, started_at, finished_at FROM settle_run "
                           "WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"run {run_id} has no report")
        stored = str(row[0])
        if stored not in END_STATUSES:
            if status not in UNPUBLISHED_ENDS:
                raise ValueError(f"run {run_id} is {stored}: only swap() publishes")
            _end(conn, run_id, status)
            stored = status
        effective = cast(RunStatus, stored)
        ended_at = conn.execute("SELECT finished_at FROM settle_run WHERE run_id = ?",
                                (run_id,)).fetchone()[0]
        finished = report.summary.finished_at or (
            datetime.fromisoformat(str(ended_at)) if ended_at is not None else _now_utc())
        duration = report.summary.duration_s
        if duration is None:
            duration = max(0.0, (finished - datetime.fromisoformat(str(row[1]))
                                 ).total_seconds())
        failure = report.failure
        cancelled_by = report.summary.cancelled_by or (failure.cancelled_by if failure else None)
        conn.execute(
            "UPDATE settle_run SET finished_at = ?, duration_s = ?, "
            "cancelled_by = COALESCE(?, cancelled_by), resumed = ?, "
            "next_run_id = COALESCE(?, next_run_id), "
            "eta_initial_s = COALESCE(eta_initial_s, ?) WHERE run_id = ?",
            (finished.isoformat(), duration, cancelled_by, int(report.summary.resumed),
             report.summary.next_run_id, report.summary.eta_initial_s, run_id))
        _store_report(conn, run_id, _overlay(conn, run_id, report))
    return effective


def _run_row(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    cur = conn.execute(f"SELECT {', '.join(_RUN_COLUMNS)} FROM settle_run WHERE run_id = ?",
                       (run_id,))
    cur.row_factory = sqlite3.Row
    row: sqlite3.Row | None = cur.fetchone()
    return row


def _had_changes_sql(alias: str) -> str:
    return (f"(EXISTS (SELECT 1 FROM settle_change_log WHERE settled_run_id = {alias}.run_id) "
            f"OR EXISTS (SELECT 1 FROM settle_change_agg WHERE settled_run_id = "
            f"{alias}.run_id))")


def _summary(row: sqlite3.Row, *, had_changes: bool, stored: RunSummary | None) -> RunSummary:
    status = cast(RunStatus, str(row["status"]))
    resumed = bool(row["resumed"])
    next_id = row["next_run_id"]
    return RunSummary(
        run_id=int(row["run_id"]), status=status,
        status_text=run_status_text(status, resumed=resumed, next_run_id=next_id),
        resumed=resumed, reason=str(row["reason"]), started_by=row["started_by"],
        cancelled_by=row["cancelled_by"], trigger_text=str(row["trigger_text"]),
        started_at=datetime.fromisoformat(str(row["started_at"])),
        finished_at=(None if row["finished_at"] is None
                     else datetime.fromisoformat(str(row["finished_at"]))),
        duration_s=row["duration_s"], eta_initial_s=row["eta_initial_s"],
        range_from=_day(row["range_from"]), range_to=_day(row["range_to"]), days=row["days"],
        generation_before=row["generation_before"], generation_after=row["generation_after"],
        settled_through_before=_day(row["settled_through_before"]),
        settled_through_after=_day(row["settled_through_after"]),
        previous_run_id=row["previous_run_id"], next_run_id=next_id,
        had_changes=had_changes,
        selfcheck_diff=bool(stored.selfcheck_diff) if stored is not None else False,
    )


def _overlay(conn: sqlite3.Connection, run_id: int, report: RunReport) -> RunReport:
    row = _run_row(conn, run_id)
    if row is None:
        return report
    had = bool(conn.execute(
        f"SELECT {_had_changes_sql('r')} FROM settle_run r WHERE r.run_id = ?",
        (run_id,)).fetchone()[0])
    summary = _summary(row, had_changes=had, stored=report.summary)
    versions = report.versions.model_copy(update={
        k: v for k, v in {
            "generation_before": row["generation_before"],
            "generation_after": row["generation_after"],
            "settled_through_before": _day(row["settled_through_before"]),
            "settled_through_after": _day(row["settled_through_after"]),
            "calc_version_before": row["calc_version_before"],
            "calc_version": row["calc_version_after"],
            "reporting_ccy_before": (None if row["reporting_ccy_before"] is None
                                     else Currency(row["reporting_ccy_before"])),
            "reporting_ccy": (None if row["reporting_ccy_after"] is None
                              else Currency(row["reporting_ccy_after"])),
        }.items() if v is not None})
    rng = report.range.model_copy(update={
        k: v for k, v in {"from_": _day(row["range_from"]), "to": _day(row["range_to"]),
                          "days": row["days"]}.items() if v is not None})
    return report.model_copy(update={"summary": summary, "versions": versions, "range": rng})


def get_run(conn: sqlite3.Connection, run_id: int) -> RunReport | None:
    """A run's report (``None`` when absent or pruned), its summary rebuilt from the columns."""
    report = _load_report(conn, run_id)
    if report is None:
        return None
    return _overlay(conn, run_id, report)


@dataclass(frozen=True)
class RunPage:
    items: list[RunSummary]
    total_count: int


def _summaries(conn: sqlite3.Connection, where: str, params: Sequence[Any], *,
               order: str, limit: int, offset: int) -> RunPage:
    total = int(conn.execute(f"SELECT COUNT(*) FROM settle_run r WHERE {where}",
                             params).fetchone()[0])
    cur = conn.execute(
        f"SELECT {', '.join(f'r.{c}' for c in _RUN_COLUMNS)}, {_had_changes_sql('r')} AS had "
        f"FROM settle_run r WHERE {where} ORDER BY r.run_id {order} LIMIT ? OFFSET ?",
        [*params, limit, offset])
    cur.row_factory = sqlite3.Row
    items: list[RunSummary] = []
    for row in cur.fetchall():
        stored = RunReport.model_validate(json.loads(row["report"])).summary
        items.append(_summary(row, had_changes=bool(row["had"]), stored=stored))
    return RunPage(items=items, total_count=total)


def list_runs(conn: sqlite3.Connection, *, limit: int = 50, offset: int = 0,
              only_changes: bool = False) -> RunPage:
    """Run summaries, newest first; *only_changes*: runs that settled at least one change."""
    where = _had_changes_sql("r") if only_changes else "1"
    return _summaries(conn, where, (), order="DESC", limit=limit, offset=offset)


def active_or_unfinished_runs(conn: sqlite3.Connection) -> list[RunSummary]:
    """Every ``preparing`` / ``running`` report, oldest first (boot finalises those whose OS
    lock is free, §9.6)."""
    marks = ", ".join("?" for _ in ACTIVE_RUN_STATUSES)
    return _summaries(conn, f"r.status IN ({marks})", sorted(ACTIVE_RUN_STATUSES),
                      order="ASC", limit=-1, offset=0).items


def write_diff(conn: sqlite3.Connection, run_id: int, rows: Iterable[DiffRow]) -> int:
    """Store a run's changed days (§9.6 Result — history), one row per day and field."""
    params = []
    for row in rows:
        if row.run_id != run_id:
            raise ValueError(f"diff row of run {row.run_id} written for run {run_id}")
        params.append((
            run_id, row.day.isoformat(), row.field, row.kind,
            None if row.before is None else to_db(row.before),
            None if row.after is None else to_db(row.after),
            _json(row.flags_before), _json(row.flags_after)))
    with _atomic(conn):
        conn.executemany(
            "INSERT INTO settle_run_diff (run_id, day, field, kind, before, after, "
            "flags_before, flags_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", params)
    return len(params)


@dataclass(frozen=True)
class DiffPage:
    items: list[DiffRow]
    total_count: int


def diff_page(conn: sqlite3.Connection, run_id: int, *, field: str | None = None,
              kind: str | None = None, limit: int = 100, offset: int = 0) -> DiffPage:
    """One page of a run's diff rows (by day, then field), optionally one field / kind."""
    where = "run_id = ?"
    params: list[Any] = [run_id]
    if field is not None:
        where += " AND field = ?"
        params.append(field)
    if kind is not None:
        where += " AND kind = ?"
        params.append(kind)
    total = int(conn.execute(f"SELECT COUNT(*) FROM settle_run_diff WHERE {where}",
                             params).fetchone()[0])
    items = [
        DiffRow(run_id=int(r[0]), day=date.fromisoformat(r[1]), field=r[2], kind=r[3],
                before=_dec(r[4]), after=_dec(r[5]),
                flags_before=json.loads(r[6]) if r[6] else {},
                flags_after=json.loads(r[7]) if r[7] else {})
        for r in conn.execute(
            "SELECT run_id, day, field, kind, before, after, flags_before, flags_after "
            f"FROM settle_run_diff WHERE {where} ORDER BY day, field LIMIT ? OFFSET ?",
            [*params, limit, offset])
    ]
    return DiffPage(items=items, total_count=total)


_CHANGE_KINDS = ("changed", "value_to_none", "none_to_value", "flag_flip")


def history_from_diff(conn: sqlite3.Connection, run_id: int, *,
                      base: HistoryResult | None = None,
                      last_common_day: DayValue | None = None) -> HistoryResult:
    """The report's "Result — history" summary recomputed from the run's stored diff rows:
    counts per field (every kind but new / dropped day), value ↔ none and flag flips, the
    first and last changed day, the largest net-worth change, 「新結算 N 天」 / 「移除 N 天」.
    *base* keeps its ``currency_changed`` and ``note``; *last_common_day* is passed in (it
    reads both generations, which only the worker and the swap hold)."""
    marks = ", ".join("?" for _ in _CHANGE_KINDS)
    counts = {str(r[0]): int(r[1]) for r in conn.execute(
        f"SELECT field, COUNT(*) FROM settle_run_diff WHERE run_id = ? AND kind IN ({marks}) "
        "GROUP BY field ORDER BY field", (run_id, *_CHANGE_KINDS))}
    by_kind = {str(r[0]): int(r[1]) for r in conn.execute(
        "SELECT kind, COUNT(DISTINCT day) FROM settle_run_diff WHERE run_id = ? GROUP BY kind",
        (run_id,))}
    span = conn.execute(
        f"SELECT MIN(day), MAX(day) FROM settle_run_diff WHERE run_id = ? AND kind IN ({marks})",
        (run_id, *_CHANGE_KINDS)).fetchone()
    largest: DayValue | None = None
    best: Decimal | None = None
    for r in conn.execute(
            "SELECT day, before, after FROM settle_run_diff WHERE run_id = ? "
            "AND field = 'net_worth' AND kind = 'changed' ORDER BY day", (run_id,)):
        before, after = _dec(r[1]), _dec(r[2])
        if before is None or after is None:
            continue
        move = abs(after - before)
        if best is None or move > best:
            best = move
            largest = DayValue(day=date.fromisoformat(r[0]), before=before, after=after)
    flips = int(conn.execute(
        "SELECT COUNT(*) FROM settle_run_diff WHERE run_id = ? AND kind = 'flag_flip'",
        (run_id,)).fetchone()[0])
    v2n = int(conn.execute(
        "SELECT COUNT(*) FROM settle_run_diff WHERE run_id = ? AND kind = 'value_to_none'",
        (run_id,)).fetchone()[0])
    n2v = int(conn.execute(
        "SELECT COUNT(*) FROM settle_run_diff WHERE run_id = ? AND kind = 'none_to_value'",
        (run_id,)).fetchone()[0])
    return HistoryResult(
        counts=counts, value_to_none=v2n, none_to_value=n2v, flag_flips=flips,
        first_changed_day=_day(span[0]), last_changed_day=_day(span[1]),
        largest_net_worth_change=largest, last_common_day=last_common_day,
        new_days=by_kind.get("new_day", 0), dropped_days=by_kind.get("dropped_day", 0),
        currency_changed=base.currency_changed if base is not None else False,
        note=base.note if base is not None else None,
    )


@dataclass(frozen=True)
class RestatementDraft:
    """A restatement to write (:func:`write_restatements` assigns the id and the run)."""

    kind: Literal["M", "Y"]
    period: str
    field: str
    label: str
    unit: str
    old_value: Decimal | None
    new_value: Decimal | None
    reason: str
    at: datetime
    notes_before: Mapping[str, Any] | None = None
    notes_after: Mapping[str, Any] | None = None
    source: str | None = None
    cutover: bool = False


def write_restatements(conn: sqlite3.Connection, run_id: int,
                       drafts: Iterable[RestatementDraft]) -> list[int]:
    """Store the closes this run moved (§14.6); returns their ids. Never pruned."""
    ids: list[int] = []
    with _atomic(conn):
        for d in drafts:
            cur = conn.execute(
                "INSERT INTO settle_restatement (kind, period, field, label, unit, old_value, "
                "new_value, notes_before, notes_after, run_id, reason, source, at, cutover) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (d.kind, d.period, d.field, d.label, d.unit,
                 None if d.old_value is None else to_db(d.old_value),
                 None if d.new_value is None else to_db(d.new_value),
                 None if d.notes_before is None else _json(dict(d.notes_before)),
                 None if d.notes_after is None else _json(dict(d.notes_after)),
                 run_id, d.reason, d.source, d.at.isoformat(), int(d.cutover)))
            ids.append(int(cur.lastrowid or 0))
    return ids


def _restatement(row: Sequence[Any]) -> Restatement:
    return Restatement(
        id=int(row[0]), kind=row[1], period=str(row[2]), field=str(row[3]),
        label=str(row[4]), unit=str(row[5]), old_value=_dec(row[6]), new_value=_dec(row[7]),
        notes_before=json.loads(row[8]) if row[8] else None,
        notes_after=json.loads(row[9]) if row[9] else None, run_id=int(row[10]),
        reason=str(row[11]), source=row[12], at=datetime.fromisoformat(str(row[13])),
        cutover=bool(row[14]))


_RESTATEMENT_COLUMNS = ("id, kind, period, field, label, unit, old_value, new_value, "
                        "notes_before, notes_after, run_id, reason, source, at, cutover")


def restatements_for(conn: sqlite3.Connection, kind: str, period: str) -> list[Restatement]:
    """Every restatement of one close, oldest first (月度成績's 已重述 explanation)."""
    return [_restatement(r) for r in conn.execute(
        f"SELECT {_RESTATEMENT_COLUMNS} FROM settle_restatement WHERE kind = ? AND period = ? "
        "ORDER BY id", (kind, period))]


def restatements_of_run(conn: sqlite3.Connection, run_id: int) -> list[Restatement]:
    """The restatements one run wrote (its report's "Result — closes")."""
    return [_restatement(r) for r in conn.execute(
        f"SELECT {_RESTATEMENT_COLUMNS} FROM settle_restatement WHERE run_id = ? ORDER BY id",
        (run_id,))]


#: A close's last calendar day from its kind and period, in SQL (restatements carry no date).
_PERIOD_END_SQL = ("CASE WHEN kind = 'M' THEN date(period || '-01', '+1 month', '-1 day') "
                   "ELSE period || '-12-31' END")


# =============================================================================================
# Generations (§6 "Generations", §9.4)
# =============================================================================================


@dataclass(frozen=True)
class SettledDay:
    """A ``settled_day`` row: one trend point's settled figures, reporting currency."""

    day: date
    total_value: Decimal
    net_invested: Decimal
    net_worth: Decimal | None
    cash_total: Decimal | None
    cash_incomplete: bool
    incomplete: bool
    options_open: bool

    @property
    def placeholder(self) -> bool:
        """(W2-D) A day the settlement could not value: the trend is unavailable from it on (a
        flow without an on-or-before rate in the trend's pair set — v0.1.31 shows no trend at
        all then). Written as ``total_value = net_invested = 0``, ``incomplete``,
        ``cash_incomplete`` and ``net_worth`` / ``cash_total`` NULL — the only rows with a NULL
        ``cash_total`` (a valued day always stores its cash, 0 without any). It carries no
        figure: a reader serves the day live, a run values it again
        (:func:`first_placeholder_day`)."""
        return self.cash_total is None


@dataclass(frozen=True)
class SettledPeriod:
    """A ``settled_period`` row: one month or year close."""

    kind: Literal["M", "Y"]
    period: str
    end_day: date
    kpi: PeriodKpi
    activity: PeriodActivity = field(default_factory=PeriodActivity)


def period_end(kind: str, period: str) -> date:
    """The last calendar day of month ``YYYY-MM`` (kind ``M``) or year ``YYYY`` (kind ``Y``)."""
    if kind == "M":
        year, month = int(period[:4]), int(period[5:7])
        return date(year, month, calendar.monthrange(year, month)[1])
    if kind == "Y":
        return date(int(period), 12, 31)
    raise ValueError(f"unknown period kind {kind!r}")


def period_ends(first: date, through: date) -> list[tuple[Literal["M", "Y"], str, date]]:
    """Every month end and year end in ``[first, through]``, in date order (month first)."""
    out: list[tuple[Literal["M", "Y"], str, date]] = []
    year, month = first.year, first.month
    while True:
        end = date(year, month, calendar.monthrange(year, month)[1])
        if end > through:
            break
        if end >= first:
            out.append(("M", f"{year:04d}-{month:02d}", end))
            if month == 12:
                out.append(("Y", f"{year:04d}", end))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def next_generation(conn: sqlite3.Connection) -> int:
    """``G + 1``: the generation a run writes."""
    row = conn.execute("SELECT generation FROM settle_meta WHERE id = 1").fetchone()
    return int(row[0]) + 1


def _require_unpublished(conn: sqlite3.Connection, generation: int) -> None:
    current = int(conn.execute("SELECT generation FROM settle_meta WHERE id = 1").fetchone()[0])
    if generation <= current:
        raise ValueError(f"generation {generation} is published (current {current}); a run "
                         "writes only above it")


def _month(day: date) -> tuple[int, int]:
    return day.year, day.month


def _day_params(generation: int, d: SettledDay) -> tuple[Any, ...]:
    return (generation, d.day.isoformat(), to_db(d.total_value), to_db(d.net_invested),
            None if d.net_worth is None else to_db(d.net_worth),
            None if d.cash_total is None else to_db(d.cash_total),
            int(d.cash_incomplete), int(d.incomplete), int(d.options_open))


def write_days(conn: sqlite3.Connection, generation: int, days: Iterable[SettledDay]) -> int:
    """Write a run's day rows into *generation* (> the published one), one short
    transaction per calendar month; returns the rows written."""
    _require_unpublished(conn, generation)
    total = 0
    chunk: list[SettledDay] = []

    def flush() -> None:
        with _atomic(conn):
            conn.executemany(
                "INSERT INTO settled_day (generation, day, total_value, net_invested, "
                "net_worth, cash_total, cash_incomplete, incomplete, options_open) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [_day_params(generation, d) for d in chunk])

    for d in days:
        if chunk and _month(chunk[-1].day) != _month(d.day):
            flush()
            total += len(chunk)
            chunk = []
        chunk.append(d)
    if chunk:
        flush()
        total += len(chunk)
    return total


def _period_params(generation: int, p: SettledPeriod) -> tuple[Any, ...]:
    if p.end_day != period_end(p.kind, p.period):
        raise ValueError(f"{p.kind} {p.period} ends on {period_end(p.kind, p.period)}, "
                         f"not {p.end_day}")
    return (generation, p.kind, p.period, p.end_day.isoformat(),
            _json(p.kpi.model_dump(mode="json", by_alias=True)),
            _json(p.activity.model_dump(mode="json", by_alias=True)))


def write_periods(conn: sqlite3.Connection, generation: int,
                  periods: Iterable[SettledPeriod]) -> int:
    """Write a run's closes into *generation*, one short transaction per end month."""
    _require_unpublished(conn, generation)
    total = 0
    chunk: list[SettledPeriod] = []

    def flush() -> None:
        with _atomic(conn):
            conn.executemany(
                "INSERT INTO settled_period (generation, kind, period, end_day, kpi, activity) "
                "VALUES (?, ?, ?, ?, ?, ?)", [_period_params(generation, p) for p in chunk])

    for p in periods:
        if chunk and _month(chunk[-1].end_day) != _month(p.end_day):
            flush()
            total += len(chunk)
            chunk = []
        chunk.append(p)
    if chunk:
        flush()
        total += len(chunk)
    return total


def write_period_holdings(conn: sqlite3.Connection, generation: int, kind: str, period: str,
                          rows: Iterable[PeriodHolding]) -> int:
    """Write one close's positions at its end day (one transaction)."""
    _require_unpublished(conn, generation)
    params = [
        (generation, kind, period, h.account_id, h.symbol, h.asset_type, h.quote_ccy.value,
         to_db(h.shares), to_db(h.original_cost_total), to_db(h.adjusted_cost_total),
         None if h.price is None else to_db(h.price), _iso(h.price_as_of),
         None if h.market_value is None else to_db(h.market_value),
         None if h.unrealized is None else to_db(h.unrealized), _json(h.flags))
        for h in rows
    ]
    with _atomic(conn):
        conn.executemany(
            "INSERT INTO settled_period_holding (generation, kind, period, account_id, symbol, "
            "asset_type, quote_ccy, shares, original_cost_total, adjusted_cost_total, price, "
            "price_as_of, market_value, unrealized, flags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", params)
    return len(params)


def write_period_cash(conn: sqlite3.Connection, generation: int, kind: str, period: str,
                      rows: Iterable[PeriodCash]) -> int:
    """Write one close's cash balances at its end day (one transaction)."""
    _require_unpublished(conn, generation)
    params = [
        (generation, kind, period, c.account_id, c.ccy.value, to_db(c.balance),
         None if c.reporting_value is None else to_db(c.reporting_value),
         None if c.fx_rate is None else to_db(c.fx_rate), _iso(c.fx_as_of))
        for c in rows
    ]
    with _atomic(conn):
        conn.executemany(
            "INSERT INTO settled_period_cash (generation, kind, period, account_id, ccy, "
            "balance, reporting_value, fx_rate, fx_as_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            params)
    return len(params)


_DAY_COLS = ("day, total_value, net_invested, net_worth, cash_total, cash_incomplete, "
             "incomplete, options_open")
_PERIOD_COLS = "kind, period, end_day, kpi, activity"
_HOLDING_COLS = ("kind, period, account_id, symbol, asset_type, quote_ccy, shares, "
                 "original_cost_total, adjusted_cost_total, price, price_as_of, market_value, "
                 "unrealized, flags")
_CASH_COLS = "kind, period, account_id, ccy, balance, reporting_value, fx_rate, fx_as_of"


def _copy_rows(conn: sqlite3.Connection, src: int, dst: int, *, day_cond: str,
               period_cond: str, params: Sequence[Any]) -> int:
    """Copy *src*'s day rows matching *day_cond* and its closes matching *period_cond* (both
    over the row's own columns), with their positions and cash, into *dst*."""
    copied = conn.execute(
        f"INSERT INTO settled_day (generation, {_DAY_COLS}) SELECT ?, {_DAY_COLS} "
        f"FROM settled_day WHERE generation = ? AND {day_cond}", (dst, src, *params)).rowcount
    conn.execute(
        f"INSERT INTO settled_period (generation, {_PERIOD_COLS}) SELECT ?, {_PERIOD_COLS} "
        f"FROM settled_period WHERE generation = ? AND {period_cond}", (dst, src, *params))
    for table, cols in (("settled_period_holding", _HOLDING_COLS),
                        ("settled_period_cash", _CASH_COLS)):
        selected = ", ".join(f"h.{c.strip()}" for c in cols.split(","))
        conn.execute(
            f"INSERT INTO {table} (generation, {cols}) SELECT ?, {selected} FROM {table} h "
            "JOIN settled_period p ON p.generation = h.generation AND p.kind = h.kind "
            f"AND p.period = h.period WHERE h.generation = ? AND "
            f"{period_cond.replace('end_day', 'p.end_day')}", (dst, src, *params))
    return copied


def copy_before(conn: sqlite3.Connection, src: int, dst: int, day: date) -> int:
    """Copy generation *src*'s rows before *day* into *dst* (one ``INSERT … SELECT`` per
    table, one transaction): day rows dated before it and the closes ending before it, with
    their positions and cash. Returns the day rows copied."""
    with _atomic(conn):
        _require_unpublished(conn, dst)
        return _copy_rows(conn, src, dst, day_cond="day < ?", period_cond="end_day < ?",
                          params=(day.isoformat(),))


#: The generation tables' deletes, each table spelled out: ``tests/architecture/
#: test_ledger_delete_doors.py`` reads ``DELETE FROM {placeholder}`` as a possible ledger delete.
_DELETE_ABOVE = (
    "DELETE FROM settled_day WHERE generation > ?",
    "DELETE FROM settled_period WHERE generation > ?",
    "DELETE FROM settled_period_holding WHERE generation > ?",
    "DELETE FROM settled_period_cash WHERE generation > ?",
)
_DELETE_BELOW = (
    "DELETE FROM settled_day WHERE generation < ?",
    "DELETE FROM settled_period WHERE generation < ?",
    "DELETE FROM settled_period_holding WHERE generation < ?",
    "DELETE FROM settled_period_cash WHERE generation < ?",
)
_DELETE_ONE = (
    "DELETE FROM settled_day WHERE generation = ?",
    "DELETE FROM settled_period WHERE generation = ?",
    "DELETE FROM settled_period_holding WHERE generation = ?",
    "DELETE FROM settled_period_cash WHERE generation = ?",
)
#: A generation's closes ending after a day, positions and cash first.
_DELETE_CLOSES_AFTER = (
    "DELETE FROM settled_period_holding WHERE generation = ? AND (kind, period) IN (SELECT "
    "kind, period FROM settled_period WHERE generation = ? AND end_day > ?)",
    "DELETE FROM settled_period_cash WHERE generation = ? AND (kind, period) IN (SELECT "
    "kind, period FROM settled_period WHERE generation = ? AND end_day > ?)",
)


def _cleanup_orphans(conn: sqlite3.Connection) -> int:
    current = int(conn.execute("SELECT generation FROM settle_meta WHERE id = 1").fetchone()[0])
    return sum(conn.execute(sql, (current,)).rowcount for sql in _DELETE_ABOVE)


def cleanup_orphans(conn: sqlite3.Connection) -> int:
    """Delete the rows of every generation above the published one — a dead run's (§6).
    Called at boot (after the interrupted runs are ended) and by :func:`claim`; never while a
    run is writing. Returns the rows removed."""
    with _atomic(conn):
        return _cleanup_orphans(conn)


def _end(conn: sqlite3.Connection, run_id: int, status: str) -> tuple[int, int] | None:
    """:func:`end_without_publish`'s body; returns the rows put back, ``None`` when the run
    had already ended (or has no report)."""
    row = conn.execute("SELECT status FROM settle_run WHERE run_id = ?", (run_id,)).fetchone()
    if row is None or str(row[0]) in END_STATUSES:
        return None
    meta = _meta_row(conn)
    if meta is not None and meta["run_id"] is not None and int(meta["run_id"]) == run_id:
        # put back the dirty day the run CLAIMED (its changes are back in the trail), never
        # its whole range: a cancelled full run on a clean store leaves the store clean, and
        # the published generation is untouched either way (final senior re-verification,
        # 2026-10-20 — the range had marked all history 更新前版本 after a cancel)
        claimed = meta["run_dirty_from"]
        if claimed is not None:
            conn.execute(
                "UPDATE settle_dirty SET dirty_from = CASE WHEN dirty_from IS NULL OR ? < "
                "dirty_from THEN ? ELSE dirty_from END WHERE id = 1", (claimed, claimed))
        conn.execute("UPDATE settle_meta SET run_id = NULL, run_from = NULL, run_through = NULL, "
                     "run_dirty_from = NULL WHERE id = 1")
        _cleanup_orphans(conn)
    put = _put_back(conn, run_id, None)
    conn.execute("UPDATE settle_run SET status = ?, finished_at = COALESCE(finished_at, ?) "
                 "WHERE run_id = ?", (status, _now_utc().isoformat(), run_id))
    return put


def end_without_publish(conn: sqlite3.Connection, run_id: int, status: str) -> bool:
    """Every end but a publish — failure, cancel, crash, boot cleanup, ``superseded`` — as ONE
    transaction (§9.4 "Every other end"):

    - ``dirty_from`` = the earlier of ``dirty_from`` and ``run_from`` — never an overwrite, so
      an earlier change that arrived during the run is kept;
    - the run fields cleared and the generations above the published one deleted — only when
      this run is still the active one (an orphan's end never touches another run's state);
    - the claimed changes put back (:func:`put_back`);
    - the report moved to *status*.

    Returns ``False`` (and does nothing) when the run already ended — e.g. it published before
    a cancel arrived.
    """
    if status not in UNPUBLISHED_ENDS:
        raise ValueError(f"not an unpublished end: {status!r}")
    with _atomic(conn):
        return _end(conn, run_id, status) is not None


def _prune(conn: sqlite3.Connection, keep: int) -> int:
    marks = ", ".join("?" for _ in ACTIVE_RUN_STATUSES)
    victims = [int(r[0]) for r in conn.execute(
        f"SELECT run_id FROM (SELECT run_id, status FROM settle_run ORDER BY run_id DESC "
        f"LIMIT -1 OFFSET ?) WHERE status NOT IN ({marks})", (keep, *sorted(ACTIVE_RUN_STATUSES)))]
    for start in range(0, len(victims), 500):
        ids = victims[start:start + 500]
        marks_ids = ", ".join("?" for _ in ids)
        conn.execute(f"DELETE FROM settle_run_diff WHERE run_id IN ({marks_ids})", ids)
        conn.execute(f"DELETE FROM settle_change_log WHERE settled_run_id IN ({marks_ids})", ids)
        conn.execute(f"DELETE FROM settle_change_agg WHERE settled_run_id IN ({marks_ids})", ids)
        conn.execute(f"DELETE FROM settle_run WHERE run_id IN ({marks_ids})", ids)
    return len(victims)


def prune(conn: sqlite3.Connection, keep: int = KEEP_RUNS) -> int:
    """Keep the latest *keep* reports (§9.6 retention): older ended reports, their diff rows
    and the trail rows they settled are deleted together. ``settle_restatement`` is never
    pruned, nor an active run. Returns the reports removed. :func:`swap` calls it."""
    with _atomic(conn):
        return _prune(conn, keep)


class SwapRefused(RuntimeError):
    """:func:`swap` refused to publish; nothing changed.

    ``reason``: ``not_current_run`` (an orphan: boot cleaned up or another run owns the store),
    ``not_running`` (the report is not ``running``), ``through_mismatch`` (*through* is not
    the ``run_through`` the claim recorded, :attr:`Claim.run_through`), ``incomplete`` (the
    completeness assertion failed — the run must end as failed)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class SwapResult:
    """What :func:`swap` published."""

    status: Literal["published", "partial", "superseded"]
    generation: int
    settled_through: date | None
    held_through: date | None
    #: A partial publish: the chained run's first day (``N``).
    handed_from: date | None = None
    settled_records: int = 0
    settled_counters: int = 0
    put_back_records: int = 0
    put_back_counters: int = 0
    pruned: int = 0


def _check_complete(conn: sqlite3.Connection, generation: int, first: date,
                    through: date, *, closes_from: date | None = None) -> str | None:
    """``None`` when *generation* holds exactly one day row per day in ``[first, through]`` and
    one close per month and year end in ``[closes_from or first, through]``, else what is
    wrong. (W2-D) The closes may start before the series: at the month of the ledger's first
    row, so a month funded before its first trade has its close."""
    expected_days = (through - first).days + 1 if through >= first else 0
    row = conn.execute(
        "SELECT COUNT(*), MIN(day), MAX(day) FROM settled_day WHERE generation = ?",
        (generation,)).fetchone()
    count = int(row[0])
    if count != expected_days or (count and (row[1] != first.isoformat()
                                             or row[2] != through.isoformat())):
        return (f"{count} day rows {row[1]}..{row[2]}, expected {expected_days} "
                f"{first}..{through}")
    expected = {(k, p, e.isoformat()) for k, p, e in period_ends(
        first if closes_from is None else min(first, closes_from), through)}
    actual = {(str(r[0]), str(r[1]), str(r[2])) for r in conn.execute(
        "SELECT kind, period, end_day FROM settled_period WHERE generation = ?", (generation,))}
    if actual != expected:
        missing = sorted(expected - actual)[:3]
        extra = sorted(actual - expected)[:3]
        return f"closes missing {missing} extra {extra}"
    return None


def _net_worth(conn: sqlite3.Connection, generation: int, day: date) -> tuple[bool, Decimal | None]:
    row = conn.execute("SELECT net_worth FROM settled_day WHERE generation = ? AND day = ?",
                       (generation, day.isoformat())).fetchone()
    return (False, None) if row is None else (True, _dec(row[0]))


def _last_common_day(conn: sqlite3.Connection, old: int, new: int,
                     through: date) -> DayValue | None:
    """The last day ≤ *through* both generations hold, with its net worth before → after."""
    row = conn.execute(
        "SELECT MAX(a.day) FROM settled_day a JOIN settled_day b ON b.day = a.day "
        "AND b.generation = ? WHERE a.generation = ? AND a.day <= ?",
        (new, old, through.isoformat())).fetchone()
    if row is None or row[0] is None:
        return None
    day = date.fromisoformat(row[0])
    return DayValue(day=day, before=_net_worth(conn, old, day)[1],
                    after=_net_worth(conn, new, day)[1])


def swap(conn: sqlite3.Connection, run_id: int, *, calc_version: str,
         bench_state: str | Mapping[str, Any] | None, through: date, first_day: date | None,
         reporting: Currency, built_at: datetime | None = None,
         closes_from: date | None = None) -> SwapResult:
    """Publish a run's generation ``G + 1`` in ONE transaction (§9.4 "Publishing").

    Guarded by ``settle_meta.run_id`` (an orphan child can never publish over boot's cleanup).
    With ``N`` = the ``dirty_from`` found inside the transaction and ``range_from`` = the run's
    first day:

    - **full** (``N`` NULL or after *through*): ``settled_through = through``; every claimed
      change settled; the bench state published; status ``published``;
    - **partial** (``range_from < N ≤ through``): ``settled_through = N − 1``; the claimed
      records dated ≥ ``N`` put back per record (``dirty_from`` is already ``N``); this run's
      diff rows dated ≥ ``N`` and restatements ending ≥ ``N`` deleted and the history summary
      recomputed; the bench state NULL; status ``partial``;
    - **superseded** (``N ≤ range_from``): nothing published —
      :func:`end_without_publish`'s path, status ``superseded``.

    On a publish, ``G + 1``'s rows after the published day are replaced by ``G``'s rows up to
    its ``held_through`` (what the owner was last shown, served as the labelled stale range,
    §10), ``held_through = max(previous held_through, published day)``, the completeness
    assertion runs over ``[first_day, held_through]`` (:class:`SwapRefused` ``incomplete``),
    the start-time *calc_version* and *reporting* are written, ``G`` is deleted and the
    reports are pruned. ``settle_dirty`` is never written. *first_day* ``None`` means the
    ledger has no row: generation 0 stays 0, otherwise an empty generation is published.
    *closes_from* (W2-D, optional): the closes start there instead of at *first_day* — the
    ledger's first row, when it precedes the series.
    """
    bench = (None if bench_state is None else
             bench_state if isinstance(bench_state, str) else _json(dict(bench_state)))
    built = (built_at or _now_utc()).isoformat()
    with _atomic(conn):
        meta = _meta_row(conn)
        if meta is None or meta["run_id"] is None or int(meta["run_id"]) != run_id:
            raise SwapRefused("not_current_run", f"run {run_id} is not the active run")
        run = _run_row(conn, run_id)
        if run is None or run["status"] != "running":
            raise SwapRefused("not_running", f"run {run_id} is not running")
        claimed_through = _day(meta["run_through"])
        if claimed_through != through:
            # publish exactly the claimed range: short of it leaves held days behind, past it
            # publishes days whose changes the triggers never recorded (the bound)
            raise SwapRefused("through_mismatch",
                              f"run {run_id} claimed through {claimed_through}, not {through}")
        n_row = conn.execute("SELECT dirty_from FROM settle_dirty WHERE id = 1").fetchone()
        arrived = _day(n_row[0])
        range_from = _day(run["range_from"])
        old = int(meta["generation"])
        new = old + 1
        if arrived is not None and range_from is not None and arrived <= range_from:
            put = _end(conn, run_id, "superseded") or (0, 0)
            return SwapResult(status="superseded", generation=old,
                              settled_through=_day(meta["settled_through"]),
                              held_through=_day(meta["held_through"]),
                              put_back_records=put[0], put_back_counters=put[1])
        full = arrived is None or arrived > through
        published = through if arrived is None or arrived > through else arrived - _ONE_DAY
        prev_held = _day(meta["held_through"]) or _day(meta["settled_through"])
        if first_day is None and old == 0:
            # an empty ledger on an empty store: nothing to publish, generation stays 0
            settled = _settle_claimed(conn, run_id, None)
            conn.execute("UPDATE settle_meta SET run_id = NULL, run_from = NULL, "
                         "run_through = NULL, run_dirty_from = NULL WHERE id = 1")
            _cleanup_orphans(conn)
            conn.execute("UPDATE settle_run SET status = 'published', generation_after = 0 "
                         "WHERE run_id = ?", (run_id,))
            return SwapResult(status="published", generation=0, settled_through=None,
                              held_through=None, settled_records=settled[0],
                              settled_counters=settled[1])
        # G + 1 holds this run's rows through the published day, then G's held days
        conn.execute("DELETE FROM settled_day WHERE generation = ? AND day > ?",
                     (new, published.isoformat()))
        for sql in _DELETE_CLOSES_AFTER:
            conn.execute(sql, (new, new, published.isoformat()))
        conn.execute("DELETE FROM settled_period WHERE generation = ? AND end_day > ?",
                     (new, published.isoformat()))
        held = published if prev_held is None else max(prev_held, published)
        if first_day is None:
            for sql in _DELETE_ONE:
                conn.execute(sql, (new,))
            held = published
        elif old > 0 and prev_held is not None and prev_held > published:
            _copy_rows(conn, old, new, day_cond="day > ? AND day <= ?",
                       period_cond="end_day > ? AND end_day <= ?",
                       params=(published.isoformat(), prev_held.isoformat()))
        if first_day is not None:
            problem = _check_complete(conn, new, first_day, held, closes_from=closes_from)
            if problem is not None:
                raise SwapRefused("incomplete", problem)
        last_common = None if old == 0 else _last_common_day(conn, old, new, published)
        if full:
            put = (0, 0)
        else:
            assert arrived is not None
            put = _put_back(conn, run_id, arrived)
            conn.execute("DELETE FROM settle_run_diff WHERE run_id = ? AND day >= ?",
                         (run_id, arrived.isoformat()))
            conn.execute(f"DELETE FROM settle_restatement WHERE run_id = ? AND "
                         f"{_PERIOD_END_SQL} >= ?", (run_id, arrived.isoformat()))
        settled = _settle_claimed(conn, run_id, None)
        conn.execute(
            "UPDATE settle_meta SET generation = ?, settled_through = ?, held_through = ?, "
            "first_day = ?, calc_version = ?, reporting_ccy = ?, bench_state = ?, built_at = ?, "
            "run_id = NULL, run_from = NULL, run_through = NULL, run_dirty_from = NULL "
            "WHERE id = 1",
            (new, published.isoformat(), held.isoformat(), _iso(first_day), calc_version,
             reporting.value, bench if full else None, built))
        for sql in _DELETE_BELOW:
            conn.execute(sql, (new,))
        status: Literal["published", "partial"] = "published" if full else "partial"
        conn.execute(
            "UPDATE settle_run SET status = ?, generation_after = ?, settled_through_after = ?, "
            "calc_version_after = ?, reporting_ccy_after = ? WHERE run_id = ?",
            (status, new, published.isoformat(), calc_version, reporting.value, run_id))
        report = _load_report(conn, run_id)
        if report is not None:
            update: dict[str, Any] = {}
            if not full:
                assert arrived is not None
                update["range"] = report.range.model_copy(
                    update={"published_to": published, "handed_from": arrived})
                restated = {r.id for r in restatements_of_run(conn, run_id)}
                update["restatement_ids"] = [i for i in report.restatement_ids if i in restated]
                if report.history is not None and not report.history.currency_changed:
                    update["history"] = history_from_diff(
                        conn, run_id, base=report.history, last_common_day=last_common)
            _store_report(conn, run_id, report.model_copy(update=update))
        pruned = _prune(conn, KEEP_RUNS)
    return SwapResult(
        status=status, generation=new, settled_through=published, held_through=held,
        handed_from=None if full else arrived, settled_records=settled[0],
        settled_counters=settled[1], put_back_records=put[0], put_back_counters=put[1],
        pruned=pruned)


# =============================================================================================
# Reads of a generation (§10 step 4, §11.4)
# =============================================================================================


def read_days(conn: sqlite3.Connection, generation: int, *, start: date | None = None,
              end: date | None = None) -> list[SettledDay]:
    """A generation's day rows in ``[start, end]`` (either bound optional), by day."""
    where = "generation = ?"
    params: list[Any] = [generation]
    if start is not None:
        where += " AND day >= ?"
        params.append(start.isoformat())
    if end is not None:
        where += " AND day <= ?"
        params.append(end.isoformat())
    return [
        SettledDay(day=date.fromisoformat(r[0]), total_value=from_db(r[1]),
                   net_invested=from_db(r[2]), net_worth=_dec(r[3]), cash_total=_dec(r[4]),
                   cash_incomplete=bool(r[5]), incomplete=bool(r[6]), options_open=bool(r[7]))
        for r in conn.execute(
            f"SELECT {_DAY_COLS} FROM settled_day WHERE {where} ORDER BY day", params)
    ]


def _period(row: Sequence[Any]) -> SettledPeriod:
    return SettledPeriod(kind=row[0], period=str(row[1]), end_day=date.fromisoformat(row[2]),
                         kpi=PeriodKpi.model_validate(json.loads(row[3])),
                         activity=PeriodActivity.model_validate(json.loads(row[4])))


def read_periods(conn: sqlite3.Connection, generation: int, *, kind: str | None = None,
                 start: date | None = None, end: date | None = None) -> list[SettledPeriod]:
    """A generation's closes by end day (then kind: a December month before its year)."""
    where = "generation = ?"
    params: list[Any] = [generation]
    if kind is not None:
        where += " AND kind = ?"
        params.append(kind)
    if start is not None:
        where += " AND end_day >= ?"
        params.append(start.isoformat())
    if end is not None:
        where += " AND end_day <= ?"
        params.append(end.isoformat())
    return [_period(r) for r in conn.execute(
        f"SELECT {_PERIOD_COLS} FROM settled_period WHERE {where} ORDER BY end_day, kind",
        params)]


def read_period(conn: sqlite3.Connection, generation: int, kind: str,
                period: str) -> SettledPeriod | None:
    row = conn.execute(
        f"SELECT {_PERIOD_COLS} FROM settled_period WHERE generation = ? AND kind = ? "
        "AND period = ?", (generation, kind, period)).fetchone()
    return None if row is None else _period(row)


def read_period_holdings(conn: sqlite3.Connection, generation: int, kind: str,
                         period: str) -> list[PeriodHolding]:
    """One close's positions, by account and symbol."""
    return [
        PeriodHolding(account_id=r[2], symbol=r[3], asset_type=r[4], quote_ccy=Currency(r[5]),
                      shares=from_db(r[6]), original_cost_total=from_db(r[7]),
                      adjusted_cost_total=from_db(r[8]), price=_dec(r[9]),
                      price_as_of=_day(r[10]), market_value=_dec(r[11]),
                      unrealized=_dec(r[12]), flags=json.loads(r[13]) if r[13] else {})
        for r in conn.execute(
            f"SELECT {_HOLDING_COLS} FROM settled_period_holding WHERE generation = ? "
            "AND kind = ? AND period = ? ORDER BY account_id, symbol",
            (generation, kind, period))
    ]


def read_period_cash(conn: sqlite3.Connection, generation: int, kind: str,
                     period: str) -> list[PeriodCash]:
    """One close's cash balances, by account and currency."""
    return [
        PeriodCash(account_id=r[2], ccy=Currency(r[3]), balance=from_db(r[4]),
                   reporting_value=_dec(r[5]), fx_rate=_dec(r[6]), fx_as_of=_day(r[7]))
        for r in conn.execute(
            f"SELECT {_CASH_COLS} FROM settled_period_cash WHERE generation = ? AND kind = ? "
            "AND period = ? ORDER BY account_id, ccy", (generation, kind, period))
    ]



# =============================================================================================
# The worker's reads (W2-D, additions): what a run reports about its own claim and its past
# =============================================================================================


@dataclass(frozen=True)
class ClaimedAttempts:
    """This run's claimed changes that an earlier run had claimed and not published."""

    #: The earlier runs, sorted (「曾於 #411 嘗試」).
    run_ids: list[int]
    #: How many of the claimed records and counters they are.
    changes: int


def claimed_attempts(conn: sqlite3.Connection, run_id: int) -> ClaimedAttempts:
    """The earlier runs that claimed one of this run's claimed changes and did not publish it
    (「曾於 #411 嘗試」), and how many such changes there are."""
    ids: set[int] = set()
    changes = 0
    for table in ("settle_change_log", "settle_change_agg"):
        for row in conn.execute(
                f"SELECT attempted_run_ids FROM {table} WHERE claimed_run_id = ? "
                "AND attempted_run_ids != ''", (run_id,)):
            earlier = set(_attempts(row[0])) - {run_id}
            if earlier:
                ids.update(earlier)
                changes += 1
    return ClaimedAttempts(run_ids=sorted(ids), changes=changes)


@dataclass(frozen=True)
class ClaimedChange:
    """The earliest change a run claimed: its table, record key and affected day."""

    table: str
    row_key: str
    day: date


def earliest_claimed_change(conn: sqlite3.Connection, run_id: int) -> ClaimedChange | None:
    """The claimed change with the earliest affected day — a ledger record, or a price / FX
    counter by its key — the "source" a restatement names (§14.6)."""
    best: ClaimedChange | None = None
    row = conn.execute(
        f"SELECT table_name, row_key, {_AFFECTED_SQL} AS d FROM settle_change_log "
        "WHERE claimed_run_id = ? AND d IS NOT NULL ORDER BY d, id LIMIT 1",
        (run_id,)).fetchone()
    if row is not None:
        best = ClaimedChange(table=str(row[0]), row_key=str(row[1]),
                             day=date.fromisoformat(str(row[2])))
    agg = conn.execute(
        "SELECT table_name, key, earliest FROM settle_change_agg WHERE claimed_run_id = ? "
        "ORDER BY earliest, id LIMIT 1", (run_id,)).fetchone()
    if agg is not None:
        day = date.fromisoformat(str(agg[2]))
        if best is None or day < best.day:
            best = ClaimedChange(table=str(agg[0]), row_key=str(agg[1]), day=day)
    return best


def discard_run_results(conn: sqlite3.Connection, run_id: int) -> int:
    """Delete the diff and restatement rows a run wrote before its swap when it ends without
    publishing (failed, superseded): they describe a generation nobody was shown. Only for a
    run that did not publish — a published run's restatements are never deleted (§14.6).
    Returns the rows removed."""
    with _atomic(conn):
        status = conn.execute("SELECT status FROM settle_run WHERE run_id = ?",
                              (run_id,)).fetchone()
        if status is not None and str(status[0]) in ("published", "partial"):
            raise ValueError(f"run {run_id} published: its results stay")
        removed = conn.execute("DELETE FROM settle_run_diff WHERE run_id = ?",
                               (run_id,)).rowcount
        removed += conn.execute("DELETE FROM settle_restatement WHERE run_id = ?",
                                (run_id,)).rowcount
    return removed


def last_successful_run(conn: sqlite3.Connection, *, before: int) -> RunReport | None:
    """The newest report before run *before* that published (``published`` / ``partial``) —
    the basis of a run's ETA (§9.5)."""
    row = conn.execute(
        "SELECT run_id FROM settle_run WHERE run_id < ? AND status IN ('published', 'partial') "
        "ORDER BY run_id DESC LIMIT 1", (before,)).fetchone()
    return None if row is None else get_run(conn, int(row[0]))


@dataclass(frozen=True)
class SnapshotRow:
    """One ``portfolio_snapshots`` row (the retired 23:50 freeze), as stored."""

    month: str
    #: When the nightly job last wrote the row (its own clock, an aware ISO time).
    as_of: datetime | None
    reporting_ccy: str
    total_value: Decimal | None
    total_return: Decimal | None
    total_return_rate: Decimal | None
    xirr: Decimal | None
    by_currency: dict[str, Decimal]


def cutover_snapshots(conn: sqlite3.Connection, reporting: Currency) -> dict[str, SnapshotRow]:
    """The month-end snapshots the go-live restates once (§11.4, §14.6 ``cutover``): every
    ``portfolio_snapshots`` row in the configured reporting currency, by month.

    Reads the borrowed ``portfolio_snapshots`` table (``api/snapshots.py`` owns it) by direct
    SQL; a database without it reads as no rows, never ``OperationalError``. A row in another
    currency is not compared (the same rule as every restatement)."""
    if not _table_exists(conn, "portfolio_snapshots"):
        return {}
    out: dict[str, SnapshotRow] = {}
    for r in conn.execute(
            "SELECT month, reporting_ccy, total_value, total_return, total_return_rate, xirr, "
            "by_currency, as_of FROM portfolio_snapshots WHERE reporting_ccy = ? ORDER BY month",
            (reporting.value,)):
        by_ccy = json.loads(r[6] or "{}")
        try:
            as_of = None if r[7] is None else datetime.fromisoformat(str(r[7]))
        except ValueError:
            as_of = None
        out[str(r[0])] = SnapshotRow(
            month=str(r[0]), as_of=as_of, reporting_ccy=str(r[1]), total_value=_dec(r[2]),
            total_return=_dec(r[3]), total_return_rate=_dec(r[4]), xirr=_dec(r[5]),
            by_currency={str(k): from_db(str(v)) for k, v in by_ccy.items()})
    return out


def first_placeholder_day(conn: sqlite3.Connection, generation: int) -> date | None:
    """The first day *generation* stores as a placeholder (:attr:`SettledDay.placeholder`), or
    ``None``. A run that starts after it values again from it (a placeholder never outlives the
    condition that made it); a reader whose live series is available serves it live."""
    row = conn.execute("SELECT MIN(day) FROM settled_day WHERE generation = ? "
                       "AND cash_total IS NULL", (generation,)).fetchone()
    return _day(row[0]) if row is not None else None


def generation_rows(conn: sqlite3.Connection, generation: int, *, start: date,
                    end: date) -> dict[str, dict[str, Any]]:
    """Every stored row of *generation* in ``[start, end]`` by table, keyed for a row-by-row
    comparison (the weekly self-check, §9.1): day rows by day; closes, positions and cash by
    their key (closes by end day). Values are the stored TEXT, so a comparison is byte for
    byte."""
    lo, hi = start.isoformat(), end.isoformat()
    out: dict[str, dict[str, Any]] = {t: {} for t in (
        "settled_day", "settled_period", "settled_period_holding", "settled_period_cash")}
    for r in conn.execute(
            f"SELECT {_DAY_COLS} FROM settled_day WHERE generation = ? AND day BETWEEN ? AND ? "
            "ORDER BY day", (generation, lo, hi)):
        out["settled_day"][str(r[0])] = tuple(r)
    for r in conn.execute(
            f"SELECT {_PERIOD_COLS} FROM settled_period WHERE generation = ? "
            "AND end_day BETWEEN ? AND ? ORDER BY end_day, kind", (generation, lo, hi)):
        out["settled_period"][f"{r[2]}|{r[0]}|{r[1]}"] = (
            json.loads(r[3]), json.loads(r[4]))
    for table, cols in (("settled_period_holding", _HOLDING_COLS),
                        ("settled_period_cash", _CASH_COLS)):
        selected = ", ".join(f"h.{c.strip()}" for c in cols.split(","))
        for r in conn.execute(
                f"SELECT p.end_day, {selected} FROM {table} h JOIN settled_period p "
                "ON p.generation = h.generation AND p.kind = h.kind AND p.period = h.period "
                "WHERE h.generation = ? AND p.end_day BETWEEN ? AND ?", (generation, lo, hi)):
            key = "|".join(str(v) for v in (r[0], r[1], r[2], r[3], r[4]))
            out[table][key] = tuple(r[1:])
    return out


# =============================================================================================
# The read path (W3-G, additions): what a reader serves from the store (§10, §11.2, §11.4)
# =============================================================================================


_Date = date


class _DatedRow(Protocol):
    """A cash movement or an FX conversion, stored or not: a row with its own date."""

    @property
    def date(self) -> _Date: ...


@dataclass(frozen=True)
class SettledPrefix:
    """The trend days a reader serves from the store (§10 step 4).

    *days* are the published generation's rows for ``[first_day, live_from)``, contiguous and
    none of them a placeholder; *live_from* is the first day the reader values live; *stale*
    the part of the store's stale range (PERF-D3) inside *days*, or ``None``.
    """

    days: list[SettledDay]
    live_from: date
    stale: tuple[date, date] | None


def settled_prefix(conn: sqlite3.Connection, state: StoreState, *, first_day: date,
                   today: date) -> SettledPrefix:
    """The days before the live part of a build as at *today*, from the published generation
    (§10 steps 3–4). Reads only.

    The stored rows start at the series' OWN first day *first_day* (a row the store holds
    before it — the first trade deleted — is not served) and stop at the earliest of:

    - :func:`live_from` (never after *today*: a store settled past a reader's clock serves
      only the reader's days);
    - the first PLACEHOLDER day (:func:`first_placeholder_day`, W2-D): a placeholder carries
      no figure, so from it on the reader values live — and when the live series is
      unavailable the whole trend is, exactly as v0.1.31 answers (PERF-F2);
    - the first day the generation does not hold (a gap; the series start moved earlier than
      the generation's ``first_day``): never a day served from nowhere.

    The served ``live_from`` is never LATER than that stop (review G #1): with no stored day
    served — the series' first day on or after the store's ``live_from``, e.g. the ledger's
    first trade re-dated later — it is the stop itself, not the series' first day. A period
    close ending before the served ``live_from`` is read from the store
    (``api/snapshots.py``), so a later one would serve the previous generation's close of a
    dirty period, unmarked.

    **The status chip can say more than this serves** (review G #4, a known false positive).
    ``GET /api/settlement/status`` reports the store-level range — :func:`live_from` and
    :func:`stale_range` — while a build serves live from EARLIER in two cases: the first
    placeholder day when the live series is available, and the series' first day when the
    generation does not hold it (a row back-dated before the series' first day). Then the
    banner reads 「更新前版本 · 尚未包含 … 起的變更」 over days this build already values live.
    Only the build can clip it: the placeholder rule turns on whether the LIVE series is
    available (every flow converts — ``timeseries.build_reporting_flows`` over the whole
    ledger), which the status poll does not evaluate. The banner is never a false NEGATIVE —
    every day it does not mark is current — and the store is dirty in both cases, so the run
    it prompts for is due anyway.

    An unusable store serves nothing: every day is live.
    """
    if not state.usable:
        return SettledPrefix(days=[], live_from=first_day, stale=None)
    stop = min(live_from(state, today), today + _ONE_DAY)
    placeholder = first_placeholder_day(conn, state.generation)
    if placeholder is not None:
        stop = min(stop, placeholder)
    days: list[SettledDay] = []
    if stop > first_day:
        expected = first_day
        for d in read_days(conn, state.generation, start=first_day, end=stop - _ONE_DAY):
            if d.day != expected or d.placeholder:
                break
            days.append(d)
            expected += _ONE_DAY
    served_live = min(first_day + timedelta(days=len(days)), stop)
    stale = stale_range(state, today)
    served_stale = None
    if stale is not None and stale[0] < served_live:
        served_stale = (stale[0], min(stale[1], served_live - _ONE_DAY))
    return SettledPrefix(days=days, live_from=served_live, stale=served_stale)


#: 待結算 (§11.2 ``period_twr``): a period whose ``(previous end, D]`` contains the first live
#: day or overlaps the stale range would join stale and live days — a back-dated position's
#: whole gain booked as one day's return (fold verification #2).
TWR_PENDING_ZH = "待結算：此期間包含尚未重新結算的日子，當期報酬暫不計算"


def twr_pending(start: date, end: date, *, live_from: date | None,
                stale: tuple[date, date] | None) -> bool:
    """Whether the period ``[start, end]`` (``start`` = the day after the previous period's
    end) reads 待結算 (:data:`TWR_PENDING_ZH`): only while some history is stale, when the
    period contains *live_from* or overlaps *stale*. Without a stale range every served day is
    correct, so the join is too."""
    if stale is None:
        return False
    if live_from is not None and start <= live_from <= end:
        return True
    return start <= stale[1] and stale[0] <= end


def dividends_receivable(dividends: Iterable[Dividend], instruments: Mapping[str, Instrument],
                         day: date, *, refusals: DividendRefusals | None = None
                         ) -> list[ReceivableDividend]:
    """``notes.dividends_receivable`` of a close at *day* (§11.2), computed when the close is
    READ: every non-STOCK dividend with ``ex_date ≤ day < payment date`` — the price already
    dropped, the payout counts from its payment date. Shown, never counted. A 配股 counts from
    its ex-date, so it is already in *day*'s shares and never listed; a row without an ex-date
    is never guessed onto the list. Ledger order.

    X1 (owner ruling 2026-10-05): a row the replay REFUSES (*refusals*, the one decision —
    ``returns.DividendRefusals`` of the whole-ledger replay, as every listing reads it) is
    LISTED, marked as every dividend listing marks it — ``booked`` false,
    ``unbooked_note`` 「未入帳（待釐清）」 and ``unbooked_reason``, the replay's own sentence
    (``api/wire.unbooked_wire``'s keys) — never dropped: between its ex-date and its payment
    date it would otherwise appear nowhere. The list carries no total, so nothing sums it."""
    out: list[ReceivableDividend] = []
    for dv in dividends:
        if dv.type is DividendType.STOCK or dv.ex_date is None:
            continue
        if not dv.ex_date <= day < dv.date:
            continue
        inst = instruments.get(dv.symbol)
        if inst is None:
            continue
        why = None if refusals is None else refusals.why(dv)
        out.append(ReceivableDividend(
            account_id=dv.account_id, symbol=dv.symbol, ex_date=dv.ex_date, pay_date=dv.date,
            ccy=inst.quote_ccy, net=dv.net, booked=why is None,
            unbooked_note=None if why is None else UNBOOKED_DIVIDEND_NOTE,
            unbooked_reason=None if why is None else why[1]))
    return out


def ledger_first_row(cut: LedgerBundle, movements: Iterable[_DatedRow],
                     conversions: Iterable[_DatedRow], through: date) -> date | None:
    """The first day any ledger row counts on by *through* — where the first period's activity
    starts, and so the first close (W2-D: "the closes start at the month of the ledger's first
    row"): trades, dividends by their effective date and openings of *cut* (the registered
    ledger), its corporate actions, and the cash movements and FX conversions counting by
    *through*. ``tests/settlement/test_read_path_periods.py`` holds it equal to the worker's
    own rule on every corpus."""
    days = ([t.trade_date for t in cut.transactions]
            + [d.effective_date for d in cut.dividends]
            + [o.build_date for o in cut.opening] + [a.date for a in cut.actions]
            + [m.date for m in movements if counts_by(m.date, through)]
            + [c.date for c in conversions if counts_by(c.date, through)])
    return min(days) if days else None


__all__ = [
    "CALC_PACKAGES",
    "TWR_PENDING_ZH",
    "SettledPrefix",
    "dividends_receivable",
    "ledger_first_row",
    "settled_prefix",
    "twr_pending",
    "ClaimedAttempts",
    "ClaimedChange",
    "SnapshotRow",
    "claimed_attempts",
    "cutover_snapshots",
    "discard_run_results",
    "first_placeholder_day",
    "earliest_claimed_change",
    "generation_rows",
    "last_successful_run",
    "CODE_DIGEST",
    "DIVIDEND_EFFECTIVE_DAY_SQL",
    "END_STATUSES",
    "EXEMPT_COLUMNS",
    "INPUT_COLUMNS",
    "REPLACE_TABLES",
    "KEEP_RUNS",
    "SETTLE_TABLES",
    "STORE_SCHEMA_VERSION",
    "TRIGGER_PREFIX",
    "TRIGGER_REBUILT_KIND",
    "TRIGGER_REBUILT_ZH",
    "TRIGGER_TABLES",
    "UNPUBLISHED_ENDS",
    "ChangePage",
    "Claim",
    "ClaimRefused",
    "DiffPage",
    "RestatementDraft",
    "RunPage",
    "SettledDay",
    "SettledPeriod",
    "StoreState",
    "SwapRefused",
    "SwapResult",
    "TriggerCheck",
    "active_or_unfinished_runs",
    "calc_version",
    "claim",
    "cleanup_orphans",
    "code_digest",
    "copy_before",
    "create_run",
    "create_tables",
    "default_benchmark_keys",
    "diff_page",
    "drop_store",
    "end_without_publish",
    "expected_triggers",
    "finalise_run",
    "first_ledger_day",
    "get_run",
    "history_from_diff",
    "install_or_verify",
    "list_runs",
    "live_from",
    "next_generation",
    "pending_changes",
    "period_end",
    "period_ends",
    "prune",
    "put_back",
    "read_days",
    "read_period",
    "read_period_cash",
    "read_period_holdings",
    "read_periods",
    "record_schema_version",
    "restatements_for",
    "restatements_of_run",
    "run_changes",
    "settle_claimed",
    "stale_range",
    "store_state",
    "swap",
    "triggers_verified",
    "update_run",
    "verify_triggers",
    "watched_columns",
    "window_start",
    "write_days",
    "write_diff",
    "write_period_cash",
    "write_period_holdings",
    "write_periods",
    "write_restatements",
]
