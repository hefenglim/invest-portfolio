"""Per-symbol news-link discovery from FinMind / yfinance / Yahoo-TW (pure + injectable).

Turns a held symbol into a list of :class:`NewsLink` (title + url + source + date + lang)
by market: TW → FinMind (中文) + yfinance(.TW, 英文) + Yahoo-TW list page; US → yfinance;
MY → yfinance(.KL). Every network client is INJECTED (a callable), so this module — and
its tests — never touch the network; the real clients are wired in the pipeline/api seam.
Normalization is defensive: a source returning an unexpected shape yields no links rather
than raising (a nightly job must not crash on one bad row).
"""

import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

# Injectable clients (all optional; a None client is simply skipped):
#   FinMindClient(data_id, start_date) -> raw rows [{date, stock_id, link, source, title}]
#   YfClient(ticker)                   -> raw yfinance news items
#   YahooFetcher(url)                  -> the Yahoo list page HTML (or None on failure)
FinMindClient = Callable[[str, str], list[dict[str, Any]]]
YfClient = Callable[[str], list[dict[str, Any]]]
YahooFetcher = Callable[[str], str | None]


class NewsLink(BaseModel):
    """One discovered article reference (pre-fetch, pre-organize)."""

    title: str
    link: str
    source: str | None = None
    date: str | None = None  # YYYY-MM-DD when known
    lang: str | None = None  # "zh" | "en"


def _tw_ticker(symbol: str) -> str:
    """yfinance TW ticker: 4-digit TWSE → .TW (TPEx .TWO is not distinguished here; the
    real client may try both). Kept simple; a miss just yields no yfinance links."""
    return f"{symbol}.TW"


def from_finmind(rows: list[dict[str, Any]]) -> list[NewsLink]:
    """Normalize FinMind ``TaiwanStockNews`` rows (Chinese news)."""
    out: list[NewsLink] = []
    for r in rows:
        link = r.get("link")
        title = r.get("title")
        if not link or not title:
            continue
        raw_date = str(r.get("date") or "")[:10] or None
        out.append(NewsLink(title=str(title), link=str(link),
                             source=r.get("source"), date=raw_date, lang="zh"))
    return out


def _yf_field(item: dict[str, Any], *keys: str) -> Any:
    """Pull a field from a yfinance news item, tolerating the {content:{...}} nesting."""
    nested = item.get("content")
    content: dict[str, Any] = nested if isinstance(nested, dict) else item
    for k in keys:
        if k in content and content[k]:
            return content[k]
    return None


def from_yfinance(items: list[dict[str, Any]], *, lang: str = "en") -> list[NewsLink]:
    """Normalize yfinance ``Ticker.news`` items (shape varies across versions)."""
    out: list[NewsLink] = []
    for it in items:
        url = _yf_field(it, "clickThroughUrl", "canonicalUrl", "link")
        if isinstance(url, dict):
            url = url.get("url")
        title = _yf_field(it, "title")
        if not url or not title:
            continue
        provider = _yf_field(it, "provider")
        source = provider.get("displayName") if isinstance(provider, dict) else provider
        raw_date = _yf_field(it, "pubDate", "displayTime", "providerPublishTime")
        date = str(raw_date)[:10] if raw_date and str(raw_date)[:4].isdigit() else None
        out.append(NewsLink(title=str(title), link=str(url),
                            source=str(source) if source else None, date=date, lang=lang))
    return out


_YAHOO_ANCHOR = re.compile(
    r'<a[^>]+href="(?P<href>https://[^"]*?/news/[^"]+?\.html)"[^>]*>(?P<title>[^<]{6,})</a>',
    re.IGNORECASE,
)


def parse_yahoo_list(html: str) -> list[NewsLink]:
    """Best-effort parse of a Yahoo-TW quote/news list page (中文 headlines + article urls).

    Regex over anchor tags pointing at ``/news/*.html``. Yahoo renders much of the page
    from an in-HTML JSON SPA, so yield varies — this returns whatever server-rendered
    anchors exist and ``[]`` otherwise (documented fragile source; stability evaluated in
    the quality report). De-dupes by url.
    """
    seen: set[str] = set()
    out: list[NewsLink] = []
    for m in _YAHOO_ANCHOR.finditer(html):
        href, title = m.group("href"), m.group("title").strip()
        if href in seen:
            continue
        seen.add(href)
        out.append(NewsLink(title=title, link=href, source="Yahoo 股市", lang="zh"))
    return out


def discover_links(
    symbol: str,
    market: str,
    *,
    finmind_client: FinMindClient | None = None,
    yf_client: YfClient | None = None,
    yahoo_fetcher: YahooFetcher | None = None,
    finmind_start: str | None = None,
) -> list[NewsLink]:
    """Discover article links for *symbol* by market, de-duped by url (first source wins).

    TW: FinMind (中文, needs ``finmind_start``) + yfinance(.TW, 英文) + Yahoo-TW list.
    US: yfinance(symbol). MY: yfinance(.KL). A None client for a source skips it; any
    client raising is swallowed (one bad source never sinks the others).
    """
    collected: list[NewsLink] = []

    def _safe(fn: Callable[[], list[NewsLink]]) -> None:
        try:
            collected.extend(fn())
        except Exception:  # noqa: BLE001 — one source failing must not sink discovery
            pass

    if market == "TW":
        if finmind_client is not None and finmind_start is not None:
            _safe(lambda: from_finmind(finmind_client(symbol, finmind_start)))
        if yf_client is not None:
            _safe(lambda: from_yfinance(yf_client(_tw_ticker(symbol)), lang="en"))
        if yahoo_fetcher is not None:
            def _yahoo() -> list[NewsLink]:
                html = yahoo_fetcher(
                    f"https://tw.stock.yahoo.com/quote/{_tw_ticker(symbol)}/news"
                )
                return parse_yahoo_list(html) if html else []
            _safe(_yahoo)
    elif market == "US":
        if yf_client is not None:
            _safe(lambda: from_yfinance(yf_client(symbol), lang="en"))
    elif market == "MY":
        if yf_client is not None:
            _safe(lambda: from_yfinance(yf_client(f"{symbol}.KL"), lang="en"))

    # de-dupe by url, preserving first-seen order (source priority above).
    seen: set[str] = set()
    unique: list[NewsLink] = []
    for link in collected:
        if link.link in seen:
            continue
        seen.add(link.link)
        unique.append(link)
    return unique
