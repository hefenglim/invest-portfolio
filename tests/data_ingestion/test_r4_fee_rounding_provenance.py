"""R4 / QA-07 — ``fee_rule_snapshot`` may only ever name the regime that actually ran.

``fees.py`` hard-codes its quantization per market: the TW branch always calls ``_floor_int``
(ROUND_DOWN to integer NT$ — FE-D3, owner sign-off 2026-07-15, 財政部 角以下免收) and the
US / MY branches always call ``_cent`` (ROUND_HALF_UP to the 2-dp minor unit). Nothing in the
engine dispatches on ``FeeRuleSet.rounding``. Yet ``rounding`` was in ``EDITABLE_FIELDS``, so
an owner could override it, ``get_fee_rule_set`` reported the override as *effective*, the
fee did not move — and every row booked afterwards stamped the overridden value into
``fee_rule_snapshot`` as the regime that produced its numbers.

``data-and-pricing.md``: ``fee_rule_snapshot`` is **PROVENANCE** — "which regime produced
those two numbers". A snapshot naming a regime that never ran is a provenance lie written
permanently into the ledger.

Master ruling: the read-only route. TW floor rounding is a MARKET FACT, not a user
preference, so the override is REFUSED rather than accepted-and-ignored, and each engine
branch stamps the regime IT implements. Existing rows are never rewritten — the fix is
forward-only. The FE-D3 arithmetic is unchanged and is pinned below.
"""

import json
import sqlite3
from collections.abc import Iterator
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion import fee_overrides
from portfolio_dash.data_ingestion.config_seed import FEE_RULES, get_fee_rule_set
from portfolio_dash.data_ingestion.fee_overrides import FeeOverrideError
from portfolio_dash.data_ingestion.fees import compute_fees
from portfolio_dash.shared.models.enums import Side


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    # The overlay table is created lazily on first write — do not create it here.
    yield c
    c.close()


# --- the override door ---------------------------------------------------------------------


def test_rounding_is_not_an_editable_field() -> None:
    """It is a market rule the engine hard-codes, so it is not in the editable registry."""
    assert "rounding" not in fee_overrides.EDITABLE_FIELDS
    assert "rounding" not in fee_overrides.EDITABLE_FIELD_ORDER
    # Still SURFACED, just not writable, so the owner can see which regime applies.
    assert "rounding" in fee_overrides.READ_ONLY_FIELDS
    assert "rounding" in fee_overrides.DISPLAY_FIELD_ORDER
    assert fee_overrides.is_editable("rounding") is False
    assert fee_overrides.is_editable("brokerage") is True


def test_an_attempted_rounding_override_is_refused(conn: sqlite3.Connection) -> None:
    """QA-07: the door rejects the write instead of accepting one the engine will ignore."""
    with pytest.raises(FeeOverrideError) as exc:
        fee_overrides.set_overrides(conn, "tw", {"rounding": "half_up"})
    assert exc.value.field == "rounding"
    # ...and nothing was written, so the effective regime is still the real one.
    assert fee_overrides.overlay_for(conn, "tw") is None
    assert get_fee_rule_set("tw", conn).rounding == "floor"


def test_a_rounding_revert_is_refused_too(conn: sqlite3.Connection) -> None:
    """``null`` (revert) travels the same door; a non-editable field is rejected either way."""
    with pytest.raises(FeeOverrideError) as exc:
        fee_overrides.set_overrides(conn, "tw", {"rounding": None})
    assert exc.value.field == "rounding"


def test_a_rounding_override_does_not_poison_a_valid_batch(conn: sqlite3.Connection) -> None:
    """Validation stays atomic: the whole batch is rejected, nothing is written."""
    with pytest.raises(FeeOverrideError):
        fee_overrides.set_overrides(
            conn, "tw", {"brokerage": "0.001", "rounding": "half_up"})
    assert fee_overrides.overlay_for(conn, "tw") is None
    assert get_fee_rule_set("tw", conn) == get_fee_rule_set("tw")


def test_a_legacy_stored_rounding_override_is_ignored_on_read(
    conn: sqlite3.Connection,
) -> None:
    """A row saved before this fix must stop taking effect WITHOUT rewriting history.

    The overlay read filters to the editable whitelist, so a stale ``rounding`` key is inert
    from the next read on; the effective rule set reports the regime the engine really runs.
    """
    fee_overrides.ensure_tables(conn)
    conn.execute(
        "INSERT INTO fee_rule_overrides (rule_set, overrides, updated_at) VALUES (?, ?, ?)",
        ("tw", json.dumps({"rounding": "half_up", "brokerage": "0.001"}), "2026-07-01T00:00:00"),
    )
    conn.commit()
    overlay = fee_overrides.overlay_for(conn, "tw")
    assert overlay is not None
    assert "rounding" not in overlay.fields          # inert
    assert overlay.fields["brokerage"] == "0.001"    # the legal override still applies
    effective = get_fee_rule_set("tw", conn)
    assert effective.rounding == "floor"
    assert effective.brokerage == Decimal("0.001")


# --- the snapshot ---------------------------------------------------------------------------


def test_tw_snapshot_names_the_regime_that_actually_ran() -> None:
    """QA-07's core: the TW branch floors unconditionally, so it may only stamp ``floor``.

    The rule set here CLAIMS ``half_up`` (the shape a pre-fix overlay produced). The fee is
    142 either way — the arithmetic never read the field — so the snapshot must report the
    floor regime that produced it, not the claim.
    """
    rules = FEE_RULES["tw"].model_copy(update={"rounding": "half_up"})
    res = compute_fees(rules, Side.BUY, Decimal("1000"), Decimal("100"))
    assert res.fee == Decimal("142")  # 100000 * 0.001425 = 142.5 -> floor 142 (FE-D3)
    assert res.snapshot["rounding"] == "floor"


def test_us_and_my_snapshots_name_their_own_regime() -> None:
    """The mirror case: the cent-quantizing branches may only ever stamp ``half_up``."""
    us = FEE_RULES["moomoo_us"].model_copy(update={"rounding": "floor"})
    my = FEE_RULES["moomoo_my"].model_copy(update={"rounding": "floor"})
    assert compute_fees(us, Side.BUY, Decimal("10"), Decimal("100")).snapshot[
        "rounding"] == "half_up"
    assert compute_fees(my, Side.BUY, Decimal("100"), Decimal("10")).snapshot[
        "rounding"] == "half_up"


def test_fe_d3_arithmetic_is_unchanged() -> None:
    """The floor, and the NT$20 minimum applied AFTER it (群益 142.5 -> 142; 5.5 -> 5 -> 20)."""
    tw = FEE_RULES["tw"]
    assert tw.rounding == "floor"
    big = compute_fees(tw, Side.BUY, Decimal("1000"), Decimal("100"))
    assert big.fee == Decimal("142")            # floor(142.5), NOT 143
    small = compute_fees(tw, Side.BUY, Decimal("10"), Decimal("400"))
    assert small.fee == Decimal("20")           # floor(5.7) = 5 -> min NT$20
    sell = compute_fees(tw, Side.SELL, Decimal("1000"), Decimal("100"))
    assert sell.tax == Decimal("300")           # floor(0.003 * 100000)
    assert sell.snapshot["rounding"] == "floor"
