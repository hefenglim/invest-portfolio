from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import FeeRuleSet, get_fee_rule_set
from portfolio_dash.data_ingestion.fees import compute_fees
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.models.enums import Side

TW = FeeRuleSet(market=Market.TW, brokerage=Decimal("0.001425"), discount=Decimal("1"),
                min_fee=Decimal("20"), tax_normal=Decimal("0.003"),
                tax_etf=Decimal("0.001"), tax_daytrade=Decimal("0.0015"), round_integer=True)
SCHWAB = FeeRuleSet(market=Market.US, sec_fee=Decimal("0.0000278"))
MOOMOO_US = FeeRuleSet(market=Market.US, flat_fee=Decimal("0.99"), sec_fee=Decimal("0.0000278"))
MOOMOO_MY = FeeRuleSet(market=Market.MY, brokerage=Decimal("0.0008"), min_fee=Decimal("3"),
                       clearing=Decimal("0.0003"), clearing_cap=Decimal("1000"),
                       stamp_duty_rate=Decimal("0.001"))


def test_w1_tw_buy() -> None:  # 612500*0.001425=872.8125 -> 873
    r = compute_fees(TW, Side.BUY, Decimal("1000"), Decimal("612.5"))
    assert r.fee == Decimal("873") and r.tax == Decimal("0")


def test_w2_tw_sell_normal() -> None:  # fee 170.43->170; tax 119600*0.003=358.8->359
    r = compute_fees(TW, Side.SELL, Decimal("200"), Decimal("598"))
    assert r.fee == Decimal("170") and r.tax == Decimal("359")


def test_w3_tw_sell_etf() -> None:  # fee 110.01->110; tax 77200*0.001=77.2->77
    r = compute_fees(TW, Side.SELL, Decimal("2000"), Decimal("38.6"), is_etf=True)
    assert r.fee == Decimal("110") and r.tax == Decimal("77")


def test_w4_tw_buy_min_fee() -> None:  # 3860*0.001425=5.5005 -> min 20
    r = compute_fees(TW, Side.BUY, Decimal("100"), Decimal("38.6"))
    assert r.fee == Decimal("20") and r.tax == Decimal("0")


def test_w5_tw_daytrade_sell_halfup_boundary() -> None:  # 119000*0.0015=178.5 -> 179
    r = compute_fees(TW, Side.SELL, Decimal("200"), Decimal("595"), daytrade=True)
    assert r.fee == Decimal("170") and r.tax == Decimal("179")


def test_w6_schwab_sell_sec_fee() -> None:  # 1002.50*0.0000278=0.0278695 -> 0.03
    r = compute_fees(SCHWAB, Side.SELL, Decimal("5"), Decimal("200.50"))
    assert r.fee == Decimal("0.03") and r.tax == Decimal("0.00")


def test_w7_moomoo_us_buy_flat_fee() -> None:  # flat 0.99
    r = compute_fees(MOOMOO_US, Side.BUY, Decimal("10"), Decimal("165.20"))
    assert r.fee == Decimal("0.99") and r.tax == Decimal("0.00")


def test_w8_moomoo_my_buy() -> None:
    # notional 2886: comm 2886*0.0008=2.3088 -> min 3; clearing 2886*0.0003=0.8658;
    # fee = 3 + 0.8658 = 3.8658 -> 3.87. stamp(tax) 2886*0.001=2.886 -> 2.89.
    r = compute_fees(MOOMOO_MY, Side.BUY, Decimal("300"), Decimal("9.62"))
    assert r.fee == Decimal("3.87") and r.tax == Decimal("2.89")


def test_w9_moomoo_my_clearing_cap() -> None:
    # notional 4,000,000: clearing 1200 -> cap 1000; comm 3200; fee = 3200 + 1000 = 4200.
    # stamp(tax) 0.001*4,000,000 = 4000 (no stamp cap set on this rule).
    r = compute_fees(MOOMOO_MY, Side.BUY, Decimal("400000"), Decimal("10"))
    assert r.fee == Decimal("4200.00") and r.tax == Decimal("4000.00")


def test_tw_zero_notional_no_min_fee() -> None:  # guard: notional 0 must not charge min_fee
    r = compute_fees(TW, Side.BUY, Decimal("0"), Decimal("612.5"))
    assert r.fee == Decimal("0")


def test_seeded_schwab_has_sec_fee() -> None:
    assert get_fee_rule_set("schwab").sec_fee == Decimal("0.0000278")


def test_seeded_moomoo_us_has_flat_fee() -> None:
    assert get_fee_rule_set("moomoo_us").flat_fee == Decimal("0.99")


def test_seeded_moomoo_my_rates() -> None:
    r = get_fee_rule_set("moomoo_my")
    assert r.brokerage == Decimal("0.0008") and r.min_fee == Decimal("3")
    assert r.clearing == Decimal("0.0003") and r.clearing_cap == Decimal("1000")
    assert r.stamp_duty_rate == Decimal("0.001")


def test_seeded_moomoo_us_end_to_end_w7() -> None:
    r = compute_fees(get_fee_rule_set("moomoo_us"), Side.BUY, Decimal("10"), Decimal("165.20"))
    assert r.fee == Decimal("0.99")
