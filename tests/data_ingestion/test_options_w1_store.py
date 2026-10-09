"""Options W1 — schema, store and registration (spec §3, OPT-D7, OPT-D22).

Additive columns migrate every existing row to the stock meaning; the store carries
``asset_type`` and ``close_reason`` end to end; ``load_ledger_bundle`` hands the replay each
row's id; an option contract is registered from its symbol alone, with no quote lookup.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.schema import create_tables
from portfolio_dash.data_ingestion.store import (
    get_instrument,
    get_transaction,
    insert_transaction,
    list_instruments,
    list_transactions,
    load_ledger_bundle,
    update_transaction,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.options import OptionSymbolError

_PUT = "XYZ261218P00050000"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    return c


def _cols(c: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in c.execute(f"PRAGMA table_info({table})")]


# --- schema -------------------------------------------------------------------------------


def test_fresh_and_migrated_databases_list_the_new_columns_last() -> None:
    fresh = _conn()
    assert _cols(fresh, "transactions")[-1] == "close_reason"
    assert _cols(fresh, "instruments")[-1] == "asset_type"


def test_the_migration_keeps_every_row_a_stock_and_every_close_reason_null() -> None:
    legacy = sqlite3.connect(":memory:")
    legacy.row_factory = sqlite3.Row
    legacy.executescript(
        "CREATE TABLE instruments (symbol TEXT PRIMARY KEY, market TEXT NOT NULL, "
        "quote_ccy TEXT NOT NULL, sector TEXT, name TEXT);"
        "CREATE TABLE transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "account_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, "
        "quantity TEXT NOT NULL, price TEXT NOT NULL, fees TEXT NOT NULL, tax TEXT NOT NULL, "
        "trade_date TEXT NOT NULL, fee_rule_snapshot TEXT, note TEXT);"
        "INSERT INTO instruments VALUES ('AAPL','US','USD','Technology','Apple');"
        "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
        "trade_date) VALUES ('schwab','AAPL','BUY','10','190','0','0','2026-01-05');")
    create_tables(legacy)
    assert [i.asset_type for i in list_instruments(legacy)] == ["stock"]
    assert [t.close_reason for t in list_transactions(legacy)] == [None]
    assert {r[0] for r in legacy.execute("SELECT asset_type FROM instruments")} == {"stock"}
    assert _cols(legacy, "transactions")[-1] == "close_reason"


# --- store --------------------------------------------------------------------------------


def _option_txn(c: sqlite3.Connection, **over: object) -> int:
    row: dict[str, object] = dict(
        account_id="schwab", symbol=_PUT, side=Side.BUY, quantity=Decimal("100"),
        price=Decimal("0"), fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 12, 18),
        close_reason="expired")
    row.update(over)
    return insert_transaction(c, **row)  # type: ignore[arg-type]


def test_close_reason_round_trips_and_an_edit_keeps_it() -> None:
    c = _conn()
    register_option_contract(c, _PUT)
    tid = _option_txn(c)
    stored = get_transaction(c, tid)
    assert stored is not None and stored.close_reason == "expired"
    assert [t.close_reason for t in list_transactions(c)] == ["expired"]
    assert update_transaction(c, tid, account_id="schwab", symbol=_PUT, side=Side.BUY,
                              quantity=Decimal("100"), price=Decimal("0"), fees=Decimal("0"),
                              tax=Decimal("0"), trade_date=date(2026, 12, 18), daytrade=False)
    again = get_transaction(c, tid)
    assert again is not None and again.close_reason == "expired"   # absent = keep (§4.2)


def test_an_ordinary_trade_has_no_close_reason() -> None:
    c = _conn()
    upsert_instrument(c, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                    sector="", name="Apple"))
    tid = insert_transaction(c, account_id="schwab", symbol="AAPL", side=Side.BUY,
                             quantity=Decimal(1), price=Decimal(190), fees=Decimal(0),
                             tax=Decimal(0), trade_date=date(2026, 1, 5))
    stored = get_transaction(c, tid)
    assert stored is not None and stored.close_reason is None


def test_the_bundle_carries_each_rows_id_and_close_reason() -> None:
    c = _conn()
    register_option_contract(c, _PUT)
    first = _option_txn(c, side=Side.SELL, price=Decimal("2"), close_reason=None,
                        short_sale=True, trade_date=date(2026, 3, 2))
    second = _option_txn(c)
    bundle = load_ledger_bundle(c)
    assert [(t.id, t.close_reason) for t in bundle.transactions] == [
        (first, None), (second, "expired")]
    assert bundle.instruments[_PUT].is_option


def test_asset_type_is_written_and_never_downgraded_by_a_stock_write() -> None:
    c = _conn()
    inst = register_option_contract(c, _PUT)
    assert inst.is_option
    # a writer that built the row without knowing its type (e.g. a rename) keeps the option
    upsert_instrument(c, Instrument(symbol=_PUT, market=Market.US, quote_ccy=Currency.USD,
                                    sector="", name="renamed"))
    got = get_instrument(c, _PUT)
    assert got is not None and got.is_option and got.name == "renamed"


# --- registration (OPT-D7) -------------------------------------------------------------------


def test_registration_from_the_symbol_alone() -> None:
    c = _conn()
    inst = register_option_contract(c, " xyz261218p00050000 ")
    assert (inst.symbol, inst.market, inst.quote_ccy, inst.asset_type) == (
        _PUT, Market.US, Currency.USD, "option")
    assert (inst.name, inst.sector, inst.board, inst.is_etf, inst.etf_flag_unknown) == (
        "XYZ 2026-12-18 50 Put", "", "", False, False)
    assert inst.option is not None and inst.option.strike == Decimal("50")
    stored = get_instrument(c, _PUT)
    assert stored == inst
    # the underlying need not be registered, and registering never quotes or adds it
    assert get_instrument(c, "XYZ") is None


def test_registration_is_idempotent() -> None:
    c = _conn()
    first = register_option_contract(c, _PUT)
    upsert_instrument(c, first.model_copy(update={"name": "kept"}))
    again = register_option_contract(c, _PUT)
    assert again.name == "kept" and len(list_instruments(c)) == 1


def test_registration_inside_a_caller_transaction_does_not_commit() -> None:
    c = _conn()
    register_option_contract(c, _PUT, commit=False)
    c.rollback()
    assert get_instrument(c, _PUT) is None


@pytest.mark.parametrize("symbol", ["XYZ", "2330261218P00050000", "XYZ1261218P00050000", ""])
def test_registration_refuses_a_non_contract(symbol: str) -> None:
    with pytest.raises(OptionSymbolError) as exc:
        register_option_contract(_conn(), symbol)
    assert "選擇權" in str(exc.value)


def test_registration_refuses_a_symbol_already_registered_outside_us() -> None:
    c = _conn()
    upsert_instrument(c, Instrument(symbol=_PUT, market=Market.TW, quote_ccy=Currency.TWD,
                                    sector="", name="odd"))
    with pytest.raises(OptionSymbolError, match="美股"):
        register_option_contract(c, _PUT)


def test_a_legacy_us_row_with_a_contract_symbol_becomes_an_option() -> None:
    c = _conn()
    upsert_instrument(c, Instrument(symbol=_PUT, market=Market.US, quote_ccy=Currency.USD,
                                    sector="", name="typed by hand"))
    inst = register_option_contract(c, _PUT)
    assert inst.is_option and inst.name == "typed by hand"


def test_the_audit_reader_names_the_new_columns_in_words() -> None:
    from portfolio_dash.export.ledger_audit import FIELD_LABELS, _display

    assert FIELD_LABELS["close_reason"] and FIELD_LABELS["asset_type"]
    assert _display("transactions", "close_reason", "expired") == "到期作廢"
    assert _display("instruments", "asset_type", "option") == "選擇權"
