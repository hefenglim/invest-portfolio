"""General HTML fetcher: URL -> bounded readable text (stdlib only, graceful degrade).

Used by the news pipeline to pull an article body before the LLM organizes it. Kept
dependency-free (regex tag-strip + embedded-JSON recovery on a byte-bounded window — no
bs4/lxml/trafilatura, all rejected by the locked stack): the organizer LLM tolerates rough
text, so a full readability pass is unnecessary and the cap keeps tokens + memory bounded.

Every fetch produces a :class:`FetchOutcome` — text (or ``None``) plus a classified status
(``ok`` / ``http_error`` / ``non_html`` / ``too_short`` / ``salvaged`` / ``blocked_scheme``
/ ``error``) and a short detail. This is the observability seam: an empty body is no longer
silent — the status + a WARNING log record WHY. ``fetch_article_text`` keeps the old
``str | None`` shape (compat) by returning ``outcome.text``. The network call is injectable
so tests never touch the network.

Extraction runs a fallback CHAIN before giving up (owner directive: over-fetch, let the LLM
trim junk): (a) block-strip; (b) embedded-JSON recovery — JSON-LD ``articleBody`` (incl.
``@graph`` arrays) and Yahoo-style embedded state; (c) largest ``<p>``-tag cluster; (d) if
the prose guard still rejects but non-trivial text exists, return it as ``salvaged`` rather
than ``None`` (the DB then flags a low-confidence row instead of dropping the body silently).
"""

import http.cookiejar
import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Type of the injectable low-level fetch: url -> raw bytes (or raises).
Opener = Callable[[str], bytes]

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Browser-like negotiation headers so consent/anti-bot walls that branch on Accept /
# Accept-Language serve the real article. No Accept-Encoding: gzip — urllib does NOT
# auto-decompress, so identity keeps the body readable (owner sign-off: over-fetch is fine).
_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_ACCEPT_LANG = "zh-TW,zh;q=0.9,en;q=0.8"
_MAX_BYTES = 1_500_000      # read cap off the wire (before stripping); raised from 200 KB so
#                             a body pushed past the old cap by a large <style>/<head> survives
_MAX_TEXT_CHARS = 8_000     # text handed to the LLM organizer (token bound)
_MIN_TEXT_CHARS = 40        # below this the extraction produced nothing usable
_TIMEOUT_S = 20

# Elements whose CONTENT is noise; drop the whole block before tag-stripping.
_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|template|svg|nav|footer|header|form)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKLINES = re.compile(r"\n\s*\n+")

# Non-prose guard (FM10 backend fix, 2026-07-07): thresholds for _looks_like_prose.
# Real article prose (zh or en) is dominated by letters/ideographs and nearly free of
# code punctuation; CSS/JS soup that survives the block-strip (e.g. a <style> block cut
# mid-way by the byte cap) fails one of the two. NOTE (2026-07-21): the guard no longer
# DISCARDS on failure — it now only LABELS (ok vs salvaged); non-trivial text is still
# returned so the pipeline stores a visible ``salvaged`` row instead of an empty one.
_MIN_PROSE_RATIO = 0.5     # (letters + CJK) / non-whitespace chars
_MAX_CODE_DENSITY = 0.05   # {};:=<># density among non-whitespace chars
_CODE_CHARS = set("{};:=<>#")

# Embedded-JSON recovery. Article bodies are often ONLY in structured data, not server-
# rendered <p> tags: JSON-LD ``articleBody`` (publishers) and Yahoo-style state blobs.
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
# The STRING VALUE of an articleBody field, wherever it appears (JSON-LD or embedded state),
# captured tolerant of escaped quotes — targeting one field sidesteps nested-brace parsing.
_ARTICLE_BODY_RE = re.compile(r'"articleBody"\s*:\s*"((?:[^"\\]|\\.)*)"')
# Yahoo/caas style: a ``body`` field carrying HTML markup (must contain a <p> to qualify,
# so we don't grab unrelated short ``body`` keys).
_EMBEDDED_HTML_BODY_RE = re.compile(
    r'"body"\s*:\s*"((?:[^"\\]|\\.)*?<p[ >](?:[^"\\]|\\.)*?)"', re.IGNORECASE
)
_P_TAG_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class FetchOutcome:
    """One fetch result: recovered ``text`` (or ``None``) + a classified ``status``.

    ``status`` is one of ``ok`` / ``http_error`` / ``non_html`` / ``too_short`` /
    ``salvaged`` / ``blocked_scheme`` / ``error``. ``salvaged`` = text recovered by a
    fallback stage or that failed the prose guard yet is non-trivial — still usable, the
    LLM trims it downstream. ``detail`` is a short human hint (``HTTP 403``, an exception
    class name, or the salvage source stage).
    """

    text: str | None
    status: str
    detail: str = ""


