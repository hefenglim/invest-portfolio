"""Fee-engine v2 worked examples (2026-07-15) — the arbitration numbers for each broker.

Sources: docs/reference/broker-fee-schedules-2026-07.md (owner schedules) +
docs/reports/2026-07-15-fee-engine-v2-minispec.md (Wave A translation). The 群益
charge-first walk is reproduced verbatim (buy 142; sell fee 156, tax 330; rebate 109/120).
"""

from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.fees import compute_fees, forecast_tw_rebate
from portfolio_dash.shared.models.enums import Side

TW = get_fee_rule_set("tw")
SCHWAB = get_fee_rule_set("schwab")
MOOMOO_US = get_fee_rule_set("moomoo_us")
MOOMOO_MY = get_fee_rule_set("moomoo_my")


# ---------------------------------------------------------------- TW 群益 charge-first walk
def test_gunyi_buy_floor() -> None:  # 100,000 × 0.1425% = 142.5 -> floor 142 (FE-D3)
    r = compute_fees(TW, Side.BUY, Decimal("1000"), Decimal("100"))
    assert r.fee == Decimal("142") and r.tax == Decimal("0")


def test_gunyi_sell_floor_and_tax() -> None:  # fee 156.75->156; tax 0.3%×110,000=330
    r = compute_fees(TW, Side.SELL, Decimal("1000"), Decimal("110"))
    assert r.fee == Decimal("156") and r.tax == Decimal("330")


def test_gunyi_rebate_forecast_floor() -> None:  # floor(142×0.77)=109, floor(156×0.77)=120
    assert forecast_tw_rebate(Decimal("142"), TW.rebate_rate) == Decimal("109")
    assert forecast_tw_rebate(Decimal("156"), TW.rebate_rate) == Decimal("120")


def test_rebate_rate_never_used_by_compute_fees() -> None:
    # FE-D1: compute_fees charges the FULL price; the rebate never lowers fee/tax.
    r = compute_fees(TW, Side.BUY, Decimal("1000"), Decimal("100"))
    assert r.fee == Decimal("142")  # not 142×0.23
    assert r.snapshot["rebate_rate"] == "0.77"  # recorded for the forecaster only


def test_tw_min_fee_after_floor() -> None:  # 3,860 × 0.1425% = 5.5 -> floor 5 -> min 20
    r = compute_fees(TW, Side.BUY, Decimal("100"), Decimal("38.6"))
    assert r.fee == Decimal("20")


def test_tw_min_fee_boundary_floor_below_20() -> None:  # notional s.t. floor is 19 -> min 20
    # 13,965 × 0.1425% = 19.900125 -> floor 19 -> max(19,20)=20
    r = compute_fees(TW, Side.BUY, Decimal("100"), Decimal("139.65"))
    assert r.fee == Decimal("20")


def test_tw_sell_etf_tax_floor() -> None:  # 0.1% × 7,000 = 7.0 -> 7
    r = compute_fees(TW, Side.SELL, Decimal("50"), Decimal("140"), is_etf=True)
    assert r.fee == Decimal("20") and r.tax == Decimal("7")


def test_tw_sell_daytrade_tax_floor() -> None:  # 0.15% × 119,000 = 178.5 -> floor 178
    r = compute_fees(TW, Side.SELL, Decimal("200"), Decimal("595"), daytrade=True)
    assert r.tax == Decimal("178")


# ---------------------------------------------------------------- Schwab (US, SELL-only regs)
def test_schwab_buy_zero() -> None:
    r = compute_fees(SCHWAB, Side.BUY, Decimal("100"), Decimal("180"))
    assert r.fee == Decimal("0") and r.tax == Decimal("0")


def test_schwab_sell_sec_taf() -> None:
    # notional 30,000: sec=max(0.0000206×30000=0.618,0.01)=0.62; taf=max(0.000195×100,0.01)=0.02
    r = compute_fees(SCHWAB, Side.SELL, Decimal("100"), Decimal("300"))
    assert r.fee == Decimal("0.64") and r.tax == Decimal("0.00")


def test_schwab_sell_taf_cap() -> None:  # 100,000 shares: taf capped at 9.79; sec 20.60
    r = compute_fees(SCHWAB, Side.SELL, Decimal("100000"), Decimal("10"))
    assert r.fee == Decimal("30.39")  # 20.60 + 9.79


# ---------------------------------------------------------------- Moomoo US (+ MY stamp FE-D2)
def test_moomoo_us_buy_components() -> None:
    # 30@500 notional 15,000: comm max(4.5,0.01)=4.50; platform 0.99; settle min(0.09,150)=0.09;
    # cat 0.000003×30=0.00; fee=5.58. stamp: ceil(15000×4.3/1000)=65 -> 65/4.3=15.12
    r = compute_fees(MOOMOO_US, Side.BUY, Decimal("30"), Decimal("500"), stamp_fx=Decimal("4.3"))
    assert r.fee == Decimal("5.58") and r.tax == Decimal("15.12")


