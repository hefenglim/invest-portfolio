"""DEF-037 (functional test G-09, 2026-09-23): an alert is dispatched by what it is ABOUT.

Measured on the demo: 排程中心 › alert_scan › 立即執行 → run #158 「35 alert(s)…, 18
dispatched」 → AI 洞察 › 持倉健診 showed a card whose symbol was ``moomoo_my`` — an ACCOUNT —
reading 「Moomoo 交易商警示｜收到 moomoo 交易商的『服務不可用』警示…」: pure invention, because
the model was told neither which alert fired nor what it said.

Root cause: ``scheduler/jobs.py::_alert_symbol`` recovered "the symbol" as the suffix of the
alert's string id (``rule:suffix``) — its own docstring's example was ``fx_drift:schwab`` — and
the dispatcher handed that suffix to the per-symbol card as ``fired_symbol``. The suffix is a
symbol for ten rules, an account for ``fx_drift``, a sector name for ``sector_weight``, a
currency code for ``currency_weight``, and an insight-task id for ``calibration_regression``.

The fix makes the subject STRUCTURE (``Alert.scope`` + ``Alert.subject``, carried into
``alert_events.scope``) and dispatches by it: a symbol alert → the per-symbol card; a
portfolio-wide alert → the portfolio-context card (unchanged); any other scope is recorded
and pushed as before but never becomes a per-symbol card, and the run detail says so.
"""

import ast
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.llm_insight import alerts_bridge as ab
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.scheduler import jobs
from portfolio_dash.strategy.alerts import Alert

NOW = datetime(2026, 9, 23, 15, 0, tzinfo=ZoneInfo("Asia/Taipei"))
_ALERTS_SRC = Path(__file__).resolve().parents[2] / "portfolio_dash" / "strategy" / "alerts.py"


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
    jobs.register_alert_held_fn(None)
    yield
    jobs.register_insight_runner(None)
    jobs.register_alert_held_fn(None)


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, c: sqlite3.Connection, insight_type_id: int, *, now: datetime,
                 fired_rule: str, fired_symbol: str | None, trigger: Any) -> None:
        self.calls.append({"id": insight_type_id, "rule": fired_rule,
                           "symbol": fired_symbol, "trigger": trigger})


def _scan(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
          alerts: list[Alert]) -> tuple[_Recorder, str]:
    monkeypatch.setattr(jobs, "_compute_alerts_for_scan", lambda c, *, now: alerts)
    cs.create_insight_type(conn, name="持倉提點", scope="on_alert", alert_rules="all",
                           enabled=True, now=NOW)
    rec = _Recorder()
    jobs.register_insight_runner(rec)
    # DEF-041: every symbol these scope tests alert on is HELD — the held-vs-watchlist rule
    # has its own file (test_def041_alert_cards_only_for_held.py).
    held = {a.subject for a in alerts if a.scope == "symbol" and a.subject}
    jobs.register_alert_held_fn(lambda c, *, now: held)
    return rec, jobs.alert_scan(conn, now=NOW)


