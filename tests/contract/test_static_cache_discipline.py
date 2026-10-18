"""Static-asset cache discipline (2026-07-07 stale-cache incident).

The frontend has no build step, so there is no filename fingerprinting. Two guards keep
a deploy from pairing fresh HTML with a stale cached script (the ``f.aiAttrib is not a
function`` class — insights/news/index lost every AI card while the API kept working):

1. **Server header** — the static mount serves ``Cache-Control: no-cache`` so browsers
   revalidate (ETag 304) instead of trusting heuristic freshness for days.
2. **HTML stamp** — every local ``.js``/``.css`` reference in ``web/*.html`` carries
   ``?v=<portfolio_dash.__version__>`` (scripts/stamp_asset_version.py, rerun on version
   bump), which flushes clients that cached assets before the header existed.

PERF spec 2026-10-18 §13 adds the second policy (``api/static_cache.py``): a request whose
``?v=`` IS the running version names one build of that file, so it is answered
``immutable``; HTML, unversioned and other-version requests keep ``no-cache``. Its tests run
on a minimal app mounting ``StaticCacheFiles`` until the main session mounts it in
``create_app`` (wave-1 merge) — then ``test_static_mount_sends_cache_control_no_cache``
below becomes the HTML / unversioned half of the same mount, and
``test_the_app_mounts_the_versioned_cache_policy`` is added (the snippet is in its place).

These are deterministic source/route scans: no golden DB, no lifespan, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from portfolio_dash import __version__
from portfolio_dash.api.app import create_app
from portfolio_dash.api.static_cache import IMMUTABLE, NO_CACHE, StaticCacheFiles

# TestClient's anyio portal opens a socketpair self-pipe on Windows (not real network
# I/O) which the global --disable-socket ban blocks — same exception as
# tests/contract/test_app_skeleton.py.
pytestmark = pytest.mark.enable_socket

# tests/contract/this_file.py -> parents[2] == worktree root (web/ lives here).
_WORKTREE_ROOT = Path(__file__).resolve().parents[2]
_WEB_DIR = _WORKTREE_ROOT / "web"

# Mirrors scripts/stamp_asset_version.py: a LOCAL asset reference (bare relative
# filename — CDN/absolute URLs never match), with an optional ?v= token captured.
_ASSET_REF = re.compile(
    r'(?:src|href)="(?P<name>[A-Za-z0-9._-]+\.(?:js|css))(?:\?v=(?P<ver>[^"]*))?"'
)


def test_every_local_asset_reference_is_version_stamped() -> None:
    """Every local .js/.css tag in every page carries ?v=<current app version>."""
    offenders: list[str] = []
    seen = 0
    for html in sorted(_WEB_DIR.glob("*.html")):
        for m in _ASSET_REF.finditer(html.read_text(encoding="utf-8")):
            seen += 1
            if m.group("ver") != __version__:
                offenders.append(f"{html.name} -> {m.group(0)}")
    assert seen > 0, "expected local asset references to scan (web/*.html)"
    assert not offenders, (
        f"local asset refs missing ?v={__version__} — rerun "
        "scripts/stamp_asset_version.py after a version bump: " + ", ".join(offenders)
    )


def test_static_mount_sends_cache_control_no_cache() -> None:
    """The static mount answers with Cache-Control: no-cache (fresh AND 304 paths)."""
    # No `with` block: the lifespan (DB bootstrap/scheduler) is irrelevant to the static
    # mount, and skipping it keeps this hermetic (no DB file, no sockets beyond ASGI).
    client = TestClient(create_app())
    fresh = client.get("/format.js")
    assert fresh.status_code == 200
    assert fresh.headers.get("cache-control") == "no-cache"
    etag = fresh.headers.get("etag")
    assert etag, "StaticFiles should still emit ETag (revalidation depends on it)"
    revalidated = client.get("/format.js", headers={"If-None-Match": etag})
    assert revalidated.status_code == 304
    assert revalidated.headers.get("cache-control") == "no-cache"
    page = client.get("/insights.html")
    assert page.status_code == 200
    assert page.headers.get("cache-control") == "no-cache"


# --- PERF §13: versioned assets are immutable (api/static_cache.py) -------------------------
#
# Added by the main session when it mounts StaticCacheFiles in create_app (wave-1 merge):
#
#     def test_the_app_mounts_the_versioned_cache_policy() -> None:
#         client = TestClient(create_app())
#         r = client.get(f"/format.js?v={__version__}")
#         assert r.headers.get("cache-control") == IMMUTABLE


def _static_client() -> TestClient:
    app = FastAPI()
    app.mount("/", StaticCacheFiles(directory=_WEB_DIR, html=True), name="web")
    return TestClient(app)


def test_every_stamped_reference_is_served_immutable() -> None:
    """Every local asset URL exactly as a page writes it gets the year-long immutable policy
    — fresh and on a 304 revalidation (a browser that cached it under the old policy)."""
    client = _static_client()
    urls = sorted({f"/{m.group('name')}?v={m.group('ver')}"
                   for html in _WEB_DIR.glob("*.html")
                   for m in _ASSET_REF.finditer(html.read_text(encoding="utf-8"))})
    assert urls, "expected local asset references to scan (web/*.html)"
    for url in urls:
        r = client.get(url)
        assert r.status_code == 200, url
        assert r.headers.get("cache-control") == IMMUTABLE, url
    etag = client.get(urls[0]).headers["etag"]
    again = client.get(urls[0], headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.headers.get("cache-control") == IMMUTABLE


@pytest.mark.parametrize("url", [
    "/format.js",                        # unversioned: an injected or hand-typed reference
    "/format.js?v=0.0.1",                # another version: an old page, or a mid-deploy pair
    "/format.js?v=",
    f"/insights.html?v={__version__}",   # HTML carries the stamps: always revalidated
    "/insights.html",
    "/",                                 # the directory index (index.html)
])
def test_html_unversioned_and_other_version_requests_keep_no_cache(url: str) -> None:
    r = _static_client().get(url)
    assert r.status_code == 200, url
    assert r.headers.get("cache-control") == NO_CACHE, url
    etag = r.headers.get("etag")
    assert etag
    again = _static_client().get(url, headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.headers.get("cache-control") == NO_CACHE, url


def test_a_missing_versioned_asset_is_never_cached_immutable() -> None:
    r = _static_client().get(f"/no-such-file.js?v={__version__}")
    assert r.status_code == 404
    assert r.headers.get("cache-control") != IMMUTABLE
