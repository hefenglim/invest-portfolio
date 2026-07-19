"""R6 (2026-07-19): one-time idempotent sector migration + the nullable industry column.

The migration folds legacy stored sectors into the canonical GICS vocabulary
(Semiconductors → Information Technology, Shipping → Industrials, Healthcare → Health Care,
zh synonyms, …) while leaving unrecognized values and blank/NULL sectors untouched. It runs
at the schema/boot seam (``schema.create_tables``) and must be a no-op on a second pass.
"""

import sqlite3
from decimal import Decimal

from portfolio_dash.data_ingestion.schema import create_tables
from portfolio_dash.data_ingestion.store import (
    get_instrument,
    migrate_instrument_sectors,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


def _inst(symbol: str, sector: str) -> Instrument:
    return Instrument(symbol=symbol, market=Market.US, quote_ccy=Currency.USD,
                      sector=sector, name=symbol)


def _sector(conn: sqlite3.Connection, symbol: str) -> str | None:
    """Read the stored sector via raw SQL — tolerates the genuinely-NULL row (the Instrument
    model requires a str, so get_instrument cannot represent a NULL sector; the migration also
    reads raw SQL, so this mirrors what it sees)."""
    row = conn.execute(
        "SELECT sector FROM instruments WHERE symbol=?", (symbol,)
    ).fetchone()
    assert row is not None
    sector: str | None = row["sector"]
    return sector


def _all_sectors(conn: sqlite3.Connection) -> dict[str, str | None]:
    return {
        r["symbol"]: r["sector"]
        for r in conn.execute("SELECT symbol, sector FROM instruments")
    }


def _seed_mixed(conn: sqlite3.Connection) -> None:
    """Seed rows via upsert (stores the raw sector verbatim — no canonicalization on write)."""
    upsert_instrument(conn, _inst("SEMI", "Semiconductors"))   # -> Information Technology
    upsert_instrument(conn, _inst("SHIP", "Shipping"))         # -> Industrials
    upsert_instrument(conn, _inst("HLTH", "Healthcare"))       # -> Health Care (space added)
    upsert_instrument(conn, _inst("TECH", "Tech"))             # -> Information Technology
    upsert_instrument(conn, _inst("FIN1", "金融"))             # zh synonym -> Financials
    upsert_instrument(conn, _inst("ELEC", "Electronics"))      # unknown -> unchanged
    upsert_instrument(conn, _inst("ETFX", "ETF"))              # already canonical -> unchanged
    upsert_instrument(conn, _inst("FIN2", "Financials"))       # already canonical -> unchanged
    upsert_instrument(conn, _inst("BLNK", ""))                 # blank -> untouched (stays "")
    # A genuinely NULL sector (only reachable via raw SQL — the model field is a str).
    conn.execute(
        "INSERT INTO instruments (symbol, market, quote_ccy, sector, name) "
        "VALUES ('NUL0','US','USD',NULL,'NUL0')"
    )
    conn.commit()


def test_migration_folds_legacy_keys_and_leaves_others(conn: sqlite3.Connection) -> None:
    _seed_mixed(conn)
    migrated = migrate_instrument_sectors(conn)
    assert migrated == 5  # SEMI, SHIP, HLTH, TECH, FIN1

    assert _sector(conn, "SEMI") == "Information Technology"
    assert _sector(conn, "SHIP") == "Industrials"
    assert _sector(conn, "HLTH") == "Health Care"
    assert _sector(conn, "TECH") == "Information Technology"
    assert _sector(conn, "FIN1") == "Financials"
    # Unrecognized / already-canonical / blank / NULL are all left exactly as stored.
    assert _sector(conn, "ELEC") == "Electronics"
    assert _sector(conn, "ETFX") == "ETF"
    assert _sector(conn, "FIN2") == "Financials"
    assert _sector(conn, "BLNK") == ""
    assert _sector(conn, "NUL0") is None


def test_migration_is_idempotent_on_second_run(conn: sqlite3.Connection) -> None:
    _seed_mixed(conn)
    assert migrate_instrument_sectors(conn) == 5
    snapshot = _all_sectors(conn)
    # A second pass finds everything already canonical -> 0 rewrites, nothing changes.
    assert migrate_instrument_sectors(conn) == 0
    assert _all_sectors(conn) == snapshot
    # A third pass for good measure.
    assert migrate_instrument_sectors(conn) == 0


def test_create_tables_runs_the_migration_at_the_boot_seam(
    conn: sqlite3.Connection,
) -> None:
    """A legacy row written between boots is folded when create_tables re-runs (idempotent)."""
    upsert_instrument(conn, _inst("SEMI", "Semiconductors"))
    assert _sector(conn, "SEMI") == "Semiconductors"  # stored verbatim
    create_tables(conn)  # the real boot seam invokes migrate_instrument_sectors
    assert _sector(conn, "SEMI") == "Information Technology"
    # Re-running the boot seam again is a clean no-op.
    create_tables(conn)
    assert _sector(conn, "SEMI") == "Information Technology"


def test_industry_column_round_trips(conn: sqlite3.Connection) -> None:
    """The nullable GICS industry column persists through the instrument CRUD (R6)."""
    upsert_instrument(conn, Instrument(
        symbol="NVDA", market=Market.US, quote_ccy=Currency.USD,
        sector="Information Technology", name="NVIDIA",
        target_low=Decimal("100"), is_etf=False, industry="Semiconductors"))
    saved = get_instrument(conn, "NVDA")
    assert saved is not None
    assert saved.industry == "Semiconductors"

    # Default is None; an update that omits industry leaves the model default (None) written.
    upsert_instrument(conn, Instrument(
        symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
        sector="Information Technology", name="Apple"))
    aapl = get_instrument(conn, "AAPL")
    assert aapl is not None and aapl.industry is None
