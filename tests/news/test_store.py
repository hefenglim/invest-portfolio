"""Unit tests for the separate-SQLite news store (schema, upsert/dedup, symbol query)."""

import sqlite3

from portfolio_dash.news import store as ns


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ns.create_tables(c)
    return c


def _item(link: str, date: str, stocks: list[str], title: str = "T") -> ns.OrganizedNews:
    return ns.OrganizedNews(
        link=link, title=title, news_date=date, body_summary="摘要。",
        related_stocks=stocks, source="src", lang="zh",
        fetched_at="2026-07-06T00:00:00+08:00", organized_at="2026-07-06T00:05:00+08:00",
    )


def test_upsert_and_query_by_symbol() -> None:
    conn = _conn()
    ns.upsert_news(conn, _item("http://a", "2026-07-05", ["2330", "2454"]))
    ns.upsert_news(conn, _item("http://b", "2026-07-04", ["AAPL"]))
    tw = ns.query_by_symbol(conn, "2330", since_date="2026-07-01")
    assert [n.link for n in tw] == ["http://a"]
    assert ns.query_by_symbol(conn, "2454", since_date="2026-07-01")[0].link == "http://a"
    assert ns.query_by_symbol(conn, "AAPL", since_date="2026-07-01")[0].link == "http://b"


def test_discovered_for_is_always_a_mention() -> None:
    # a card for the discovered symbol finds the news even if the model didn't name it.
    conn = _conn()
    ns.upsert_news(conn, _item("http://c", "2026-07-05", ["NVDA"]), discovered_for="2330")
    assert ns.query_by_symbol(conn, "2330", since_date="2026-07-01")[0].link == "http://c"


def test_dedup_on_link_and_link_exists() -> None:
    conn = _conn()
    ns.upsert_news(conn, _item("http://a", "2026-07-05", ["2330"], title="v1"))
    assert ns.link_exists(conn, "http://a") is True
    ns.upsert_news(conn, _item("http://a", "2026-07-05", ["2330"], title="v2"))
    rows = conn.execute("SELECT COUNT(*) AS n FROM organized_news").fetchone()
    assert rows["n"] == 1  # updated, not duplicated
    assert ns.query_by_symbol(conn, "2330", since_date="2026-07-01")[0].title == "v2"


def test_precise_ticker_match_no_substring_collision() -> None:
    conn = _conn()
    ns.upsert_news(conn, _item("http://x", "2026-07-05", ["23301"]))  # different ticker
    assert ns.query_by_symbol(conn, "2330", since_date="2026-07-01") == []


def test_date_window_lower_bound() -> None:
    conn = _conn()
    ns.upsert_news(conn, _item("http://old", "2026-06-01", ["2330"]))
    ns.upsert_news(conn, _item("http://new", "2026-07-05", ["2330"]))
    recent = ns.query_by_symbol(conn, "2330", since_date="2026-07-01")
    assert [n.link for n in recent] == ["http://new"]


def test_news_db_path_beside_ledger() -> None:
    p = ns.news_db_path()
    assert p.name == "news.db"
    assert p.parent == get_settings().db_path.parent


from portfolio_dash.shared.config import get_settings  # noqa: E402
