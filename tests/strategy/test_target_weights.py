"""Target-weights store (D8) + the rules_config default-on-read merge for new installs."""

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.strategy import target_weights as tw
from portfolio_dash.strategy.rules_config import _parse

_NOW = datetime(2026, 7, 13, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_load_is_empty_before_seed(conn: sqlite3.Connection) -> None:
    # No table yet -> honest {} (never crashes the dashboard/alerts read path).
    assert tw.load_target_weights(conn) == {}
    assert tw.get_updated_at(conn) is None


def test_seed_then_load_empty(conn: sqlite3.Connection) -> None:
    tw.ensure_target_weights_seeded(conn)
    assert tw.load_target_weights(conn) == {}


def test_save_round_trip_preserves_decimals(conn: sqlite3.Connection) -> None:
    tw.save_target_weights(conn, {"2330": Decimal("0.2500"), "AAPL": Decimal("0.1")}, now=_NOW)
    loaded = tw.load_target_weights(conn)
    assert loaded == {"2330": Decimal("0.2500"), "AAPL": Decimal("0.1")}
    assert tw.get_updated_at(conn) == _NOW.isoformat()


def test_save_empty_clears(conn: sqlite3.Connection) -> None:
    tw.save_target_weights(conn, {"2330": Decimal("0.3")}, now=_NOW)
    tw.save_target_weights(conn, {}, now=_NOW)
    assert tw.load_target_weights(conn) == {}


def test_stored_values_are_strings(conn: sqlite3.Connection) -> None:
    tw.save_target_weights(conn, {"2330": Decimal("0.25")}, now=_NOW)
    raw = conn.execute("SELECT weights_json FROM target_weights_config WHERE id=1").fetchone()[0]
    assert json.loads(raw) == {"2330": "0.25"}  # canonical Decimal string, not a float


# --- rules_config default-on-read: new rules come online on an existing install ---------


def test_parse_defaults_absent_new_rules() -> None:
    # A config saved BEFORE the P3 rules existed (only the 8 legacy keys).
    legacy = {
        "single_weight": {"enabled": True, "value": "0.30"},
        "sector_weight": {"enabled": True, "value": "0.60"},
        "stale_price": {"enabled": True, "value": None},
        "missing_price": {"enabled": True, "value": None},
        "fx_drift": {"enabled": True, "value": "0.03"},
        "exdiv_upcoming": {"enabled": True, "value": "14"},
        "quota_low": {"enabled": True, "value": None},
        "calib_gap": {"enabled": True, "value": "15"},
    }
    rules = _parse(json.dumps(legacy))
    # The 4 new rules come online at their RULE_META defaults (enabled + default value),
    # NOT value=None (which would silently disable them).
    assert rules.drawdown_from_peak.enabled is True
    assert rules.drawdown_from_peak.value == Decimal("0.20")  # RULE_META default
    assert rules.vol_spike.value == Decimal("1.8")
    assert rules.rebalance_drift.value == Decimal("0.05")
    assert rules.consensus_change.value == Decimal("0.5")


def test_parse_preserves_explicitly_stored_disable() -> None:
    # An explicitly-disabled new rule is respected (not overwritten by the default).
    stored = {"drawdown_from_peak": {"enabled": False, "value": "0.20"}}
    rules = _parse(json.dumps(stored))
    assert rules.drawdown_from_peak.enabled is False
