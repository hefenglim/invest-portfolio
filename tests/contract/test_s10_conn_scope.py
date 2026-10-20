"""PERF-S10 — a request's connection commits BEFORE its response starts.

``get_conn`` is a yield dependency. Under FastAPI's default (request) scope its exit — and so
``shared/db.session()``'s commit — runs after the response has started, so a client that
received 200 and immediately asked again could read the state before its own write, and a
commit failure was a silent 200. Every ``Depends(get_conn)`` therefore carries
``scope="function"``: the dependency exits when the path function returns, before
``http.response.start``. The gate (``auth_store.require_session``) and the route must use the
SAME scope, or FastAPI's dependency cache gives them two connections (measured, FastAPI
0.136.3).

TestClient cannot show the order (``put()`` returns only after both the commit and the
response), so the probe drives the ASGI app directly and watches ``send``.
"""

from __future__ import annotations

import asyncio
import io
import sqlite3
import tokenize
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from pytest_socket import disable_socket, enable_socket

import portfolio_dash
from portfolio_dash.api import deps
from portfolio_dash.api.app import create_app
from portfolio_dash.shared import db as shared_db
from portfolio_dash.shared.config import get_settings
from tests.conftest import init_golden_base

_PKG = Path(portfolio_dash.__file__).resolve().parent


@pytest.fixture
def probe_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A file-backed DB with the full schema, as the configured database."""
    path = tmp_path / "probe.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    conn.commit()
    conn.close()
    monkeypatch.setenv("DB_PATH", str(path))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


def _drive(
    method: str, path: str, body: bytes, send_hook: Any, app: FastAPI | None = None
) -> list[str]:
    """Run one request through the ASGI app; return the ``send`` message types in order."""
    app = app if app is not None else create_app()
    events: list[str] = []
    scope: dict[str, Any] = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "root_path": "", "query_string": b"",
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
        "server": ("probe", 80), "client": ("probe", 1),
    }
    sent_body = False

    async def receive() -> dict[str, Any]:
        nonlocal sent_body
        if sent_body:
            await asyncio.sleep(3600)
        sent_body = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            send_hook(message)
        events.append(message["type"])

    # The event loop's self-pipe is a real socket on Windows; the request itself is
    # in-process (the same reasoning as tests/conftest.py::api_client).
    enable_socket()
    try:
        asyncio.run(app(scope, receive, send))
    finally:
        disable_socket(allow_unix_socket=True)
    return events


def test_a_write_is_committed_when_the_response_starts(probe_db: Path) -> None:
    """At ``http.response.start`` another connection already reads the route's write.

    Most write helpers leave the commit to ``session()`` (the store functions never commit),
    so the probe route does the same: it writes and returns, declared with exactly the form
    the scan below enforces on every route.
    """
    app = create_app()

    def probe_write(
        conn: sqlite3.Connection = Depends(deps.get_conn, scope="function"),
    ) -> dict[str, bool]:
        conn.execute("INSERT INTO s10_probe (v) VALUES (1)")
        return {"ok": True}

    app.add_api_route("/api/s10-probe", probe_write, methods=["POST"])
    app.router.routes.insert(0, app.router.routes.pop())  # ahead of the "/" static mount
    setup = sqlite3.connect(probe_db)
    setup.execute("CREATE TABLE s10_probe (v INTEGER)")
    setup.commit()
    setup.close()
    seen: list[int] = []

    def at_start(message: dict[str, Any]) -> None:
        other = sqlite3.connect(probe_db)
        try:
            seen.append(int(other.execute("SELECT COUNT(*) FROM s10_probe").fetchone()[0]))
        finally:
            other.close()
        assert message["status"] == 200

    events = _drive("POST", "/api/s10-probe", b"{}", at_start, app)
    assert events[0] == "http.response.start"
    assert seen == [1]


def test_the_gate_and_the_route_share_one_connection(
    probe_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``require_session`` and the route receive the same cached connection; it is closed
    (committed) before the response starts."""
    opened: list[sqlite3.Connection] = []
    order: list[str] = []
    real = shared_db.session

    @contextmanager
    def recording() -> Iterator[sqlite3.Connection]:
        with real() as conn:
            opened.append(conn)
            yield conn
        order.append("session_exit")

    monkeypatch.setattr(deps, "session", recording)
    _drive("GET", "/api/accounts", b"", lambda _m: order.append("response_start"))
    assert len(opened) == 1
    assert order == ["session_exit", "response_start"]


def _get_conn_depends(source: str) -> list[tuple[int, str]]:
    """Every ``Depends(get_conn …)`` call in CODE (docstrings and comments skipped), with the
    text of its argument list."""
    tokens = [t for t in tokenize.generate_tokens(io.StringIO(source).readline)
              if t.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                                tokenize.INDENT, tokenize.DEDENT)]
    found: list[tuple[int, str]] = []
    for i, tok in enumerate(tokens[:-2]):
        if (tok.type == tokenize.NAME and tok.string == "Depends"
                and tokens[i + 1].string == "(" and tokens[i + 2].string == "get_conn"):
            depth, j, parts = 0, i + 1, []
            while True:
                t = tokens[j]
                depth += t.string == "("
                depth -= t.string == ")"
                parts.append(t.string)
                if depth == 0:
                    break
                j += 1
            found.append((tok.start[0], "".join(parts)))
    return found


def test_every_get_conn_dependency_is_function_scoped() -> None:
    """The scan covers the package only; a new route without the scope fails here."""
    wrong: list[str] = []
    total = 0
    for path in sorted(_PKG.rglob("*.py")):
        for line, args in _get_conn_depends(path.read_text(encoding="utf-8")):
            total += 1
            if args != '(get_conn,scope="function")':
                wrong.append(f"{path.relative_to(_PKG)}:{line} Depends{args}")
    assert not wrong, "Depends(get_conn) without scope=\"function\":\n" + "\n".join(wrong)
    assert total >= 190  # the scan really reads the routers (193 sites at 2026-10-18)
