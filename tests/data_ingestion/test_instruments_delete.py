"""FU-D13 store layer: watchlist delete / archive + the held⇒not-archived seam.

Covers the store helpers directly (the router-level 422 matrix lives in the contract
suite): the ledger-history predicate, the full derived-row cleanup on a true delete, the
audit write, graceful behaviour on a partial DB, and the un-archive-on-booking invariant.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal

import pytest

from portfolio_dash.api.dividend_inbox import ensure_tables as ensure_skip_tables
from portfolio_dash.api.dividend_inbox import mark_skipped
from portfolio_dash.data_ingestion.store import (
    delete_instrument,
    get_instrument,
    has_ledger_history,
    insert_dividend,
    insert_transaction,
    list_ledger_audit,
    set_instrument_archived,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.strategy.target_weights import (
    ensure_target_weights_seeded,
    load_target_weights,
    save_target_weights,
)
from tests.conftest import init_golden_base

_NOW = datetime(2026, 7, 16, 12, 0)


def _inst(symbol: str, *, market: Market = Market.US, ccy: Currency = Currency.USD) -> Instrument:
    return Instrument(symbol=symbol, market=market, quote_ccy=ccy, sector="Tech", name=symbol)


@pytest.fixture
def full_conn() -> Iterator[sqlite3.Connection]:
    """A DB with every table a delete touches (init_golden_base + the two lazily-created
    derived tables), so the cleanup can be asserted end to end."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_golden_base(c)
    ensure_target_weights_seeded(c)
    ensure_skip_tables(c)
    yield c
    c.close()


# --- has_ledger_history matrix ------------------------------------------------


def test_has_ledger_history_never_traded_is_false(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, _inst("WATCH"))
    assert has_ledger_history(conn, "WATCH") is False


def test_has_ledger_history_transaction(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, _inst("TX"))
    insert_transaction(conn, account_id="schwab", symbol="TX", side=Side.BUY,
                       quantity=Decimal("1"), price=Decimal("10"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 5))
    assert has_ledger_history(conn, "TX") is True


def test_has_ledger_history_dividend(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, _inst("DV"))
    insert_dividend(conn, account_id="schwab", symbol="DV", div_date=date(2026, 3, 1),
                    div_type="CASH", gross=Decimal("1"), withholding=Decimal("0"),
                    net=Decimal("1"))
    assert has_ledger_history(conn, "DV") is True


def test_has_ledger_history_opening(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, _inst("OP"))
    upsert_opening(conn, account_id="schwab", symbol="OP", shares=Decimal("5"),
                   original_cost_total=Decimal("50"),
                   build_date=date(2026, 1, 1))
    assert has_ledger_history(conn, "OP") is True


# --- delete_instrument: full derived-row cleanup + audit ----------------------


def _seed_derived(conn: sqlite3.Connection, symbol: str) -> None:
    """Give *symbol* one row in every table delete_instrument must clean."""
    upsert_prices(conn, [PriceRow(instrument=symbol, market=Market.US,
                                  as_of=date(2026, 7, 1), close=Decimal("10"), source="t")],
                  fetched_at=_NOW)
    conn.execute("INSERT INTO dividend_events (instrument, market, ex_date, source, fetched_at) "
                 "VALUES (?,?,?,?,?)", (symbol, "US", "2026-07-01", "t", _NOW.isoformat()))
    conn.execute("INSERT INTO signal_states (symbol, params_version, as_of, updated_at) "
                 "VALUES (?,?,?,?)", (symbol, "v1", _NOW.isoformat(), _NOW.isoformat()))
    conn.execute("INSERT INTO alert_events (rule_id, symbol, fired_at) VALUES (?,?,?)",
                 ("price_target", symbol, _NOW.isoformat()))
    mark_skipped(conn, f"div:schwab:{symbol}:2026-07-01", now=_NOW)
    conn.commit()
    # NOTE: target_weights is a single shared JSON map (not per-symbol rows) — set by the
    # individual tests below, since a per-symbol save() here would overwrite the whole map.


def test_delete_instrument_removes_symbol_and_all_derived_rows(
    full_conn: sqlite3.Connection,
) -> None:
    upsert_instrument(full_conn, _inst("GONE"))
    _seed_derived(full_conn, "GONE")
    save_target_weights(full_conn, {"GONE": Decimal("0.10")}, now=_NOW)
    # sanity: rows exist before
    assert get_instrument(full_conn, "GONE") is not None

    assert delete_instrument(full_conn, "GONE") is True

    assert get_instrument(full_conn, "GONE") is None
    counts = {
        "prices": "SELECT COUNT(*) FROM prices WHERE instrument='GONE'",
        "dividend_events": "SELECT COUNT(*) FROM dividend_events WHERE instrument='GONE'",
        "signal_states": "SELECT COUNT(*) FROM signal_states WHERE symbol='GONE'",
        "alert_events": "SELECT COUNT(*) FROM alert_events WHERE symbol='GONE'",
        "pending_dividend_skips":
            "SELECT COUNT(*) FROM pending_dividend_skips WHERE fingerprint LIKE 'div:%:GONE:%'",
    }
    for table, sql in counts.items():
        assert full_conn.execute(sql).fetchone()[0] == 0, f"{table} not cleaned"
    assert "GONE" not in load_target_weights(full_conn)


