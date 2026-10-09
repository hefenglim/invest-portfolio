"""DEF-033 (functional test H-03, owner ruling 2026-09-24) — the HTTP half.

The verifier's path is 設定 › AI 提示詞 › 策略卡 → edit → 儲存 (PUT ``/api/strategy-prompts/{id}``).
Every door that writes a strategy body is driven here through the real router and must leave
a version with an honest ``source``: 新增 (POST), 自官方模板加入 (from-template copy and the
official pack), 儲存 (PUT), 同步官方 (from-template replace), 回復 (restore). Then the history
list, one version's body, the server-side diff, the restore endpoint, the action-log label of
the one write the history adds, and the card's own record on ``GET /api/insights``.
"""

import sqlite3
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from portfolio_dash.api.action_log import label_for
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight import official_templates
from portfolio_dash.llm_insight.cards import InsightCard, Prediction
from portfolio_dash.llm_insight.composer_store import StrategyVersionRef

NOW = datetime(2026, 9, 24, 10, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _versions(client: TestClient, sid: int) -> list[dict[str, object]]:
    r = client.get("/api/strategy-prompt-versions", params={"strategy_id": sid})
    assert r.status_code == 200, r.text
    out: list[dict[str, object]] = r.json()["versions"]
    return out


def _mk(client: TestClient, body: str = "第一版\n共同") -> int:
    r = client.post("/api/strategy-prompts", json={"name": "我的策略", "body": body})
    assert r.status_code == 200
    assert r.json()["current_version"] == 1
    return int(r.json()["id"])


def test_every_save_keeps_a_version_the_list_can_show(api_client: TestClient) -> None:
    sid = _mk(api_client)
    r = api_client.put(f"/api/strategy-prompts/{sid}",
                       json={"name": "我的策略", "body": "第二版\n共同", "enabled": True})
    assert r.status_code == 200 and r.json()["current_version"] == 2
    # the enable toggle PUTs the stored body — no new version
    api_client.put(f"/api/strategy-prompts/{sid}",
                   json={"name": "我的策略", "body": "第二版\n共同", "enabled": False})
    vs = _versions(api_client, sid)
    assert [(v["version"], v["source"], v["source_label"], v["is_current"]) for v in vs] == [
        (2, "user_save", "儲存", True), (1, "create", "新增", False),
    ]
    assert vs[0]["lines"] == 2 and "body" not in vs[0]  # the list carries no bodies
    listed = {s["id"]: s for s in api_client.get("/api/strategy-prompts").json()}
    assert listed[sid]["current_version"] == 2


def test_one_version_reads_back_with_its_body(api_client: TestClient) -> None:
    sid = _mk(api_client)
    api_client.put(f"/api/strategy-prompts/{sid}",
                   json={"name": "我的策略", "body": "改過", "enabled": True})
    v1 = next(v for v in _versions(api_client, sid) if v["version"] == 1)
    r = api_client.get(f"/api/strategy-prompt-versions/{v1['id']}")
    assert r.status_code == 200
    assert r.json()["body"] == "第一版\n共同" and r.json()["is_current"] is False
    assert api_client.get("/api/strategy-prompt-versions/99999").status_code == 404


def test_the_diff_is_computed_by_the_server_in_version_order(api_client: TestClient) -> None:
    sid = _mk(api_client)
    api_client.put(f"/api/strategy-prompts/{sid}",
                   json={"name": "我的策略", "body": "第二版\n共同\n新增一行", "enabled": True})
    vs = {v["version"]: v for v in _versions(api_client, sid)}
    # v1 against the current version → from v1 to v2
    d = api_client.get(f"/api/strategy-prompt-versions/{vs[1]['id']}/diff").json()
    assert d["from"]["version"] == 1 and d["to"]["version"] == 2
    assert [(ln["op"], ln["text"]) for ln in d["lines"]] == [
        ("del", "第一版"), ("add", "第二版"), ("same", "共同"), ("add", "新增一行"),
    ]
    assert (d["added"], d["removed"], d["identical"]) == (2, 1, False)
    # "what did this save change" = the current version against its previous one
    prev = api_client.get(f"/api/strategy-prompt-versions/{vs[2]['id']}/diff",
                          params={"against": "previous"}).json()
    assert prev["lines"] == d["lines"]
    # v1 has no previous: every line reads as added, and there is no "from"
    first = api_client.get(f"/api/strategy-prompt-versions/{vs[1]['id']}/diff",
                           params={"against": "previous"}).json()
    assert first["from"] is None and {ln["op"] for ln in first["lines"]} == {"add"}
    # against a named version id, either way round, still reads older → newer
    named = api_client.get(f"/api/strategy-prompt-versions/{vs[2]['id']}/diff",
                           params={"against": str(vs[1]["id"])}).json()
    assert named["lines"] == d["lines"]


def test_the_diff_refuses_nonsense(api_client: TestClient) -> None:
    a = _mk(api_client)
    b = _mk(api_client, body="別的")
    va = _versions(api_client, a)[0]["id"]
    vb = _versions(api_client, b)[0]["id"]
    bad = api_client.get(f"/api/strategy-prompt-versions/{va}/diff", params={"against": "x"})
    assert bad.status_code == 400
    cross = api_client.get(f"/api/strategy-prompt-versions/{va}/diff",
                           params={"against": str(vb)})
    assert cross.status_code == 400
    assert "同一則" in cross.json()["error"]["message"]


def test_restore_any_version_is_a_new_version(api_client: TestClient) -> None:
    sid = _mk(api_client)
    for body in ("二", "三"):
        api_client.put(f"/api/strategy-prompts/{sid}",
                       json={"name": "我的策略", "body": body, "enabled": True})
    v1 = next(v for v in _versions(api_client, sid) if v["version"] == 1)
    r = api_client.post(f"/api/strategy-prompt-versions/{v1['id']}/restore")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["changed"] is True and got["restored_from"] == 1 and got["current_version"] == 4
    assert got["strategy"]["body"] == "第一版\n共同" and got["strategy"]["scope"] == "portfolio"
    vs = _versions(api_client, sid)
    assert [(v["version"], v["source"], v["restored_from"]) for v in vs] == [
        (4, "restore", 1), (3, "user_save", None), (2, "user_save", None),
        (1, "create", None),
    ]
    assert vs[0]["source_label"] == "回復"
    # restoring what is already current writes nothing
    again = api_client.post(f"/api/strategy-prompt-versions/{vs[0]['id']}/restore").json()
    assert again["changed"] is False and len(_versions(api_client, sid)) == 4
    assert api_client.post("/api/strategy-prompt-versions/99999/restore").status_code == 404


def test_an_archived_strategy_refuses_a_restore(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    sid = _mk(api_client)
    api_client.put(f"/api/strategy-prompts/{sid}",
                   json={"name": "我的策略", "body": "二", "enabled": True})
    golden_db.execute("UPDATE strategy_prompts SET archived = 1 WHERE id = ?", (sid,))
    golden_db.commit()
    v1 = next(v for v in _versions(api_client, sid) if v["version"] == 1)
    r = api_client.post(f"/api/strategy-prompt-versions/{v1['id']}/restore")
    assert r.status_code == 409 and "封存" in r.json()["error"]["message"]


def test_the_official_doors_record_their_own_source(api_client: TestClient) -> None:
    tpl = official_templates.STRATEGY_TEMPLATES[0]
    r = api_client.post("/api/strategy-prompts/from-template", json={"name": tpl["name"]})
    sid = int(r.json()["id"])
    assert [v["source"] for v in _versions(api_client, sid)] == ["official_copy"]
    api_client.put(f"/api/strategy-prompts/{sid}",
                   json={"name": tpl["name"], "body": "我的修改", "enabled": True})
    r = api_client.post("/api/strategy-prompts/from-template",
                        json={"name": tpl["name"], "mode": "replace", "strategy_id": sid})
    assert r.status_code == 200
    vs = _versions(api_client, sid)
    assert [v["source"] for v in vs] == ["sync_official", "user_save", "official_copy"]
    assert vs[0]["source_label"] == "同步官方"
    # the owner's overwritten edit is still readable — 同步官方 no longer destroys it
    body = api_client.get(f"/api/strategy-prompt-versions/{vs[1]['id']}").json()["body"]
    assert body == "我的修改"


def test_the_official_pack_records_its_strategies_as_official_copies(
    api_client: TestClient,
) -> None:
    api_client.post("/api/insight-tasks/official-pack")
    for s in api_client.get("/api/strategy-prompts").json():
        assert [v["source"] for v in _versions(api_client, int(s["id"]))] == ["official_copy"]


def test_the_restore_write_has_its_own_action_log_label() -> None:
    assert label_for("POST", "/api/strategy-prompt-versions/5/restore") == "策略模板回復版本"
    # and the older prefix rows still label their own doors
    assert label_for("POST", "/api/strategy-prompts") == "策略模板新增"


def test_a_card_shows_the_version_it_was_generated_with(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    istore.ensure_tables(golden_db)
    card = InsightCard(title="t", summary="s", body_md="b", confidence=50,
                       prediction=Prediction(metric="price_change", direction="up",
                                             horizon_days=5))
    common = {"insight_type_id": 1, "calibration_version": None, "horizon_days": 5,
              "input_snapshot": "x", "model": "m", "cost_usd": Decimal("0"), "now": NOW}
    istore.add_card(golden_db, card=card, fingerprint="legacy", **common)  # type: ignore[arg-type]
    istore.add_card(golden_db, card=card, fingerprint="new", **common,  # type: ignore[arg-type]
                    strategy_versions=[StrategyVersionRef(strategy_id=3, name="持倉健診",
                                                          version=4)])
    rows = api_client.get("/api/insights").json()["rows"]
    by_fp = {r["id"]: r for r in rows}
    newest, legacy = rows[0], rows[1]
    assert newest["prompt_versions"] == [{"strategy_id": 3, "name": "持倉健診", "version": 4}]
    assert legacy["prompt_versions"] is None  # 「生成時版本未記錄」, never a guess
    assert len(by_fp) == 2
