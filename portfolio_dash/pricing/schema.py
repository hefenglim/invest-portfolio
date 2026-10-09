"""DDL for the pricing tables + their additive migrations.

The ``prices`` migrations live HERE, not in ``data_ingestion/schema.py`` (F-14): boot
order is ``bootstrap_db`` then ``create_pricing_tables`` (``api/app.py``), so an
``ALTER TABLE prices`` issued from the ledger schema module runs before the table
exists and raises ``no such table: prices`` on a fresh database. A table's migrations
belong to the module that owns the table.
"""

import sqlite3

# ``close_raw`` / ``split_basis`` are the two-column price basis (spec §5.1(b), D30).
# They are declared LAST in the column list — the position ``ALTER TABLE ADD COLUMN``
# uses — so a fresh database and a migrated one end up with byte-identical schemas,
# column order included.
#
# ⚠ ``close`` and ``open``/``high``/``low`` CAN BE ON DIFFERENT BASES ON THE SAME ROW.
# Only ``close`` is re-expressed by a split (spec §5.1, amended 2026-08-10): it is the only
# price with a preserved source, so it is the only one a reconcile can restate or reverse.
# ``open``/``high``/``low`` carry the SAME two-column basis as ``close`` (W8, owner ruling
# 2026-08-27, spec ``AI-D58``): each has its own ``*_raw`` holding the provider's value as
# delivered, and the stored column is ``raw × split_basis``. Before W8 they kept the
# provider's basis untouched, so a row could carry a post-split close beside pre-split highs
# and lows — invisible only because nothing read them, and a trap for the first candlestick
# chart. ``volume`` is still deliberately untouched: it is a count, not a price, and a split
# restates it in the opposite direction.
_DDL = """
CREATE TABLE IF NOT EXISTS prices (
    instrument TEXT NOT NULL, market TEXT NOT NULL, as_of_date TEXT NOT NULL,
    close TEXT NOT NULL, open TEXT, high TEXT, low TEXT, volume TEXT,
    source TEXT NOT NULL, fetched_at TEXT NOT NULL,
    close_raw TEXT, split_basis TEXT NOT NULL DEFAULT '1',
    -- W8's three raws sit AFTER split_basis, which reads oddly beside close_raw but is
    -- required: a migrated database gets them by ALTER TABLE, which can only append, and
    -- `test_fresh_and_migrated_schemas_agree_column_for_column` demands the fresh DDL and
    -- the migrated one agree column for column. DDL order follows migration order.
    open_raw TEXT, high_raw TEXT, low_raw TEXT,
    PRIMARY KEY (instrument, as_of_date)
);
CREATE TABLE IF NOT EXISTS fx_rates (
    base TEXT NOT NULL, quote TEXT NOT NULL, as_of_date TEXT NOT NULL,
    rate TEXT NOT NULL, source TEXT NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY (base, quote, as_of_date)
);
CREATE TABLE IF NOT EXISTS dividend_events (
    instrument TEXT NOT NULL, market TEXT NOT NULL, ex_date TEXT NOT NULL,
    pay_date TEXT, cash_amount TEXT, stock_amount TEXT, currency TEXT,
    source TEXT NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY (instrument, ex_date)
);
"""


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    """Add ``column`` to ``table`` if absent (additive, idempotent migration).

    A LOCAL copy of the ``data_ingestion`` PRAGMA pattern, intentionally NOT imported:
    ``pricing/`` must not gain a dependency on ``data_ingestion`` (``architecture.md``;
    ``scheduler/jobs.py`` keeps the same local copy for the same reason).
    ``PRAGMA table_info`` row index 1 is the column name, which is row_factory-agnostic.
    """
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def create_tables(conn: sqlite3.Connection) -> None:
    """Create the pricing tables idempotently and apply the price-basis migration.

    **The price basis (spec §5.1(b), D30).** A stored close means "as traded on
    ``as_of_date``", but a provider re-states its history after a split, so the value it
    delivers depends on WHEN it was fetched. ``close_raw`` keeps the provider's value
    exactly as delivered and ``split_basis`` records the factor applied to it, so the
    stored close is always ``close_raw × split_basis`` — recomputed, never rescaled in
    place. Nothing authoritative is overwritten (CLAUDE.md #7).

    **Legacy backfill.** A database created before these columns carries rows whose
    close was written with no factor at all, and at migration time the corporate-action
    ledger cannot have applied one to them — so their basis is exactly ``1`` (the column
    default) and their raw value is exactly their stored close. The ``UPDATE`` states
    that rather than leaving a NULL for a later reconcile to guess at. It is guarded on
    ``IS NULL``, so it is a no-op on every subsequent boot and on a fresh database.
    """
    conn.executescript(_DDL)
    _add_column_if_missing(conn, "prices", "close_raw", "TEXT")
    _add_column_if_missing(conn, "prices", "split_basis", "TEXT NOT NULL DEFAULT '1'")
    conn.execute("UPDATE prices SET close_raw = close WHERE close_raw IS NULL")
    # W8: the same treatment for the other three OHLC columns. A pre-W8 row was never
    # multiplied, so its basis really is 1 and its raw value really is its stored value —
    # the UPDATE says so instead of leaving a NULL for a later reconcile to guess at.
    # Guarded on ``IS NULL`` (and on the value being present), so it is a no-op on every
    # later boot and never invents a raw for a column the provider omitted.
    for column in ("open", "high", "low"):
        _add_column_if_missing(conn, "prices", f"{column}_raw", "TEXT")
        conn.execute(
            f"UPDATE prices SET {column}_raw = {column} "
            f"WHERE {column}_raw IS NULL AND {column} IS NOT NULL"
        )
    conn.commit()
