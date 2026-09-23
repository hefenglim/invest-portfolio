"""DEF-037: a card says what triggered it, and an alert card is TOLD which alert fired.

Measured on the demo: 2884's alert card #207 was titled 「玉山金(2884) - RSI過熱警示」 while the
alert that actually fired was ``target_cross`` (price below the owner's 50 floor), and the
card API carried no field naming the rule. The on_alert template asks the model to "state in
one sentence what fired (rule and value)", but no variable ever carried the fired alert — the
prompt said only 「本卡由風險預警觸發」. The model therefore guessed, and a guess is what it
printed. Two different alerts on one symbol on one day also produced the SAME prompt, so the
second was a fingerprint cache hit: its card was never written at all.

The fix records the trigger on every card (``insights.trigger_json``: alert / schedule /
manual), feeds the fired alert's own title and detail — the rule engine's computed text,
never an LLM number — into the on_alert note, and serves both through the card API.
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import alert_inputs
from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import generate
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.llm_insight.cards import InsightCard
from portfolio_dash.llm_insight.generate import RunInputs
from portfolio_dash.llm_insight.insights_store import InsightTrigger
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
_WEB = Path(__file__).resolve().parents[2] / "web"
_CARD_JSON = (
    '{"title":"2330 提點","summary":"跌破目標價","body_md":"觀察。","tags":["TW"],'
    '"symbol":null,"confidence":60,"prediction":null}'
)


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [type("M", (), {"message": type("X", (), {"content": content})()})()]
        self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5})()


@pytest.fixture
def conn(golden_db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    cs.ensure_seeded(golden_db)
    istore.ensure_tables(golden_db)
    ensure_llm_seeded(golden_db)
    upsert_model(golden_db, ModelConfig(
        id="m", model_alias="m", provider="openai", model_name="m",
        input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2"),
    ))
    set_role(golden_db, LLMRole.DEFAULT, "m")
    add_topup(golden_db, Decimal("100"))
    yield golden_db


@pytest.fixture
def prompts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the LLM and capture every prompt it is handed."""
    seen: list[str] = []

    def completion(**kw: Any) -> _Resp:
        seen.append("\n".join(str(m.get("content")) for m in kw["messages"]))
        return _Resp(_CARD_JSON)

    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)
    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    return seen


def _ctx(conn: sqlite3.Connection, symbol: str | None = None) -> V.VarContext:
    return V.VarContext(data=build_dashboard(conn, now=NOW, reporting=Currency.TWD),
                        now=NOW, symbol=symbol)


def _alert_task(conn: sqlite3.Connection) -> int:
    sp = cs.create_strategy(conn, name="提點", body="{{symbol_detail_json}}", now=NOW)
    it = cs.create_insight_type(conn, name="持倉提點", scope="on_alert", alert_rules="all",
                                enabled=True, now=NOW)
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    return it.id


def _trigger(rule: str, title: str, detail: str, alert_id: int) -> InsightTrigger:
    return InsightTrigger(source="alert", rule=rule, alert_id=alert_id,
                          fired_at=NOW.isoformat(), scope="symbol", subject="2330",
                          title=title, detail=detail)


def _run_alert(conn: sqlite3.Connection, it_id: int, trig: InsightTrigger) -> None:
    generate.run_insight_type(
        conn, it_id, var_contexts={"2330": _ctx(conn, "2330")},
        inputs=RunInputs(budget_remaining=Decimal("100"), fired_rule=trig.rule,
                         fired_symbol="2330", trigger=trig),
        now=NOW,
    )


def test_an_alert_card_is_fed_the_fired_alert_and_records_it(
    conn: sqlite3.Connection, prompts: list[str]
) -> None:
    it_id = _alert_task(conn)
    trig = _trigger("target_cross", "2330 跌破目標價", "現價 480 ≤ 目標下限 500", 7)
    _run_alert(conn, it_id, trig)
    assert len(prompts) == 1
    assert "2330 跌破目標價" in prompts[0] and "現價 480 ≤ 目標下限 500" in prompts[0]
    cards = istore.list_cards(conn, insight_type_id=it_id)
    assert len(cards) == 1
    got = cards[0].trigger
    assert got is not None
    assert (got.source, got.rule, got.alert_id, got.subject) == (
        "alert", "target_cross", 7, "2330")


