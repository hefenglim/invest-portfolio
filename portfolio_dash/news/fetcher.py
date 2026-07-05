"""General HTML fetcher: URL -> bounded readable text (stdlib only, graceful degrade).

Used by the news pipeline to pull an article body before the LLM organizes it. Kept
dependency-free (regex tag-strip on a byte-bounded window — no bs4/lxml): the organizer
LLM tolerates rough text, so a full readability pass is unnecessary and the cap keeps
tokens + memory bounded. Every failure (timeout, paywall, bot-block, non-HTML) degrades
to ``None`` — the caller then keeps just the headline. The network call is injectable so
tests never touch the network.
"""

import re
import urllib.error
import urllib.request
from collections.abc import Callable

# Type of the injectable low-level fetch: url -> raw bytes (or raises).
Opener = Callable[[str], bytes]

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_MAX_BYTES = 200_000        # read cap off the wire (before stripping)
_MAX_TEXT_CHARS = 8_000     # text handed to the LLM organizer (token bound)
_TIMEOUT_S = 20

# Elements whose CONTENT is noise; drop the whole block before tag-stripping.
_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|template|svg|nav|footer|header|form)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKLINES = re.compile(r"\n\s*\n+")


def _default_opener(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype.lower() and ctype:
            raise ValueError(f"non-HTML content-type: {ctype}")
        return bytes(resp.read(_MAX_BYTES))


def html_to_text(html: str) -> str:
    """Strip an HTML document to readable text (drop script/style/nav blocks, then tags)."""
    body = _DROP_BLOCKS.sub(" ", html)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = re.sub(r"</(p|div|li|h[1-6])>", "\n", body, flags=re.IGNORECASE)
    body = _TAG.sub("", body)
    # decode the few HTML entities that matter for readability
    for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                    ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        body = body.replace(ent, ch)
    body = _WS.sub(" ", body)
    body = _BLANKLINES.sub("\n", body)
    return body.strip()


def fetch_html(url: str, *, opener: Opener | None = None) -> str | None:
    """Fetch *url* and return the RAW decoded HTML (bounded), or None on failure.

    Used for pages parsed by structure (e.g. the Yahoo-TW list page needs its anchor
    tags intact — ``fetch_article_text`` would strip them). Never raises.
    """
    fetch = opener or _default_opener
    try:
        raw = fetch(url)
        return raw.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001 — a fetcher must never crash the nightly job
        return None


def fetch_article_text(
    url: str, *, opener: Opener | None = None, max_chars: int = _MAX_TEXT_CHARS
) -> str | None:
    """Fetch *url* and return up to ``max_chars`` of readable text, or ``None`` on failure.

    Never raises — a fetch/parse failure (timeout, paywall, bot-block, non-HTML, empty)
    yields ``None`` so the pipeline degrades to the headline. ``opener`` is injectable
    for tests (defaults to a UA-bearing urllib fetch).
    """
    fetch = opener or _default_opener
    try:
        raw = fetch(url)
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return None
    except Exception:  # noqa: BLE001 — a fetcher must never crash the nightly job
        return None
    try:
        html = raw.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return None
    text = html_to_text(html)
    if len(text) < 40:  # nothing usable extracted (paywall shell / JS-only page)
        return None
    return text[:max_chars]