def test_delete_instrument_preserve_market_data_keeps_prices(
    full_conn: sqlite3.Connection,
) -> None:
    """FU-D32 benchmark guard: preserve_market_data=True keeps the market-data rows (prices /
    dividend_events, keyed instrument) so a benchmark series under the same key survives, while
    the registry row + every PERSONAL artifact (signals / alerts / skips / target weights) are
    still removed."""
    upsert_instrument(full_conn, _inst("0050", market=Market.TW, ccy=Currency.TWD))
    _seed_derived(full_conn, "0050")
    save_target_weights(full_conn, {"0050": Decimal("0.10")}, now=_NOW)

    assert delete_instrument(full_conn, "0050", preserve_market_data=True) is True

    # registry row gone; market data SURVIVES
    assert get_instrument(full_conn, "0050") is None
    assert full_conn.execute(
        "SELECT COUNT(*) FROM prices WHERE instrument='0050'").fetchone()[0] == 1
    assert full_conn.execute(
        "SELECT COUNT(*) FROM dividend_events WHERE instrument='0050'").fetchone()[0] == 1
    # personal artifacts still cleaned
    assert full_conn.execute(
        "SELECT COUNT(*) FROM signal_states WHERE symbol='0050'").fetchone()[0] == 0
    assert full_conn.execute(
        "SELECT COUNT(*) FROM alert_events WHERE symbol='0050'").fetchone()[0] == 0
    assert full_conn.execute(
        "SELECT COUNT(*) FROM pending_dividend_skips WHERE fingerprint LIKE 'div:%:0050:%'"
    ).fetchone()[0] == 0
    assert "0050" not in load_target_weights(full_conn)


def test_delete_instrument_writes_audit(full_conn: sqlite3.Connection) -> None:
    upsert_instrument(full_conn, _inst("AUD"))
    delete_instrument(full_conn, "AUD")
    audit = list_ledger_audit(full_conn, table_name="instruments")
    assert any(a["row_id"] == "AUD" and a["action"] == "delete" for a in audit)


def test_delete_instrument_keeps_other_symbols(full_conn: sqlite3.Connection) -> None:
    upsert_instrument(full_conn, _inst("KEEP"))
    upsert_instrument(full_conn, _inst("DROP"))
    _seed_derived(full_conn, "KEEP")
    _seed_derived(full_conn, "DROP")
    save_target_weights(full_conn, {"KEEP": Decimal("0.10"), "DROP": Decimal("0.20")}, now=_NOW)
    delete_instrument(full_conn, "DROP")
    # KEEP's derived rows survive; only DROP's target-weight entry is pruned.
    assert full_conn.execute(
        "SELECT COUNT(*) FROM prices WHERE instrument='KEEP'").fetchone()[0] == 1
    weights = load_target_weights(full_conn)
    assert weights.get("KEEP") == Decimal("0.10") and "DROP" not in weights


def test_delete_instrument_missing_tables_ok(conn: sqlite3.Connection) -> None:
    """A partial DB (only the ledger tables) must not crash the cleanup — the derived
    tables simply do not exist yet; the instruments row is still removed."""
    upsert_instrument(conn, _inst("BARE"))
    assert delete_instrument(conn, "BARE") is True
    assert get_instrument(conn, "BARE") is None


# --- held ⇒ not archived: booking un-archives (the single seam) ---------------


def test_insert_transaction_unarchives(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, _inst("ARCH"))
    set_instrument_archived(conn, "ARCH", True)
    assert get_instrument(conn, "ARCH").archived is True  # type: ignore[union-attr]
    insert_transaction(conn, account_id="schwab", symbol="ARCH", side=Side.BUY,
                       quantity=Decimal("1"), price=Decimal("10"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 5))
    assert get_instrument(conn, "ARCH").archived is False  # type: ignore[union-attr]


def test_upsert_opening_unarchives(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, _inst("ARCO"))
    set_instrument_archived(conn, "ARCO", True)
    upsert_opening(conn, account_id="schwab", symbol="ARCO", shares=Decimal("5"),
                   original_cost_total=Decimal("50"),
                   build_date=date(2026, 1, 1))
    assert get_instrument(conn, "ARCO").archived is False  # type: ignore[union-attr]


# --- archived column round-trips + is preserved by an edit --------------------


def test_set_and_read_archived(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, _inst("FLAG"))
    assert get_instrument(conn, "FLAG").archived is False  # type: ignore[union-attr]
    assert set_instrument_archived(conn, "FLAG", True) is True
    assert get_instrument(conn, "FLAG").archived is True  # type: ignore[union-attr]
    assert set_instrument_archived(conn, "FLAG", False) is True
    assert get_instrument(conn, "FLAG").archived is False  # type: ignore[union-attr]


def test_set_archived_unknown_symbol_returns_false(conn: sqlite3.Connection) -> None:
    assert set_instrument_archived(conn, "NOPE", True) is False


def test_upsert_instrument_preserves_archived(conn: sqlite3.Connection) -> None:
    """A metadata edit (PUT flow → upsert_instrument) must NOT reset the archived flag."""
    upsert_instrument(conn, _inst("EDIT"))
    set_instrument_archived(conn, "EDIT", True)
    current = get_instrument(conn, "EDIT")
    assert current is not None
    upsert_instrument(conn, current.model_copy(update={"sector": "NewSector"}))
    saved = get_instrument(conn, "EDIT")
    assert saved is not None
    assert saved.archived is True and saved.sector == "NewSector"
