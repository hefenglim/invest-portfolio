"""Unit tests for the general HTML fetcher (injectable opener; never touches network)."""

from collections.abc import Callable

from portfolio_dash.news import fetcher as F

_HTML = (
    "<html><head><title>t</title><style>.x{color:red}</style>"
    "<script>var a=1;</script></head><body><nav>menu home about</nav>"
    "<h1>台積電法說會 7/16 登場</h1>"
    "<p>台積電將於 7 月 16 日召開法人說明會，聚焦全年成長目標與資本支出。</p>"
    "<p>市場預期 AI 需求續強。</p><footer>copyright</footer></body></html>"
)


def test_html_to_text_drops_noise_and_keeps_body() -> None:
    text = F.html_to_text(_HTML)
    assert "台積電法說會" in text and "聚焦全年成長目標" in text
    assert "var a=1" not in text  # script dropped
    assert "color:red" not in text  # style dropped
    assert "menu home about" not in text  # nav dropped
    assert "copyright" not in text  # footer dropped


def test_fetch_article_text_via_injected_opener() -> None:
    text = F.fetch_article_text("http://x", opener=lambda u: _HTML.encode("utf-8"))
    assert text is not None and "法說會" in text


def test_fetch_degrades_to_none_on_error() -> None:
    def boom(url: str) -> bytes:
        raise TimeoutError("slow")

    assert F.fetch_article_text("http://x", opener=boom) is None


def test_fetch_none_on_empty_or_paywall_shell() -> None:
    # a JS-only / paywall shell strips to < 40 chars -> None (degrade to headline)
    assert F.fetch_article_text(
        "http://x", opener=lambda u: b"<html><body><div></div></body></html>"
    ) is None


def test_fetch_respects_max_chars() -> None:
    big = "<p>" + ("台積電 " * 5000) + "</p>"
    text = F.fetch_article_text("http://x", opener=lambda u: big.encode("utf-8"),
                                max_chars=500)
    assert text is not None and len(text) <= 500


# A CSS-soup page: an unterminated <style> block that the block-strip regex cannot drop
# (the byte cap cut off the closing tag) — the class of garbage observed live (FM10:
# a summary literally describing "文章內容主要為網頁CSS程式碼").
_CSS_SOUP = (
    "<html><body><style>"
    + ".header{display:flex;align-items:center;margin:0 auto;padding:12px 8px;}"
      ".nav-item:hover{color:#0a66c2;text-decoration:underline;}"
      "@media(max-width:640px){.grid{grid-template-columns:1fr;gap:4px;}}" * 20
)


def test_css_soup_is_salvaged_not_dropped() -> None:
    # 2026-07-21: the prose guard now LABELS instead of DISCARDS — non-trivial CSS/JS soup
    # that fails the guard is returned as ``salvaged`` (owner directive: over-fetch, let the
    # LLM trim junk) so the row is a visible salvaged item, never a silent empty body.
    out = F.fetch_article("http://x", opener=lambda u: _CSS_SOUP.encode("utf-8"))
    assert out.status == "salvaged" and out.text is not None
    # the compat shape surfaces the salvaged text (no longer None for this case)
    assert F.fetch_article_text("http://x", opener=lambda u: _CSS_SOUP.encode("utf-8")) is not None


def test_looks_like_prose_accepts_zh_and_en_articles() -> None:
    zh = "台積電將於七月十六日召開法人說明會，市場聚焦全年成長目標與資本支出，法人普遍看好。"
    en = ("Apple reported quarterly revenue of 90.8 billion dollars, beating analyst "
          "expectations on strong services growth in every region.")
    assert F._looks_like_prose(zh * 3) is True
    assert F._looks_like_prose(en * 3) is True


def test_looks_like_prose_rejects_code_noise() -> None:
    css = ".a{margin:0;padding:0;}.b{color:#fff;}" * 10
    js = "function f(x){return x==null?0:x;} var a=1;" * 10
    assert F._looks_like_prose(css) is False
    assert F._looks_like_prose(js) is False
    assert F._looks_like_prose("") is False


def test_fetch_real_article_still_passes_prose_guard() -> None:
    text = F.fetch_article_text("http://x", opener=lambda u: _HTML.encode("utf-8"))
    assert text is not None and "法說會" in text


