"""Pricing DDL + the price-basis migration on BOTH boot paths (W6a, F-14).

The migration lives in ``pricing/schema.py`` because ``pricing/`` owns ``prices``.
Aimed at ``data_ingestion/schema.py`` — where ``_add_column_if_missing`` happens to
live — it would run inside ``bootstrap_db``, which ``api/app.py`` calls BEFORE
``create_pricing_tables``, and raise ``no such table: prices`` on a fresh database.

Both live sites predate these columns, so the legacy path is the one that matters in
production; the fresh path is what a new install and every hermetic test takes.
"""

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.schema import create_tables
from portfolio_dash.pricing.store import get_latest_price, get_price_history, upsert_prices
from portfolio_dash.shared.enums import Market

_NOW = datetime(2026, 6, 8, 12, 0, 0)

# The ``prices`` DDL exactly as it stood before W6a — the schema both live sites are on.
_LEGACY_PRICES_DDL = """
CREATE TABLE prices (
    instrument TEXT NOT NULL, market TEXT NOT NULL, as_of_date TEXT NOT NULL,
    close TEXT NOT NULL, open TEXT, high TEXT, low TEXT, volume TEXT,
    source TEXT NOT NULL, fetched_at TEXT NOT NULL,
    PRIMARY KEY (instrument, as_of_date)
);
"""


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def test_create_tables_idempotent() -> None:
    c = sqlite3.connect(":memory:")
    create_tables(c)
    create_tables(c)  # second call must not error
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"prices", "fx_rates", "dividend_events"}.issubset(names)


# --- path 1: a fresh 0-byte database ------------------------------------------------


def test_fresh_db_carries_the_basis_columns(tmp_path: Path) -> None:
    """A brand-new file database boots and has both basis columns.

    Uses a real file (not ``:memory:``) so this is the 0-byte first-boot path, and calls
    the pricing DDL on its own — no ``bootstrap_db`` — proving the migration does not
    depend on a table another module creates.
    """
    db = sqlite3.connect(str(tmp_path / "fresh.db"))
    db.row_factory = sqlite3.Row
    create_tables(db)
    cols = _columns(db, "prices")
    assert "close_raw" in cols and "split_basis" in cols
    upsert_prices(db, [PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 5),
                                close=Decimal("100"), source="fake")], fetched_at=_NOW)
    r = get_latest_price(db, "AAPL", now=_NOW)
    assert r is not None and r.value == Decimal("100")
    db.close()


# --- path 2: a database created at the OLD schema -----------------------------------


def _legacy_db() -> sqlite3.Connection:
    """A connection holding one pre-W6a ``prices`` row and no basis columns."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_LEGACY_PRICES_DDL)
    c.execute(
        "INSERT INTO prices (instrument, market, as_of_date, close, open, high, low, "
        "volume, source, fetched_at) VALUES ('2330','TW','2026-06-05','1085','1080',"
        "'1090','1075','27997826','twse','2026-06-05T14:00:00')"
    )
    c.commit()
    return c


def test_legacy_db_gains_the_columns_and_still_reads() -> None:
    """The production path: an existing DB migrates in place and serves reads."""
    c = _legacy_db()
    assert "split_basis" not in _columns(c, "prices")  # precondition: really legacy
    create_tables(c)
    assert {"close_raw", "split_basis"} <= set(_columns(c, "prices"))
    r = get_latest_price(c, "2330", now=_NOW)
    assert r is not None and r.value == Decimal("1085") and r.as_of == date(2026, 6, 5)
    hist = get_price_history(c, "2330", date(2026, 6, 1), date(2026, 6, 30))
    assert [h.value for h in hist] == [Decimal("1085")]
    c.close()


def test_legacy_rows_are_backfilled_to_the_identity_basis() -> None:
    """A migrated row states its basis rather than leaving a NULL to be guessed at.

    At migration the corporate-action ledger cannot have been applied to these rows, so
    their basis is exactly 1 and their raw value is exactly their stored close. Writing
    that down is what makes a migrated row indistinguishable from one the new write seam
    produced for a symbol with no action — and it is what lets W6b's reconcile rebuild
    from ``close_raw`` without a special case for "row predates the column".
    """
    c = _legacy_db()
    create_tables(c)
    row = c.execute("SELECT close, close_raw, split_basis FROM prices").fetchone()
    assert (row["close"], row["close_raw"], row["split_basis"]) == ("1085", "1085", "1")
    c.close()


def test_legacy_migration_is_idempotent_and_does_not_move_a_written_row() -> None:
    """Re-running the migration must not re-touch a row the write seam already owns."""
    c = _legacy_db()
    create_tables(c)
    upsert_prices(c, [PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 5),
                               close=Decimal("0.14166666865348816"), source="fake")],
                  fetched_at=_NOW)
    before = [tuple(r) for r in c.execute(
        "SELECT instrument, close, close_raw, split_basis FROM prices ORDER BY instrument")]
    create_tables(c)  # reboot
    create_tables(c)  # and again
    after = [tuple(r) for r in c.execute(
        "SELECT instrument, close, close_raw, split_basis FROM prices ORDER BY instrument")]
    assert before == after
    # the un-capped raw survives the reboot un-rewritten (W6b rebuilds the close from it)
    assert before == [("2330", "1085", "1085", "1"),
                      ("AAPL", "0.1417", "0.14166666865348816", "1")]
    c.close()


def test_fresh_and_migrated_schemas_agree_column_for_column() -> None:
    """Same names, same ORDER. ``ADD COLUMN`` appends, so the fresh DDL declares the two
    basis columns last as well — otherwise the two populations of database would differ
    in a way no test on either one alone could see."""
    fresh = sqlite3.connect(":memory:")
    create_tables(fresh)
    migrated = _legacy_db()
    create_tables(migrated)
    assert _columns(fresh, "prices") == _columns(migrated, "prices")
    fresh.close()
    migrated.close()
