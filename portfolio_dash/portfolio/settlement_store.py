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

Transactions: every function that writes does so in ONE short transaction of its own
(``BEGIN IMMEDIATE`` … ``COMMIT``); called while the connection already holds an open
transaction, it nests as a SAVEPOINT and the caller commits. The chunked writers
(:func:`write_days`, :func:`write_periods`) commit once per calendar month.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import portfolio_dash
from portfolio_dash.portfolio.settlement_models import (
    CHANGE_TABLE_ZH,
)
from portfolio_dash.pricing.benchmarks import BENCHMARKS
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.money import from_db

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
    return None if value is None else date.fromisoformat(str(value))


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
DIVIDEND_EFFECTIVE_DAY_SQL = (
    "CASE WHEN type = 'STOCK' AND ex_date IS NOT NULL THEN ex_date ELSE date END"
)


def _dividend_day(row: str) -> str:
    return (f"CASE WHEN {row}.type = 'STOCK' AND {row}.ex_date IS NOT NULL "
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


_PROVENANCE = frozenset({"import_batch_id", "source_row_hash"})

_SPECS: tuple[_Spec, ...] = (
    _Spec("transactions", "{r}.trade_date",
          exempt=frozenset({"fee_rule_snapshot", "note"}) | _PROVENANCE,
          detail=(("side", "side"),)),
    _Spec("dividends", _dividend_day("{r}"), exempt=_PROVENANCE,
          detail=(("type", "type"),)),
    _Spec("opening_inventory", "{r}.build_date",
          row_key="{r}.account_id || '|' || {r}.symbol"),
    _Spec("cash_movements", "{r}.date", exempt=frozenset({"note"}) | _PROVENANCE,
          symbol=None, detail=(("kind", "kind"), ("ccy", "ccy"))),
    _Spec("fx_conversions", "{r}.date", exempt=_PROVENANCE, symbol=None,
          detail=(("from_ccy", "from_ccy"), ("to_ccy", "to_ccy"))),
    _Spec("corporate_actions", "{r}.date",
          exempt=frozenset({"child_seed_json", "note"}) | _PROVENANCE,
          symbol="{r}.from_symbol",
          detail=(("kind", "kind"), ("to_symbol", "to_symbol"))),
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


def _detail(spec: _Spec, op: str) -> str:
    if not spec.detail:
        return "NULL"

    def obj(row: str) -> str:
        pairs = ", ".join(f"'{key}', {row}.{col}" for key, col in spec.detail)
        return f"json_object({pairs})"

    parts: list[str] = []
    if op in ("update", "delete"):
        parts.append(f"'old', {obj('OLD')}")
    if op in ("insert", "update"):
        parts.append(f"'new', {obj('NEW')}")
    return f"json_object({', '.join(parts)})"


def _log_stmt(spec: _Spec, op: str, columns: Sequence[str]) -> str:
    old = op in ("update", "delete")
    new = op in ("insert", "update")
    key_row = "OLD" if op == "delete" else "NEW"

    def pick(template: str | None, row: str, present: bool) -> str:
        return _r(template, row) if template is not None and present else "NULL"

    batch = (f"{key_row}.import_batch_id" if "import_batch_id" in columns else "NULL")
    values = [
        "(SELECT seq FROM settle_dirty WHERE id = 1)",
        _sql_literal(spec.table),
        _sql_literal(op),
        _r(spec.row_key, key_row),
        pick(spec.account, "OLD", old), pick(spec.account, "NEW", new),
        pick(spec.symbol, "OLD", old), pick(spec.symbol, "NEW", new),
        _detail(spec, op),
        pick(spec.day, "OLD", old), pick(spec.day, "NEW", new),
        batch,
        _NOW_UTC,
    ]
    return ("INSERT INTO settle_change_log (seq, table_name, op, row_key, old_account_id, "
            "new_account_id, old_symbol, new_symbol, detail, old_day, new_day, "
            f"import_batch_id, written_at) VALUES ({', '.join(values)})")


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
                first = _day(meta["first_day"]) or first_ledger_day(conn)
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
        "bench_state, built_at, run_id, run_from, run_through, held_through, "
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
        """``Dd``: the earlier of ``dirty_from`` and ``run_from`` — a range a run has claimed
        is still dirty until its swap publishes it (review A #1). ``None`` = ∞."""
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


__all__ = [
    "CALC_PACKAGES",
    "CODE_DIGEST",
    "DIVIDEND_EFFECTIVE_DAY_SQL",
    "SETTLE_TABLES",
    "STORE_SCHEMA_VERSION",
    "TRIGGER_PREFIX",
    "TRIGGER_REBUILT_KIND",
    "TRIGGER_REBUILT_ZH",
    "TRIGGER_TABLES",
    "StoreState",
    "TriggerCheck",
    "calc_version",
    "code_digest",
    "create_tables",
    "default_benchmark_keys",
    "drop_store",
    "expected_triggers",
    "first_ledger_day",
    "install_or_verify",
    "live_from",
    "record_schema_version",
    "stale_range",
    "store_state",
    "triggers_verified",
    "verify_triggers",
    "window_start",
]
