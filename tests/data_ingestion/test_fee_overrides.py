"""Unit tests: the user fee-rule overlay (FU-D1) over config_seed FEE_RULES v2.

Covers the merge (override one field -> effective changes, others byte-identical), null-revert,
per-set + global reset, validation rejects, and the no-overlay purity guarantee (effective ==
defaults exactly, so the oracle / hermetic tests stay deterministic).
"""

import sqlite3
from collections.abc import Iterator
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion import fee_overrides
from portfolio_dash.data_ingestion.config_seed import (
    FEE_RULES,
    get_effective_fee_rules,
    get_fee_rule_set,
)
from portfolio_dash.data_ingestion.fee_overrides import FeeOverrideError


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    # NOTE: the overlay table is created lazily on first write, so we do NOT create it here —
    # the read paths must degrade gracefully when it is absent.
    yield c
    c.close()


def test_no_overlay_effective_equals_defaults_exactly(conn: sqlite3.Connection) -> None:
    # conn-aware resolution with NO overlay must be byte-identical to the pure defaults.
    for name in FEE_RULES:
        assert get_fee_rule_set(name, conn) == get_fee_rule_set(name)
    # And even before any override is saved, the overlay table need not exist.
    assert fee_overrides.overlay_for(conn, "tw") is None


def test_pure_default_when_conn_none() -> None:
    # conn=None is the oracle path — always the raw v2 default.
    assert get_fee_rule_set("tw") is FEE_RULES["tw"]


def test_override_one_field_changes_only_that_field(conn: sqlite3.Connection) -> None:
    base = get_fee_rule_set("tw")
    fee_overrides.set_overrides(conn, "tw", {"brokerage": "0.001000"})
    eff = get_fee_rule_set("tw", conn)
    assert eff.brokerage == Decimal("0.001000")
    # Every OTHER field is byte-identical to the default (model equality field-by-field).
    for key in base.model_dump():
        if key == "brokerage":
            continue
        assert getattr(eff, key) == getattr(base, key), key


def test_override_persists_and_is_visible_in_bulk(conn: sqlite3.Connection) -> None:
    fee_overrides.set_overrides(conn, "moomoo_my", {"sst_rate": "0.06"})
    bulk = get_effective_fee_rules(conn)
    assert bulk["moomoo_my"].sst_rate == Decimal("0.06")
    assert bulk["tw"].brokerage == Decimal("0.001425")  # untouched set unchanged


def test_null_reverts_single_field(conn: sqlite3.Connection) -> None:
    fee_overrides.set_overrides(conn, "tw", {"brokerage": "0.001", "min_fee": "10"})
    assert get_fee_rule_set("tw", conn).brokerage == Decimal("0.001")
    # null reverts brokerage but keeps min_fee overridden.
    fee_overrides.set_overrides(conn, "tw", {"brokerage": None})
    eff = get_fee_rule_set("tw", conn)
    assert eff.brokerage == Decimal("0.001425")  # back to default
    assert eff.min_fee == Decimal("10")  # still overridden
    overlay = fee_overrides.overlay_for(conn, "tw")
    assert overlay is not None and set(overlay.fields) == {"min_fee"}


def test_reverting_last_field_deletes_row(conn: sqlite3.Connection) -> None:
    fee_overrides.set_overrides(conn, "tw", {"min_fee": "10"})
    fee_overrides.set_overrides(conn, "tw", {"min_fee": None})
    assert fee_overrides.overlay_for(conn, "tw") is None
    assert get_fee_rule_set("tw", conn) == get_fee_rule_set("tw")


def test_reset_one_rule_set(conn: sqlite3.Connection) -> None:
    fee_overrides.set_overrides(conn, "tw", {"brokerage": "0.001"})
    fee_overrides.set_overrides(conn, "schwab", {"sec_rate": "0.00001"})
    assert fee_overrides.reset(conn, "tw") is True
    assert get_fee_rule_set("tw", conn) == get_fee_rule_set("tw")
    # Other rule set's overlay untouched.
    assert get_fee_rule_set("schwab", conn).sec_rate == Decimal("0.00001")
    assert fee_overrides.reset(conn, "tw") is False  # already clean


def test_reset_all(conn: sqlite3.Connection) -> None:
    fee_overrides.set_overrides(conn, "tw", {"brokerage": "0.001"})
    fee_overrides.set_overrides(conn, "moomoo_my", {"sst_rate": "0.06"})
    removed = fee_overrides.reset_all(conn)
    assert removed == 2
    for name in FEE_RULES:
        assert get_fee_rule_set(name, conn) == get_fee_rule_set(name)


def test_rounding_override(conn: sqlite3.Connection) -> None:
    fee_overrides.set_overrides(conn, "schwab", {"rounding": "floor"})
    assert get_fee_rule_set("schwab", conn).rounding == "floor"


def test_reject_unknown_field(conn: sqlite3.Connection) -> None:
    with pytest.raises(FeeOverrideError) as exc:
        fee_overrides.set_overrides(conn, "tw", {"market": "US"})
    assert exc.value.field == "market"
    # Non-editable / unknown field even when null (revert) is rejected.
    with pytest.raises(FeeOverrideError):
        fee_overrides.set_overrides(conn, "tw", {"nope": None})


def test_reject_negative_amount(conn: sqlite3.Connection) -> None:
    with pytest.raises(FeeOverrideError) as exc:
        fee_overrides.set_overrides(conn, "tw", {"min_fee": "-1"})
    assert exc.value.field == "min_fee"


def test_reject_rate_above_one(conn: sqlite3.Connection) -> None:
    with pytest.raises(FeeOverrideError) as exc:
        fee_overrides.set_overrides(conn, "tw", {"brokerage": "1.5"})
    assert exc.value.field == "brokerage"


def test_reject_bad_rounding(conn: sqlite3.Connection) -> None:
    with pytest.raises(FeeOverrideError) as exc:
        fee_overrides.set_overrides(conn, "tw", {"rounding": "banker"})
    assert exc.value.field == "rounding"


def test_reject_non_numeric(conn: sqlite3.Connection) -> None:
    with pytest.raises(FeeOverrideError):
        fee_overrides.set_overrides(conn, "tw", {"brokerage": "abc"})


def test_bad_field_is_atomic(conn: sqlite3.Connection) -> None:
    # A batch with one bad field writes NOTHING (validate-before-apply).
    with pytest.raises(FeeOverrideError):
        fee_overrides.set_overrides(conn, "tw", {"brokerage": "0.001", "min_fee": "-1"})
    assert fee_overrides.overlay_for(conn, "tw") is None


def test_cap_override_and_zero_cap(conn: sqlite3.Connection) -> None:
    # taf_cap is a nullable Decimal|None cap; a numeric override applies, 0 is a valid cap.
    fee_overrides.set_overrides(conn, "schwab", {"taf_cap": "5"})
    assert get_fee_rule_set("schwab", conn).taf_cap == Decimal("5")
    fee_overrides.set_overrides(conn, "moomoo_my", {"stamp_cap_etf": "0"})
    assert get_fee_rule_set("moomoo_my", conn).stamp_cap_etf == Decimal("0")


def test_field_order_matches_editable_set() -> None:
    assert set(fee_overrides.EDITABLE_FIELD_ORDER) == set(fee_overrides.EDITABLE_FIELDS)
    assert len(fee_overrides.EDITABLE_FIELD_ORDER) == len(fee_overrides.EDITABLE_FIELDS)