class _FetchError(Exception):
    """A classified transport-level failure raised by the default opener (carries status)."""

    def __init__(self, status: str, detail: str = "") -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"{status}: {detail}")


def _looks_like_prose(text: str) -> bool:
    """True when *text* reads like article prose, not CSS/JS/nav soup.

    ``str.isalpha`` covers both latin letters and CJK ideographs, so the ratio check
    works for zh and en articles alike.
    """
    chars = [ch for ch in text if not ch.isspace()]
    if not chars:
        return False
    n = len(chars)
    wordish = sum(1 for ch in chars if ch.isalpha())
    code = sum(1 for ch in chars if ch in _CODE_CHARS)
    return wordish / n >= _MIN_PROSE_RATIO and code / n <= _MAX_CODE_DENSITY


def _build_opener() -> urllib.request.OpenerDirector:
    """An opener carrying an in-memory cookie jar so consent/redirect flows that SET a
    cookie and then redirect (common on TW/US news sites) complete instead of looping."""
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _default_opener(url: str) -> bytes:
    # SR fix (2026-07-06): only fetch http(s). urllib honours file://, ftp://, etc.;
    # a malicious/compromised feed link (file:///etc/passwd) must never be read + shipped
    # to the organizer LLM. Reject anything else BEFORE the request.
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"refusing non-http(s) URL: {url[:40]}")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _UA, "Accept": _ACCEPT, "Accept-Language": _ACCEPT_LANG},
    )
    opener = _build_opener()
    with opener.open(req, timeout=_TIMEOUT_S) as resp:
        ctype = resp.headers.get("Content-Type", "")
        if ctype and "html" not in ctype.lower():
            raise _FetchError("non_html", ctype)
        return bytes(resp.read(_MAX_BYTES))


def _decode_entities(body: str) -> str:
    """Decode the few HTML entities that matter for readability."""
    for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                    ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        body = body.replace(ent, ch)
    return body


def html_to_text(html: str) -> str:
    """Strip an HTML document to readable text (drop script/style/nav blocks, then tags)."""
    body = _DROP_BLOCKS.sub(" ", html)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = re.sub(r"</(p|div|li|h[1-6])>", "\n", body, flags=re.IGNORECASE)
    body = _TAG.sub("", body)
    body = _decode_entities(body)
    body = _WS.sub(" ", body)
    body = _BLANKLINES.sub("\n", body)
    return body.strip()


def _clean_fragment(fragment: str) -> str:
    """Reduce a recovered HTML/text fragment to readable prose (tags + entities + ws)."""
    body = _TAG.sub("", fragment)
    body = _decode_entities(body)
    body = _WS.sub(" ", body)
    body = _BLANKLINES.sub("\n", body)
    return body.strip()


