"""Which columns are inputs, and every way a row can change them (wave-1 review of package B).

- the exclusion lists equal spec 2026-10-18 §7's, and the watched set is ``PRAGMA table_info``
  minus them (inputs by default);
- every watched column of every trigger table, changed on an existing row, lowers
  ``dirty_from``; every exempt column changes silently;
- the price gate reads OLD and NEW symbol and every ledger; an instrument's day reads its OLD
  and NEW symbol;
- a ``REPLACE`` that removes a conflicting row marks the removed row's day (no DELETE trigger
  fires for it), while an unchanged upsert stays silent;
- a ``DROP COLUMN`` migration on a trigger table does not abort boot;
- an external ``ex_date = ''`` reads as no ex-date.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion import schema
from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import dividend_effective_date
from tests.conftest import init_golden_base
from tests.reference.corpora import open_db
from tests.settlement.conftest import CorpusFactory

TODAY = "2026-06-11"
FIRST = "2026-01-05"

#: Spec §7's exclusion lists, transcribed. ``accounts``: "name / display labels" — the two
#: display labels are ``name`` and ``broker``; ``account_market_rules`` has none.
SPEC_EXEMPT: dict[str, frozenset[str]] = {
    "transactions": frozenset({"fee_rule_snapshot", "note", "import_batch_id",
                               "source_row_hash"}),
    "dividends": frozenset({"import_batch_id", "source_row_hash"}),
    "opening_inventory": frozenset(),
    "cash_movements": frozenset({"note", "import_batch_id", "source_row_hash"}),
    "fx_conversions": frozenset({"import_batch_id", "source_row_hash"}),
    "corporate_actions": frozenset({"child_seed_json", "note", "import_batch_id",
                                    "source_row_hash"}),
    "instruments": frozenset({"name", "sector", "industry", "aliases", "board", "board_status",
                              "target_low", "target_high", "target_set_at", "archived",
                              "etf_flag_unknown", "is_etf"}),
    "accounts": frozenset({"name", "broker"}),
    "account_market_rules": frozenset(),
}
SPEC_INPUTS: dict[str, frozenset[str]] = {
    "prices": frozenset({"close", "instrument", "as_of_date"}),
    "fx_rates": frozenset({"rate", "base", "quote", "as_of_date"}),
}


def _schema_columns() -> dict[str, list[str]]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    out = {t: [str(r[1]) for r in conn.execute(f"PRAGMA table_info({t})")]
           for t in st.TRIGGER_TABLES}
    conn.close()
    return out


COLUMNS = _schema_columns()
CASES = [
    (table, column, column not in SPEC_INPUTS.get(table, frozenset())
     if table in SPEC_INPUTS else column in SPEC_EXEMPT[table])
    for table in st.TRIGGER_TABLES for column in COLUMNS[table]
]


def _settle(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE settle_meta SET generation = 1, settled_through = ?, held_through = ?, "
                 "first_day = ? WHERE id = 1", (TODAY, TODAY, FIRST))
    _clean(conn)


def _clean(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE settle_dirty SET dirty_from = NULL WHERE id = 1")
    conn.execute("DELETE FROM settle_change_log")
    conn.execute("DELETE FROM settle_change_agg")
    conn.commit()


def _dirt(conn: sqlite3.Connection) -> tuple[str | None, int]:
    row = conn.execute("SELECT dirty_from, seq FROM settle_dirty WHERE id = 1").fetchone()
    return (None if row[0] is None else str(row[0])), int(row[1])


@pytest.fixture
def store(corpus_db: CorpusFactory) -> Iterator[sqlite3.Connection]:
    """Golden, settled through its day, with one row in every trigger table."""
    conn = open_db(corpus_db("golden").path)
    try:
        conn.execute("INSERT INTO opening_inventory (account_id, symbol, shares, "
                     "original_cost_total, build_date) VALUES ('tw_broker', '2330', '10', "
                     "'5000', '2026-02-01')")
        conn.execute("INSERT INTO cash_movements (account_id, date, kind, ccy, amount) VALUES "
                     "('tw_broker', '2026-02-02', 'DEPOSIT', 'TWD', '1000')")
        conn.execute("INSERT INTO corporate_actions (account_id, date, kind, from_symbol, "
                     "to_symbol, ratio_to, ratio_from) VALUES ('tw_broker', '2026-04-01', "
                     "'SPLIT', '2330', '2330', '2', '1')")
        conn.commit()
        st.install_or_verify(conn, calc_version_changed=False)
        _settle(conn)
        yield conn
    finally:
        conn.close()


#: The row each table's generic case changes.
_ROW: dict[str, str] = {
    "transactions": "symbol = '2330'",
    "dividends": "symbol = '2330'",
    "opening_inventory": "symbol = '2330'",
    "cash_movements": "1",
    "fx_conversions": "1",
    "corporate_actions": "1",
    "prices": "instrument = '2330' AND as_of_date = '2026-06-09'",
    "fx_rates": "base = 'USD' AND quote = 'TWD' AND as_of_date = '2026-06-09'",
    "instruments": "symbol = '2330'",
    "accounts": "account_id = 'tw_broker'",
    "account_market_rules": "account_id = 'tw_broker'",
}


def _different(value: object, column: str) -> object:
    """A value that differs from *value* (and keeps every key unique)."""
    if value is None:
        return "1"
    if isinstance(value, int):
        return value + 100_000 if column == "id" else value + 1
    text = str(value)
    try:
        date.fromisoformat(text)
    except ValueError:
        return text + "Z"
    return "2000-01-03"


# --- the column sets ---------------------------------------------------------------------------


def test_the_exclusion_lists_are_spec_section_7s() -> None:
    assert st.EXEMPT_COLUMNS == SPEC_EXEMPT
    assert {t: frozenset(c) for t, c in st.INPUT_COLUMNS.items()} == SPEC_INPUTS
    assert set(st.EXEMPT_COLUMNS) | set(st.INPUT_COLUMNS) == set(st.TRIGGER_TABLES)


@pytest.mark.parametrize("table", st.TRIGGER_TABLES)
def test_the_watched_set_is_table_info_minus_the_exclusion_list(
        store: sqlite3.Connection, table: str) -> None:
    columns = [str(r[1]) for r in store.execute(f"PRAGMA table_info({table})")]
    if table in SPEC_INPUTS:
        expected = sorted(SPEC_INPUTS[table])
    else:
        expected = sorted(set(columns) - SPEC_EXEMPT[table])
    assert st.watched_columns(store, table) == expected
    assert SPEC_EXEMPT.get(table, frozenset()) <= set(columns), "an exemption names no column"


@pytest.mark.parametrize(("table", "column", "exempt"), CASES,
                         ids=[f"{t}.{c}" for t, c, _e in CASES])
def test_every_column_change_is_recorded_unless_exempt(
        store: sqlite3.Connection, table: str, column: str, exempt: bool) -> None:
    row = store.execute(f"SELECT rowid, {column} FROM {table} WHERE {_ROW[table]} "
                        "ORDER BY rowid LIMIT 1").fetchone()
    assert row is not None, f"no {table} row to change"
    _, seq = _dirt(store)
    store.execute(f"UPDATE {table} SET {column} = ? WHERE rowid = ?",
                  (_different(row[1], column), row[0]))
    store.commit()
    dirty, after = _dirt(store)
    if exempt:
        assert (dirty, after) == (None, seq), f"{table}.{column} is exempt but recorded"
    else:
        assert dirty is not None and dirty <= TODAY, f"{table}.{column} changed silently"
        assert after == seq + 1


# --- the price gate and the instrument day ----------------------------------------------------


def _price(conn: sqlite3.Connection, symbol: str, day: str) -> None:
    conn.execute("INSERT INTO prices (instrument, market, as_of_date, close, source, fetched_at) "
                 "VALUES (?, 'TW', ?, '10', 'test', '2026-01-01T00:00:00')", (symbol, day))
    conn.commit()


def test_a_price_counts_for_a_symbol_held_only_through_an_opening(
        store: sqlite3.Connection) -> None:
    _price(store, "OPNX", "2026-03-02")
    assert _dirt(store)[0] is None
    store.execute("INSERT INTO opening_inventory (account_id, symbol, shares, original_cost_total, "
                  "build_date) VALUES ('tw_broker', 'OPNX', '1', '1', '2026-01-02')")
    store.commit()
    _clean(store)
    _price(store, "OPNX", "2026-03-03")
    assert _dirt(store)[0] == "2026-03-03"


@pytest.mark.parametrize(("frm", "to"), [("2330", "NOPE"), ("NOPE", "2330")])
def test_a_price_moved_between_a_ledger_and_a_stranger_symbol_fires_both_ways(
        store: sqlite3.Connection, frm: str, to: str) -> None:
    _price(store, "NOPE", "2026-03-04")
    if frm == "2330":
        _price(store, "2330", "2026-03-05")
    _clean(store)
    store.execute("UPDATE prices SET instrument = ?, as_of_date = '2026-03-06' WHERE "
                  "instrument = ? AND as_of_date IN ('2026-03-04', '2026-03-05')", (to, frm))
    store.commit()
    assert _dirt(store)[0] is not None


def test_an_instrument_rename_marks_from_the_old_or_the_new_symbol(
        store: sqlite3.Connection) -> None:
    store.execute("UPDATE instruments SET symbol = '2330R' WHERE symbol = '2330'")  # OLD has rows
    store.commit()
    assert _dirt(store)[0] is not None
    _clean(store)
    insert_transaction(store, account_id="tw_broker", symbol="NEWS", side=Side.BUY,
                       quantity=Decimal("1"), price=Decimal("1"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 17))
    store.execute("INSERT INTO instruments (symbol, market, quote_ccy) VALUES ('IDLE', 'TW', "
                  "'TWD')")
    store.commit()
    _clean(store)
    store.execute("UPDATE instruments SET symbol = 'NEWS' WHERE symbol = 'IDLE'")  # NEW has rows
    store.commit()
    assert _dirt(store)[0] == "2026-02-17"


# --- REPLACE ------------------------------------------------------------------------------------

#: Each REPLACE table's affected-day column (the dividend's is ``date`` for its CASH row).
_DAY_COLUMN = {"transactions": "trade_date", "dividends": "date",
               "opening_inventory": "build_date", "cash_movements": "date",
               "fx_conversions": "date", "corporate_actions": "date"}


def test_the_replace_tables_are_the_ledgers_whose_day_is_outside_their_key() -> None:
    assert set(st.REPLACE_TABLES) == set(_DAY_COLUMN)


@pytest.mark.parametrize("table", sorted(_DAY_COLUMN))
def test_a_replace_that_moves_a_row_later_marks_its_old_day(store: sqlite3.Connection,
                                                            table: str) -> None:
    columns = [str(r[1]) for r in store.execute(f"PRAGMA table_info({table})")]
    day_col = _DAY_COLUMN[table]
    row = store.execute(f"SELECT rowid, {day_col} FROM {table} WHERE {_ROW[table]} "
                        "ORDER BY rowid LIMIT 1").fetchone()
    old_day = str(row[1])
    picked = ", ".join("'2026-06-01'" if c == day_col else c for c in columns)
    store.execute(f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) SELECT {picked} "
                  f"FROM {table} WHERE rowid = ?", (row[0],))
    store.commit()
    assert _dirt(store)[0] == old_day, f"{table}: the removed row's {old_day} was not marked"


_ID_TABLES = sorted(t for t in _DAY_COLUMN if t != "opening_inventory")


def _replace_day(conn: sqlite3.Connection, table: str, rowid: int, day: str) -> None:
    """``INSERT OR REPLACE`` the row *rowid* of *table* with its day column set to *day*."""
    columns = [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")]
    picked = ", ".join("?" if c == _DAY_COLUMN[table] else c for c in columns)
    conn.execute(f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) SELECT {picked} "
                 f"FROM {table} WHERE rowid = ?", (day, rowid))
    conn.commit()


def _log_rows(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [tuple(r) for r in conn.execute(
        "SELECT table_name, op, row_key, old_account_id, new_account_id, old_symbol, "
        "new_symbol, old_day, new_day FROM settle_change_log ORDER BY id")]


@pytest.mark.parametrize("table", _ID_TABLES)
def test_a_replace_that_moves_a_settled_row_past_the_bound_reads_as_its_delete(
        store: sqlite3.Connection, table: str) -> None:
    """The new day is past the bound, so the AFTER INSERT records nothing: the REPLACE trigger
    marks the removed day AND writes the removed row as a ``delete`` with one ``seq``."""
    row = store.execute(f"SELECT rowid, {_DAY_COLUMN[table]} FROM {table} WHERE "
                        f"{_ROW[table]} ORDER BY rowid LIMIT 1").fetchone()
    old_day = str(row[1])
    _, seq = _dirt(store)
    _replace_day(store, table, row[0], "2026-07-01")  # past the bound (settled 06-11)
    assert _dirt(store) == (old_day, seq + 1)
    [line] = _log_rows(store)
    assert (line[0], line[1], line[2]) == (table, "delete", str(row[0]))
    assert (line[4], line[6], line[7], line[8]) == (None, None, old_day, None)
    assert line[3] is not None  # the removed row's account
    page = st.pending_changes(store)
    assert page.total_count == 1 and page.records[0].op == "delete"
    assert page.records[0].seq == seq + 1


@pytest.mark.parametrize("table", _ID_TABLES)
def test_a_replace_inside_the_bound_is_recorded_once_by_its_insert(
        store: sqlite3.Connection, table: str) -> None:
    row = store.execute(f"SELECT rowid, {_DAY_COLUMN[table]} FROM {table} WHERE "
                        f"{_ROW[table]} ORDER BY rowid LIMIT 1").fetchone()
    _, seq = _dirt(store)
    _replace_day(store, table, row[0], "2026-06-01")
    assert _dirt(store) == (str(row[1]), seq + 1)
    assert [line[1] for line in _log_rows(store)] == ["insert"]


def test_a_replace_keeps_an_earlier_dirt(store: sqlite3.Connection) -> None:
    store.execute("UPDATE settle_dirty SET dirty_from = '2026-01-02' WHERE id = 1")
    store.commit()
    tid = store.execute("SELECT id FROM transactions WHERE symbol = 'AAPL'").fetchone()[0]
    _replace_day(store, "transactions", tid, "2026-06-01")  # removed 01-10, new 06-01
    assert _dirt(store)[0] == "2026-01-02"
    tid = store.execute("SELECT id FROM transactions WHERE symbol = '2330'").fetchone()[0]
    _replace_day(store, "transactions", tid, "2026-07-01")  # past the bound
    assert _dirt(store)[0] == "2026-01-02", "a REPLACE never raises dirty_from"


def test_a_replace_of_a_row_past_the_bound_records_nothing(store: sqlite3.Connection) -> None:
    store.execute("INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, "
                  "tax, trade_date) VALUES ('tw_broker', '2330', 'BUY', '1', '1', '0', '0', "
                  "'2026-06-20')")
    store.commit()  # dated after the settled day: the live tail's
    tid = store.execute("SELECT MAX(id) FROM transactions").fetchone()[0]
    _, seq = _dirt(store)
    _replace_day(store, "transactions", tid, "2026-06-25")
    assert _dirt(store) == (None, seq) and _log_rows(store) == []


def test_a_replace_changing_only_a_stock_dividends_ex_date_marks_the_removed_day(
        store: sqlite3.Connection) -> None:
    store.execute("INSERT INTO dividends (account_id, symbol, date, type, gross, withholding, "
                  "net, reinvest_shares, ex_date) VALUES ('tw_broker', '2330', '2026-04-20', "
                  "'STOCK', '0', '0', '0', '5', '2026-03-15')")
    store.commit()
    _clean(store)
    did = store.execute("SELECT MAX(id) FROM dividends").fetchone()[0]
    store.execute("INSERT OR REPLACE INTO dividends (id, account_id, symbol, date, type, gross, "
                  "withholding, net, reinvest_shares, ex_date) SELECT id, account_id, symbol, "
                  "date, type, gross, withholding, net, reinvest_shares, '2026-04-10' FROM "
                  "dividends WHERE id = ?", (did,))
    store.commit()
    assert _dirt(store)[0] == "2026-03-15", "the removed row counted from its ex-date"


@pytest.mark.parametrize("build_date", ["2026-05-01", "2026-07-01"])
def test_a_changed_upsert_opening_is_recorded_exactly_once(store: sqlite3.Connection,
                                                           build_date: str) -> None:
    _, seq = _dirt(store)
    upsert_opening(store, account_id="tw_broker", symbol="2330", shares=Decimal("10"),
                   original_cost_total=Decimal("5000"),
                   build_date=date.fromisoformat(build_date))
    assert _dirt(store) == ("2026-02-01", seq + 1)
    assert [line[1] for line in _log_rows(store)] == ["update"]


def test_the_reviewers_two_replace_moves(store: sqlite3.Connection) -> None:
    store.execute("INSERT INTO opening_inventory (account_id, symbol, shares, original_cost_total, "
                  "build_date) VALUES ('tw_broker', 'AAPL', '5', '500', '2026-02-01')")
    store.commit()
    _clean(store)
    store.execute("REPLACE INTO opening_inventory (account_id, symbol, shares, "
                  "original_cost_total, build_date) VALUES ('tw_broker', 'AAPL', '5', '500', "
                  "'2026-05-01')")
    store.commit()
    assert _dirt(store)[0] == "2026-02-01"
    _clean(store)
    tid, day = store.execute(
        "SELECT id, trade_date FROM transactions ORDER BY trade_date LIMIT 1").fetchone()
    store.execute("INSERT OR REPLACE INTO transactions (id, account_id, symbol, side, quantity, "
                  "price, fees, tax, trade_date) SELECT id, account_id, symbol, side, quantity, "
                  "price, fees, tax, '2026-06-01' FROM transactions WHERE id = ?", (tid,))
    store.commit()
    assert _dirt(store)[0] == day


def test_an_unchanged_upsert_and_an_ordinary_insert_stay_quiet_before_insert(
        store: sqlite3.Connection) -> None:
    _, seq = _dirt(store)
    upsert_opening(store, account_id="tw_broker", symbol="2330", shares=Decimal("10"),
                   original_cost_total=Decimal("5000"), build_date=date(2026, 2, 1))
    assert _dirt(store) == (None, seq)
    upsert_opening(store, account_id="tw_broker", symbol="2330", shares=Decimal("10"),
                   original_cost_total=Decimal("5000"), build_date=date(2026, 5, 1))
    dirty, after = _dirt(store)
    assert dirty == "2026-02-01" and after == seq + 1, "one change, recorded once"
    _clean(store)
    insert_transaction(store, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1"), price=Decimal("1"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 5, 20))
    assert _dirt(store)[0] == "2026-05-20", "an insert without an id conflicts with nothing"


def test_a_reference_replace_marks_through_its_insert_and_an_option_upsert_stays_quiet(
        store: sqlite3.Connection) -> None:
    store.execute("INSERT OR REPLACE INTO instruments (symbol, market, quote_ccy, name) VALUES "
                  "('2330', 'TW', 'USD', 'x')")
    store.commit()
    assert _dirt(store)[0] == FIRST, "the AFTER INSERT marks the symbol's first day"
    _clean(store)
    contract = "XYZ261218P00050000"
    store.execute("INSERT INTO instruments (symbol, market, quote_ccy, asset_type) VALUES "
                  "(?, 'US', 'USD', 'option')", (contract,))
    insert_transaction(store, account_id="schwab", symbol=contract, side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("1"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 3, 2))
    _clean(store)
    upsert_instrument(store, Instrument(symbol=contract, market=Market.US,
                                        quote_ccy=Currency.USD, name="renamed",
                                        sector="Energy"))  # proposes asset_type 'stock'
    assert store.execute("SELECT asset_type FROM instruments WHERE symbol = ?",
                         (contract,)).fetchone()[0] == "option"
    assert _dirt(store)[0] is None


# --- migrations and odd values ------------------------------------------------------------------


def test_a_drop_column_migration_on_a_trigger_table_does_not_abort_boot(
        store: sqlite3.Connection) -> None:
    store.execute("ALTER TABLE transactions ADD COLUMN scratch TEXT")
    st.install_or_verify(store, calc_version_changed=False)  # the triggers now name it
    _clean(store)
    with pytest.raises(sqlite3.OperationalError, match="trigger"):
        store.execute("ALTER TABLE transactions DROP COLUMN scratch")
    schema._drop_column_if_present(store, "transactions", "scratch")
    assert "scratch" not in {str(r[1]) for r in store.execute("PRAGMA table_info(transactions)")}
    left = {str(r[0]) for r in store.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'transactions'")}
    assert not {n for n in left if n.startswith(st.TRIGGER_PREFIX)}
    check = st.install_or_verify(store, calc_version_changed=False)
    assert f"{st.TRIGGER_PREFIX}transactions_upd" in check.created and check.marked_dirty
    assert st.verify_triggers(store) and _dirt(store)[0] == FIRST


def test_a_drop_column_read_by_another_tables_trigger_drops_that_trigger_too(
        store: sqlite3.Connection) -> None:
    """``dividends.ex_date`` is read by the instruments / accounts triggers (an instrument's and
    the ledger's first day): those go too; a trigger that never reads ``dividends`` stays."""
    readers = {str(r[0]) for r in store.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name <> 'dividends' "
        "AND instr(sql, 'dividends') > 0")}
    assert {f"{st.TRIGGER_PREFIX}instruments_ins", f"{st.TRIGGER_PREFIX}accounts_upd"} <= readers
    keep = {f"{st.TRIGGER_PREFIX}transactions_upd", f"{st.TRIGGER_PREFIX}fx_rates_ins",
            f"{st.TRIGGER_PREFIX}cash_movements_del"}
    schema._drop_column_if_present(store, "dividends", "ex_date")
    assert "ex_date" not in {str(r[1]) for r in store.execute("PRAGMA table_info(dividends)")}
    left = {str(r[0]) for r in store.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert not readers & left
    assert keep <= left, "a trigger that does not read the table must survive"


def test_a_recreated_trigger_marks_from_the_earlier_of_the_generation_and_the_ledger(
        store: sqlite3.Connection) -> None:
    store.execute(f"DROP TRIGGER {st.TRIGGER_PREFIX}opening_inventory_ins")
    store.execute("INSERT INTO opening_inventory (account_id, symbol, shares, original_cost_total, "
                  "build_date) VALUES ('schwab', 'AAPL', '1', '100', '2025-11-03')")
    store.commit()  # before the generation's first day, while the trigger was missing
    st.install_or_verify(store, calc_version_changed=False)
    assert _dirt(store)[0] == "2025-11-03"


@pytest.mark.parametrize("div_type", list(DividendType))
def test_an_empty_ex_date_is_no_ex_date(div_type: DividendType) -> None:
    conn = sqlite3.connect(":memory:")
    got = conn.execute(f"SELECT {st.DIVIDEND_EFFECTIVE_DAY_SQL} FROM (SELECT ? AS type, "
                       "'2026-03-01' AS date, '' AS ex_date)", (div_type.value,)).fetchone()[0]
    assert got == dividend_effective_date(div_type, date(2026, 3, 1), None).isoformat()


def test_an_external_empty_ex_date_marks_the_payment_day_and_reads_cleanly(
        store: sqlite3.Connection) -> None:
    store.execute("INSERT INTO dividends (account_id, symbol, date, type, gross, withholding, "
                  "net, reinvest_shares, ex_date) VALUES ('tw_broker', '2330', '2026-04-20', "
                  "'STOCK', '0', '0', '0', '5', '')")
    store.commit()
    assert _dirt(store)[0] == "2026-04-20"
    [line] = st.pending_changes(store).records
    assert line.new_day == date(2026, 4, 20)
    assert st.store_state(store, reporting=Currency.TWD).dirty_from == date(2026, 4, 20)
    assert st._day("") is None
    assert st.first_ledger_day(store) == date(2026, 1, 5)
