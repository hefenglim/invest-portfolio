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


def test_index_symbols_constrains_mentions_but_stores_all() -> None:
    # SR fix: related_stocks are STORED for display, but only held tickers (+discovered_for)
    # enter the mentions index — a hallucinated/injected ticker can't surface elsewhere.
    conn = _conn()
    ns.upsert_news(conn, _item("http://a", "2026-07-05", ["2330", "9999", "AAPL"]),
                   discovered_for="2330", index_symbols={"2330", "AAPL"})
    # stored related_stocks keep all three (display)
    row = ns.query_by_symbol(conn, "2330", since_date="2026-07-01")[0]
    assert set(row.related_stocks) == {"2330", "9999", "AAPL"}
    # but 9999 (not held) is NOT indexed -> no card retrieval under it
    assert ns.query_by_symbol(conn, "9999", since_date="2026-07-01") == []
    assert ns.query_by_symbol(conn, "AAPL", since_date="2026-07-01")[0].link == "http://a"


def test_is_fully_organized_vs_headline_only() -> None:
    conn = _conn()
    # headline-only degrade (empty summary)
    ns.upsert_news(conn, ns.OrganizedNews(
        link="http://h", title="t", news_date="2026-07-05", body_summary="",
        related_stocks=["2330"], source="s", lang="zh",
        fetched_at="x", organized_at="x"), discovered_for="2330")
    assert ns.link_exists(conn, "http://h") is True
    assert ns.is_fully_organized(conn, "http://h") is False  # retriable
    ns.upsert_news(conn, _item("http://h", "2026-07-05", ["2330"]))  # now has summary
    assert ns.is_fully_organized(conn, "http://h") is True


def test_upsert_merges_mentions_across_symbols() -> None:
    # M2 fix (2026-07-07): a link first stored headline-only under A, later upgraded
    # under B, must keep BOTH mentions (the old DELETE-then-rewrite wiped A's).
    conn = _conn()
    ns.upsert_news(conn, ns.OrganizedNews(
        link="http://l", title="t", news_date="2026-07-05", body_summary="",
        related_stocks=[], source="s", lang="zh",
        fetched_at="x", organized_at="x"), discovered_for="2330",
        index_symbols={"2330", "2317"})
    assert ns.query_by_symbol(conn, "2330", since_date="2026-07-01")[0].link == "http://l"
    # second night: B's feed retries the same link and the fetch/LLM succeeds.
    ns.upsert_news(conn, _item("http://l", "2026-07-05", ["2317"]),
                   discovered_for="2317", index_symbols={"2330", "2317"})
    assert ns.is_fully_organized(conn, "http://l") is True
    # BOTH symbols still surface the article.
    assert ns.query_by_symbol(conn, "2330", since_date="2026-07-01")[0].link == "http://l"
    assert ns.query_by_symbol(conn, "2317", since_date="2026-07-01")[0].link == "http://l"


def test_upsert_merge_still_respects_index_allowlist() -> None:
    # The merge unions with EXISTING mentions, but new mentions still pass the
    # held-universe allowlist (a hallucinated ticker never enters the index).
    conn = _conn()
    ns.upsert_news(conn, _item("http://m", "2026-07-05", ["2330"]),
                   discovered_for="2330", index_symbols={"2330"})
    ns.upsert_news(conn, _item("http://m", "2026-07-05", ["9999"]),
                   discovered_for="2317", index_symbols={"2330", "2317"})
    assert ns.query_by_symbol(conn, "2330", since_date="2026-07-01")[0].link == "http://m"
    assert ns.query_by_symbol(conn, "2317", since_date="2026-07-01")[0].link == "http://m"
    assert ns.query_by_symbol(conn, "9999", since_date="2026-07-01") == []


def test_query_news_filters_and_cost_totals() -> None:
    from decimal import Decimal
    conn = _conn()
    ns.upsert_news(conn, ns.OrganizedNews(
        link="http://1", title="台積", news_date="2026-07-05", body_summary="s1",
        related_stocks=["2330"], source="CMoney", lang="zh",
        cost_usd=Decimal("0.003"), tokens_in=500, tokens_out=60,
        fetched_at="x", organized_at="x"), discovered_for="2330")
    ns.upsert_news(conn, ns.OrganizedNews(
        link="http://2", title="AAPL", news_date="2026-06-20", body_summary="s2",
        related_stocks=["AAPL"], source="Reuters", lang="en",
        cost_usd=Decimal("0.004"), tokens_in=600, tokens_out=70,
        fetched_at="x", organized_at="x"), discovered_for="AAPL")
    rows, totals = ns.query_news(conn, limit=50)
    assert totals["count"] == 2 and totals["total_cost_usd"] == Decimal("0.007")
    # symbol filter
    tw, t2 = ns.query_news(conn, symbol="2330")
    assert [r.link for r in tw] == ["http://1"] and t2["count"] == 1
    # date filter
    recent, t3 = ns.query_news(conn, date_from="2026-07-01")
    assert [r.link for r in recent] == ["http://1"]
    # source filter
    reut, _ = ns.query_news(conn, source="Reuters")
    assert [r.link for r in reut] == ["http://2"]
    assert ns.distinct_symbols(conn) == ["2330", "AAPL"]
    assert ns.distinct_sources(conn) == ["CMoney", "Reuters"]
    # cost round-trips on the stored row
    assert tw[0].cost_usd == Decimal("0.003") and tw[0].tokens_in == 500