def test_an_account_level_alert_never_becomes_a_symbol_card(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = Alert(id="fx_drift:moomoo_my", sev="info", rule="fx_drift", title="Moomoo 匯率偏離成本",
               detail="即期匯率偏離成本匯率 6.1%＞門檻 5.0%", href="cash.html#fx",
               scope="account", subject="moomoo_my")
    rec, detail = _scan(conn, monkeypatch, [fx])
    assert rec.calls == [], "an account id reached the per-symbol card as fired_symbol"
    row = conn.execute("SELECT rule_id, symbol, scope, consumed FROM alert_events").fetchone()
    assert (row["rule_id"], row["scope"], row["consumed"]) == ("fx_drift", "account", 1)
    # the run says what it skipped, naming the account by its display-name token
    assert "略過 1 條非個股預警" in detail
    assert "{account:moomoo_my}" in detail


@pytest.mark.parametrize(("alert_id", "rule", "scope", "subject"), [
    ("sector_weight:Information Technology", "sector_weight", "sector",
     "Information Technology"),
    ("currency_weight:USD", "currency_weight", "currency", "USD"),
])
def test_sector_and_currency_alerts_are_skipped_too(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    alert_id: str, rule: str, scope: str, subject: str,
) -> None:
    a = Alert(id=alert_id, sev="risk", rule=rule, title="t", detail="d", href="index.html",
              scope=scope, subject=subject)  # type: ignore[arg-type]
    rec, detail = _scan(conn, monkeypatch, [a])
    assert rec.calls == []
    assert subject in detail


def test_a_symbol_alert_reaches_the_card_with_its_trigger(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = Alert(id="target_cross:2884:low", sev="warn", rule="target_cross",
              title="2884 跌破目標價", detail="現價 48.5 ≤ 目標下限 50", href="/symbol/2884",
              scope="symbol", subject="2884")
    rec, _ = _scan(conn, monkeypatch, [a])
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["rule"] == "target_cross" and call["symbol"] == "2884"
    trig = call["trigger"]
    event_id = conn.execute("SELECT id FROM alert_events").fetchone()["id"]
    assert trig.source == "alert"
    assert trig.rule == "target_cross" and trig.alert_id == event_id
    assert trig.scope == "symbol" and trig.subject == "2884"
    assert trig.title == "2884 跌破目標價" and trig.detail == "現價 48.5 ≤ 目標下限 50"
    assert trig.fired_at and trig.fired_at.startswith("2026-09-23")


def test_a_portfolio_alert_still_reaches_the_portfolio_card(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = Alert(id="portfolio_drawdown", sev="risk", rule="portfolio_drawdown",
              title="組合自高點回撤", detail="目前自高點回撤 12.0%＞門檻 10.0%",
              href="index.html#trend-chart", scope="portfolio")
    rec, _ = _scan(conn, monkeypatch, [a])
    assert len(rec.calls) == 1 and rec.calls[0]["symbol"] is None
    assert rec.calls[0]["trigger"].scope == "portfolio"


def test_events_recorded_without_a_scope_are_not_guessed_into_symbol_cards(
    conn: sqlite3.Connection
) -> None:
    # calibration_regression is recorded with the insight-TASK id in the symbol column
    # (api/insight_service.py) — the same class; a scope-less non-signal event with a subject
    # is skipped, never dispatched as a per-symbol card. signal_* transitions are per-symbol
    # by construction (strategy/signal_states), so they keep dispatching.
    cs.create_insight_type(conn, name="全部", scope="on_alert",
                           alert_rules=["calibration_regression", "signal_trend"],
                           enabled=True, now=NOW)
    ab.record_event(conn, rule_id="calibration_regression", symbol="3", now=NOW)
    ab.record_event(conn, rule_id="signal_trend", symbol="2330", now=NOW)
    rec = _Recorder()
    result = ab.dispatch_alert_events_ex(conn, rec, now=NOW, held_symbols=lambda: {"2330"})
    assert [(c["rule"], c["symbol"]) for c in rec.calls] == [("signal_trend", "2330")]
    assert [(e.rule_id, e.symbol) for e in result.skipped] == [("calibration_regression", "3")]


def test_the_alert_model_refuses_an_id_that_disagrees_with_its_subject() -> None:
    with pytest.raises(ValidationError):
        Alert(id="fx_drift:schwab", sev="info", rule="fx_drift", title="t", detail="d",
              scope="account", subject="moomoo_my")
    with pytest.raises(ValidationError):
        Alert(id="fx_drift:schwab", sev="info", rule="fx_drift", title="t", detail="d",
              scope="account")                      # a scoped alert without its subject
    with pytest.raises(ValidationError):
        Alert(id="quota_low", sev="warn", rule="quota_low", title="t", detail="d",
              scope="portfolio", subject="x")       # a portfolio alert has no subject


def test_the_structure_stays_off_the_wire() -> None:
    # the spec-17 golden payload (and GET /api/alerts) keep their exact shape
    a = Alert(id="single_weight:2330", sev="risk", rule="single_weight", title="t",
              detail="d", href="/symbol/2330", scope="symbol", subject="2330")
    assert set(a.model_dump()) == {"id", "sev", "rule", "title", "detail", "href"}


def _alert_calls() -> list[ast.Call]:
    tree = ast.parse(_ALERTS_SRC.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Alert"]


def test_every_alert_construction_states_its_scope() -> None:
    """The rule engine is where the subject is KNOWN; no construction may leave it implicit."""
    calls = _alert_calls()
    assert len(calls) >= 14, f"the scan found {len(calls)} constructions — a guard matching nothing"
    offenders: list[tuple[int, str]] = []
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        scope = kw.get("scope")
        if not isinstance(scope, ast.Constant):
            offenders.append((call.lineno, "scope missing or not a literal"))
            continue
        if scope.value != "portfolio" and "subject" not in kw:
            offenders.append((call.lineno, f"scope={scope.value!r} without subject"))
    assert not offenders, offenders
