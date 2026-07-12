"""Editable alert-rule thresholds (spec 03 §3.1). Single-row JSON config; pure config,
no ledger writes. quota_low's threshold lives in shared.llm_config (spec 16).

``calib_gap`` (spec 03/04 I1) is a global rule: when the portfolio-wide AI calibration
error (``llm_insight.scoring.calibration_error``, in PERCENTAGE POINTS) exceeds its
threshold, a single warn alert fires. The threshold is therefore in **pp** (default
15pp), NOT a ratio — the engine compares pp-vs-pp. The gap value itself is fed into the
PURE engine by ``api.insight_service`` (strategy/ never imports llm_insight). The
``calibration_regression`` event (spec 04c) is a separate concern — it is recorded in
``alert_events`` (the bell feed), NOT surfaced by this rule-derived view."""

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
    calib_gap: Rule
    # P3 batch 2 (alerts taxonomy v2): market-risk rules. Each threshold is fed to the PURE
    # engine; the per-symbol market metrics they compare against are assembled at the
    # api/scheduler seam (api.alert_inputs), never inside strategy/ (which cannot read pricing).
    drawdown_from_peak: Rule
    vol_spike: Rule
    rebalance_drift: Rule
    consensus_change: Rule


# id -> (default_value | None, unit | None, min | None, max | None); all numerics are strings.
RULE_META: dict[str, tuple[str | None, str | None, str | None, str | None]] = {
    "single_weight": ("0.30", "ratio", "0.05", "1"),
    "sector_weight": ("0.60", "ratio", "0.10", "1"),
    "stale_price": (None, None, None, None),
    "missing_price": (None, None, None, None),
    "fx_drift": ("0.03", "ratio", "0.005", "0.50"),
    "exdiv_upcoming": ("14", "days", "1", "90"),
    "quota_low": (None, None, None, None),
    # calib_gap threshold is in PERCENTAGE POINTS (matches scoring.calibration_error's
    # pp output) — NOT a ratio. 15pp default, clamped 5..50pp.
    "calib_gap": ("15", "pp", "5", "50"),
    # drawdown_from_peak: the RISK drawdown magnitude as a ratio (0.20 = −20% from the 52-week
    # high). warn fires at HALF this value (−10% at the default) — one editable knob, a
    # documented two-level severity (mini-spec §2: "−20% risk; −10% 先給 warn").
    "drawdown_from_peak": ("0.20", "ratio", "0.02", "0.90"),
    # vol_spike: the multiple (×) of the 90d annualized-vol baseline the 30d vol must reach.
    "vol_spike": ("1.8", "x", "1", "10"),
    # rebalance_drift: the ABSOLUTE band (ratio) of the Swedroe 5/25 rule; the RELATIVE leg
    # (25% of the target, base = target) is a fixed named constant in strategy/alerts.
    # The TIGHTER of the two bands governs (min) — whichever leg is crossed first fires.
    "rebalance_drift": ("0.05", "ratio", "0.01", "0.50"),
    # consensus_change: the rating-score worsening threshold (1=best..5=worst scale, so
    # "worse" = increase). The mean-target-price cut leg (−10%) is a fixed named constant.
    "consensus_change": ("0.5", "score", "0.1", "4"),
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
        if rid not in data:
            # A rule added AFTER this config row was last saved (an existing install upgrading
            # into P3): come online at its RULE_META default rather than value=None (which
            # would silently disable numeric rules). This is the additive default-on-read
            # merge — a stored rule keeps its saved enabled/value verbatim (incl. an explicit
            # None for toggle-only rules); only genuinely-absent keys take the default.
            fields[rid] = _default_rule(RULE_META[rid][0])
            continue
        entry = data[rid]
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
