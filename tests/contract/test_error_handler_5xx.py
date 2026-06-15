"""A genuine unhandled exception in a route returns the generic 500 envelope and is
logged with a traceback (spec 19.4).

Hermetic: uses the in-process golden DB via dependency override; no real ``data/`` or
log file is touched (TestClient skips lifespan, so configure_logging never runs here —
the traceback is asserted via pytest ``caplog``, not a log file).
"""

import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api.app import create_app
from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.api.routers import dashboard as dashboard_router
from portfolio_dash.shared.enums import Currency
from tests.conftest import GOLDEN_NOW


def test_unhandled_exception_returns_internal_error_envelope_and_logs_traceback(
    golden_db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("synthetic dashboard failure")

    monkeypatch.setattr(dashboard_router, "build_dashboard", _boom)

    from pytest_socket import disable_socket, enable_socket

    enable_socket()
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: golden_db
    app.dependency_overrides[get_now] = lambda: GOLDEN_NOW
    app.dependency_overrides[get_reporting] = lambda: Currency.TWD
    # raise_server_exceptions=False -> the 500 response is returned, not re-raised.
    client = TestClient(app, raise_server_exceptions=False)
    try:
        with caplog.at_level(logging.ERROR, logger="portfolio_dash.api.errors"):
            r = client.get("/api/dashboard")
    finally:
        app.dependency_overrides.clear()
        disable_socket(allow_unix_socket=True)

    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["message"] == "internal error"
    # The synthetic detail must NOT leak into the response body.
    assert "synthetic dashboard failure" not in r.text

    # An ERROR record with exc_info (the traceback) was captured.
    errors = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert errors, "expected an ERROR log record from the catch-all handler"
    assert any(rec.exc_info is not None for rec in errors)
    assert any("RuntimeError: synthetic dashboard failure" in (rec.exc_text or
               (logging.Formatter().formatException(rec.exc_info) if rec.exc_info else ""))
               for rec in errors)
