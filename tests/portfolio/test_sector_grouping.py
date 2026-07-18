"""FU-D31 grouping seam: build_dashboard canonicalizes sectors for the allocation donut.

The donut (allocation.by_sector / weights) AND the sector_weight alert both consume this
one SectorAllocation, so canonicalizing here fixes both. Holding ROWS keep their raw stored
sector (read-time only; stored rows are not migrated this round).
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

_NOW = datetime(2026, 6, 10, 12, 0)
_USD = Currency.USD


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    create_pricing_tables(c)
    seed_accounts(c)
    yield c
    c.close()


def _seed_one(conn: sqlite3.Connection, symbol: str, sector: str) -> None:
    upsert_instrument(conn, Instrument(symbol=symbol, market=Market.US, quote_ccy=_USD,
                                       sector=sector, name=symbol))
    insert_transaction(conn, account_id="schwab", symbol=symbol, side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 10))
    upsert_prices(conn, [PriceRow(instrument=symbol, market=Market.US,
                                  as_of=date(2026, 6, 9), close=Decimal("100"),
                                  source="test")], fetched_at=_NOW)


def test_sectors_canonicalized_at_the_donut_seam(conn: sqlite3.Connection) -> None:
    # Two Technology synonyms + one blank sector.
    _seed_one(conn, "AAPL", "Tech")
    _seed_one(conn, "MSFT", "Technology")
    _seed_one(conn, "BLNK", "")  # blank → Unclassified bucket

    data = build_dashboard(conn, now=_NOW, reporting=_USD)

    assert data.allocation is not None
    by_sector = data.allocation.by_sector
    # 'Tech' + 'Technology' merged into ONE canonical group; blank surfaced as Unclassified.
    assert set(by_sector.keys()) == {"Technology", "Unclassified"}
    assert by_sector["Technology"] == Decimal("2000")   # AAPL 1000 + MSFT 1000
    assert by_sector["Unclassified"] == Decimal("1000")  # BLNK
    # weights re-sum to 1 over the merged groups (no value lost in relabeling).
    assert data.allocation.weights["Technology"] == Decimal("2000") / Decimal("3000")
    assert data.allocation.weights["Unclassified"] == Decimal("1000") / Decimal("3000")


def test_holding_rows_keep_raw_sector(conn: sqlite3.Connection) -> None:
    """Read-time only: the per-holding sector display is NOT canonicalized (stored value)."""
    _seed_one(conn, "AAPL", "Tech")
    _seed_one(conn, "MSFT", "Technology")
    _seed_one(conn, "BLNK", "")

    data = build_dashboard(conn, now=_NOW, reporting=_USD)
    by_symbol = {h.symbol: h for h in data.holdings}
    assert by_symbol["AAPL"].sector == "Tech"        # raw, unchanged
    assert by_symbol["MSFT"].sector == "Technology"  # raw, unchanged
    assert by_symbol["BLNK"].sector == ""            # raw blank preserved
