"""DEF-057 (owner ruling 2026-09-24) — the HTTP half.

The verifier's path is 設定 › AI 提示詞 › 系統提示詞 / 新聞整理提示詞 → edit → 儲存 (PUT
``/api/system-prompt`` / ``/api/news-prompt``) and 重置回官方版 (POST ``…/reset``). Every door
that writes one of the two bodies is driven here through the real router and must leave a
version with an honest ``source``; then each prompt's history list, one version's body, the
server-side diff, the restore endpoint (a NEW version), the refusal of the other prompt's
version ids, the action-log label of the one write the history adds, and what a card
(``GET /api/insights``) and a news row (``GET /api/news``) record.
"""

import sqlite3
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api.action_log import label_for
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight import official_templates
from portfolio_dash.llm_insight.cards import InsightCard
from portfolio_dash.llm_insight.system_prompt import SystemPromptRef
from portfolio_dash.news import store as ns
from portfolio_dash.news.store import OrganizedNews
from tests.conftest import GOLDEN_NOW

OFFICIAL_SYS = official_templates.SYSTEM_PROMPT_BODY
OFFICIAL_NEWS = official_templates.NEWS_ORGANIZER_PROMPT
KINDS = [("system", "/api/system-prompt", "系統提示詞"),
         ("news", "/api/news-prompt", "新聞整理提示詞")]


def _versions(client: TestClient, base: str) -> list[dict[str, object]]:
    r = client.get(base + "/versions")
    assert r.status_code == 200, r.text
    out: list[dict[str, object]] = r.json()["versions"]
    return out


@pytest.mark.parametrize(("kind", "base", "name"), KINDS)
def test_every_save_and_reset_keeps_a_version_the_list_can_show(
    api_client: TestClient, kind: str, base: str, name: str
) -> None:
    assert api_client.get(base).json()["current_version"] == 1  # the back-filled body
    r = api_client.put(base, json={"body": "我的版本\n共同"})
    assert r.status_code == 200 and r.json()["current_version"] == 2
    api_client.put(base, json={"body": "我的版本\n共同"})  # unchanged → no version
    r = api_client.post(base + "/reset")
    assert r.status_code == 200 and r.json()["current_version"] == 3
    api_client.post(base + "/reset")  # already official → no version
    listed = api_client.get(base + "/versions").json()
    assert listed["kind"] == kind and listed["name"] == name
    assert listed["current_version"] == 3
    vs = listed["versions"]
    assert [(v["version"], v["source"], v["source_label"], v["is_current"]) for v in vs] == [
        (3, "reset_official", "還原官方", True), (2, "user_save", "儲存", False),
        (1, "migration", "啟用版本記錄時的內容", False),
    ]
    assert vs[1]["lines"] == 2 and "body" not in vs[1]  # the list carries no bodies
    assert api_client.get(base).json()["current_version"] == 3


@pytest.mark.parametrize(("kind", "base", "name"), KINDS)
def test_a_version_reads_back_diffs_and_restores_as_a_new_version(
    api_client: TestClient, kind: str, base: str, name: str
) -> None:
    api_client.put(base, json={"body": "第一版\n共同"})
    api_client.put(base, json={"body": "第二版\n共同\n新增一行"})
    vs = {v["version"]: v for v in _versions(api_client, base)}
    # one version, with its body
    one = api_client.get(f"{base}/versions/{vs[2]['id']}").json()
    assert one["body"] == "第一版\n共同" and one["is_current"] is False and one["kind"] == kind
    # diff v2 → current (v3), chronological
    d = api_client.get(f"{base}/versions/{vs[2]['id']}/diff").json()
    assert d["from"]["version"] == 2 and d["to"]["version"] == 3
    assert [(ln["op"], ln["text"]) for ln in d["lines"]] == [
        ("del", "第一版"), ("add", "第二版"), ("same", "共同"), ("add", "新增一行"),
    ]
    prev = api_client.get(f"{base}/versions/{vs[3]['id']}/diff",
                          params={"against": "previous"}).json()
    assert prev["lines"] == d["lines"]
    first = api_client.get(f"{base}/versions/{vs[1]['id']}/diff",
                           params={"against": "previous"}).json()
    assert first["from"] is None and {ln["op"] for ln in first["lines"]} == {"add"}
    bad = api_client.get(f"{base}/versions/{vs[1]['id']}/diff", params={"against": "x"})
    assert bad.status_code == 400
    # restore v2 → a NEW v4; v1..v3 untouched
    r = api_client.post(f"{base}/versions/{vs[2]['id']}/restore")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["changed"] is True and got["restored_from"] == 2 and got["current_version"] == 4
    assert got["prompt"]["body"] == "第一版\n共同" and got["prompt"]["current_version"] == 4
    assert api_client.get(base).json()["body"] == "第一版\n共同"
    after = _versions(api_client, base)
    assert [(v["version"], v["source"], v["restored_from"]) for v in after] == [
        (4, "restore", 2), (3, "user_save", None), (2, "user_save", None),
        (1, "migration", None),
    ]
    assert after[0]["source_label"] == "回復"
    again = api_client.post(f"{base}/versions/{after[0]['id']}/restore").json()
    assert again["changed"] is False and len(_versions(api_client, base)) == 4
    assert api_client.post(f"{base}/versions/99999/restore").status_code == 404
    assert api_client.get(f"{base}/versions/99999").status_code == 404


