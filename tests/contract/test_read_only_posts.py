"""PERF plan P2: the declared read-only POSTs are mounted, unlogged, and write nothing.

``api/read_only.py`` is the one table the result cache and the change actor consult; an entry
that wrote a row would let a cached answer outlive its own write, and one that is logged
writes an action-log row (which is why the insight preflight is NOT an entry).
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api import action_log
from portfolio_dash.api.app import create_app
from portfolio_dash.api.read_only import READ_ONLY_POSTS, is_read_only
from portfolio_dash.shared import flight
from portfolio_dash.shared.config import get_settings
from tests.conftest import _seed_golden

_BODIES: dict[str, dict[str, Any]] = {
    "/api/whatif": {"symbol": "2330", "side": "buy", "shares": "100", "price": "600",
                    "account_id": "tw_broker"},
    "/api/rebalance/preview": {"targets": {"2330": "0.30", "AAPL": "0.70"}},
    "/api/prompts/preview": {"body": "{{dividends_json}}", "scope": "portfolio",
                             "symbol": None},
}


def test_every_entry_has_a_body_here() -> None:
    assert {path for _m, path in READ_ONLY_POSTS} == set(_BODIES)


def test_every_entry_is_a_mounted_post_route() -> None:
    routes = {
        (method, route.path)
        for route in create_app().routes if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert READ_ONLY_POSTS <= routes


@pytest.mark.parametrize(("method", "path"), sorted(READ_ONLY_POSTS))
def test_every_entry_is_excluded_from_the_action_log(method: str, path: str) -> None:
    assert not action_log.should_log(method, path)


def test_the_preflight_is_an_ordinary_post() -> None:
    path = "/api/insight-tasks/1/preflight"
    assert action_log.should_log("POST", path)
    assert not is_read_only("POST", path)


@pytest.mark.parametrize("path", sorted(_BODIES))
def test_every_entry_writes_nothing(
    path: str, api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    before = golden_db.total_changes
    response = api_client.post(path, json=_BODIES[path])
    assert response.status_code == 200, response.text
    assert golden_db.total_changes == before
    assert not golden_db.in_transaction


def test_reads_are_read_only_and_writes_are_not() -> None:
    assert is_read_only("GET", "/api/dashboard")
    assert is_read_only("post", "/api/whatif/")
    assert not is_read_only("POST", "/api/input/manual")
    assert not is_read_only("PUT", "/api/ui-prefs")


# --- under a held write lock, every statement traced (the DEF-065 method) -------------------
# Copied, not imported, from tests/contract/test_def065_get_never_writes.py (another
# package's registry this wave): the same regexes, the same lock, the same booted database.

_WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP)\b", re.I)
_NOOP_DDL = re.compile(r"^\s*CREATE\s+(TABLE|INDEX|UNIQUE\s+INDEX)\s+IF\s+NOT\s+EXISTS\b", re.I)
_BUSY_S = 0.5


class _WriteLock:
    """Hold SQLite's write lock (``BEGIN IMMEDIATE``) on each file for the with-block."""

    def __init__(self, *paths: Path) -> None:
        self._conns = [sqlite3.connect(str(p), isolation_level=None) for p in paths if p.exists()]

    def __enter__(self) -> _WriteLock:
        for c in self._conns:
            c.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, *exc: object) -> None:
        for c in self._conns:
            c.execute("ROLLBACK")
            c.close()


@pytest.fixture
def db_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db = tmp_path / "portfolio.db"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("PD_DISABLE_SCHEDULER", "1")
    get_settings.cache_clear()
    enable_socket()
    try:
        yield db
    finally:
        disable_socket(allow_unix_socket=True)
        get_settings.cache_clear()


def test_every_entry_writes_nothing_under_a_held_write_lock(
    db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A booted database (the REAL lifespan) with golden's rows; each read-only POST is sent
    while other connections hold the write lock on both database files, with every SQL
    statement on every connection traced: none writes, and each still answers 200 (a POST
    that tried to write would wait out the 0.5 s busy timeout and answer 500)."""
    news_db = db_file.parent / "news.db"
    traced: dict[str, list[str]] = defaultdict(list)
    current = ["boot"]
    real = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["timeout"] = _BUSY_S
        conn: sqlite3.Connection = real(*args, **kwargs)
        conn.set_trace_callback(lambda sql: traced[current[0]].append(sql))
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        seed = real(str(db_file))
        seed.row_factory = sqlite3.Row
        _seed_golden(seed)
        seed.commit()
        seed.close()
        for method, path in sorted(READ_ONLY_POSTS):
            current[0] = path
            with _WriteLock(db_file, news_db):
                r = client.request(method, path, json=_BODIES[path])
            assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:300]}"
    writers = {
        path: [s.strip()[:120] for s in traced[path]
               if _WRITE.match(s) and not _NOOP_DDL.match(s)]
        for _m, path in READ_ONLY_POSTS
    }
    assert not any(writers.values()), f"read-only POSTs that write: {writers}"
    assert all(traced[path] for _m, path in READ_ONLY_POSTS), "the trace saw no statement"


# --- the write-window predicate ---------------------------------------------------------------


def _probe_app() -> FastAPI:
    """A minimal app whose write-window middleware is the WIRED one: ``api/app.py``'s
    ``_write_window_middleware`` with its method test replaced by ``is_read_only`` (the
    one-line change the main session makes at the wave-1 merge)."""
    app = FastAPI()

    @app.middleware("http")
    async def _write_window_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if is_read_only(request.method, request.url.path):
            return await call_next(request)
        flight.begin_write()
        try:
            return await call_next(request)
        finally:
            flight.end_write()

    def generation() -> dict[str, int | None]:
        return {"generation": flight.write_generation()}

    for method, path in READ_ONLY_POSTS:
        app.add_api_route(path, generation, methods=[method])
    app.add_api_route("/api/input/manual", generation, methods=["POST"])
    app.add_api_route("/api/dashboard", generation, methods=["GET"])
    return app


@pytest.mark.enable_socket
def test_the_wired_predicate_opens_no_write_window_for_a_read_only_post() -> None:
    client = TestClient(_probe_app())
    for method, path in sorted(READ_ONLY_POSTS):
        assert client.request(method, path).json()["generation"] is not None, path
    assert client.get("/api/dashboard").json()["generation"] is not None
    # an ordinary POST still runs inside a window, whose edges move the write generation (so
    # nothing cached before it or during it is served after it); a read-only POST moves none
    assert client.post("/api/input/manual").json()["generation"] is None
    assert flight.write_generation() is not None
