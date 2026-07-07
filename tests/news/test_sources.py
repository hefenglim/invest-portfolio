"""Unit tests for news-link discovery (injectable clients; no network)."""

from portfolio_dash.news import sources as S


def test_from_finmind_normalizes_and_skips_bad_rows() -> None:
    rows = [
        {"date": "2026-07-05 00:00:18", "stock_id": "2330", "link": "http://a",
         "source": "CMoney", "title": "台積電擴產"},
        {"date": "2026-07-05", "stock_id": "2330", "link": "", "title": "no link"},  # skip
    ]
    out = S.from_finmind(rows)
    assert len(out) == 1
    assert out[0].link == "http://a" and out[0].lang == "zh"
    assert out[0].date == "2026-07-05" and out[0].source == "CMoney"


def test_from_yfinance_handles_nested_content_shape() -> None:
    items = [
        {"id": "1", "content": {
            "title": "Why TSMC Rose", "pubDate": "2026-07-04T10:00:00Z",
            "provider": {"displayName": "Yahoo"},
            "clickThroughUrl": {"url": "http://y1"}}},
        {"id": "2", "content": {"title": "no url"}},  # skipped
    ]
    out = S.from_yfinance(items)
    assert len(out) == 1
    assert out[0].link == "http://y1" and out[0].lang == "en"
    assert out[0].source == "Yahoo" and out[0].date == "2026-07-04"


def test_parse_yahoo_list_extracts_article_anchors() -> None:
    html = (
        '<div><a href="https://tw.stock.yahoo.com/news/foo-bar-123.html">'
        '台積電法說會前瞻</a></div>'
        '<a href="https://tw.stock.yahoo.com/news/foo-bar-123.html">dup</a>'  # de-duped
        '<a href="/quote/2330.TW">not news</a>'
    )
    out = S.parse_yahoo_list(html)
    assert len(out) == 1
    assert out[0].link.endswith("foo-bar-123.html") and out[0].source == "Yahoo 股市"


def test_discover_tw_merges_finmind_yf_yahoo_and_dedupes() -> None:
    fin = lambda sid, start: [  # noqa: E731
        {"date": "2026-07-05", "link": "http://shared", "title": "中文新聞", "source": "CM"}]
    yf = lambda tkr: [  # noqa: E731
        {"content": {"title": "English", "clickThroughUrl": {"url": "http://shared"}}},  # dup url
        {"content": {"title": "EN2", "clickThroughUrl": {"url": "http://en2"}}}]
    yahoo = lambda url: (  # noqa: E731
        '<a href="https://tw.stock.yahoo.com/news/z.html">Yahoo標題</a>')
    out = S.discover_links("2330", "TW", finmind_client=fin, yf_client=yf,
                           yahoo_fetcher=yahoo, finmind_start="2026-07-01")
    urls = [n.link for n in out]
    assert "http://shared" in urls and "http://en2" in urls
    assert urls.count("http://shared") == 1  # de-duped across FinMind + yfinance
    assert any(u.endswith("z.html") for u in urls)  # Yahoo merged


def test_discover_us_uses_yfinance_only() -> None:
    yf = lambda tkr: [{"content": {"title": "T", "clickThroughUrl": {"url": "http://u"}}}]  # noqa: E731
    out = S.discover_links("AAPL", "US", yf_client=yf)
    assert [n.link for n in out] == ["http://u"] and out[0].lang == "en"


def test_discover_swallows_a_failing_source() -> None:
    def boom(sid: str, start: str) -> list[dict[str, object]]:
        raise RuntimeError("finmind down")

    yf = lambda tkr: [{"content": {"title": "T", "clickThroughUrl": {"url": "http://ok"}}}]  # noqa: E731
    out = S.discover_links("2330", "TW", finmind_client=boom, yf_client=yf,
                           finmind_start="2026-07-01")
    assert [n.link for n in out] == ["http://ok"]  # yfinance still delivered


def test_discover_tw_falls_back_to_two_when_tw_empty() -> None:
    # L6 fix: a TPEx (上櫃) symbol yields nothing under .TW — retry .TWO on empty only.
    seen: list[str] = []

    def yf(tkr: str) -> list[dict[str, object]]:
        seen.append(tkr)
        if tkr.endswith(".TWO"):
            return [{"content": {"title": "TPEx news",
                                 "clickThroughUrl": {"url": "http://tpex"}}}]
        return []

    out = S.discover_links("8069", "TW", yf_client=yf)
    assert seen == ["8069.TW", "8069.TWO"]
    assert [n.link for n in out] == ["http://tpex"]


def test_discover_tw_no_second_call_when_tw_yields() -> None:
    # The fallback is cheap: a TWSE symbol answering under .TW never triggers .TWO.
    seen: list[str] = []

    def yf(tkr: str) -> list[dict[str, object]]:
        seen.append(tkr)
        return [{"content": {"title": "T", "clickThroughUrl": {"url": "http://tw"}}}]

    out = S.discover_links("2330", "TW", yf_client=yf)
    assert seen == ["2330.TW"]
    assert [n.link for n in out] == ["http://tw"]


def test_parse_yf_date_iso_and_epoch_and_garbage() -> None:
    assert S._parse_yf_date("2026-07-04T10:00:00Z") == "2026-07-04"
    assert S._parse_yf_date(1751600000) == "2025-07-04"  # UNIX epoch -> real date
    assert S._parse_yf_date("1720000000") == "2024-07-03"  # epoch string, not "1720"
    assert S._parse_yf_date("not-a-date") is None
    assert S._parse_yf_date(None) is None
    assert S._parse_yf_date(42) is None  # too small to be epoch seconds
