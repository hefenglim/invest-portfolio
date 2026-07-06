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
from datetime import UTC, datetime
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
    """yfinance TW ticker, TWSE-first: ``.TW``. TPEx symbols fall back to ``.TWO`` in
    :func:`discover_links` when ``.TW`` yields zero items (L6 fix — cheap: the second
    call happens only on an empty first result)."""
    return f"{symbol}.TW"


def _tw_ticker_tpex(symbol: str) -> str:
    """The TPEx (上櫃) yfinance ticker fallback."""
    return f"{symbol}.TWO"


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


def _parse_yf_date(raw: Any) -> str | None:
    """Parse a yfinance date to ``YYYY-MM-DD``: ISO strings AND UNIX-epoch ints.

    SR fix (2026-07-06): the old ``str(raw)[:4].isdigit()`` accepted a UNIX epoch
    (``1720000000`` → ``"1720"``) and produced a bogus ``news_date`` that sorted below
    every real date. Validate a real ISO date via ``strptime`` and convert epoch seconds.
    """
    if raw is None:
        return None
    s = str(raw)
    if len(s) >= 10 and s[4:5] == "-":
        try:
            datetime.strptime(s[:10], "%Y-%m-%d")
            return s[:10]
        except ValueError:
            pass
    try:  # providerPublishTime = UNIX seconds
        ts = int(raw)
    except (ValueError, TypeError):
        return None
    if ts > 1_000_000_000:  # plausible epoch seconds (2001+)
        return datetime.fromtimestamp(ts, tz=UTC).date().isoformat()
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
        date = _parse_yf_date(_yf_field(it, "pubDate", "displayTime", "providerPublishTime"))
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
            def _yf_tw() -> list[NewsLink]:
                # L6 fix: a TPEx (上櫃) symbol yields nothing under .TW — retry .TWO
                # only on an empty result (no extra call for TWSE symbols).
                links = from_yfinance(yf_client(_tw_ticker(symbol)), lang="en")
                if not links:
                    links = from_yfinance(yf_client(_tw_ticker_tpex(symbol)), lang="en")
                return links
            _safe(_yf_tw)
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
