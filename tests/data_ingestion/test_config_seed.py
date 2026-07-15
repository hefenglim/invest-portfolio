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
    assert tw.rounding == "floor"  # FE-D3: unconditional floor to integer NT$
    assert tw.rebate_rate == Decimal("0.77")  # forecast-only; never used by compute_fees


def test_us_fee_rules_v2_shape() -> None:
    schwab = get_fee_rule_set("schwab")
    assert schwab.sec_rate == Decimal("0.0000206") and schwab.taf_cap == Decimal("9.79")
    assert not schwab.has_us_stamp  # Schwab has no MY stamp
    mu = get_fee_rule_set("moomoo_us")
    assert mu.platform_fee == Decimal("0.99") and mu.has_us_stamp
    assert mu.stamp_cap_stock == Decimal("1000") and mu.stamp_cap_etf == Decimal("200")


def test_my_fee_rule_v2_shape() -> None:
    my = get_fee_rule_set("moomoo_my")
    assert my.commission_rate == Decimal("0.0003") and my.platform_fee == Decimal("3.00")
    assert my.sst_rate == Decimal("0.08")
    assert my.stamp_cap_stock == Decimal("1000") and my.stamp_cap_etf == Decimal("0")


def test_default_accounts_reference_valid_fee_sets() -> None:
    for acc in DEFAULT_ACCOUNTS:
        assert get_fee_rule_set(acc.fee_rule_set) is not None
