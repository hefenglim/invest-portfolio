"""Unit tests for the general HTML fetcher (injectable opener; never touches network)."""

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


def test_fetch_none_on_css_soup() -> None:
    # FM10 fix: clearly non-prose text (stylesheet/JS soup) degrades to None
    # (headline-only, retriable) instead of being shipped to the paid organizer.
    assert F.fetch_article_text(
        "http://x", opener=lambda u: _CSS_SOUP.encode("utf-8")
    ) is None


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
