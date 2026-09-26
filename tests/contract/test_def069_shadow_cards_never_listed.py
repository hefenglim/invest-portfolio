"""DEF-069 (R6) — a shadow card is never a user-facing card.

Spec 04 §4.6: 「影子同批次並行產出（不展示）」.

The verifier's G-08 run stored, for one per_symbol batch, 9 shown cards (no calibration) and 9
shadow cards (calibration v2). ``GET /api/insights`` returned all 18: 持倉健診 headed every
symbol with its SHADOW card and folded the real one into 「歷史 2 筆」, and the drawer's AI 建議
(``/api/insights?symbol=``) could open on it. Only the dashboard embed (``latest_cards``)
filtered ``is_shadow = 0``.

Each case stores ONE shown card and, one id later (so it would lead any newest-first read), ONE
shadow card for the same symbol and task, then reads every shape the page uses. The battle
record is the deliberate exception: 預測明細 keeps the shadow evaluation row, labelled, because
comparing a shadow with the shown version is exactly what it is for.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import evaluations_store as es
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight.cards import InsightCard, Prediction

NOW = datetime(2026, 9, 10, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def _seed(golden_db: sqlite3.Connection) -> tuple[int, int, int]:
    """(task id, shown card id, shadow card id) — the shadow card is the NEWER row."""
    it = cs.create_insight_type(
        golden_db, name="個股健檢", scope="per_symbol", self_correct=True, now=NOW,
    )
    cs.create_calibration(golden_db, it.id, body="v1", cause=None, now=NOW)
    cs.create_calibration(golden_db, it.id, body="v2", cause=None, now=NOW)
    ids = []
    for shadow, title in ((False, "生效卡"), (True, "影子卡")):
        rec = istore.add_card(
            golden_db, insight_type_id=it.id,
            card=InsightCard(
                title=title, summary="s", body_md="b", symbol="2330", confidence=70,
                prediction=Prediction(metric="price_change", direction="up", horizon_days=5),
            ),
            fingerprint=f"fp-{title}", calibration_version=2 if shadow else None,
            horizon_days=5, input_snapshot="x", model="m", cost_usd=Decimal("0"), now=NOW,
            is_shadow=shadow,
        )
        ids.append(rec.id)
    return it.id, ids[0], ids[1]


def test_every_list_shape_returns_the_shown_card_only(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    tid, shown, shadow = _seed(golden_db)
    assert shadow > shown  # newest-first reads would have led with the shadow

    flat = api_client.get("/api/insights").json()
    assert [r["id"] for r in flat["rows"]] == [shown]
    assert flat["total_count"] == 1

    for query in (f"insight_type={tid}", "scope=symbol", "symbol=2330", "limit=500"):
        body = api_client.get(f"/api/insights?{query}").json()
        assert [r["id"] for r in body["rows"]] == [shown], query
        assert body["total_count"] == 1, query
        assert all(r["is_shadow"] is False for r in body["rows"]), query

    grouped = api_client.get("/api/insights?group=symbol&history_limit=5").json()
    assert grouped["total_count"] == 1
    (group,) = grouped["groups"]
    assert group["symbol"] == "2330"
    assert group["total"] == 1  # 「歷史 N 筆」 counts shown cards only
    assert [c["id"] for c in group["cards"]] == [shown]
    assert group["cards"][0]["title"] == "生效卡"  # the head card is the shown one


def test_a_symbol_with_only_shadow_cards_is_not_a_group(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    it = cs.create_insight_type(
        golden_db, name="個股健檢", scope="per_symbol", self_correct=True, now=NOW,
    )
    istore.add_card(
        golden_db, insight_type_id=it.id,
        card=InsightCard(title="影子卡", summary="s", body_md="b", symbol="AAPL"),
        fingerprint="fp", calibration_version=2, horizon_days=5, input_snapshot="x",
        model="m", cost_usd=Decimal("0"), now=NOW, is_shadow=True,
    )
    grouped = api_client.get("/api/insights?group=symbol").json()
    assert grouped == {
        "groups": [], "total_count": 0, "limit": 100, "offset": 0, "history_limit": 5,
    }
    assert api_client.get("/api/insights?symbol=AAPL").json()["rows"] == []


def test_the_battle_record_keeps_the_shadow_row_labelled(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    tid, shown, shadow = _seed(golden_db)
    for card_id, is_shadow, version in ((shown, False, None), (shadow, True, 2)):
        es.add_evaluation(
            golden_db, insight_id=card_id, insight_type_id=tid, calibration_version=version,
            is_shadow=is_shadow, status="scored", quant_hit=True, narrative_score=None,
            miss=False, actual_value=None, confidence=70, now=NOW,
        )
    rows = api_client.get("/api/ai-score").json()["rows"]
    assert sorted((r["insight_id"], r["is_shadow"]) for r in rows) == [
        (shown, False), (shadow, True),
    ]


def test_the_store_lists_shadow_cards_only_when_asked(golden_db: sqlite3.Connection) -> None:
    tid, shown, shadow = _seed(golden_db)
    assert [c.id for c in istore.list_cards(golden_db, insight_type_id=tid)] == [shown]
    assert istore.count_cards(golden_db, insight_type_id=tid) == 1
    both = istore.list_cards(golden_db, insight_type_id=tid, include_shadow=True)
    assert [c.id for c in both] == [shadow, shown]
    assert istore.count_cards(golden_db, insight_type_id=tid, include_shadow=True) == 2