def test_moomoo_us_sell_adds_sec_taf() -> None:
    # 25@600 notional 15,000: base 5.57 (settle 0.075->0.08) + sec 0.31 + taf 0.01 = 5.89
    r = compute_fees(MOOMOO_US, Side.SELL, Decimal("25"), Decimal("600"), stamp_fx=Decimal("4.3"))
    assert r.fee == Decimal("5.89") and r.tax == Decimal("15.12")


def test_moomoo_us_settlement_cap() -> None:
    # 1000@0.10 notional 100: settle min(0.003×1000=3.00, 0.01×100=1.00)=1.00 (cap wins)
    r = compute_fees(MOOMOO_US, Side.BUY, Decimal("1000"), Decimal("0.10"), stamp_fx=Decimal("4.3"))
    assert r.fee == Decimal("2.02")  # 0.03 + 0.99 + 1.00 + 0.00


def test_moomoo_us_stamp_missing_fx_degrades() -> None:
    # No USD/MYR rate -> stamp 0 + snapshot note; the fee is unaffected.
    r = compute_fees(MOOMOO_US, Side.BUY, Decimal("30"), Decimal("500"), stamp_fx=None)
    assert r.tax == Decimal("0") and r.snapshot["stamp_fx_missing"] == "1"
    assert r.fee == Decimal("5.58")


def test_moomoo_us_etf_stamp_cap_200() -> None:
    # ETF stamp cap RM200 (US), not exempt. 10,000,000 notional × 4.5 -> capped 200 -> 44.44
    r = compute_fees(MOOMOO_US, Side.BUY, Decimal("100000"), Decimal("100"),
                     is_etf=True, stamp_fx=Decimal("4.5"))
    assert r.tax == Decimal("44.44")  # 200 / 4.5


# ---------------------------------------------------------------- Moomoo MY (native MYR)
def test_moomoo_my_buy_with_sst_and_stamp() -> None:
    # 1000@9.50 notional 9,500: comm 2.85 + platform 3.00 + clearing 2.85 + sst 0.70 = 9.40;
    # stamp ceil(9500/1000)=10 -> 10.00
    r = compute_fees(MOOMOO_MY, Side.BUY, Decimal("1000"), Decimal("9.50"))
    assert r.fee == Decimal("9.40") and r.tax == Decimal("10.00")


def test_moomoo_my_sell_stamp_step() -> None:
    # 400@11.00 notional 4,400: comm 1.32 + 3.00 + clearing 1.32 + sst 0.45 = 6.09;
    # stamp ceil(4400/1000)=5 -> 5.00
    r = compute_fees(MOOMOO_MY, Side.SELL, Decimal("400"), Decimal("11.00"))
    assert r.fee == Decimal("6.09") and r.tax == Decimal("5.00")


def test_moomoo_my_etf_stamp_exempt() -> None:  # MY ETF stamp = RM0 (cap 0)
    r = compute_fees(MOOMOO_MY, Side.BUY, Decimal("1000"), Decimal("9.50"), is_etf=True)
    assert r.tax == Decimal("0.00") and r.fee == Decimal("9.40")


def test_moomoo_my_clearing_and_stamp_caps() -> None:
    # 400000@10 notional 4,000,000: clearing capped 1000, stamp capped 1000
    r = compute_fees(MOOMOO_MY, Side.BUY, Decimal("400000"), Decimal("10"))
    assert r.fee == Decimal("2379.24") and r.tax == Decimal("1000.00")


# ---------------------------------------------------------------- guards / config
def test_zero_notional_no_fees() -> None:
    assert compute_fees(TW, Side.BUY, Decimal("0"), Decimal("100")).fee == Decimal("0")
    assert compute_fees(MOOMOO_US, Side.BUY, Decimal("0"), Decimal("100")).fee == Decimal("0")
    assert compute_fees(MOOMOO_MY, Side.BUY, Decimal("0"), Decimal("100")).fee == Decimal("0")


def test_snapshot_engine_v2_and_components() -> None:
    r = compute_fees(MOOMOO_US, Side.SELL, Decimal("25"), Decimal("600"), stamp_fx=Decimal("4.3"))
    s = r.snapshot
    assert s["engine"] == "v2"
    for k in ("commission", "platform", "settlement", "cat", "sec", "taf",
              "stamp_fx_rate", "stamp_myr", "stamp_usd"):
        assert k in s