def test_one_prompt_never_reads_or_restores_the_other_ones_versions(
    api_client: TestClient,
) -> None:
    api_client.put("/api/news-prompt", json={"body": "新聞二"})
    news_v1 = next(v for v in _versions(api_client, "/api/news-prompt") if v["version"] == 1)
    sys_v1 = _versions(api_client, "/api/system-prompt")[0]
    r = api_client.post(f"/api/system-prompt/versions/{news_v1['id']}/restore")
    assert r.status_code == 404 and "系統提示詞" in r.json()["error"]["message"]
    assert api_client.get(f"/api/system-prompt/versions/{news_v1['id']}").status_code == 404
    cross = api_client.get(f"/api/news-prompt/versions/{news_v1['id']}/diff",
                           params={"against": str(sys_v1["id"])})
    assert cross.status_code == 400 and "同一則" in cross.json()["error"]["message"]
    assert api_client.get("/api/system-prompt").json()["body"] == OFFICIAL_SYS


def test_blank_is_still_refused_and_leaves_no_version(api_client: TestClient) -> None:
    for base in ("/api/system-prompt", "/api/news-prompt"):
        assert api_client.put(base, json={"body": "   "}).status_code == 422
        assert len(_versions(api_client, base)) == 1


def test_each_restore_write_has_its_own_action_log_label() -> None:
    assert label_for("POST", "/api/system-prompt/versions/5/restore") == "系統提示詞回復版本"
    assert label_for("POST", "/api/news-prompt/versions/5/restore") == "新聞提示詞回復版本"
    # the older rows still label their own doors
    assert label_for("POST", "/api/system-prompt/reset") == "系統提示詞重設"
    assert label_for("PUT", "/api/news-prompt") == "新聞提示詞變更"


def test_a_card_shows_the_system_prompt_version_it_was_generated_with(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    istore.ensure_tables(golden_db)
    card = InsightCard(title="t", summary="s", body_md="b")
    common = {"insight_type_id": 1, "calibration_version": None, "horizon_days": 5,
              "input_snapshot": "x", "model": "m", "cost_usd": Decimal("0"),
              "now": GOLDEN_NOW}
    istore.add_card(golden_db, card=card, fingerprint="legacy", **common)  # type: ignore[arg-type]
    istore.add_card(golden_db, card=card, fingerprint="off", **common,  # type: ignore[arg-type]
                    strategy_versions=[], system_prompt_ref=SystemPromptRef(used=False))
    istore.add_card(golden_db, card=card, fingerprint="new", **common,  # type: ignore[arg-type]
                    strategy_versions=[], system_prompt_ref=SystemPromptRef(used=True,
                                                                            version=3))
    rows = api_client.get("/api/insights").json()["rows"]
    newest, off, legacy = rows[0], rows[1], rows[2]
    assert newest["system_prompt_version"] == {"used": True, "version": 3}
    assert off["system_prompt_version"] == {"used": False, "version": None}
    assert legacy["system_prompt_version"] is None  # 「生成時版本未記錄」, never a guess


def test_a_news_row_shows_the_organizer_prompt_version(api_client: TestClient) -> None:
    with ns.news_session() as conn:
        conn.execute("DELETE FROM news_mentions")
        conn.execute("DELETE FROM organized_news")
        for link, ver in (("http://a", 4), ("http://b", None)):
            ns.upsert_news(conn, OrganizedNews(
                link=link, title="標題", news_date=GOLDEN_NOW.date().isoformat(),
                body_summary="摘要" if ver else "", related_stocks=["2330"], source="s",
                lang="zh", prompt_version=ver, fetched_at=GOLDEN_NOW.isoformat(),
                organized_at=GOLDEN_NOW.isoformat()), discovered_for="2330")
    items = {i["link"]: i for i in api_client.get("/api/news").json()["items"]}
    assert items["http://a"]["prompt_version"] == 4
    assert items["http://b"]["prompt_version"] is None  # headline-only: no prompt used
