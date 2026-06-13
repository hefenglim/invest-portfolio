"""Editable alert-rule thresholds (spec 03 §3.1). Single-row JSON config; pure config,
no ledger writes. quota_low's threshold lives in shared.llm_config (spec 16). The
calib_gap / calibration_regression rules are deferred to spec 04 (absent here)."""

import json
import sqlite3
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.shared import config_store


class Rule(BaseModel):
    enabled: bool = True
    value: Decimal | None = None


class AlertRules(BaseModel):
    single_weight: Rule
    sector_weight: Rule
    stale_price: Rule
    missing_price: Rule
    fx_drift: Rule
    exdiv_upcoming: Rule
    quota_low: Rule


# id -> (default_value | None, unit | None, min | None, max | None); all numerics are strings.
RULE_META: dict[str, tuple[str | None, str | None, str | None, str | None]] = {
    "single_weight": ("0.30", "ratio", "0.05", "1"),
    "sector_weight": ("0.60", "ratio", "0.10", "1"),
    "stale_price": (None, None, None, None),
    "missing_price": (None, None, None, None),
    "fx_drift": ("0.03", "ratio", "0.005", "0.50"),
    "exdiv_upcoming": ("14", "days", "1", "90"),
    "quota_low": (None, None, None, None),
}
RULE_IDS = list(RULE_META)  # preserves order


def _default_rule(default_value: str | None) -> Rule:
    return Rule(enabled=True,
                value=Decimal(default_value) if default_value is not None else None)


DEFAULT_RULES: AlertRules = AlertRules(
    **{rid: _default_rule(meta[0]) for rid, meta in RULE_META.items()}
)


def _create(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS alert_rules_config "
        "(id INTEGER PRIMARY KEY CHECK (id = 1), rules_json TEXT NOT NULL)"
    )


def _serialize(rules: AlertRules) -> str:
    payload: dict[str, dict[str, object]] = {}
    for rid in RULE_IDS:
        rule: Rule = getattr(rules, rid)
        payload[rid] = {
            "enabled": rule.enabled,
            "value": None if rule.value is None else str(rule.value),
        }
    return json.dumps(payload)


def _parse(raw: str) -> AlertRules:
    data = json.loads(raw)
    fields: dict[str, Rule] = {}
    for rid in RULE_IDS:
        entry = data.get(rid, {})
        raw_value = entry.get("value")
        fields[rid] = Rule(
            enabled=bool(entry.get("enabled", True)),
            value=Decimal(str(raw_value)) if raw_value is not None else None,
        )
    return AlertRules(**fields)


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO alert_rules_config (id, rules_json) VALUES (1, ?)",
        (_serialize(DEFAULT_RULES),),
    )


def ensure_alert_rules_seeded(conn: sqlite3.Connection) -> None:
    """Ensure the single-row alert-rules config table exists and is seeded once."""
    config_store.ensure_seeded(conn, "alert_rules", create=_create, seed=_seed)


def get_alert_rules(conn: sqlite3.Connection) -> AlertRules:
    """Read the editable alert-rule thresholds; defaults when never seeded."""
    row = conn.execute(
        "SELECT rules_json FROM alert_rules_config WHERE id = 1"
    ).fetchone()
    if row is None or row[0] is None:
        return DEFAULT_RULES
    return _parse(row[0])


def set_alert_rules(conn: sqlite3.Connection, rules: AlertRules) -> None:
    """Persist the alert-rule thresholds (single-row upsert). Values stored as strings."""
    conn.execute(
        "INSERT INTO alert_rules_config (id, rules_json) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET rules_json = excluded.rules_json",
        (_serialize(rules),),
    )
    conn.commit()
