"""The settlement store's trigger set (spec 2026-10-18 §7, plan W1-B "Trigger DDL", "Install and
verify", and the "Writers" / "Dates and symbols" / "Trigger set" rows of the test list).

Every test runs on a private copy of the ``golden`` corpus with the triggers installed and a
settled generation 1 through the corpus day (``_settle``), so every past day is inside the
bound and a write either records a change or is silent for a stated reason.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from portfolio_dash.data_ingestion import provenance
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.moomoo_merge import migrate_moomoo_accounts
from portfolio_dash.data_ingestion.store import (
    delete_cash_movement,
    delete_dividend,
    delete_opening,
    delete_transaction,
    insert_cash_movement,
    insert_corporate_action,
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    update_cash_movement,
    update_transaction,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.portfolio import settlement_store as st
from portfolio_dash.pricing.cross import derive_cross_rates
from portfolio_dash.pricing.reconcile import reconcile_prices
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.seed import remove_seed_price, write_seed_price
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import dividend_effective_date
from tests.reference.corpora import open_db
from tests.settlement.conftest import CorpusFactory

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=UTC)
TODAY = "2026-06-11"
FIRST = "2026-01-05"


def _settle(conn: sqlite3.Connection, *, through: str = TODAY) -> None:
    """A settled generation 1 through *through*, clean, trail empty."""
    conn.execute(
        "UPDATE settle_meta SET generation = 1, settled_through = ?, held_through = ?, "
        "first_day = ?, run_id = NULL, run_from = NULL, run_through = NULL WHERE id = 1",
        (through, through, FIRST))
    _clean(conn)


def _clean(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE settle_dirty SET dirty_from = NULL WHERE id = 1")
    conn.execute("DELETE FROM settle_change_log")
    conn.execute("DELETE FROM settle_change_agg")
    conn.commit()


def _dirt(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT dirty_from FROM settle_dirty WHERE id = 1").fetchone()
    return None if row[0] is None else str(row[0])


def _seq(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT seq FROM settle_dirty WHERE id = 1").fetchone()[0])


def _log(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cur = conn.execute("SELECT * FROM settle_change_log ORDER BY id")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def _agg(conn: sqlite3.Connection) -> dict[tuple[str, str], tuple[int, str, str]]:
    return {(str(r[0]), str(r[1])): (int(r[2]), str(r[3]), str(r[4])) for r in conn.execute(
        "SELECT table_name, key, changes, earliest, latest FROM settle_change_agg")}


@pytest.fixture
def store(corpus_db: CorpusFactory) -> Iterator[sqlite3.Connection]:
    db = corpus_db("golden")
    conn = open_db(db.path)
    try:
        st.install_or_verify(conn, calc_version_changed=False)
        _settle(conn)
        yield conn
    finally:
        conn.close()


def _buy(conn: sqlite3.Connection, day: date, *, symbol: str = "2330",
         account: str = "tw_broker", qty: str = "10") -> int:
    return insert_transaction(conn, account_id=account, symbol=symbol, side=Side.BUY,
                              quantity=Decimal(qty), price=Decimal("500"), fees=Decimal("0"),
                              tax=Decimal("0"), trade_date=day)


# --- the stored form and install / verify -----------------------------------------------------


def test_every_expected_trigger_is_stored_in_exactly_its_generated_form(
        store: sqlite3.Connection) -> None:
    expected = st.expected_triggers(store)
    stored = {str(r[0]): str(r[1]) for r in store.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")}
    assert expected, "no trigger generated"
    assert {n for n in stored if n.startswith(st.TRIGGER_PREFIX)} == set(expected)
    for name, sql in expected.items():
        assert stored[name] == sql, name
        assert not sql.endswith(";") and "IF NOT EXISTS" not in sql and "main." not in sql
    tables = {str(r[0]) for r in store.execute(
        "SELECT DISTINCT tbl_name FROM sqlite_master WHERE type = 'trigger'")}
    assert tables == set(st.TRIGGER_TABLES)


def test_a_second_install_recreates_nothing_and_moves_no_schema_version(
        store: sqlite3.Connection) -> None:
    before = store.execute("PRAGMA schema_version").fetchone()[0]
    changes = store.total_changes
    check = st.install_or_verify(store, calc_version_changed=False)
    assert not check.recreated and check == st.TriggerCheck()
    assert store.execute("PRAGMA schema_version").fetchone()[0] == before
    assert store.total_changes == changes
    assert st.verify_triggers(store)


def test_the_first_install_reports_every_trigger_and_marks_nothing(
        corpus_db: CorpusFactory) -> None:
    conn = open_db(corpus_db("golden").path)
    try:
        check = st.install_or_verify(conn, calc_version_changed=False)
        assert check.first_install and check.recreated
        assert set(check.created) == set(st.expected_triggers(conn))
        assert not check.marked_dirty  # generation 0: nothing settled to protect
        assert _log(conn) == [] and _dirt(conn) is None
    finally:
        conn.close()


def test_install_refuses_without_the_settle_tables(corpus_db: CorpusFactory) -> None:
    conn = open_db(corpus_db("golden").path)
    try:
        st.drop_store(conn)
        with pytest.raises(RuntimeError, match="settle tables"):
            st.install_or_verify(conn, calc_version_changed=False)
        conn.execute("UPDATE prices SET close = '1' WHERE instrument = '2330'")  # still writes
    finally:
        conn.close()


def test_a_dropped_trigger_makes_the_store_unverified_and_its_recreation_marks_everything(
        store: sqlite3.Connection) -> None:
    st.record_schema_version(store)
    assert st.triggers_verified(store)
    store.execute(f"DROP TRIGGER {st.TRIGGER_PREFIX}transactions_upd")
    assert not st.triggers_verified(store), "a schema change must read as unverified"
    assert not st.verify_triggers(store)
    # the edit made while the trigger was missing reached nothing
    store.execute("UPDATE transactions SET quantity = '999' WHERE symbol = '2330'")
    store.commit()
    assert _dirt(store) is None
    check = st.install_or_verify(store, calc_version_changed=False)
    assert check.created == (f"{st.TRIGGER_PREFIX}transactions_upd",)
    assert check.tables == ("transactions",) and check.marked_dirty
    assert _dirt(store) == FIRST
    line = _log(store)[-1]
    assert line["table_name"] == "transactions" and line["row_key"] == "*"
    assert json.loads(line["detail"]) == {"kind": st.TRIGGER_REBUILT_KIND,
                                          "text": "觸發器重建：交易"}
    assert not st.triggers_verified(store)  # boot records it LAST
    st.record_schema_version(store)
    assert st.triggers_verified(store) and st.verify_triggers(store)


def test_a_different_trigger_is_replaced_and_a_stale_one_dropped(
        store: sqlite3.Connection) -> None:
    name = f"{st.TRIGGER_PREFIX}dividends_del"
    store.execute(f"DROP TRIGGER {name}")
    store.execute(f"CREATE TRIGGER {name} AFTER DELETE ON dividends BEGIN SELECT 1; END")
    store.execute(f"CREATE TRIGGER {st.TRIGGER_PREFIX}gone AFTER DELETE ON dividends "
                  "BEGIN SELECT 1; END")
    check = st.install_or_verify(store, calc_version_changed=True)
    assert check.replaced == (name,) and check.dropped == (f"{st.TRIGGER_PREFIX}gone",)
    assert not check.marked_dirty and _dirt(store) is None  # a calc change re-settles anyway
    assert st.verify_triggers(store)


def test_a_new_ledger_column_changes_the_text_and_is_an_input_by_default(
        store: sqlite3.Connection) -> None:
    store.execute("ALTER TABLE transactions ADD COLUMN venue TEXT")
    assert not st.verify_triggers(store)
    check = st.install_or_verify(store, calc_version_changed=False)
    assert check.replaced == (f"{st.TRIGGER_PREFIX}transactions_rep",
                              f"{st.TRIGGER_PREFIX}transactions_upd")
    _clean(store)
    store.execute("UPDATE transactions SET venue = 'X' WHERE symbol = '2330'")
    store.commit()
    assert _dirt(store) == FIRST


def test_settling_off_keeps_the_triggers_recording(store: sqlite3.Connection,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PD_SETTLE", "off")
    _buy(store, date(2026, 2, 2))
    assert _dirt(store) == "2026-02-02"


# --- the bound and the dirt ---------------------------------------------------------------------


def test_nothing_is_recorded_before_anything_is_settled(corpus_db: CorpusFactory) -> None:
    conn = open_db(corpus_db("golden").path)
    try:
        st.install_or_verify(conn, calc_version_changed=False)
        _buy(conn, date(2026, 2, 2))
        upsert_prices(conn, [PriceRow(instrument="2330", market=Market.TW,
                                      as_of=date(2026, 2, 2), close=Decimal("590"),
                                      source="test")], fetched_at=NOW)
        assert _dirt(conn) is None and _log(conn) == [] and _agg(conn) == {}
    finally:
        conn.close()


def test_a_change_after_the_settled_range_is_left_to_the_live_tail(
        store: sqlite3.Connection) -> None:
    _settle(store, through="2026-05-31")
    _buy(store, date(2026, 6, 5))
    assert _dirt(store) is None and _log(store) == []
    _buy(store, date(2026, 5, 31))
    assert _dirt(store) == "2026-05-31"


@pytest.mark.parametrize("column", ["held_through", "run_through"])
def test_held_and_run_ranges_extend_the_bound(store: sqlite3.Connection, column: str) -> None:
    _settle(store, through="2026-03-31")
    store.execute(f"UPDATE settle_meta SET {column} = '2026-05-31' WHERE id = 1")
    store.commit()
    _buy(store, date(2026, 5, 20))
    assert _dirt(store) == "2026-05-20"
    _buy(store, date(2026, 6, 2))
    assert _dirt(store) == "2026-05-20"


def test_dirty_from_is_lowered_never_raised_and_seq_counts_every_change(
        store: sqlite3.Connection) -> None:
    seq = _seq(store)
    _buy(store, date(2026, 3, 3))
    _buy(store, date(2026, 4, 4))
    assert _dirt(store) == "2026-03-03"
    _buy(store, date(2026, 2, 2))
    assert _dirt(store) == "2026-02-02"
    assert _seq(store) == seq + 3
    assert [r["seq"] for r in _log(store)] == [seq + 1, seq + 2, seq + 3]


# --- ledger writers --------------------------------------------------------------------------


def test_a_trade_insert_update_and_delete_each_record_one_line(
        store: sqlite3.Connection) -> None:
    tid = _buy(store, date(2026, 2, 3))
    [ins] = _log(store)
    assert (ins["table_name"], ins["op"], ins["row_key"], ins["new_account_id"],
            ins["new_symbol"], ins["new_day"], ins["old_day"]) == (
        "transactions", "insert", str(tid), "tw_broker", "2330", "2026-02-03", None)
    assert json.loads(ins["detail"]) == {"new": {"side": "BUY"}}
    assert ins["written_at"].endswith("Z") and ins["actor"] is None
    _clean(store)
    # a later-date move marks from OLD
    update_transaction(store, tid, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("500"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 5, 5), daytrade=False)
    assert _dirt(store) == "2026-02-03"
    [upd] = _log(store)
    assert (upd["op"], upd["old_day"], upd["new_day"]) == ("update", "2026-02-03", "2026-05-05")
    _clean(store)
    delete_transaction(store, tid)
    [dele] = _log(store)
    assert (dele["op"], dele["old_day"], dele["new_day"], dele["old_symbol"]) == (
        "delete", "2026-05-05", None, "2330")
    assert _dirt(store) == "2026-05-05"


def test_a_trade_note_or_snapshot_edit_is_silent(store: sqlite3.Connection) -> None:
    tid = int(store.execute("SELECT MIN(id) FROM transactions").fetchone()[0])
    store.execute("UPDATE transactions SET note = 'n', fee_rule_snapshot = '{}', "
                  "import_batch_id = 7, source_row_hash = 'h' WHERE id = ?", (tid,))
    store.commit()
    assert _dirt(store) is None and _log(store) == []


def test_both_the_old_and_the_new_symbol_are_named(store: sqlite3.Connection) -> None:
    tid = _buy(store, date(2026, 3, 3))
    _clean(store)
    store.execute("UPDATE transactions SET symbol = 'AAPL', account_id = 'schwab' WHERE id = ?",
                  (tid,))
    store.commit()
    [line] = _log(store)
    assert (line["old_symbol"], line["new_symbol"]) == ("2330", "AAPL")
    assert (line["old_account_id"], line["new_account_id"]) == ("tw_broker", "schwab")


@pytest.mark.parametrize("div_type", list(DividendType))
@pytest.mark.parametrize("ex_date", [None, date(2026, 2, 10), date(2026, 3, 20)])
def test_the_dividend_day_sql_is_the_effective_date(div_type: DividendType,
                                                    ex_date: date | None) -> None:
    pay = date(2026, 3, 1)
    conn = sqlite3.connect(":memory:")
    got = conn.execute(f"SELECT {st.DIVIDEND_EFFECTIVE_DAY_SQL} FROM (SELECT ? AS type, "
                       "? AS date, ? AS ex_date)",
                       (div_type.value, pay.isoformat(),
                        None if ex_date is None else ex_date.isoformat())).fetchone()[0]
    assert got == dividend_effective_date(div_type, pay, ex_date).isoformat()


def test_a_dividend_marks_its_effective_date(store: sqlite3.Connection) -> None:
    insert_dividend(store, account_id="tw_broker", symbol="2330", div_date=date(2026, 4, 20),
                    div_type="STOCK", gross=Decimal("0"), withholding=Decimal("0"),
                    net=Decimal("0"), reinvest_shares=Decimal("10"), ex_date=date(2026, 3, 15))
    assert _dirt(store) == "2026-03-15"
    _clean(store)
    did = insert_dividend(store, account_id="tw_broker", symbol="2330",
                          div_date=date(2026, 4, 20), div_type="CASH", gross=Decimal("10"),
                          withholding=Decimal("0"), net=Decimal("10"),
                          ex_date=date(2026, 3, 15))
    assert _dirt(store) == "2026-04-20", "a cash dividend counts from its payment date"
    _clean(store)
    store.execute("UPDATE dividends SET ex_date = NULL WHERE id = ?", (did,))
    store.commit()
    assert _dirt(store) == "2026-04-20" and _log(store)[0]["op"] == "update"
    _clean(store)
    delete_dividend(store, did)
    assert _dirt(store) == "2026-04-20"


def test_openings_fire_on_any_column_and_an_unchanged_upsert_is_silent(
        store: sqlite3.Connection) -> None:
    upsert_opening(store, account_id="tw_broker", symbol="AAPL", shares=Decimal("3"),
                   original_cost_total=Decimal("300"), build_date=date(2026, 2, 1))
    [line] = _log(store)
    assert (line["row_key"], line["new_day"]) == ("tw_broker|AAPL", "2026-02-01")
    _clean(store)
    upsert_opening(store, account_id="tw_broker", symbol="AAPL", shares=Decimal("3"),
                   original_cost_total=Decimal("300"), build_date=date(2026, 2, 1))
    assert _log(store) == []
    store.execute("UPDATE opening_inventory SET account_id = 'schwab' WHERE symbol = 'AAPL'")
    store.commit()
    assert _dirt(store) == "2026-02-01"
    _clean(store)
    delete_opening(store, "schwab", "AAPL")
    assert _log(store)[0]["op"] == "delete"


def test_cash_movements_fire_except_for_a_note(store: sqlite3.Connection) -> None:
    mid = insert_cash_movement(store, account_id="tw_broker", move_date=date(2026, 2, 2),
                               kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("1000"))
    [line] = _log(store)
    assert json.loads(line["detail"]) == {"new": {"kind": "DEPOSIT", "ccy": "TWD"}}
    assert line["new_symbol"] is None
    _clean(store)
    update_cash_movement(store, mid, account_id="tw_broker", move_date=date(2026, 2, 2),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("1000"), note="memo")
    assert _log(store) == []
    update_cash_movement(store, mid, account_id="tw_broker", move_date=date(2026, 2, 2),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("1001"), note="memo")
    assert _dirt(store) == "2026-02-02"
    _clean(store)
    delete_cash_movement(store, mid)
    assert _log(store)[0]["op"] == "delete"


def test_fx_conversions_fire(store: sqlite3.Connection) -> None:
    insert_fx_conversion(store, account_id="schwab", date=date(2026, 2, 9),
                         from_ccy=Currency.TWD, from_amount=Decimal("3200"),
                         to_ccy=Currency.USD, to_amount=Decimal("100"))
    [line] = _log(store)
    assert line["table_name"] == "fx_conversions" and line["new_day"] == "2026-02-09"


def test_corporate_actions_fire_but_not_on_their_seed_record(
        store: sqlite3.Connection) -> None:
    aid = insert_corporate_action(
        store, account_id="tw_broker", action_date=date(2026, 3, 9),
        kind=CorporateActionKind.SPLIT, from_symbol="2330", to_symbol="2330",
        ratio_to=Decimal("2"), ratio_from=Decimal("1"))
    [line] = _log(store)
    assert (line["new_symbol"], json.loads(line["detail"])) == (
        "2330", {"new": {"kind": "SPLIT", "to_symbol": "2330"}})
    _clean(store)
    store.execute("UPDATE corporate_actions SET child_seed_json = '{}', note = 'n' WHERE id = ?",
                  (aid,))
    store.commit()
    assert _log(store) == []
    store.execute("UPDATE corporate_actions SET ratio_to = '3' WHERE id = ?", (aid,))
    store.commit()
    assert _dirt(store) == "2026-03-09"


# --- prices and FX -----------------------------------------------------------------------------


def _price(conn: sqlite3.Connection, symbol: str, day: date, close: str,
           market: Market = Market.TW) -> None:
    upsert_prices(conn, [PriceRow(instrument=symbol, market=market, as_of=day,
                                  close=Decimal(close), source="test")], fetched_at=NOW)


def test_a_restated_close_counts_and_a_restamp_is_silent(store: sqlite3.Connection) -> None:
    _price(store, "2330", date(2026, 6, 9), "600")  # the stored value: a restamp
    assert _agg(store) == {} and _dirt(store) is None
    _price(store, "2330", date(2026, 6, 9), "601")
    _price(store, "2330", date(2026, 3, 2), "550")
    assert _agg(store) == {("prices", "2330"): (2, "2026-03-02", "2026-06-09")}
    assert _dirt(store) == "2026-03-02"
    assert _log(store) == [], "prices count, they do not log one line per row"


def test_a_price_counts_only_for_a_ledger_symbol_or_a_benchmark(
        store: sqlite3.Connection) -> None:
    _price(store, "9999", date(2026, 3, 2), "10")
    assert _agg(store) == {}
    _price(store, "0050", date(2026, 3, 2), "150")
    _price(store, "^GSPC", date(2026, 3, 3), "5000", Market.US)
    assert set(_agg(store)) == {("prices", "0050"), ("prices", "^GSPC")}


def test_a_spinoff_childs_backfill_fires_through_its_to_symbol(
        store: sqlite3.Connection) -> None:
    _price(store, "CHLD", date(2026, 4, 1), "12")
    assert _agg(store) == {}
    insert_corporate_action(
        store, account_id="tw_broker", action_date=date(2026, 4, 1),
        kind=CorporateActionKind.SPINOFF, from_symbol="2330", to_symbol="CHLD",
        ratio_to=Decimal("1"), ratio_from=Decimal("10"), cost_carry=Decimal("0.1"))
    _clean(store)
    _price(store, "CHLD", date(2026, 4, 2), "13")
    assert _agg(store) == {("prices", "CHLD"): (1, "2026-04-02", "2026-04-02")}


def test_a_price_row_moved_to_another_day_or_symbol_fires(store: sqlite3.Connection) -> None:
    store.execute("UPDATE prices SET as_of_date = '2026-04-01' WHERE instrument = '2330' "
                  "AND as_of_date = '2026-06-09'")
    store.commit()
    assert _agg(store) == {("prices", "2330"): (1, "2026-04-01", "2026-06-09")}
    assert _dirt(store) == "2026-04-01"
    _clean(store)
    store.execute("UPDATE prices SET instrument = 'AAPL', as_of_date = '2026-04-02' "
                  "WHERE instrument = '2330' AND as_of_date = '2026-04-01'")
    store.commit()
    assert _agg(store) == {("prices", "AAPL"): (1, "2026-04-02", "2026-04-02"),
                           ("prices", "2330"): (1, "2026-04-01", "2026-04-01")}
    assert _dirt(store) == "2026-04-01"
    _clean(store)
    store.execute("DELETE FROM prices WHERE instrument = 'AAPL' AND as_of_date = '2026-04-02'")
    store.commit()
    assert _agg(store) == {("prices", "AAPL"): (1, "2026-04-02", "2026-04-02")}


def test_fx_rates_count_per_pair_and_a_restamp_is_silent(store: sqlite3.Connection) -> None:
    fx = [FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 1, 8),
                rate=Decimal("32"), source="other")]
    upsert_fx(store, fx, fetched_at=NOW)
    assert _agg(store) == {}
    upsert_fx(store, [fx[0].model_copy(update={"rate": Decimal("32.1")})], fetched_at=NOW)
    assert _agg(store) == {("fx_rates", "USD/TWD"): (1, "2026-01-08", "2026-01-08")}
    store.execute("UPDATE fx_rates SET quote = 'MYR' WHERE base = 'USD' AND quote = 'TWD' "
                  "AND as_of_date = '2026-01-08'")
    store.commit()
    assert set(_agg(store)) == {("fx_rates", "USD/TWD"), ("fx_rates", "USD/MYR")}


def test_reconcile_cross_rates_and_seeds_fire(store: sqlite3.Connection) -> None:
    reconcile_prices(store, ["2330"], factor_of=lambda s, *, after, through: Decimal(2))
    store.commit()
    assert ("prices", "2330") in _agg(store)
    _clean(store)
    derive_cross_rates(store, fetched_at=NOW)
    assert ("fx_rates", "MYR/TWD") in _agg(store)  # 33 / 4.4 replaces the stored 7
    _clean(store)
    write_seed_price(store, symbol="2330", market=Market.TW, on=date(2026, 4, 7),
                     close=Decimal("555"), tz=UTC)
    assert _agg(store) == {("prices", "2330"): (1, "2026-04-07", "2026-04-07")}
    _clean(store)
    remove_seed_price(store, symbol="2330", on=date(2026, 4, 7))
    assert _agg(store) == {("prices", "2330"): (1, "2026-04-07", "2026-04-07")}


# --- reference data ---------------------------------------------------------------------------


def test_an_instrument_edit_of_a_display_field_is_silent(store: sqlite3.Connection) -> None:
    upsert_instrument(store, Instrument(symbol="2330", market=Market.TW,
                                        quote_ccy=Currency.TWD, sector="Information Technology",
                                        name="台積電", board="TWSE"))
    store.execute("UPDATE instruments SET archived = 1, aliases = '[\"x\"]', "
                  "target_low = '1', target_high = '9', target_set_at = '2026-01-01', "
                  "is_etf = 1, etf_flag_unknown = 1, board_status = 'x', industry = 'y'")
    store.commit()
    assert _dirt(store) is None and _log(store) == []
    store.execute("UPDATE instruments SET quote_ccy = 'USD' WHERE symbol = '2330'")
    store.commit()
    assert _dirt(store) == FIRST  # 2330's first trade


def test_registering_a_symbol_with_ledger_rows_marks_its_first_day(
        store: sqlite3.Connection) -> None:
    _buy(store, date(2026, 2, 17), symbol="NEWS")  # unregistered: excluded from the replay
    _clean(store)
    upsert_instrument(store, Instrument(symbol="NEWS", market=Market.TW,
                                        quote_ccy=Currency.TWD, name="new", sector="Energy"))
    [line] = _log(store)
    assert (line["table_name"], line["op"], line["row_key"], line["new_day"]) == (
        "instruments", "insert", "NEWS", "2026-02-17")
    _clean(store)
    upsert_instrument(store, Instrument(symbol="IDLE", market=Market.TW,
                                        quote_ccy=Currency.TWD, name="idle", sector="Energy"))
    assert _log(store) == [], "an instrument with no ledger row changes no settled day"


def test_registering_an_unregistered_to_symbol_fires_from_its_action(
        store: sqlite3.Connection) -> None:
    insert_corporate_action(
        store, account_id="tw_broker", action_date=date(2026, 3, 30),
        kind=CorporateActionKind.EXCHANGE, from_symbol="2330", to_symbol="NEWCO",
        ratio_to=Decimal("1"), ratio_from=Decimal("1"))
    _clean(store)
    upsert_instrument(store, Instrument(symbol="NEWCO", market=Market.TW,
                                        quote_ccy=Currency.TWD, name="newco", sector="Energy"))
    assert _dirt(store) == "2026-03-30"


def test_boot_seed_accounts_is_silent_and_a_rule_change_resettles_in_full(
        store: sqlite3.Connection) -> None:
    seed_accounts(store)
    assert _dirt(store) is None and _log(store) == []
    store.execute("UPDATE accounts SET name = 'renamed', broker = 'b' WHERE account_id = 'schwab'")
    store.commit()
    assert _log(store) == []
    store.execute("UPDATE accounts SET dividend_model = 'cash' WHERE account_id = 'schwab'")
    store.commit()
    first = store.execute("SELECT MIN(d) FROM (SELECT trade_date d FROM transactions UNION ALL "
                          "SELECT date FROM fx_conversions UNION ALL SELECT date FROM dividends)"
                          ).fetchone()[0]
    assert _dirt(store) == first
    _clean(store)
    store.execute("UPDATE account_market_rules SET fee_rule_set = 'x' "
                  "WHERE account_id = 'schwab'")
    store.commit()
    [line] = _log(store)
    assert line["table_name"] == "account_market_rules" and line["row_key"].startswith("schwab|")


# --- bulk writers -------------------------------------------------------------------------------


def test_an_import_groups_by_batch_and_its_undo_too(store: sqlite3.Connection) -> None:
    batch = provenance.open_batch(store, kind="transactions", csv_text="a,b\n1,2\n")
    ids = []
    for day in (date(2026, 2, 5), date(2026, 2, 6)):
        tid = insert_transaction(store, account_id="tw_broker", symbol="2330", side=Side.BUY,
                                 quantity=Decimal("1"), price=Decimal("500"),
                                 fees=Decimal("0"), tax=Decimal("0"), trade_date=day,
                                 commit=False)
        provenance.stamp_row(store, kind="transactions", row_id=tid, batch_id=batch,
                             source_hash=f"h{tid}")
        ids.append(tid)
    provenance.close_batch(store, batch, row_count=2)
    store.commit()
    lines = _log(store)
    assert [(r["op"], r["import_batch_id"]) for r in lines] == [("insert", str(batch))] * 2
    _clean(store)
    removed = provenance.delete_batch(store, batch, delete_actions=lambda event: None,
                                      guard=lambda rows: True)
    assert removed == 2
    lines = _log(store)
    assert [(r["op"], r["import_batch_id"], r["row_key"]) for r in lines] == [
        ("delete", str(batch), str(i)) for i in ids]
    assert _dirt(store) == "2026-02-05"


def test_the_moomoo_merge_relabel_fires(corpus_db: CorpusFactory) -> None:
    conn = open_db(corpus_db("golden").path)
    try:
        conn.executemany(
            "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
            "fee_rule_set, dividend_model) VALUES (?,?,?,?,?,?,?)",
            [("moomoo_my_us", "Moomoo MY (US)", "Moomoo MY", "USD", "MYR", "moomoo_us",
              "drip_us"),
             ("moomoo_my_my", "Moomoo MY (MY)", "Moomoo MY", "MYR", "MYR", "moomoo_my", "cash")])
        _buy(conn, date(2026, 2, 11), symbol="AAPL", account="moomoo_my_us", qty="1")
        upsert_opening(conn, account_id="moomoo_my_my", symbol="AAPL", shares=Decimal("2"),
                       original_cost_total=Decimal("200"), build_date=date(2026, 1, 20))
        conn.commit()
        st.install_or_verify(conn, calc_version_changed=False)
        _settle(conn)
        assert migrate_moomoo_accounts(conn)
        lines = _log(conn)
        relabel = {(r["table_name"], r["old_account_id"], r["new_account_id"])
                   for r in lines if r["op"] == "update"}
        assert ("transactions", "moomoo_my_us", "moomoo_my") in relabel
        assert ("opening_inventory", "moomoo_my_my", "moomoo_my") in relabel
        assert {("accounts", "delete")} <= {(r["table_name"], r["op"]) for r in lines}
        dirty = _dirt(conn)
        assert dirty is not None and dirty <= "2026-01-20"
    finally:
        conn.close()


def test_a_raw_sqlite3_writer_fires_with_no_actor(store: sqlite3.Connection) -> None:
    path = store.execute("PRAGMA database_list").fetchone()[2]
    raw = sqlite3.connect(path)
    try:
        raw.execute("INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, "
                    "tax, trade_date) VALUES ('tw_broker', '2330', 'BUY', '1', '500', '0', '0', "
                    "'2026-02-24')")
        raw.commit()
    finally:
        raw.close()
    [line] = _log(store)
    assert line["new_day"] == "2026-02-24" and line["actor"] is None
