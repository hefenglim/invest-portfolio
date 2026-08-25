import sqlite3

_DDL = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY, name TEXT NOT NULL, broker TEXT NOT NULL,
    settlement_ccy TEXT NOT NULL, funding_ccy TEXT NOT NULL,
    fee_rule_set TEXT NOT NULL, dividend_model TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY, market TEXT NOT NULL, quote_ccy TEXT NOT NULL,
    sector TEXT, name TEXT, board TEXT,
    target_low TEXT, board_status TEXT NOT NULL DEFAULT 'resolved',
    is_etf INTEGER NOT NULL DEFAULT 0,
    etf_flag_unknown INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    target_high TEXT,
    industry TEXT,
    target_set_at TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    quantity TEXT NOT NULL, price TEXT NOT NULL, fees TEXT NOT NULL, tax TEXT NOT NULL,
    trade_date TEXT NOT NULL, fee_rule_snapshot TEXT, note TEXT,
    daytrade INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dividends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL, symbol TEXT NOT NULL, date TEXT NOT NULL, type TEXT NOT NULL,
    gross TEXT, withholding TEXT, net TEXT, reinvest_shares TEXT, reinvest_price TEXT,
    ex_date TEXT
);
CREATE TABLE IF NOT EXISTS fx_conversions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL, date TEXT NOT NULL,
    from_ccy TEXT NOT NULL, from_amount TEXT NOT NULL,
    to_ccy TEXT NOT NULL, to_amount TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opening_inventory (
    account_id TEXT NOT NULL, symbol TEXT NOT NULL,
    shares TEXT NOT NULL, original_cost_total TEXT NOT NULL,
    build_date TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol)
);
CREATE TABLE IF NOT EXISTS account_market_rules (
    account_id TEXT NOT NULL, market TEXT NOT NULL,
    fee_rule_set TEXT NOT NULL, dividend_model TEXT NOT NULL,
    PRIMARY KEY (account_id, market)
);
CREATE TABLE IF NOT EXISTS cash_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL, date TEXT NOT NULL,
    kind TEXT NOT NULL,
    ccy TEXT NOT NULL, amount TEXT NOT NULL,
    note TEXT,
    acq_home_amount TEXT
);
CREATE TABLE IF NOT EXISTS corporate_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  TEXT NOT NULL,
    date        TEXT NOT NULL,          -- effective date (ISO)
    kind        TEXT NOT NULL,          -- SPLIT | EXCHANGE | SPINOFF
    from_symbol TEXT NOT NULL,
    to_symbol   TEXT NOT NULL,          -- == from_symbol for SPLIT (enforced: E20)
    -- The ratio is a RATIONAL, stored as its two terms — NEVER as one decimal. A decimal
    -- ratio is a rounded quotient, and data-and-pricing.md forbids storing a rounded
    -- quotient as the authority. BOTH terms are positive INTEGERS (D14): "Decimal > 0" was
    -- not enough — it let 0.2857 in through the CSV importer and the API, which recreates
    -- the 賣超 cascade this feature exists to prevent. Enforced at validation (E6/E6a).
    ratio_to    TEXT NOT NULL,          -- positive integer: shares received
    ratio_from  TEXT NOT NULL,          -- positive integer: shares surrendered
    cost_carry  TEXT,                   -- Decimal in [0,1]; SPINOFF only, NULL otherwise
    note        TEXT
);
-- One row per imported FILE (2026-08-13). The batch is a property of the file, not of the
-- row: it is created from the uploaded text itself (name + sha256), so an offline broker
-- converter can emit ordinary template CSVs and provenance still works. Deleting a batch
-- deletes exactly the rows it wrote, which is what makes trying an import reversible — the
-- precondition for taking a four-figure broker export onto a real ledger at all.
-- Rationale, the idempotency key's design, and why opening_inventory is excluded:
-- data_ingestion/provenance.py.
CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,     -- the template kind (transactions / dividends / …)
    broker        TEXT,              -- set by the broker converter path; NULL for a plain CSV
    source_name   TEXT,              -- the uploaded file's name, when the caller knows it
    source_sha256 TEXT NOT NULL,     -- digest of the uploaded text
    imported_at   TEXT NOT NULL,
    row_count     INTEGER NOT NULL,
    status        TEXT NOT NULL      -- open | committed
);
CREATE TABLE IF NOT EXISTS ledger_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    row_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('update','delete')),
    before_json TEXT NOT NULL,
    at TEXT NOT NULL
);
"""


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _drop_column_if_present(
    conn: sqlite3.Connection, table: str, column: str
) -> None:
    """Idempotent DROP COLUMN migration (SQLite >= 3.35; bundled 3.49). The repo's FIRST
    destructive migration (A6, 2026-07-21). Guarded by ``pragma table_info`` so it is a no-op
    once the column is gone; the column must be non-PK and unindexed (opening_inventory's
    ``original_avg_cost`` is both). Fresh DBs never have the column (the DDL omits it), so this
    only fires for a legacy DB migrating in."""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column in cols:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    # R6 (review ⑧): the EX-DIVIDEND date. NULLABLE with NO default on purpose — every
    # existing row migrates in as "unknown", never as a guessed date, and a row with
    # NULL replays byte-identically to before the column existed. `date` is pinned to
    # mean the PAYMENT date.
    _add_column_if_missing(conn, "dividends", "ex_date", "TEXT")
    _add_column_if_missing(conn, "instruments", "board", "TEXT")  # migrate legacy DBs
    _add_column_if_missing(conn, "instruments", "target_low", "TEXT")
    _add_column_if_missing(conn, "instruments", "board_status", "TEXT NOT NULL DEFAULT 'resolved'")
    _add_column_if_missing(conn, "instruments", "is_etf", "INTEGER NOT NULL DEFAULT 0")
    # etf_flag_unknown (AI-D40, 2026-08-24): the THIRD state of the ETF flag. is_etf is a
    # NOT NULL boolean and SQLite cannot relax that without a table rebuild, so "unknown"
    # rides alongside as an additive column instead. Existing rows migrate in as 0 =
    # KNOWN, which is the owner ruling verbatim: only FUTURE auto-registrations land
    # unknown; nothing reaches back and re-labels what is already in the ledger.
    _add_column_if_missing(
        conn, "instruments", "etf_flag_unknown", "INTEGER NOT NULL DEFAULT 0")
    # archived (FU-D13): a closed-with-history symbol the user stopped tracking. Excluded
    # from quote/signal/news fetch scopes but stays REGISTERED, so no money figure changes.
    _add_column_if_missing(conn, "instruments", "archived", "INTEGER NOT NULL DEFAULT 0")
    # target_high (FU-D28): the price-alert CEILING, joining target_low (the floor). Both feed
    # the target_cross rule; additive, so an existing DB migrates in without touching any row.
    _add_column_if_missing(conn, "instruments", "target_high", "TEXT")
    # industry (R6, 2026-07-19): nullable GICS industry, filled by the next wave's AI service.
    # Backend plumbing only this wave; additive, so an existing DB migrates in untouched.
    _add_column_if_missing(conn, "instruments", "industry", "TEXT")
    # target_set_at (D44, 2026-08-15): the ISO date the target band above was last CHANGED.
    # Owned end-to-end by store.upsert_instrument, which compares against the stored row —
    # so it cannot drift from the values it dates. It exists to make one question answerable:
    # "does this band predate that split?" Without it, the D44 finding has to fire on every
    # split of every banded symbol, and a bulk broker import of five years of history is
    # mostly HISTORICAL splits — i.e. mostly false positives, which is the "cries wolf"
    # failure E23 spent a fourth condition avoiding. NULL (every pre-existing row, and any
    # symbol whose band nobody has touched since) means UNKNOWN, and the finding stays
    # silent: no data, no claim.
    _add_column_if_missing(conn, "instruments", "target_set_at", "TEXT")
    _add_column_if_missing(conn, "transactions", "daytrade", "INTEGER NOT NULL DEFAULT 0")
    # short_sale (2026-07-31): a DECLARED short sale. Default 0, so every pre-existing row
    # keeps its exact meaning and the replay is unchanged for them.
    _add_column_if_missing(conn, "transactions", "short_sale", "INTEGER NOT NULL DEFAULT 0")
    # acq_home_amount (spec 2026-07-30 F1): the HOME-currency cost of a FOREIGN-currency cash
    # movement, so an opening/deposit of foreign cash can carry a cost basis into the FX pool
    # (forex/pools.py). Stores the AMOUNT, never the rate — the rate is an average and
    # `data-and-pricing.md` forbids storing an average as the authority; `fx_conversions`
    # likewise stores two amounts. NULL on every pre-existing row, and a NULL row behaves
    # exactly as before the migration (it just no longer counts as covered).
    _add_column_if_missing(conn, "cash_movements", "acq_home_amount", "TEXT")
    # Import provenance (2026-08-13). Additive and nullable on every ledger, so a row that
    # predates this — or one entered by hand through a form — is unchanged and simply has no
    # batch. ``opening_inventory`` is deliberately absent: its composite PK makes its writer
    # an UPSERT, so it is already idempotent and has no surrogate id to stamp. The full
    # rationale lives in ``data_ingestion/provenance.py``; the table list there
    # (``TABLE_BY_KIND``) and this loop are asserted equal by tests/data_ingestion.
    for _ledger in (
        "transactions", "dividends", "fx_conversions", "cash_movements", "corporate_actions"
    ):
        _add_column_if_missing(conn, _ledger, "import_batch_id", "INTEGER")
        _add_column_if_missing(conn, _ledger, "source_row_hash", "TEXT")
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_ledger}_src_hash "
            f"ON {_ledger}(source_row_hash)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_ledger}_batch "
            f"ON {_ledger}(import_batch_id)"
        )
    # original_avg_cost drop (A6, 2026-07-21): the stored rounded average is retired — cost
    # basis / XIRR key off original_cost_total only, and the average is computed on read. A
    # legacy DB carried a NOT NULL original_avg_cost column that upsert_opening no longer fills,
    # so it MUST be dropped (else a new insert would violate the NOT NULL constraint).
    _drop_column_if_present(conn, "opening_inventory", "original_avg_cost")
    conn.commit()
    # One-time idempotent sector rewrite to the canonical GICS vocabulary (R6). Runs after the
    # column adds + commit so it sees the final schema; a no-op when every value is already
    # canonical. Local import keeps schema.py free of a module-load dependency on store.py.
    from portfolio_dash.data_ingestion.store import migrate_instrument_sectors

    migrate_instrument_sectors(conn)
