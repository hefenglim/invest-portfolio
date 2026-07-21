"""Unit tests for the news pipeline orchestration (all seams injected; no I/O)."""

import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from portfolio_dash.news import pipeline as P
from portfolio_dash.news import store as ns
from portfolio_dash.news.fetcher import FetchOutcome
from portfolio_dash.news.sources import NewsLink
from portfolio_dash.shared.llm_config import AINotActivated, LLMUnavailable

NOW = datetime(2026, 7, 6, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def _recent(days: int) -> str:
    """A fetched_at *days* before real UTC now (retry candidacy uses SQLite date('now'))."""
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _seed_empty(conn: sqlite3.Connection, link: str, *, fetched_at: str,
                attempts: int = 0) -> None:
    """Seed a headline-only (empty-body) row as if a prior run had left it."""
    ns.upsert_news(conn, ns.OrganizedNews(
        link=link, title="seed", news_date="2026-07-05", body_summary="",
        related_stocks=[], source="src", lang="zh",
        fetched_at=fetched_at, organized_at=fetched_at), discovered_for="2330",
        index_symbols={"2330"})
    for _ in range(attempts):
        ns.record_fetch_attempt(conn, link, status="error")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ns.create_tables(c)
    return c


def _org(link: NewsLink, text: str) -> ns.OrganizedNews:
    return ns.OrganizedNews(
        link=link.link, title=link.title, news_date="2026-07-05",
        body_summary="整理後摘要。", related_stocks=["2330"], source=link.source,
        lang=link.lang, fetched_at=NOW.isoformat(), organized_at=NOW.isoformat())


def test_happy_path_organizes_and_stores() -> None:
    conn = _conn()
    links = [NewsLink(title="n1", link="http://1"), NewsLink(title="n2", link="http://2")]
    res = P.run_news_pipeline(
        conn, [("2330", "TW")],
        discover=lambda s, m: links, fetch=lambda u: "body text", organize=_org, now=NOW)
    assert res["organized"] == 2 and res["headline_only"] == 0
    assert len(ns.query_by_symbol(conn, "2330", since_date="2026-07-01")) == 2


def test_fetch_miss_degrades_to_headline_only() -> None:
    conn = _conn()
    res = P.run_news_pipeline(
        conn, [("2330", "TW")],
        discover=lambda s, m: [NewsLink(title="n", link="http://x")],
        fetch=lambda u: None, organize=_org, now=NOW)
    assert res["organized"] == 0 and res["headline_only"] == 1
    row = ns.query_by_symbol(conn, "2330", since_date="2026-07-01")[0]
    assert row.body_summary == "" and row.title == "n"  # headline kept, no summary


def test_dedup_skips_existing_links() -> None:
    conn = _conn()
    ns.upsert_news(conn, _org(NewsLink(title="n", link="http://dup"), "t"), discovered_for="2330")
    res = P.run_news_pipeline(
        conn, [("2330", "TW")],
        discover=lambda s, m: [NewsLink(title="n", link="http://dup")],
        fetch=lambda u: "t", organize=_org, now=NOW)
    assert res["skipped_existing"] == 1 and res["organized"] == 0


def test_per_symbol_cap() -> None:
    conn = _conn()
    links = [NewsLink(title=f"n{i}", link=f"http://{i}") for i in range(10)]
    res = P.run_news_pipeline(
        conn, [("2330", "TW")],
        discover=lambda s, m: links, fetch=lambda u: "t", organize=_org, now=NOW,
        per_symbol_cap=3)
    assert res["organized"] == 3


def test_budget_stop_ends_run_partial() -> None:
    conn = _conn()

    def organize(link: NewsLink, text: str) -> ns.OrganizedNews:
        raise AINotActivated("no default model")

    res = P.run_news_pipeline(
        conn, [("2330", "TW"), ("AAPL", "US")],
        discover=lambda s, m: [NewsLink(title="n", link=f"http://{s}")],
        fetch=lambda u: "t", organize=organize, now=NOW)
    assert res["stopped_budget"] is True
    # the first symbol's article is kept headline-only; the second symbol never runs.
    assert res["headline_only"] == 1 and res["organized"] == 0


def test_transient_unavailable_degrades_but_continues() -> None:
    conn = _conn()
    calls = {"n": 0}

    def organize(link: NewsLink, text: str) -> ns.OrganizedNews:
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMUnavailable("blip")
        return _org(link, text)

    res = P.run_news_pipeline(
        conn, [("2330", "TW")],
        discover=lambda s, m: [NewsLink(title="a", link="http://a"),
                               NewsLink(title="b", link="http://b")],
        fetch=lambda u: "t", organize=organize, now=NOW)
    assert res["headline_only"] == 1 and res["organized"] == 1  # first degraded, second ok
    assert res["stopped_budget"] is False


def test_discovery_failure_for_one_symbol_is_swallowed() -> None:
    conn = _conn()

    def discover(s: str, m: str) -> list[NewsLink]:
        if s == "2330":
            raise RuntimeError("finmind down")
        return [NewsLink(title="n", link="http://ok")]

    res = P.run_news_pipeline(
        conn, [("2330", "TW"), ("AAPL", "US")],
        discover=discover, fetch=lambda u: "t", organize=_org, now=NOW)
    assert res["organized"] == 1  # AAPL still processed


def test_skip_unions_discovered_for_into_mentions() -> None:
    # SR fix: an already-stored article surfaced by a second symbol's feed must still
    # add THAT symbol to the mentions index (same-sector coverage).
    conn = _conn()
    ns.upsert_news(conn, _org(NewsLink(title="n", link="http://x"), "t"),
                   discovered_for="2330")
    assert ns.query_by_symbol(conn, "2317", since_date="2026-07-01") == []  # not yet
    res = P.run_news_pipeline(
        conn, [("2317", "TW")],  # a different holding whose feed surfaces the same link
        discover=lambda s, m: [NewsLink(title="n", link="http://x")],
        fetch=lambda u: "t", organize=_org, now=NOW)
    assert res["skipped_existing"] == 1
    # 2317 now finds the article even though it was first ingested under 2330.
    assert ns.query_by_symbol(conn, "2317", since_date="2026-07-01")[0].link == "http://x"


def test_headline_only_row_is_retried_on_later_run() -> None:
    # SR fix: a transient fetch miss leaves a headline-only row; a later run re-fetches
    # and upgrades it (dedup skips only fully-organized links).
    conn = _conn()
    # run 1: fetch fails -> headline-only
    P.run_news_pipeline(conn, [("2330", "TW")],
        discover=lambda s, m: [NewsLink(title="n", link="http://x")],
        fetch=lambda u: None, organize=_org, now=NOW)
    assert ns.is_fully_organized(conn, "http://x") is False
    # run 2: fetch works -> organized, upgraded
    res = P.run_news_pipeline(conn, [("2330", "TW")],
        discover=lambda s, m: [NewsLink(title="n", link="http://x")],
        fetch=lambda u: "body", organize=_org, now=NOW)
    assert res["organized"] == 1 and res["skipped_existing"] == 0
    assert ns.is_fully_organized(conn, "http://x") is True


def test_headline_upgrade_under_other_symbol_keeps_first_mention() -> None:
    # M2 fix (2026-07-07): night 1 stores the link headline-only under A; night 2 B's
    # feed retries the same link and the fetch succeeds → the upgrade must PRESERVE A's
    # legitimate discovered_for mention (the old DELETE-then-rewrite wiped it).
    conn = _conn()
    P.run_news_pipeline(conn, [("2330", "TW")],
        discover=lambda s, m: [NewsLink(title="n", link="http://x")],
        fetch=lambda u: None, organize=_org, now=NOW)  # headline-only under 2330
    assert ns.query_by_symbol(conn, "2330", since_date="2026-07-01")[0].link == "http://x"
    res = P.run_news_pipeline(conn, [("2317", "TW")],
        discover=lambda s, m: [NewsLink(title="n", link="http://x")],
        fetch=lambda u: "body", organize=_org, now=NOW)  # upgraded under 2317
    assert res["organized"] == 1
    assert ns.is_fully_organized(conn, "http://x") is True
    # BOTH holdings still surface the article.
    assert ns.query_by_symbol(conn, "2330", since_date="2026-07-01")[0].link == "http://x"
    assert ns.query_by_symbol(conn, "2317", since_date="2026-07-01")[0].link == "http://x"


def test_pipeline_indexes_only_held_symbols() -> None:
    # SR fix: the organizer says an article relates to 9999 (not held); it must NOT be
    # retrievable under 9999, but IS under the held discovering symbol.
    conn = _conn()
    def organize(link: NewsLink, text: str) -> ns.OrganizedNews:
        return ns.OrganizedNews(
            link=link.link, title="t", news_date="2026-07-05", body_summary="s",
            related_stocks=["9999"], source="s", lang="zh",
            fetched_at=NOW.isoformat(), organized_at=NOW.isoformat())
    P.run_news_pipeline(conn, [("2330", "TW")],
        discover=lambda s, m: [NewsLink(title="n", link="http://x")],
        fetch=lambda u: "t", organize=organize, now=NOW)
    assert ns.query_by_symbol(conn, "9999", since_date="2026-07-01") == []  # not indexed
    assert ns.query_by_symbol(conn, "2330", since_date="2026-07-01")[0].link == "http://x"


# ---------------------------------------------------------------------------
# Fetch-status recording + retry stage (2026-07-21).
# ---------------------------------------------------------------------------

def _status(conn: sqlite3.Connection, link: str) -> tuple[str, int]:
    r = conn.execute(
        "SELECT fetch_status, fetch_attempts FROM organized_news WHERE link = ?",
        (link,)).fetchone()
    return r["fetch_status"], r["fetch_attempts"]


def test_fetch_status_and_attempts_recorded_for_organized() -> None:
    # A rich FetchOutcome flows through the seam: the classified status lands on the row and
    # one attempt is counted.
    conn = _conn()
    P.run_news_pipeline(conn, [("2330", "TW")],
        discover=lambda s, m: [NewsLink(title="n", link="http://1")],
        fetch=lambda u: FetchOutcome("body", "salvaged", "p_cluster"),
        organize=_org, now=NOW)
    status, attempts = _status(conn, "http://1")
    assert status == "salvaged" and attempts == 1


def test_fetch_status_recorded_on_degrade() -> None:
    # A failing fetch records the failure reason (not a silent empty row).
    conn = _conn()
    P.run_news_pipeline(conn, [("2330", "TW")],
        discover=lambda s, m: [NewsLink(title="n", link="http://x")],
        fetch=lambda u: FetchOutcome(None, "http_error", "HTTP 403"),
        organize=_org, now=NOW)
    status, attempts = _status(conn, "http://x")
    assert status == "http_error" and attempts == 1
    assert ns.query_by_symbol(conn, "2330", since_date="2026-07-01")[0].body_summary == ""


def test_retry_stage_recovers_aged_empty_row_discovery_no_longer_surfaces() -> None:
    # The core new behavior: an empty row from a prior run is re-fetched even though the
    # current discovery surfaces nothing, and a successful fetch organizes it.
    conn = _conn()
    _seed_empty(conn, "http://aged", fetched_at=_recent(3), attempts=1)
    res = P.run_news_pipeline(conn, [("2330", "TW")],
        discover=lambda s, m: [],  # discovery no longer finds the link
        fetch=lambda u: "recovered body", organize=_org, now=NOW)
    assert res["refetched"] == 1 and res["organized"] == 1
    assert ns.is_fully_organized(conn, "http://aged") is True
    _, attempts = _status(conn, "http://aged")
    assert attempts == 2  # 1 seeded + 1 this run
    # the recovered article is retrievable under its held symbol
    assert ns.query_by_symbol(conn, "2330", since_date="2026-07-01")[0].link == "http://aged"


def test_retry_stage_increments_attempts_on_repeat_failure() -> None:
    conn = _conn()
    _seed_empty(conn, "http://aged", fetched_at=_recent(3), attempts=1)
    res = P.run_news_pipeline(conn, [("2330", "TW")],
        discover=lambda s, m: [], fetch=lambda u: FetchOutcome(None, "too_short"),
        organize=_org, now=NOW)
    assert res["refetched"] == 1 and res["organized"] == 0
    status, attempts = _status(conn, "http://aged")
    assert attempts == 2 and status == "too_short"
    assert ns.is_fully_organized(conn, "http://aged") is False  # still empty, retriable


def test_retry_stage_respects_attempt_cap() -> None:
    conn = _conn()
    _seed_empty(conn, "http://capped", fetched_at=_recent(3), attempts=3)  # at the cap
    res = P.run_news_pipeline(conn, [("2330", "TW")],
        discover=lambda s, m: [], fetch=lambda u: "body", organize=_org, now=NOW,
        refetch_max_attempts=3)
    assert res["refetched"] == 0 and res["organized"] == 0  # not retried
    assert ns.is_fully_organized(conn, "http://capped") is False


def test_retry_stage_bounded_per_run() -> None:
    conn = _conn()
    for i in range(12):
        _seed_empty(conn, f"http://a{i}", fetched_at=_recent(2))
    res = P.run_news_pipeline(conn, [("2330", "TW")],
        discover=lambda s, m: [], fetch=lambda u: "body", organize=_org, now=NOW,
        refetch_limit=10)
    assert res["refetched"] == 10 and res["organized"] == 10  # bounded to the limit


def test_retry_stage_skips_links_fetched_in_discovery_this_run() -> None:
    # A link both discovered AND a retry candidate is fetched ONCE (main loop), never twice.
    conn = _conn()
    _seed_empty(conn, "http://x", fetched_at=_recent(1), attempts=1)
    res = P.run_news_pipeline(conn, [("2330", "TW")],
        discover=lambda s, m: [NewsLink(title="n", link="http://x")],
        fetch=lambda u: None, organize=_org, now=NOW)  # fetch keeps failing
    _, attempts = _status(conn, "http://x")
    assert attempts == 2 and res["refetched"] == 0  # main loop bumped once; retry skipped it


def test_retry_stage_skipped_when_budget_stopped() -> None:
    # A budget stop in the main loop ends the run; the retry stage must not run afterwards.
    conn = _conn()
    _seed_empty(conn, "http://aged", fetched_at=_recent(3), attempts=1)

    def organize(link: NewsLink, text: str) -> ns.OrganizedNews:
        raise AINotActivated("no default model")

    res = P.run_news_pipeline(conn, [("2330", "TW")],
        discover=lambda s, m: [NewsLink(title="n", link="http://new")],
        fetch=lambda u: "body", organize=organize, now=NOW)
    assert res["stopped_budget"] is True and res["refetched"] == 0
    _, attempts = _status(conn, "http://aged")
    assert attempts == 1  # untouched — retry never ran
