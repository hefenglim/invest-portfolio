import sqlite3

from portfolio_dash.data_ingestion.config_seed import DEFAULT_ACCOUNTS, seed_accounts
from portfolio_dash.data_ingestion.store import list_accounts
from portfolio_dash.shared.enums import Currency


def test_list_accounts_empty(conn: sqlite3.Connection) -> None:
    assert list_accounts(conn) == []


def test_list_accounts_round_trips_seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    accounts = list_accounts(conn)
    assert {a.account_id for a in accounts} == {ac.account_id for ac in DEFAULT_ACCOUNTS}
    schwab = next(a for a in accounts if a.account_id == "schwab")
    assert schwab.name == "Charles Schwab"
    assert schwab.broker == "Schwab"
    assert schwab.settlement_ccy is Currency.USD
    assert schwab.funding_ccy is Currency.TWD
