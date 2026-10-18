"""The settlement store: settled days, month / year closes and the run reports (PERF).

Spec ``docs/spec/2026-10-18-settlement-store.md`` §6. Every table here is DERIVED: the ledgers
stay the only source of truth, so dropping the store loses nothing and a run rebuilds it. This
module owns the DDL; the trigger set (§7), the change trail, the generations and the run-report
store are added by later work packages on top of it.

Created at boot only (``api/app.py`` lifespan, after every borrowed table exists) — never by a
GET (DEF-065). The trigger set reads the borrowed ledgers (``transactions``, ``dividends``,
``opening_inventory``, ``cash_movements``, ``fx_conversions``, ``corporate_actions``, ``prices``,
``fx_rates``, ``instruments``, ``accounts``, ``account_market_rules``) by direct SQL, the
cross-layer convention of ``architecture.md``.

**Never drop a settle table while its triggers exist**: every ledger write would then fail with
"no such table". :func:`drop_store` drops the triggers first.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import portfolio_dash
from portfolio_dash.shared.enums import Currency

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
    actor TEXT,  -- 'user:<name>' | 'job:<id>' | 'system:boot' | NULL = 外部程式／腳本 (§7)
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
    trigger_text TEXT NOT NULL,  -- rendered per viewer (「你」 only for started_by)
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
    field TEXT NOT NULL, label TEXT NOT NULL,  -- e.g. total_market_value / 總市值
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
    for statement in _statements(_DDL):
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


__all__ = [
    "CALC_PACKAGES",
    "CODE_DIGEST",
    "SETTLE_TABLES",
    "STORE_SCHEMA_VERSION",
    "TRIGGER_PREFIX",
    "calc_version",
    "code_digest",
    "create_tables",
    "drop_store",
]
