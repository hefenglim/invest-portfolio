"""The static mount's cache headers: versioned assets immutable, everything else revalidated.

Moved from ``api/app.py::_NoCacheStaticFiles`` (2026-07-07) and extended by PERF spec §13.

**Why ``no-cache`` at all.** Bare ``StaticFiles`` sends only ETag/Last-Modified — NO
Cache-Control — so browsers fall back to HEURISTIC freshness (~10% of the asset's age since
Last-Modified) and can keep serving a cached ``web/*.js`` for days without ever revalidating.
After a deploy that ships an HTML page together with a new helper it calls (e.g.
insights.html → ``fmt.aiAttrib``), a returning browser then mixes the fresh HTML with a STALE
cached script and the page dies client-side (live incident 2026-07-07: insights/news/index
lost every AI card to a swallowed ``f.aiAttrib is not a function``). ``no-cache`` keeps
caching but forces conditional revalidation on every use: an ETag 304 while unchanged, the new
body immediately after every future deploy. It fits the no-build-step rule (there is no
bundler to fingerprint filenames).

**Why ``immutable`` now.** Every local ``.js`` / ``.css`` reference in ``web/*.html`` carries
``?v=<portfolio_dash.__version__>`` (``scripts/stamp_asset_version.py``), so a URL with the
CURRENT version names exactly one build of that file: the next release changes the URL, not
the body behind it. Such a request is answered with ``Cache-Control: public,
max-age=31536000, immutable`` — no revalidation round trip per asset per page, which on the
VM is a request each through the whole middleware stack. Everything else keeps ``no-cache``:
an HTML page (it carries the version stamps, so it must revalidate), an unversioned request,
and a request whose ``?v=`` is NOT the running version (an old page still open, or a newer
page that reached an old process mid-deploy — caching that pairing for a year would pin it).
Only a successful answer (200 / 304) is ever marked immutable; a 404 never is.

**Wiring** (main session, ``api/app.py::create_app``): mount
``StaticCacheFiles(directory=_WEB_DIR, html=True)`` at ``"/"`` in place of
``_NoCacheStaticFiles``, and delete that class and its now-unused imports from ``app.py``.
"""

from __future__ import annotations

from urllib.parse import parse_qs

from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

import portfolio_dash

NO_CACHE = "no-cache"
IMMUTABLE = "public, max-age=31536000, immutable"


def _version_of(scope: Scope) -> str | None:
    query = scope.get("query_string", b"").decode("latin-1")
    values = parse_qs(query).get("v")
    return values[-1] if values else None


def cache_control(path: str, scope: Scope, response: Response) -> str:
    """The ``Cache-Control`` value for this static answer (see the module docstring)."""
    if response.status_code not in (200, 304):
        return NO_CACHE
    if path.endswith((".html", ".htm")) or path in ("", ".") or path.endswith("/"):
        return NO_CACHE
    if response.media_type is not None and response.media_type.startswith("text/html"):
        return NO_CACHE
    if response.headers.get("content-type", "").startswith("text/html"):
        return NO_CACHE
    if _version_of(scope) != portfolio_dash.__version__:
        return NO_CACHE
    return IMMUTABLE


class StaticCacheFiles(StaticFiles):
    """``StaticFiles`` with the cache policy of :func:`cache_control`."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = cache_control(path, scope, response)
        return response


__all__ = ["IMMUTABLE", "NO_CACHE", "StaticCacheFiles", "cache_control"]
