"""Options W3 — the option fee branch (OPT-D6, spec §4.2 / §8).

Per-RULE-SET ``option_per_contract`` — ``schwab`` 0.65, ``moomoo_us`` 0.8633 (owner ruling
2026-10-10: every rule set has ONE default value, so an option trade always carries a fee; the
Moomoo schedule's fixed per-contract items summed) — an optional ``option_reg_per_contract``
(default 0), SEC on sells on the premium notional, no MY stamp on an option in v1, and 0 / 0
at notional 0 (a close row never reaches the engine, but the branch is safe if one does).
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion import fee_overrides, fees
from portfolio_dash.data_ingestion.config_seed import FEE_RULES, get_fee_rule_set
from portfolio_dash.data_ingestion.fees import compute_fees, option_trade_fees
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.models.enums import Side


def test_the_two_new_fields_and_their_seeds() -> None:
    assert FEE_RULES["schwab"].option_per_contract == Decimal("0.65")
    # 0.20 commission + 0.45 platform + 0.18 settlement + 0.02 clearing + 0.013 ORF + 0.0003 CAT
    assert FEE_RULES["moomoo_us"].option_per_contract == Decimal("0.8633")
    for name in FEE_RULES:
        assert FEE_RULES[name].option_reg_per_contract == Decimal("0")


def test_every_us_rule_set_states_its_option_rate() -> None:
    """Owner ruling 2026-10-10: no "unset" — and no US rule set may INHERIT the model's inert
    0 (the TW / MY default, which the option branch never reads): a new US rule set that
    forgot its rate would book every option fee-free without a word."""
    us = [n for n, r in FEE_RULES.items() if r.market is Market.US]
    assert us == ["schwab", "moomoo_us"]
    for name in us:
        assert "option_per_contract" in FEE_RULES[name].model_fields_set, name
        assert FEE_RULES[name].option_per_contract > 0, name


def test_the_unset_vocabulary_is_gone() -> None:
    for gone in ("OptionFeeUnset", "OPTION_FEE_UNSET_MESSAGE", "option_fee_unset_applies",
                 "OptionFees"):
        assert not hasattr(fees, gone), gone


def test_a_buy_is_per_contract_only() -> None:
    fr = compute_fees(FEE_RULES["schwab"], Side.BUY, Decimal("200"), Decimal("1.50"),
                      is_option=True)
    assert (fr.fee, fr.tax) == (Decimal("1.30"), Decimal("0"))
    assert fr.snapshot["option"] == "true"
    assert fr.snapshot["contracts"] == "2"
    assert fr.snapshot["option_per_contract"] == "0.65"


def test_a_sell_adds_sec_on_the_premium_notional() -> None:
    fr = compute_fees(FEE_RULES["schwab"], Side.SELL, Decimal("1000"), Decimal("12.00"),
                      is_option=True)
    # 10 × 0.65 = 6.50; SEC = 0.0000206 × 12,000 = 0.2472 → 0.25; no TAF on an option.
    assert fr.fee == Decimal("6.75")
    assert fr.snapshot["sec"] == "0.25"
    assert "taf" not in fr.snapshot
    assert fr.tax == Decimal("0")


def test_the_regulatory_per_contract_is_added_when_set() -> None:
    rules = FEE_RULES["schwab"].model_copy(update={"option_reg_per_contract": Decimal("0.04")})
    fr = compute_fees(rules, Side.BUY, Decimal("300"), Decimal("1"), is_option=True)
    assert fr.fee == Decimal("1.95") + Decimal("0.12")


def test_notional_zero_is_zero_zero() -> None:
    fr = compute_fees(FEE_RULES["schwab"], Side.BUY, Decimal("200"), Decimal("0"),
                      is_option=True)
    assert (fr.fee, fr.tax) == (Decimal("0"), Decimal("0"))


def test_no_my_stamp_on_an_option() -> None:
    fr = compute_fees(FEE_RULES["moomoo_us"], Side.SELL, Decimal("100"), Decimal("5"),
                      is_option=True, stamp_fx=Decimal("4.4"))
    assert fr.tax == Decimal("0")
    assert not any(k.startswith("stamp") for k in fr.snapshot)


def test_moomoo_us_computes_its_default_rate() -> None:
    """A moomoo_us option trade with no statement fee: 0.8633 × contracts, cent-rounded, + SEC
    on a sell — none of the per-ORDER stock components (platform 0.99, settlement, CAT)."""
    rules = FEE_RULES["moomoo_us"]
    bto = compute_fees(rules, Side.BUY, Decimal("100"), Decimal("1"), is_option=True)
    assert (bto.fee, bto.tax) == (Decimal("0.86"), Decimal("0"))      # 0.8633 → 0.86
    assert bto.snapshot["option_per_contract"] == "0.8633"
    assert "platform" not in bto.snapshot and "settlement" not in bto.snapshot
    sto = compute_fees(rules, Side.SELL, Decimal("300"), Decimal("2.50"), is_option=True)
    # 3 × 0.8633 = 2.5899 → 2.59; SEC = 0.0000206 × 750 = 0.01545 → 0.02
    assert (sto.fee, sto.snapshot["sec"]) == (Decimal("2.61"), "0.02")
    big = compute_fees(rules, Side.SELL, Decimal("1000"), Decimal("12.00"), is_option=True)
    # 10 × 0.8633 = 8.633 → 8.63; SEC = 0.0000206 × 12,000 = 0.2472 → 0.25
    assert big.fee == Decimal("8.88")


def test_the_doors_one_definition_fills_a_blank_fee_and_keeps_a_supplied_one() -> None:
    rules = FEE_RULES["moomoo_us"]
    auto = option_trade_fees(rules, Side.SELL, Decimal("100"), Decimal("1.50"),
                             fee=None, tax=None)
    # 0.8633 → 0.86; SEC = 0.0000206 × 150 = 0.00309 → min 0.01
    assert (auto.fee, auto.tax) == (Decimal("0.87"), Decimal("0"))
    assert "supplied" not in auto.snapshot and auto.snapshot["option"] == "true"
    stated = option_trade_fees(rules, Side.SELL, Decimal("100"), Decimal("1.50"),
                               fee=Decimal("1.99"), tax=None)
    assert (stated.fee, stated.tax) == (Decimal("1.99"), Decimal("0"))
    assert stated.snapshot["supplied"] == "fee"


def test_a_stock_trade_is_untouched_by_the_flag_default() -> None:
    a = compute_fees(FEE_RULES["schwab"], Side.SELL, Decimal("10"), Decimal("210"))
    b = compute_fees(FEE_RULES["schwab"], Side.SELL, Decimal("10"), Decimal("210"),
                     is_option=False)
    assert a == b
    assert "option" not in a.snapshot


# --- the overlay ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    return c


def test_both_fields_are_editable_and_ordered() -> None:
    assert {"option_per_contract", "option_reg_per_contract"} <= fee_overrides.EDITABLE_FIELDS
    order = fee_overrides.EDITABLE_FIELD_ORDER
    assert order.index("option_reg_per_contract") == order.index("option_per_contract") + 1


def test_the_rate_can_be_set_and_a_null_reverts_to_the_default(
    conn: sqlite3.Connection,
) -> None:
    fee_overrides.set_overrides(conn, "moomoo_us", {"option_per_contract": "0.50"})
    assert get_fee_rule_set("moomoo_us", conn).option_per_contract == Decimal("0.50")
    fee_overrides.set_overrides(conn, "moomoo_us", {"option_per_contract": None})
    assert get_fee_rule_set("moomoo_us", conn).option_per_contract == Decimal("0.8633")
    fee_overrides.set_overrides(conn, "schwab", {"option_per_contract": "0"})
    assert get_fee_rule_set("schwab", conn).option_per_contract == Decimal("0")
    fee_overrides.set_overrides(conn, "schwab", {"option_per_contract": None})
    assert get_fee_rule_set("schwab", conn).option_per_contract == Decimal("0.65")


def test_a_negative_option_rate_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(fee_overrides.FeeOverrideError):
        fee_overrides.set_overrides(conn, "schwab", {"option_per_contract": "-1"})
    with pytest.raises(fee_overrides.FeeOverrideError):
        fee_overrides.set_overrides(conn, "schwab", {"option_reg_per_contract": "-0.01"})