def test_default_opener_rejects_non_http_scheme() -> None:
    # SR fix: file://, ftp:// etc. must be refused before any request (SSRF/LFI guard).
    import pytest
    with pytest.raises(ValueError, match="non-http"):
        F._default_opener("file:///etc/passwd")
    with pytest.raises(ValueError, match="non-http"):
        F._default_opener("ftp://internal/secret")


# ---------------------------------------------------------------------------
# FetchOutcome status classification (C4.1) — every failure/salvage path is named.
# ---------------------------------------------------------------------------

import urllib.error  # noqa: E402


def _raise(exc: Exception) -> Callable[[str], bytes]:
    def opener(url: str) -> bytes:
        raise exc
    return opener


def test_outcome_ok_carries_text() -> None:
    out = F.fetch_article("http://x", opener=lambda u: _HTML.encode("utf-8"))
    assert out.status == "ok" and out.text is not None and "法說會" in out.text


def test_outcome_http_error() -> None:
    err = urllib.error.HTTPError("http://x", 403, "Forbidden", {}, None)  # type: ignore[arg-type]
    out = F.fetch_article("http://x", opener=_raise(err))
    assert out.status == "http_error" and out.text is None and out.detail == "HTTP 403"


def test_outcome_non_html() -> None:
    # a non-HTML content-type is raised by the opener as a classified _FetchError
    out = F.fetch_article("http://x", opener=_raise(F._FetchError("non_html", "application/pdf")))
    assert out.status == "non_html" and out.text is None and "pdf" in out.detail


def test_outcome_too_short_on_empty_shell() -> None:
    out = F.fetch_article("http://x", opener=lambda u: b"<html><body><div></div></body></html>")
    assert out.status == "too_short" and out.text is None


def test_outcome_blocked_scheme_without_calling_opener() -> None:
    called = {"n": 0}

    def opener(url: str) -> bytes:
        called["n"] += 1
        return b"x"

    out = F.fetch_article("file:///etc/passwd", opener=opener)
    assert out.status == "blocked_scheme" and out.text is None and called["n"] == 0


def test_outcome_error_on_transport_failure() -> None:
    out = F.fetch_article("http://x", opener=_raise(TimeoutError("slow")))
    assert out.status == "error" and out.text is None and out.detail == "TimeoutError"


# ---------------------------------------------------------------------------
# Extraction fallback chain (C4.3): JSON-LD / @graph / embedded state / <p> cluster /
# byte-cap recovery. Prose that block-strip alone would miss is recovered downstream.
# ---------------------------------------------------------------------------

_ARTICLE_PROSE = (
    "台積電今日召開法人說明會，管理層重申全年營收將維持雙位數成長，並上修資本支出以擴充"
    "先進製程產能，法人普遍看好人工智慧需求持續帶動高效能運算晶片出貨動能。"
)


def test_jsonld_articlebody_recovered_when_no_paragraphs() -> None:
    # The readable body lives ONLY in a JSON-LD blob; block-strip yields nothing usable.
    import json as _json
    ld = _json.dumps({"@type": "NewsArticle", "articleBody": _ARTICLE_PROSE},
                     ensure_ascii=False)
    html = (f'<html><head><script type="application/ld+json">{ld}</script></head>'
            "<body><div></div></body></html>")
    out = F.fetch_article("http://x", opener=lambda u: html.encode("utf-8"))
    assert out.status == "ok" and out.text is not None and "法人說明會" in out.text


def test_jsonld_graph_list_form_recovered() -> None:
    # @graph array form (Reuters/Bloomberg style) — the recursive search must find it.
    import json as _json
    ld = _json.dumps({"@context": "https://schema.org",
                      "@graph": [{"@type": "WebPage"},
                                 {"@type": "NewsArticle", "articleBody": _ARTICLE_PROSE}]},
                     ensure_ascii=False)
    html = f'<html><body><script type="application/ld+json">{ld}</script></body></html>'
    out = F.fetch_article("http://x", opener=lambda u: html.encode("utf-8"))
    assert out.status == "ok" and out.text is not None and "資本支出" in out.text