def _find_article_bodies(obj: object) -> list[str]:
    """Recursively collect every ``articleBody`` string in a parsed JSON-LD document.

    Handles the common shapes: a bare Article object, a ``@graph`` array of objects, and
    a top-level list of documents — a blank field is skipped rather than returned hollow.
    """
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "articleBody" and isinstance(value, str) and value.strip():
                found.append(value)
            else:
                found.extend(_find_article_bodies(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_find_article_bodies(item))
    return found


def _json_unescape(raw: str) -> str:
    """Turn a captured JSON string body (escapes intact) into plain text; '' on failure."""
    try:
        value = json.loads('"' + raw + '"')
    except (ValueError, TypeError):
        return ""
    return value if isinstance(value, str) else ""


def _embedded_bodies(html: str) -> list[str]:
    """Article bodies recovered from embedded structured data (JSON-LD + Yahoo state).

    Longest first, so the richest candidate leads the fallback chain.
    """
    recovered: list[str] = []
    for block in _JSONLD_RE.finditer(html):
        try:
            data = json.loads(block.group(1).strip())
        except (ValueError, TypeError):
            continue
        recovered.extend(_find_article_bodies(data))
    for match in _ARTICLE_BODY_RE.finditer(html):
        recovered.append(_json_unescape(match.group(1)))
    for match in _EMBEDDED_HTML_BODY_RE.finditer(html):
        recovered.append(_json_unescape(match.group(1)))
    cleaned = [c for c in (_clean_fragment(r) for r in recovered) if c]
    cleaned.sort(key=len, reverse=True)
    return cleaned


def _p_cluster(html: str) -> str | None:
    """Join every ``<p>`` inner text — the article body of pages rendered as raw <p> runs."""
    parts = [c for c in (_clean_fragment(m.group(1)) for m in _P_TAG_RE.finditer(html)) if c]
    return "\n".join(parts) if parts else None


def _extraction_stages(html: str) -> list[tuple[str, str]]:
    """The ordered fallback chain: (block-strip) → (embedded JSON) → (<p> cluster)."""
    stages: list[tuple[str, str]] = []
    block = html_to_text(html)
    if block:
        stages.append(("block", block))
    stages.extend(("embedded_json", body) for body in _embedded_bodies(html))
    cluster = _p_cluster(html)
    if cluster:
        stages.append(("p_cluster", cluster))
    return stages


def _extract(raw: bytes, max_chars: int) -> FetchOutcome:
    """Run the extraction chain over *raw* bytes and classify the result."""
    try:
        html = raw.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001 — a decode blowup must degrade, not crash
        return FetchOutcome(None, "error", "decode")
    stages = _extraction_stages(html)
    # First stage that yields real prose wins.
    for source, text in stages:
        clipped = text[:max_chars]
        if len(clipped) >= _MIN_TEXT_CHARS and _looks_like_prose(clipped):
            return FetchOutcome(clipped, "ok", source)
    # Salvage: the prose guard rejected everything, but non-trivial text exists — keep it
    # (owner directive) so the row is a visible ``salvaged`` instead of a silent empty body.
    best_source, best = "", ""
    for source, text in stages:
        if len(text) > len(best):
            best, best_source = text, source
    if len(best) >= _MIN_TEXT_CHARS:
        return FetchOutcome(best[:max_chars], "salvaged", best_source)
    return FetchOutcome(None, "too_short", f"{len(best)} chars")


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


def fetch_article(
    url: str, *, opener: Opener | None = None, max_chars: int = _MAX_TEXT_CHARS
) -> FetchOutcome:
    """Fetch *url* and return a classified :class:`FetchOutcome` (never raises).

    Runs the extraction fallback chain; a transport failure, non-HTML content-type, empty
    page, or non-http(s) scheme yields a ``None``-text outcome with the reason. Every
    non-``ok`` outcome is logged once here at WARNING with the URL + status + detail — this
    is the SINGLE logging seam (the pipeline consumes the outcome and does not re-log).
    ``opener`` is injectable for tests (defaults to a cookie-bearing, browser-header urllib
    fetch); ``max_chars`` bounds the returned text.
    """
    outcome = _classify(url, opener, max_chars)
    if outcome.status != "ok":
        logger.warning(
            "news fetch non-ok: url=%s status=%s detail=%s",
            url, outcome.status, outcome.detail,
        )
    return outcome


def _classify(url: str, opener: Opener | None, max_chars: int) -> FetchOutcome:
    if not url.lower().startswith(("http://", "https://")):
        return FetchOutcome(None, "blocked_scheme", url[:60])
    fetch = opener or _default_opener
    try:
        raw = fetch(url)
    except urllib.error.HTTPError as exc:  # subclass of URLError — catch first
        return FetchOutcome(None, "http_error", f"HTTP {exc.code}")
    except _FetchError as exc:
        return FetchOutcome(None, exc.status, exc.detail)
    except Exception as exc:  # noqa: BLE001 — a fetcher must never crash the nightly job
        return FetchOutcome(None, "error", type(exc).__name__)
    return _extract(raw, max_chars)


def fetch_article_text(
    url: str, *, opener: Opener | None = None, max_chars: int = _MAX_TEXT_CHARS
) -> str | None:
    """Fetch *url* and return up to ``max_chars`` of readable text, or ``None`` on failure.

    Compatibility shape over :func:`fetch_article`: returns ``outcome.text`` — a ``str`` for
    ``ok`` and ``salvaged`` outcomes, ``None`` for every failure (``http_error`` /
    ``non_html`` / ``too_short`` / ``blocked_scheme`` / ``error``). Kept for any caller/test
    that wants only the text and does not need the classified status.
    """
    return fetch_article(url, opener=opener, max_chars=max_chars).text
