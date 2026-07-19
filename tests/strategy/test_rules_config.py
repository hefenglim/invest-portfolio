import json
import sqlite3
from decimal import Decimal

from portfolio_dash.strategy.rules_config import (
    DEFAULT_RULES,
    RULE_META,
    ensure_alert_rules_seeded,
    get_alert_rules,
    set_alert_rules,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_alert_rules_seeded(conn)
    return conn


def test_defaults_seeded() -> None:
    rules = get_alert_rules(_conn())
    assert rules.single_weight.enabled is True
    assert rules.single_weight.value == Decimal("0.30")
    assert rules.exdiv_upcoming.value == Decimal("14")
    assert rules.quota_low.value is None


def test_roundtrip_set_get() -> None:
    conn = _conn()
    rules = get_alert_rules(conn)
    rules.single_weight.value = Decimal("0.25")
    rules.fx_drift.enabled = False
    set_alert_rules(conn, rules)
    got = get_alert_rules(conn)
    assert got.single_weight.value == Decimal("0.25")
    assert got.fx_drift.enabled is False
    assert got.sector_weight.value == Decimal("0.60")  # untouched


def test_value_persists_as_decimal_string_not_float() -> None:
    conn = _conn()
    raw = conn.execute("SELECT rules_json FROM alert_rules_config WHERE id = 1").fetchone()[0]
    assert '"0.30"' in raw or '"0.3"' in raw  # stored as string, never a JSON float 0.3


def test_calib_gap_default_and_meta() -> None:
    # calib_gap is present, enabled, and defaults to 15 PERCENTAGE POINTS (not a ratio).
    assert DEFAULT_RULES.calib_gap.enabled is True
    assert DEFAULT_RULES.calib_gap.value == Decimal("15")
    # RULE_META = (default, unit, min, max) — unit is pp; clamp 5..50pp.
    assert RULE_META["calib_gap"] == ("15", "pp", "5", "50")


def test_calib_gap_roundtrip_preserves_enabled_and_value() -> None:
    conn = _conn()
    rules = get_alert_rules(conn)
    assert rules.calib_gap.value == Decimal("15")  # seeded default
    rules.calib_gap.value = Decimal("22")
    rules.calib_gap.enabled = False
    set_alert_rules(conn, rules)
    got = get_alert_rules(conn)
    assert got.calib_gap.value == Decimal("22")
    assert got.calib_gap.enabled is False


def test_target_cross_default_and_meta() -> None:
    # FU-D28: target_cross is toggle-only (no numeric threshold — per-symbol targets), enabled
    # by default, same META shape as stale_price / missing_price.
    assert DEFAULT_RULES.target_cross.enabled is True
    assert DEFAULT_RULES.target_cross.value is None
    assert RULE_META["target_cross"] == (None, None, None, None)


def test_target_cross_defaults_on_for_existing_install_missing_the_field() -> None:
    # An install whose alert_rules_config JSON predates target_cross (the key is absent) must
    # bring the rule online at its default (enabled) via the additive default-on-read merge —
    # never silently off. Simulate by writing a config row WITHOUT the target_cross key.
    conn = _conn()
    legacy = {
        "single_weight": {"enabled": True, "value": "0.30"},
        "stale_price": {"enabled": True, "value": None},
    }
    conn.execute(
        "INSERT INTO alert_rules_config (id, rules_json) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET rules_json = excluded.rules_json",
        (json.dumps(legacy),),
    )
    conn.commit()
    got = get_alert_rules(conn)
    assert got.target_cross.enabled is True and got.target_cross.value is None
    # a stored rule keeps its saved state verbatim (proves only ABSENT keys take the default)
    assert got.single_weight.value == Decimal("0.30")
