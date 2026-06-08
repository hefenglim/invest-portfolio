import sqlite3
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import (
    DEFAULT_ACCOUNTS,
    get_fee_rule_set,
    seed_accounts,
)


def test_seed_accounts_writes_four(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    seed_accounts(conn)  # idempotent
    rows = list(conn.execute("SELECT account_id FROM accounts"))
    ids = {r[0] for r in rows}
    assert ids == {"tw_broker", "schwab", "moomoo_my_us", "moomoo_my_my"}
    assert len(rows) == 4  # no duplicates


def test_tw_fee_rule_defaults() -> None:
    tw = get_fee_rule_set("tw")
    assert tw.brokerage == Decimal("0.001425")
    assert tw.min_fee == Decimal("20")
    assert tw.tax_normal == Decimal("0.003")
    assert tw.tax_etf == Decimal("0.001")
    assert tw.tax_daytrade == Decimal("0.0015")
    assert tw.round_integer is True


def test_default_accounts_reference_valid_fee_sets() -> None:
    for acc in DEFAULT_ACCOUNTS:
        assert get_fee_rule_set(acc.fee_rule_set) is not None
