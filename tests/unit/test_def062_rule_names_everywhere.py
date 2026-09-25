"""DEF-062 class scan, by RENDERER: every surface that names a fired rule names it in words.

The wizard was the instance the verifier saw; the class is "a rule id reaches the owner as
text". These drive each backend renderer with real ids and read what it produces:

* the push (``ops.notify.format_event``) — ``calibration_regression`` had no name anywhere, so
  its push read 「portfolio-dash · 洞察任務 5 calibration_regression」, and an unknown id fell
  back to itself;
* the digest (``api.digest_service`` 今日警示 / 本週警示回顧) — same fallback;
* an insight card's 「由預警「…」觸發」 chip (``api.routers.insights._trigger_wire``);
* the R7 gate message (``llm_insight.gating``) — interpolated the raw id;
* 排程中心's ``alert_scan`` run detail (``scheduler.jobs``) — listed ``[single_weight, …]`` and
  「略過 … fx_drift 帳戶 …」.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.api import digest_service as ds
from portfolio_dash.api.routers.insights import _trigger_wire
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.llm_insight import alerts_bridge as ab
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight.gating import GateContext, evaluate_gates
from portfolio_dash.llm_insight.insights_store import InsightTrigger
from portfolio_dash.ops import notify
from portfolio_dash.scheduler import jobs
from portfolio_dash.strategy import signal_states
from portfolio_dash.strategy.alerts import Alert
from portfolio_dash.strategy.rules_config import RULE_IDS
from tests.contract.test_def062_alert_rule_names import _OWNER_WORDING

NOW = datetime(2026, 9, 25, 15, 0, tzinfo=ZoneInfo("Asia/Taipei"))
_EVENTS = [signal_states.EVENT_TREND, signal_states.EVENT_CROSS,
           signal_states.EVENT_MOMENTUM, "calibration_regression"]
_ALL_IDS = [*RULE_IDS, *_EVENTS]
#: The events' names: the three signal words the push already used, and the name
#: ``calibration_regression`` never had (the push printed the id).
_EVENT_WORDING = {
    signal_states.EVENT_TREND: "趨勢反轉",
    signal_states.EVENT_CROSS: "均線交叉",
    signal_states.EVENT_MOMENTUM: "動能轉向",
    "calibration_regression": "AI 成績轉差",
}
_PINNED = {**_OWNER_WORDING, **_EVENT_WORDING}


def _leaks(text: str) -> list[str]:
    """Registered ids that appear verbatim in *text*."""
    return [rid for rid in _ALL_IDS if rid in text]


def _check_named(rid: str, text: str) -> None:
    """*text* names *rid* in words: no id, not 「未命名規則」, and the pinned wording when the
    rule has one (a rule newer than this file is still held to the first two — the contract
    test, whose parent set is the registry, fails it outright if it has no name)."""
    assert not _leaks(text), f"{rid}: shows a rule id: {text}"
    assert "未命名規則" not in text, f"{rid}: has no name: {text}"
    if rid in _PINNED:
        assert _PINNED[rid] in text, f"{rid}: want 「{_PINNED[rid]}」 in: {text}"


# --- the push ---------------------------------------------------------------------


@pytest.mark.parametrize("rid", _ALL_IDS)
def test_every_push_names_the_rule_in_words(rid: str) -> None:
    for subject, scope in (("2330", "symbol"), (None, "portfolio"), ("5", "task")):
        title, body, _sev = notify.format_event(rid, subject, scope=scope)
        _check_named(rid, title)
        _check_named(rid, body)


def test_the_calibration_regression_push_is_named() -> None:
    title, body, sev = notify.format_event("calibration_regression", "5", scope="task")
    assert title == "portfolio-dash · 洞察任務 5 AI 成績轉差", title
    assert "calibration_regression" not in body and sev == "info"


# --- the digest -------------------------------------------------------------------


def _mem_with_events(rule_ids: list[str]) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ab.ensure_tables(c)
    for rid in rule_ids:
        c.execute("INSERT INTO alert_events (rule_id, symbol, fired_at, consumed) "
                  "VALUES (?,?,?,0)", (rid, "2330", NOW.date().isoformat() + "T09:00:00"))
    c.commit()
    return c


def test_the_digest_names_every_rule_it_counts() -> None:
    ids = [rid for rid in _ALL_IDS if not rid.startswith("signal_") and rid != "quota_low"]
    c = _mem_with_events(ids)
    today = {g["rule_id"]: g["label"] for g in ds._alerts_today(c, NOW)}
    week = {g["rule_id"]: g["label"] for g in ds._alert_review_week(c, now=NOW)}
    assert set(today) == set(ids) and set(week) == set(ids)
    for rid in ids:
        _check_named(rid, today[rid])
        _check_named(rid, week[rid])
    assert today["exdiv_upcoming"] == "即將除息提醒"          # the owner's wording
    assert today["calibration_regression"] == "AI 成績轉差"   # no longer the raw id


# --- an insight card's 觸發預警 chip ------------------------------------------------


@pytest.mark.parametrize("rid", _ALL_IDS)
def test_the_card_chip_names_the_rule(rid: str) -> None:
    wire = _trigger_wire(InsightTrigger(source="alert", rule=rid))
    assert wire is not None
    _check_named(rid, str(wire["rule_label"]))


def test_the_card_chip_never_shows_an_unknown_id() -> None:
    wire = _trigger_wire(InsightTrigger(source="alert", rule="retired_rule"))
    assert wire is not None and wire["rule_label"] == "未命名規則", wire


# --- the R7 gate message ------------------------------------------------------------


def test_the_r7_gate_message_names_the_rule() -> None:
    ctx = GateContext(scope="on_alert", live_strategy_count=1, budget_remaining=Decimal("5"),
                      alert_rules=["single_weight"], fired_rule="vol_spike",
                      fired_symbol="2330")
    r7 = [g for g in evaluate_gates(ctx).gates if g.id == "R7"]
    assert r7 and "波動突升" in r7[0].msg and not _leaks(r7[0].msg), r7


# --- 排程中心 › alert_scan run detail ------------------------------------------------


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    jobs.create_scheduler_tables(c)
    cs.ensure_seeded(c)
    ab.ensure_tables(c)
    cs.create_insight_type(c, name="持倉提點", scope="on_alert", alert_rules="all",
                           enabled=True, now=NOW)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    jobs.register_insight_runner(None)
    jobs.register_alert_held_fn(None)
    yield
    jobs.register_insight_runner(None)
    jobs.register_alert_held_fn(None)


def _runner(c: sqlite3.Connection, insight_type_id: int, *, now: datetime,
            fired_rule: str, fired_symbol: str | None, trigger: Any) -> None:
    return None


def test_the_alert_scan_run_detail_names_every_rule(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    alerts = [
        Alert(id="vol_spike:2330", sev="warn", rule="vol_spike", title="2330 波動突升",
              detail="30 日年化波動 60%", href="/symbol/2330", scope="symbol", subject="2330"),
        Alert(id="drawdown_from_peak:1234", sev="risk", rule="drawdown_from_peak",
              title="1234 自高點回撤", detail="回撤 22%", href="/symbol/1234",
              scope="symbol", subject="1234"),
        Alert(id="fx_drift:moomoo_my", sev="info", rule="fx_drift", title="匯率偏離成本",
              detail="偏離 6%", href="cash.html#fx", scope="account", subject="moomoo_my"),
    ]
    monkeypatch.setattr(jobs, "_compute_alerts_for_scan", lambda c, *, now: alerts)
    jobs.register_insight_runner(_runner)
    jobs.register_alert_held_fn(lambda c, *, now: {"2330"})
    detail = jobs.alert_scan(conn, now=NOW)
    assert "[波動突升、高點回撤、匯率漂移]" in detail, detail              # the fired rules
    assert "略過 1 條非個股預警（不產個股卡）：匯率漂移 帳戶" in detail, detail  # the skipped one
    assert "略過 1 條觀察標的預警（未持有，不產卡）：高點回撤 1234" in detail, detail
    assert not _leaks(detail), f"排程中心 run detail shows rule ids: {detail}"
