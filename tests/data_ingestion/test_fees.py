from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.fees import compute_fees, forecast_tw_rebate
from portfolio_dash.shared.models.enums import Side

_TW = get_fee_rule_set("tw")
_SCHWAB = get_fee_rule_set("schwab")
_MOOMOO_US = get_fee_rule_set("moomoo_us")
_MOOMOO_MY = get_fee_rule_set("moomoo_my")


# ---- TW: floor rounding (FE-D3), min fee, sell-side tax rates ----
def test_tw_buy_fee_floor_no_tax() -> None:  # 0.1425% × 600,000 = 855.0 -> 855
    r = compute_fees(_TW, Side.BUY, Decimal("1000"), Decimal("600"))
    assert r.fee == Decimal("855") and r.tax == Decimal("0")


def test_tw_fee_floors_down_not_halfup() -> None:  # 0.1425%×593,000 = 845.025 -> floor 845
    r = compute_fees(_TW, Side.SELL, Decimal("1000"), Decimal("593"))
    assert r.fee == Decimal("845")
    assert r.tax == Decimal("1779")  # 0.3% × 593,000 = 1779.0


def test_tw_fee_floor_discards_fraction() -> None:  # 0.1425%×612,500 = 872.8125 -> floor 872
    r = compute_fees(_TW, Side.BUY, Decimal("1000"), Decimal("612.5"))
    assert r.fee == Decimal("872")  # was 873 under the old ROUND_HALF_UP regime


def test_tw_sell_normal_tax_floor() -> None:  # 0.3% × 600,000 = 1800
    r = compute_fees(_TW, Side.SELL, Decimal("1000"), Decimal("600"))
    assert r.fee == Decimal("855") and r.tax == Decimal("1800")


def test_tw_sell_etf_tax() -> None:  # 0.1% × 600,000 = 600
    r = compute_fees(_TW, Side.SELL, Decimal("1000"), Decimal("600"), is_etf=True)
    assert r.tax == Decimal("600")


def test_tw_sell_daytrade_tax_floor() -> None:  # 0.15% × 600,000 = 900
    r = compute_fees(_TW, Side.SELL, Decimal("1000"), Decimal("600"), daytrade=True)
    assert r.tax == Decimal("900")


def test_tw_min_fee_enforced() -> None:
    r = compute_fees(_TW, Side.BUY, Decimal("1"), Decimal("10"))
    assert r.fee == Decimal("20")  # min NT$20


def test_tw_forecast_rebate_floor() -> None:
    assert forecast_tw_rebate(Decimal("142"), _TW.rebate_rate) == Decimal("109")
    assert forecast_tw_rebate(Decimal("156"), _TW.rebate_rate) == Decimal("120")


# ---- Schwab: SELL-only SEC + TAF, no tax ----
def test_schwab_buy_zero() -> None:
    r = compute_fees(_SCHWAB, Side.BUY, Decimal("10"), Decimal("100"))
    assert r.fee == Decimal("0") and r.tax == Decimal("0")


def test_schwab_sell_sec_taf() -> None:  # notional 30,000 -> sec 0.62 + taf 0.02
    r = compute_fees(_SCHWAB, Side.SELL, Decimal("100"), Decimal("300"))
    assert r.fee == Decimal("0.64") and r.tax == Decimal("0.00")


# ---- Moomoo US: per-component + MY stamp (FE-D2) ----
def test_moomoo_us_buy_components_and_stamp() -> None:
    r = compute_fees(_MOOMOO_US, Side.BUY, Decimal("30"), Decimal("500"), stamp_fx=Decimal("4.3"))
    assert r.fee == Decimal("5.58") and r.tax == Decimal("15.12")


def test_moomoo_us_missing_fx_stamp_zero() -> None:
    r = compute_fees(_MOOMOO_US, Side.BUY, Decimal("30"), Decimal("500"))
    assert r.tax == Decimal("0") and r.snapshot["stamp_fx_missing"] == "1"


# ---- Moomoo MY: SST + stamp step, ETF exempt, clearing cap ----
def test_moomoo_my_buy_sst_stamp() -> None:
    r = compute_fees(_MOOMOO_MY, Side.BUY, Decimal("1000"), Decimal("9.50"))
    assert r.fee == Decimal("9.40") and r.tax == Decimal("10.00")


def test_moomoo_my_etf_stamp_exempt() -> None:
    r = compute_fees(_MOOMOO_MY, Side.BUY, Decimal("1000"), Decimal("9.50"), is_etf=True)
    assert r.tax == Decimal("0.00")


def test_moomoo_my_clearing_capped() -> None:  # notional 4,000,000 -> clearing 1000, stamp 1000
    r = compute_fees(_MOOMOO_MY, Side.BUY, Decimal("400000"), Decimal("10"))
    assert r.fee == Decimal("2379.24")
    assert r.tax == Decimal("1000.00")


def test_snapshot_records_engine_and_rates() -> None:
    r = compute_fees(_TW, Side.SELL, Decimal("1000"), Decimal("600"))
    assert r.snapshot["engine"] == "v2"
    assert r.snapshot["tax_rate"] == "0.003"
