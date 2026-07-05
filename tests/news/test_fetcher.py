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
