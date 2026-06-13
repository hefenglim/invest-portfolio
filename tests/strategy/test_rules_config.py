import sqlite3
from decimal import Decimal

from portfolio_dash.strategy.rules_config import (
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
