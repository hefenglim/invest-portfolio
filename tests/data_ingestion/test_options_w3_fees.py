"""Options W3 — the option fee branch (OPT-D6, spec §4.2 / §8).

Per-RULE-SET ``option_per_contract`` (``schwab`` 0.65, ``moomoo_us`` unset — unset is not 0),
an optional ``option_reg_per_contract`` (default 0), SEC on sells on the premium notional, no
MY stamp on an option in v1, and 0 / 0 at notional 0 (a close row never reaches the engine,
but the branch is safe if one does).
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion import fee_overrides
from portfolio_dash.data_ingestion.config_seed import FEE_RULES, get_fee_rule_set
from portfolio_dash.data_ingestion.fees import (
    OptionFeeUnset,
    compute_fees,
    option_fee_unset_applies,
)
from portfolio_dash.shared.models.enums import Side


def test_the_two_new_fields_and_their_seeds() -> None:
    assert FEE_RULES["schwab"].option_per_contract == Decimal("0.65")
    assert FEE_RULES["moomoo_us"].option_per_contract is None
    for name in FEE_RULES:
        assert FEE_RULES[name].option_reg_per_contract == Decimal("0")


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
    rules = FEE_RULES["moomoo_us"].model_copy(update={"option_per_contract": Decimal("1")})
    fr = compute_fees(rules, Side.SELL, Decimal("100"), Decimal("5"), is_option=True,
                      stamp_fx=Decimal("4.4"))
    assert fr.tax == Decimal("0")
    assert not any(k.startswith("stamp") for k in fr.snapshot)


def test_an_unset_rate_never_computes_a_guess() -> None:
    with pytest.raises(OptionFeeUnset):
        compute_fees(FEE_RULES["moomoo_us"], Side.BUY, Decimal("100"), Decimal("1"),
                     is_option=True)


def test_unset_applies_only_when_the_fee_is_not_supplied() -> None:
    moomoo, schwab = FEE_RULES["moomoo_us"], FEE_RULES["schwab"]
    assert option_fee_unset_applies(moomoo, fee_supplied=False)
    assert not option_fee_unset_applies(moomoo, fee_supplied=True)
    assert not option_fee_unset_applies(schwab, fee_supplied=False)


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


def test_the_nullable_rate_can_be_set_and_reverted(conn: sqlite3.Connection) -> None:
    fee_overrides.set_overrides(conn, "moomoo_us", {"option_per_contract": "0.50"})
    assert get_fee_rule_set("moomoo_us", conn).option_per_contract == Decimal("0.50")
    fee_overrides.set_overrides(conn, "moomoo_us", {"option_per_contract": None})
    assert get_fee_rule_set("moomoo_us", conn).option_per_contract is None


def test_a_negative_option_rate_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(fee_overrides.FeeOverrideError):
        fee_overrides.set_overrides(conn, "schwab", {"option_per_contract": "-1"})
    with pytest.raises(fee_overrides.FeeOverrideError):
        fee_overrides.set_overrides(conn, "schwab", {"option_reg_per_contract": "-0.01"})
