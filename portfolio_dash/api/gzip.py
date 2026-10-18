"""Gzip every compressible response of 1 KiB or more — never a ZIP download (PERF spec §13).

Starlette's own ``GZipMiddleware`` (no new dependency). Two things it does not do on its own,
measured on the installed Starlette 1.3.1:

* it takes only ``minimum_size`` and ``compresslevel`` — there is no excluded-types option;
* it compresses ``application/zip`` like any other body (the export downloads,
  ``export/artifact.py``): CPU on a small VM for a body that does not shrink.

So :class:`ZipAwareGZipResponder` marks a response whose content type starts with one of
:data:`EXCLUDED_CONTENT_TYPES` as excluded — the flag Starlette's own responder already honours
for ``text/event-stream`` — and :class:`GZipMiddleware` uses that responder.

**Wiring** (main session, ``api/app.py::create_app``)::

    from portfolio_dash.api.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware)        # after the two @app.middleware("http") functions

Added last, it is the OUTERMOST layer: it compresses what every inner middleware and route
produced, and it sees the final ``Content-Type``. ``minimum_size`` defaults to 1024 bytes
(spec §13); ``compresslevel`` to 6, zlib's own default, not Starlette's 9 — measured on the dev
machine (2026-10-18): a 43.6 KB dashboard payload gzips to 5,256 B in 1.6 ms at level 6 and to
5,005 B in 4.1 ms at level 9 (2.6x the CPU for 5% less), and ``echarts.min.js`` (1.0 MB) to
333.9 KB vs 333.0 KB. CPU is the VM's scarce resource.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware as _StarletteGZipMiddleware
from starlette.middleware.gzip import GZipResponder, IdentityResponder
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Content types never compressed: Starlette's own exclusion plus a ZIP download.
EXCLUDED_CONTENT_TYPES: tuple[str, ...] = ("text/event-stream", "application/zip")

MINIMUM_SIZE = 1024
COMPRESSLEVEL = 6


class ZipAwareGZipResponder(GZipResponder):
    """Starlette's ``GZipResponder`` that also passes :data:`EXCLUDED_CONTENT_TYPES` through."""

    async def send_with_compression(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            await super().send_with_compression(message)
            content_type = Headers(raw=message["headers"]).get("content-type", "")
            if content_type.startswith(EXCLUDED_CONTENT_TYPES):
                self.content_type_is_excluded = True
            return
        await super().send_with_compression(message)


class GZipMiddleware(_StarletteGZipMiddleware):
    """``GZipMiddleware`` with the PERF defaults and :class:`ZipAwareGZipResponder`."""

    def __init__(self, app: ASGIApp, minimum_size: int = MINIMUM_SIZE,
                 compresslevel: int = COMPRESSLEVEL) -> None:
        super().__init__(app, minimum_size=minimum_size, compresslevel=compresslevel)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - websockets / lifespan pass through
            await self.app(scope, receive, send)
            return
        responder: ASGIApp
        if "gzip" in Headers(scope=scope).get("Accept-Encoding", ""):
            responder = ZipAwareGZipResponder(self.app, self.minimum_size,
                                              compresslevel=self.compresslevel)
        else:
            responder = IdentityResponder(self.app, self.minimum_size)
        await responder(scope, receive, send)


__all__ = [
    "COMPRESSLEVEL",
    "EXCLUDED_CONTENT_TYPES",
    "MINIMUM_SIZE",
    "GZipMiddleware",
    "ZipAwareGZipResponder",
]
