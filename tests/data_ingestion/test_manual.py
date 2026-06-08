import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.manual import enter_transaction
from portfolio_dash.data_ingestion.store import list_transactions, upsert_instrument
from portfolio_dash.data_ingestion.validate import TxnInput
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

_TSMC = Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                   sector="Tech", name="台積電")


def _setup(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, _TSMC)


def _inp(side: Side, qty: str, *, fee: Decimal | None = None) -> TxnInput:
    return TxnInput(account_id="tw_broker", symbol="2330", side=side,
                    quantity=Decimal(qty), price=Decimal("600"),
                    trade_date=date(2026, 6, 1), fee=fee)


def test_preview_does_not_write_and_autocomputes_fee(conn: sqlite3.Connection) -> None:
    _setup(conn)
    d = enter_transaction(conn, _inp(Side.BUY, "1000"), confirm=False)
    assert d.written is False
    assert d.fee == Decimal("855") and d.tax == Decimal("0")  # auto-computed TW
    assert list_transactions(conn, account_id="tw_broker") == []


def test_confirm_writes(conn: sqlite3.Connection) -> None:
    _setup(conn)
    d = enter_transaction(conn, _inp(Side.BUY, "1000"), confirm=True)
    assert d.written is True and d.transaction_id is not None
    assert len(list_transactions(conn, account_id="tw_broker")) == 1


def test_provided_fee_overrides_autocompute(conn: sqlite3.Connection) -> None:
    _setup(conn)
    d = enter_transaction(conn, _inp(Side.BUY, "1000", fee=Decimal("10")), confirm=False)
    assert d.fee == Decimal("10")  # override preserved


def test_sell_exceeds_holdings_blocks_until_confirm(conn: sqlite3.Connection) -> None:
    _setup(conn)
    d = enter_transaction(conn, _inp(Side.SELL, "500"), confirm=False)
    assert d.written is False
    assert any(i.kind == "sell_exceeds_holdings" for i in d.issues)
    d2 = enter_transaction(conn, _inp(Side.SELL, "500"), confirm=True)
    assert d2.written is True  # user confirmed the soft issue


def test_unknown_account_hard_block(conn: sqlite3.Connection) -> None:
    _setup(conn)
    inp = TxnInput(account_id="nope", symbol="2330", side=Side.BUY, quantity=Decimal("1"),
                   price=Decimal("600"), trade_date=date(2026, 6, 1))
    d = enter_transaction(conn, inp, confirm=True)
    assert d.written is False  # hard issue blocks even with confirm
    assert any(i.kind == "unknown_account" for i in d.issues)
