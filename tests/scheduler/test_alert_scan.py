"""alert-scan job + on_alert R7 dispatch (spec 04.9 R7 / 4.10).

The ``alert_scan`` job COMPUTES spec-03 alerts (reading the dashboard — a scheduler/strategy
concern, never on page load) → records ``alert_events`` → dispatches subscribing on_alert
combos via the registered runner (24h-debounced). These tests drive the job with the
strategy alert computation stubbed so they assert the bridge wiring, not market data.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.llm_insight import alerts_bridge as ab
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.scheduler import jobs
from portfolio_dash.strategy.alerts import Alert

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    jobs.create_scheduler_tables(c)
    cs.ensure_seeded(c)
    ab.ensure_tables(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clear_runner() -> Iterator[None]:
    jobs.register_insight_runner(None)
    yield
    jobs.register_insight_runner(None)


def test_alert_scan_records_events_and_dispatches(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stub the alert computation so the job sees a single fx_drift event.
    monkeypatch.setattr(
        jobs, "_compute_alerts_for_scan",
        lambda c, *, now: [
            Alert(id="fx_drift:schwab", sev="info", rule="fx_drift", title="t", detail="d")
        ],
    )
    sub = cs.create_insight_type(
        conn, name="FX", scope="on_alert", alert_rules=["fx_drift"], enabled=True, now=NOW
    )
    calls: list[tuple[int, str, str | None]] = []

    def runner(c: sqlite3.Connection, insight_type_id: int, *, now: datetime,
               fired_rule: str, fired_symbol: str | None) -> None:
        calls.append((insight_type_id, fired_rule, fired_symbol))

    jobs.register_insight_runner(runner)
    detail = jobs.alert_scan(conn, now=NOW)
    # event recorded + consumed; subscriber dispatched once
    assert calls == [(sub.id, "fx_drift", "schwab")]
    assert ab.unconsumed_events(conn) == []
    assert "fx_drift" in detail


def test_alert_scan_no_subscribers_records_but_no_dispatch(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        jobs, "_compute_alerts_for_scan",
        lambda c, *, now: [
            Alert(id="single_weight:2330", sev="risk", rule="single_weight",
                  title="t", detail="d")
        ],
    )
    calls: list[int] = []
    jobs.register_insight_runner(lambda c, i, **kw: calls.append(i))
    jobs.alert_scan(conn, now=NOW)
    assert calls == []  # no on_alert subscriber for single_weight
    assert ab.unconsumed_events(conn) == []  # still consumed


def test_alert_scan_records_new_market_risk_rule_event(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A P3 market-risk alert (drawdown_from_peak:2330) flows through the SAME pipeline:
    # the rule:symbol id yields a per-symbol alert_events row → the push/dispatch path picks
    # it up exactly like the legacy rules.
    monkeypatch.setattr(
        jobs, "_compute_alerts_for_scan",
        lambda c, *, now: [
            Alert(id="drawdown_from_peak:2330", sev="risk", rule="drawdown_from_peak",
                  title="t", detail="d")
        ],
    )
    jobs.alert_scan(conn, now=NOW)
    row = conn.execute("SELECT rule_id, symbol FROM alert_events").fetchone()
    assert row["rule_id"] == "drawdown_from_peak" and row["symbol"] == "2330"


def test_compute_alerts_for_scan_uses_registered_runner(
    conn: sqlite3.Connection
) -> None:
    # The runner seam: when the app registers the alert-compute runner, the scan delegates to
    # it (the full P3 rule set). A scheduler-only process (no runner) falls back to the base
    # engine — proven here by clearing the runner and asserting the fallback returns a list.
    sentinel = [Alert(id="vol_spike:2330", sev="warn", rule="vol_spike", title="t", detail="d")]
    jobs.register_alert_compute_runner(lambda c, *, now: sentinel)
    try:
        assert jobs._compute_alerts_for_scan(conn, now=NOW) == sentinel
    finally:
        jobs.register_alert_compute_runner(None)
    # no runner -> base engine over the bootstrapped (empty) DB returns a list, never raises
    from portfolio_dash.strategy.rules_config import ensure_alert_rules_seeded
    ensure_alert_rules_seeded(conn)  # the base fallback reads the rules table
    assert isinstance(jobs._compute_alerts_for_scan(conn, now=NOW), list)


def test_alert_scan_is_a_registered_job() -> None:
    assert any(j.id == "alert_scan" for j in jobs.JOBS)


def test_alert_scan_symbol_extracted_from_alert_id(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A rule with no symbol suffix (e.g. quota_low) records a null-symbol event.
    monkeypatch.setattr(
        jobs, "_compute_alerts_for_scan",
        lambda c, *, now: [
            Alert(id="quota_low", sev="warn", rule="quota_low", title="t", detail="d")
        ],
    )
    jobs.alert_scan(conn, now=NOW)
    row = conn.execute("SELECT rule_id, symbol FROM alert_events").fetchone()
    assert row["rule_id"] == "quota_low"
    assert row["symbol"] is None