def test_model_column_round_trips() -> None:
    # AI attribution (2026-07-07): the organizing model's alias persists per item.
    conn = _conn()
    item = _item("http://m", "2026-07-05", ["2330"])
    item = item.model_copy(update={"model": "haiku-4.5"})
    ns.upsert_news(conn, item, discovered_for="2330")
    row = ns.query_by_symbol(conn, "2330", since_date="2026-07-01")[0]
    assert row.model == "haiku-4.5"


# ---------------------------------------------------------------------------
# Fetch-observability columns + retry backlog (2026-07-21).
# ---------------------------------------------------------------------------

from datetime import UTC, datetime, timedelta  # noqa: E402


def _days_ago(n: int) -> str:
    """A fetched_at ISO stamp *n* days before real UTC now (list_refetch_candidates uses
    SQLite date('now'), so candidates must be dated relative to wall-clock, not a fixture)."""
    return (datetime.now(UTC) - timedelta(days=n)).isoformat()


def _empty_row(link: str, *, fetched_at: str, title: str = "H") -> ns.OrganizedNews:
    """A headline-only degrade row (empty body) with a chosen fetched_at."""
    return ns.OrganizedNews(
        link=link, title=title, news_date="2026-07-05", body_summary="",
        related_stocks=["2330"], source="src", lang="zh",
        fetched_at=fetched_at, organized_at=fetched_at,
    )


def test_fetch_columns_migration_idempotent() -> None:
    # create_tables adds fetch_status / fetch_attempts via ALTER-if-missing; re-running is a
    # no-op (an already-migrated DB must not error on the second call).
    conn = _conn()
    ns.create_tables(conn)  # second call — must not raise
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(organized_news)")}
    assert {"fetch_status", "fetch_attempts"} <= cols


def test_record_fetch_attempt_bumps_and_sets_status() -> None:
    conn = _conn()
    ns.upsert_news(conn, _empty_row("http://a", fetched_at=_days_ago(1)), discovered_for="2330")
    ns.record_fetch_attempt(conn, "http://a", status="http_error")
    ns.record_fetch_attempt(conn, "http://a", status="too_short")
    row = conn.execute(
        "SELECT fetch_attempts, fetch_status FROM organized_news WHERE link = ?",
        ("http://a",)).fetchone()
    assert row["fetch_attempts"] == 2 and row["fetch_status"] == "too_short"
    # no-op on an unknown link (row upserted first by the caller in real use)
    ns.record_fetch_attempt(conn, "http://missing", status="error")  # must not raise


def test_list_refetch_candidates_excludes_non_empty_bodies() -> None:
    conn = _conn()
    ns.upsert_news(conn, _item("http://full", "2026-07-05", ["2330"]),
                   discovered_for="2330")  # has a summary
    ns.upsert_news(conn, _empty_row("http://empty", fetched_at=_days_ago(2)),
                   discovered_for="2330")
    links = [c.link for c in ns.list_refetch_candidates(conn)]
    assert links == ["http://empty"]


def test_list_refetch_candidates_age_window() -> None:
    conn = _conn()
    ns.upsert_news(conn, _empty_row("http://recent", fetched_at=_days_ago(2)),
                   discovered_for="2330")
    ns.upsert_news(conn, _empty_row("http://old", fetched_at=_days_ago(40)),
                   discovered_for="2330")
    links = [c.link for c in ns.list_refetch_candidates(conn, max_age_days=14)]
    assert links == ["http://recent"]  # the 40-day-old row aged out


def test_list_refetch_candidates_attempt_cap() -> None:
    conn = _conn()
    ns.upsert_news(conn, _empty_row("http://tries2", fetched_at=_days_ago(1)),
                   discovered_for="2330")
    ns.upsert_news(conn, _empty_row("http://tries3", fetched_at=_days_ago(1)),
                   discovered_for="2330")
    for _ in range(2):
        ns.record_fetch_attempt(conn, "http://tries2", status="error")
    for _ in range(3):
        ns.record_fetch_attempt(conn, "http://tries3", status="error")
    links = [c.link for c in ns.list_refetch_candidates(conn, max_attempts=3)]
    assert links == ["http://tries2"]  # tries3 hit the cap and drops out


def test_list_refetch_candidates_limit() -> None:
    conn = _conn()
    for i in range(15):
        ns.upsert_news(conn, _empty_row(f"http://e{i}", fetched_at=_days_ago(1)),
                       discovered_for="2330")
    assert len(ns.list_refetch_candidates(conn, limit=10)) == 10
