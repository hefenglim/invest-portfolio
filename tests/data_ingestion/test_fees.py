from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.fees import compute_fees
from portfolio_dash.shared.models.enums import Side

_TW = get_fee_rule_set("tw")


def test_tw_buy_fee_no_tax() -> None:
    r = compute_fees(_TW, Side.BUY, Decimal("1000"), Decimal("600"))
    assert r.fee == Decimal("855") and r.tax == Decimal("0")  # 0.1425% of 600000


def test_tw_sell_normal_tax() -> None:
    r = compute_fees(_TW, Side.SELL, Decimal("1000"), Decimal("600"))
    assert r.fee == Decimal("855") and r.tax == Decimal("1800")  # 0.3% of 600000


def test_tw_sell_etf_tax() -> None:
    r = compute_fees(_TW, Side.SELL, Decimal("1000"), Decimal("600"), is_etf=True)
    assert r.tax == Decimal("600")  # 0.1%


def test_tw_sell_daytrade_tax() -> None:
    r = compute_fees(_TW, Side.SELL, Decimal("1000"), Decimal("600"), daytrade=True)
    assert r.tax == Decimal("900")  # 0.15%


def test_tw_min_fee_enforced() -> None:
    r = compute_fees(_TW, Side.BUY, Decimal("1"), Decimal("10"))
    assert r.fee == Decimal("20")  # min NT$20


def test_tw_fee_rounded_to_integer() -> None:
    r = compute_fees(_TW, Side.SELL, Decimal("1000"), Decimal("593"))
    assert r.fee == Decimal("845")  # 0.1425%*593000=845.025 -> 845
    assert r.tax == Decimal("1779")  # 0.3%*593000


def test_us_sec_fee_on_sell() -> None:
    us = get_fee_rule_set("schwab")  # brokerage 0, sec_fee 0.0000278 (sell-side)
    r = compute_fees(us, Side.SELL, Decimal("100"), Decimal("300"))  # notional 30,000
    assert r.fee == Decimal("0.83")  # 0.0000278*30000=0.834 -> 0.83
    assert r.tax == Decimal("0.00")


def test_my_clearing_capped() -> None:
    my = get_fee_rule_set("moomoo_my")  # brokerage 0.08%, clearing 0.03% cap 1000, stamp 0.1%
    r = compute_fees(my, Side.BUY, Decimal("1000000"), Decimal("10"))  # notional 10,000,000
    # brokerage 0.0008*1e7=8000; clearing 0.0003*1e7=3000 -> capped 1000; fee = 8000+1000.
    assert r.fee == Decimal("9000.00")
    assert r.tax == Decimal("10000.00")  # stamp 0.001*1e7 (no stamp cap set)


def test_snapshot_records_rates() -> None:
    r = compute_fees(_TW, Side.SELL, Decimal("1000"), Decimal("600"))
    assert "tax_rate" in r.snapshot and r.snapshot["tax_rate"] == "0.003"
