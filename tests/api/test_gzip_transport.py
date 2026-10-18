"""``api/gzip.py``: compressible answers of 1 KiB or more are gzipped; a ZIP never is.

PERF spec 2026-10-18 §13. Starlette 1.3.1's ``GZipMiddleware`` has no excluded-types option
and compresses ``application/zip`` like any body (measured), so the subclass marks it
excluded. Tested on a minimal app (the main session mounts it in ``create_app``); the export
route's content type is pinned against the exclusion through ``export/artifact.py``'s own
builder, so a renamed media type cannot slip past it.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from portfolio_dash.api.gzip import (
    COMPRESSLEVEL,
    EXCLUDED_CONTENT_TYPES,
    MINIMUM_SIZE,
    GZipMiddleware,
)
from portfolio_dash.export.artifact import zip_artifact

pytestmark = pytest.mark.enable_socket

_WEB = Path(__file__).resolve().parents[2] / "web"
_BIG = {"rows": [{"symbol": "2330", "value": "1234.5678"} for _ in range(200)]}


#: Zip members that deflate cannot shrink below the threshold (hex digests).
_MEMBERS = {
    name: b"".join(hashlib.sha256(f"{name}{i}".encode()).hexdigest().encode()
                   for i in range(200))
    for name in ("a.csv", "b.csv")
}


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/big")
    def big() -> JSONResponse:
        return JSONResponse(_BIG)

    @app.get("/small")
    def small() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/zip")
    def download() -> Response:
        art = zip_artifact("tax.zip", _MEMBERS)
        return Response(content=art.content, media_type=art.media_type,
                        headers={"Content-Disposition": 'attachment; filename="tax.zip"'})

    @app.get("/csv")
    def csv() -> Response:
        return Response(content=("a,b\r\n" * 1000).encode(), media_type="text/csv; charset=utf-8")

    @app.get("/events")
    def events() -> StreamingResponse:
        return StreamingResponse(iter([b"data: x\n\n" * 200]), media_type="text/event-stream")

    @app.get("/ranged")
    def ranged() -> Response:
        # a 200 that still names a range (e.g. a proxy's): the header alone passes it through
        return Response(content=b"x" * 4096, media_type="application/octet-stream",
                        headers={"Content-Range": "bytes 0-4095/8192"})

    app.mount("/", StaticFiles(directory=_WEB, html=True), name="web")
    app.add_middleware(GZipMiddleware)
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app())


def _raw(client: TestClient, path: str, *, accept: str = "gzip") -> tuple[dict[str, str], bytes]:
    """Headers and the body bytes AS SENT (httpx would otherwise decompress them)."""
    with client.stream("GET", path, headers={"Accept-Encoding": accept}) as r:
        assert r.status_code == 200, path
        body = b"".join(r.iter_raw())
        return dict(r.headers), body


def test_the_defaults_are_the_specs() -> None:
    assert MINIMUM_SIZE == 1024
    assert COMPRESSLEVEL == 6
    assert "application/zip" in EXCLUDED_CONTENT_TYPES


def test_a_large_json_answer_is_gzipped_and_decompresses_to_the_same_bytes(
    client: TestClient,
) -> None:
    plain = client.get("/big", headers={"Accept-Encoding": "identity"}).content
    headers, body = _raw(client, "/big")
    assert headers.get("content-encoding") == "gzip"
    assert "accept-encoding" in headers.get("vary", "").lower()
    assert gzip.decompress(body) == plain
    assert len(body) < len(plain)


def test_a_small_answer_and_a_client_without_gzip_get_identity(client: TestClient) -> None:
    headers, body = _raw(client, "/small")
    assert "content-encoding" not in headers
    assert body == b'{"ok":true}'
    headers, _ = _raw(client, "/big", accept="identity")
    assert "content-encoding" not in headers


def test_a_zip_download_is_never_compressed(client: TestClient) -> None:
    headers, body = _raw(client, "/zip")
    assert len(body) >= MINIMUM_SIZE
    assert "content-encoding" not in headers
    with zipfile.ZipFile(io.BytesIO(body)) as zf:   # the bytes as written: a valid zip
        assert {n: zf.read(n) for n in zf.namelist()} == _MEMBERS


def test_text_downloads_and_static_assets_are_compressed(client: TestClient) -> None:
    headers, body = _raw(client, "/csv")
    assert headers.get("content-encoding") == "gzip"
    assert gzip.decompress(body) == ("a,b\r\n" * 1000).encode()
    headers, body = _raw(client, "/format.js")
    assert headers.get("content-encoding") == "gzip"
    assert gzip.decompress(body) == (_WEB / "format.js").read_bytes()


def test_an_event_stream_keeps_starlettes_exclusion(client: TestClient) -> None:
    headers, _ = _raw(client, "/events")
    assert "content-encoding" not in headers


def test_a_range_answer_is_never_compressed(client: TestClient) -> None:
    """A 206 slice of a static file: its Content-Range counts uncompressed bytes, so the
    body must be exactly those bytes (Starlette alone gzipped it)."""
    full = (_WEB / "echarts.min.js").read_bytes()
    with client.stream("GET", "/echarts.min.js",
                       headers={"Range": "bytes=0-4095", "Accept-Encoding": "gzip"}) as r:
        assert r.status_code == 206
        body = b"".join(r.iter_raw())
        assert "content-encoding" not in r.headers
        assert r.headers["content-range"].startswith("bytes 0-4095/")
    assert body == full[:4096]


def test_any_content_range_header_passes_through(client: TestClient) -> None:
    headers, body = _raw(client, "/ranged")
    assert "content-encoding" not in headers
    assert body == b"x" * 4096
