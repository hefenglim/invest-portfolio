"""Contract tests for the batch-④ news surfaces: symbol_news_json var + news-prompt CRUD.

The news DB is a SEPARATE SQLite file; these tests write to it via the news store and
assert the preview endpoint reads it into symbol_news_json, then exercise the editable
news-organizer prompt (get/put/reset). Uses the shared api_client (guest mode). The news
DB path derives from the configured db_path.parent, which the golden fixture points at a
temp dir, so the test writes/reads an isolated news.db.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.news import store as ns
from portfolio_dash.news.store import OrganizedNews

# The api_client's get_now returns GOLDEN_NOW (2026-06-11); the news window is computed
# from THAT, so seed relative to it, not wall-clock today.
from tests.conftest import GOLDEN_NOW

_NOW = GOLDEN_NOW


@pytest.fixture(autouse=True)
def _clean_news_db() -> None:
    """The news DB is a session-scoped file; clear it before each test for isolation."""
    with ns.news_session() as conn:
        conn.execute("DELETE FROM news_mentions")
        conn.execute("DELETE FROM organized_news")


def _seed_news(symbol: str, *, days_ago: int = 1) -> None:
    d = (GOLDEN_NOW.date() - timedelta(days=days_ago)).isoformat()
    with ns.news_session() as conn:
        ns.upsert_news(conn, OrganizedNews(
            link=f"http://news/{symbol}", title=f"{symbol} 重大新聞",
            news_date=d, body_summary="AI 整理後的摘要。", related_stocks=[symbol],
            source="測試來源", lang="zh",
            fetched_at=_NOW.isoformat(), organized_at=_NOW.isoformat()),
            discovered_for=symbol)


def test_symbol_news_var_reads_news_db(api_client: TestClient) -> None:
    _seed_news("2330", days_ago=1)
    r = api_client.post(
        "/api/prompts/preview",
        json={"body": "{{symbol_news_json}}", "scope": "per_symbol", "symbol": "2330"},
    )
    assert r.status_code == 200
    rendered = r.json()["rendered"]
    assert "2330 重大新聞" in rendered and "AI 整理後的摘要" in rendered
    assert '"count": 1' in rendered or '"count":1' in rendered


def test_symbol_news_var_empty_when_no_news(api_client: TestClient) -> None:
    import json as _json
    r = api_client.post(
        "/api/prompts/preview",
        json={"body": "{{symbol_news_json}}", "scope": "per_symbol", "symbol": "NOSUCH"},
    )
    value = _json.loads(r.json()["rendered"])
    assert value["count"] == 0 and value["items"] == []


def test_symbol_news_var_excludes_out_of_window(api_client: TestClient) -> None:
    import json as _json
    _seed_news("2412", days_ago=30)  # older than the 7-day window
    r = api_client.post(
        "/api/prompts/preview",
        json={"body": "{{symbol_news_json}}", "scope": "per_symbol", "symbol": "2412"},
    )
    assert _json.loads(r.json()["rendered"])["count"] == 0


def test_news_prompt_get_put_reset(api_client: TestClient) -> None:
    got = api_client.get("/api/news-prompt").json()
    assert got["body"] and "新聞整理員" in got["body"]  # official default seeded
    r = api_client.put("/api/news-prompt", json={"body": "我的新聞整理規則"})
    assert r.status_code == 200 and r.json()["body"] == "我的新聞整理規則"
    assert api_client.get("/api/news-prompt").json()["body"] == "我的新聞整理規則"
    reset = api_client.post("/api/news-prompt/reset").json()
    assert "新聞整理員" in reset["body"]
    assert api_client.get("/api/news-prompt").json()["body"] == reset["body"]


# --- /api/news browse endpoint (batch ④ news library page) ---------------------


def _seed_full(link: str, symbol: str, *, date: str, source: str, cost: str) -> None:
    from decimal import Decimal
    with ns.news_session() as conn:
        ns.upsert_news(conn, OrganizedNews(
            link=link, title=f"{symbol} 新聞", news_date=date,
            body_summary="AI 整理摘要。", related_stocks=[symbol], source=source,
            lang="zh", cost_usd=Decimal(cost), tokens_in=500, tokens_out=60,
            fetched_at=_NOW.isoformat(), organized_at=_NOW.isoformat()),
            discovered_for=symbol)


def test_news_list_filters_and_totals(api_client: TestClient) -> None:
    _seed_full("http://a", "2330", date="2026-07-05", source="CMoney", cost="0.003")
    _seed_full("http://b", "AAPL", date="2026-06-20", source="Reuters", cost="0.004")
    all_r = api_client.get("/api/news").json()
    assert all_r["totals"]["count"] == 2
    assert all_r["totals"]["total_cost_usd"] == "0.007"  # Decimal string, summed
    assert {i["title"] for i in all_r["items"]} == {"2330 新聞", "AAPL 新聞"}
    it = next(i for i in all_r["items"] if i["title"] == "2330 新聞")
    assert it["cost_usd"] == "0.003" and it["tokens_in"] == 500 and it["headline_only"] is False
    # symbol + date filters
    tw = api_client.get("/api/news?symbol=2330").json()
    assert tw["totals"]["count"] == 1 and tw["items"][0]["link"] == "http://a"
    recent = api_client.get("/api/news?date_from=2026-07-01").json()
    assert [i["link"] for i in recent["items"]] == ["http://a"]
    src = api_client.get("/api/news?source=Reuters").json()
    assert [i["link"] for i in src["items"]] == ["http://b"]


def test_news_filters_endpoint(api_client: TestClient) -> None:
    _seed_full("http://c", "2454", date="2026-07-05", source="鉅亨網", cost="0.002")
    f = api_client.get("/api/news/filters").json()
    assert "2454" in f["stocks"] and "鉅亨網" in f["sources"]


def test_news_list_empty_ok(api_client: TestClient) -> None:
    r = api_client.get("/api/news").json()
    assert r["items"] == [] and r["totals"]["count"] == 0 and r["totals"]["total_cost_usd"] == "0"