def test_embedded_state_body_recovered() -> None:
    # Yahoo-style embedded state: an HTML body string inside a script assignment (no ld+json).
    import json as _json
    inner = f"<p>{_ARTICLE_PROSE}</p>"
    blob = '{"context":{"dispatcher":{"stores":{"caas":{"body":' + _json.dumps(inner) + "}}}}}"
    html = f"<html><body><script>root.App.main = {blob};</script></body></html>"
    out = F.fetch_article("http://x", opener=lambda u: html.encode("utf-8"))
    assert out.status == "ok" and out.text is not None and "法人說明會" in out.text


def test_p_cluster_fallback_when_block_strip_is_noise() -> None:
    # No JSON-LD; the article is a run of <p> tags amid nav/aside noise the chain still joins.
    html = ("<html><body><aside>related links tags share</aside>"
            f"<article><p>{_ARTICLE_PROSE}</p><p>市場預期 AI 需求續強。</p></article>"
            "</body></html>")
    out = F.fetch_article("http://x", opener=lambda u: html.encode("utf-8"))
    assert out.status == "ok" and out.text is not None and "法人說明會" in out.text


def test_byte_cap_raised_to_1_5mb() -> None:
    # The read cap was raised from 200 KB so a body pushed past the old window survives.
    assert F._MAX_BYTES == 1_500_000


def test_unclosed_style_truncation_recovered_via_chain() -> None:
    # Reproduce the FM10 byte-cap artifact WITHOUT the real cap: a mid-block byte-cap
    # truncation yields an UNCLOSED <style> (no </style>), which _DROP_BLOCKS cannot remove,
    # so block-strip is dominated by CSS soup and fails the prose guard — exactly what the
    # OLD code returned None for. The fallback chain now recovers the article via the <p>
    # cluster, so the body is no longer lost.
    css = ".header{display:flex;align-items:center;margin:0;padding:12px;color:#0a66c2;}" * 400
    html = f"<html><body><style>{css}<p>{_ARTICLE_PROSE}</p></body></html>"  # note: no </style>
    assert not F._looks_like_prose(F.html_to_text(html)[:F._MAX_TEXT_CHARS])  # block-strip fails
    out = F.fetch_article("http://x", opener=lambda u: html.encode("utf-8"))
    assert out.status == "ok" and out.detail == "p_cluster"
    assert out.text is not None and "法人說明會" in out.text
    assert "align-items" not in out.text  # the CSS soup was NOT shipped


# ---------------------------------------------------------------------------
# Cookie/redirect opener + WARNING logging + compat.
# ---------------------------------------------------------------------------

def test_default_opener_carries_cookie_processor() -> None:
    # The opener is built with an HTTPCookieProcessor so consent/redirect flows that set a
    # cookie complete. Assert the handler is present on the constructed opener (deterministic,
    # no socket): the cookie jar rides across the redirect the handler performs.
    import urllib.request
    opener = F._build_opener()
    # OpenerDirector.handlers exists at runtime but is absent from typeshed's stub.
    handlers: list[object] = opener.handlers  # type: ignore[attr-defined]
    assert any(isinstance(h, urllib.request.HTTPCookieProcessor) for h in handlers)
    assert any(isinstance(h, urllib.request.HTTPRedirectHandler) for h in handlers)


def test_non_ok_outcome_logs_warning_with_url(caplog) -> None:  # type: ignore[no-untyped-def]
    import logging
    with caplog.at_level(logging.WARNING, logger="portfolio_dash.news.fetcher"):
        F.fetch_article("http://boom", opener=_raise(TimeoutError("slow")))
    rec = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert rec and "http://boom" in rec[0].getMessage() and "error" in rec[0].getMessage()


def test_ok_outcome_does_not_log(caplog) -> None:  # type: ignore[no-untyped-def]
    import logging
    with caplog.at_level(logging.WARNING, logger="portfolio_dash.news.fetcher"):
        F.fetch_article("http://x", opener=lambda u: _HTML.encode("utf-8"))
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_fetch_article_text_compat_ok_and_none() -> None:
    # Compat: fetch_article_text still returns str for ok, None for hard failures.
    assert isinstance(
        F.fetch_article_text("http://x", opener=lambda u: _HTML.encode("utf-8")), str)
    assert F.fetch_article_text("http://x", opener=_raise(TimeoutError("x"))) is None
    assert F.fetch_article_text(
        "http://x", opener=lambda u: b"<html><body></body></html>") is None