def test_two_alerts_on_one_symbol_on_one_day_are_two_cards(
    conn: sqlite3.Connection, prompts: list[str]
) -> None:
    it_id = _alert_task(conn)
    _run_alert(conn, it_id, _trigger("target_cross", "2330 跌破目標價", "現價 480 ≤ 500", 7))
    _run_alert(conn, it_id, _trigger("vol_spike", "2330 波動突升", "30 日年化波動 40%", 8))
    cards = istore.list_cards(conn, insight_type_id=it_id)
    assert sorted(str(c.trigger.rule) for c in cards if c.trigger) == ["target_cross", "vol_spike"]


def test_manual_and_scheduled_cards_record_their_source(
    conn: sqlite3.Connection, prompts: list[str]
) -> None:
    sp = cs.create_strategy(conn, name="S", body="觀察 {{kpis_json}}", now=NOW)
    it = cs.create_insight_type(conn, name="Daily", scope="portfolio", now=NOW)
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    generate.run_insight_type(
        conn, it.id, var_contexts={None: _ctx(conn)},
        inputs=RunInputs(budget_remaining=Decimal("100"),
                         trigger=InsightTrigger(source="schedule")),
        now=NOW,
    )
    cards = istore.list_cards(conn, insight_type_id=it.id)
    assert len(cards) == 1 and cards[0].trigger is not None
    assert cards[0].trigger.source == "schedule"
    # no alert text leaks into a non-alert prompt
    assert "觸發預警" not in prompts[0]


def test_the_card_api_serves_the_trigger_with_the_rule_label(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    istore.ensure_tables(golden_db)
    cs.ensure_seeded(golden_db)
    it = cs.create_insight_type(golden_db, name="持倉提點", scope="on_alert",
                                alert_rules="all", enabled=True, now=NOW)
    istore.add_card(
        golden_db, insight_type_id=it.id,
        card=InsightCard(title="2330 提點", summary="s", body_md="b", tags=[], symbol="2330"),
        fingerprint="fp", calibration_version=None, horizon_days=3, input_snapshot="x",
        model="m", cost_usd=Decimal("0"), now=NOW,
        trigger=_trigger("target_cross", "2330 跌破目標價", "現價 480 ≤ 目標下限 500", 7),
    )
    istore.add_card(
        golden_db, insight_type_id=it.id,
        card=InsightCard(title="舊卡", summary="s", body_md="b", tags=[], symbol="2330"),
        fingerprint="fp0", calibration_version=None, horizon_days=3, input_snapshot="x",
        model="m", cost_usd=Decimal("0"), now=NOW,
    )
    rows = api_client.get("/api/insights", params={"symbol": "2330"}).json()["rows"]
    by_title = {r["title"]: r for r in rows}
    t = by_title["2330 提點"]["trigger"]
    assert t["source"] == "alert" and t["rule"] == "target_cross" and t["alert_id"] == 7
    assert t["rule_label"] == "目標價穿越"
    assert t["title"] == "2330 跌破目標價"
    assert by_title["舊卡"]["trigger"] is None      # a legacy card claims nothing


def test_the_insights_page_renders_the_trigger() -> None:
    html = (_WEB / "insights.html").read_text(encoding="utf-8")
    assert "function triggerChip(card)" in html
    assert "由預警" in html and "settings.html#alerts" in html
    # both card faces draw it
    assert html.count("triggerChip(card)") >= 3


_EXPECTED_SCOPE = {
    "single_weight": "symbol", "stale_price": "symbol", "missing_price": "symbol",
    "exdiv_upcoming": "symbol", "drawdown_from_peak": "symbol", "vol_spike": "symbol",
    "rebalance_drift": "symbol", "consensus_change": "symbol", "target_cross": "symbol",
    "sector_weight": "sector", "fx_drift": "account", "currency_weight": "currency",
    "quota_low": "portfolio", "calib_gap": "portfolio", "portfolio_drawdown": "portfolio",
}


def test_the_real_rule_engine_states_each_alerts_scope(golden_db: sqlite3.Connection) -> None:
    """Over the golden ledger (fx_drift:schwab, sector_weight, single_weight … all fire)."""
    alerts = alert_inputs.compute_alerts_full(golden_db, now=NOW, reporting=Currency.TWD)
    assert {a.rule for a in alerts} >= {"fx_drift", "sector_weight", "single_weight"}
    for a in alerts:
        assert a.scope == _EXPECTED_SCOPE[a.rule], (a.id, a.scope)
        if a.scope == "portfolio":
            assert a.subject is None and a.id == a.rule
        else:
            assert a.subject and a.id.startswith(f"{a.rule}:{a.subject}")
