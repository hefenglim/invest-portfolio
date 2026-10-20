"""Call-count ceilings for the cached readers (PERF spec §12.2, plan W1-C).

A route that builds the dashboard twice, or replays the whole ledger beside a build, pays for
it on every request on a small CPU. Two layers are pinned with a counting stub around the
real computations:

* **Uncached** (an in-memory database, so nothing can be kept): every route below builds the
  dashboard exactly ONCE per request, the drawer replays the whole ledger's dividend
  refusals once, and 試算 resolves its rates through ONE ``RateResolver``;
* **Cached** (a file-backed corpus): the SECOND identical request builds nothing, replays
  nothing and assembles no alert inputs — the answer comes from ``shared/result_cache.py``.

The cached layer runs the production wiring: ``build_dashboard`` reads the cache, and a
read-only POST opens no write window (``api/read_only.py`` in ``api/app.py``'s
``_write_window_middleware``). The counting stub sits under the cache, on
``dashboard._build_dashboard``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import alert_inputs, deps
from portfolio_dash.api.app import create_app
from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.api.routers import symbol
from portfolio_dash.llm_insight import composer_store
from portfolio_dash.portfolio import dashboard
from portfolio_dash.portfolio.dashboard import RateResolver
from portfolio_dash.portfolio.returns import replay_dividend_refusals
from portfolio_dash.shared import result_cache
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import connect_path
from portfolio_dash.strategy import whatif
from tests.conftest import GOLDEN_NOW
from tests.reference.corpora import TEMPLATE_DIR_NAME, CorpusDB, copy_corpus

pytestmark = pytest.mark.enable_socket

_WHATIF = {"symbol": "2330", "side": "buy", "shares": "100", "price": "600",
           "account_id": "tw_broker"}
_REBALANCE = {"targets": {"2330": "0.30", "AAPL": "0.70"}}
_PREVIEW = {"body": "{{holdings_json}}", "scope": "portfolio", "symbol": None}


class Counter:
    def __init__(self) -> None:
        self.n = 0

    def wrap[**P, R](self, fn: Callable[P, R]) -> Callable[P, R]:
        def counted(*args: P.args, **kwargs: P.kwargs) -> R:
            self.n += 1
            return fn(*args, **kwargs)
        return counted


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    result_cache.reset()
    yield
    result_cache.reset()


@pytest.fixture
def counts(monkeypatch: pytest.MonkeyPatch) -> dict[str, Counter]:
    """Count real dashboard builds, whole-ledger refusal replays and alert-input assemblies,
    and the ``RateResolver``s 試算 creates."""
    out = {k: Counter() for k in ("build", "refusals", "assemble", "whatif_resolver")}
    monkeypatch.setattr(dashboard, "_build_dashboard",
                        out["build"].wrap(dashboard._build_dashboard))
    monkeypatch.setattr(symbol, "replay_dividend_refusals",
                        out["refusals"].wrap(replay_dividend_refusals))
    monkeypatch.setattr(alert_inputs, "assemble", out["assemble"].wrap(alert_inputs.assemble))
    class CountedResolver(RateResolver):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            out["whatif_resolver"].n += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(whatif, "RateResolver", CountedResolver)
    return out


def _preflight_path(conn: sqlite3.Connection) -> str:
    """One portfolio task (so the status route has a card to build), and its preflight."""
    composer_store.ensure_seeded(conn)
    task = composer_store.create_insight_type(
        conn, name="call-count probe", scope="portfolio", now=GOLDEN_NOW)
    conn.commit()
    return f"/api/insight-tasks/{task.id}/preflight"


#: (method, path or a path factory, body, the counters that must stay at their ceiling)
def _routes(preflight: str) -> list[tuple[str, str, dict[str, Any] | None]]:
    return [
        ("GET", "/api/dashboard", None),
        ("GET", "/api/alerts", None),
        ("GET", "/api/symbol/2330/detail", None),
        ("GET", "/api/insight-tasks/status", None),
        ("POST", "/api/whatif", _WHATIF),
        ("POST", "/api/rebalance/preview", _REBALANCE),
        ("POST", "/api/prompts/preview", _PREVIEW),
        ("POST", preflight, {}),
    ]


def _send(client: TestClient, method: str, path: str, body: dict[str, Any] | None) -> Any:
    r = client.request(method, path, json=body) if body is not None else client.get(path)
    assert r.status_code == 200, f"{method} {path}: {r.status_code} {r.text[:300]}"
    return r.json()


def test_every_cached_reader_builds_once_per_request_uncached(
    api_client: TestClient, golden_db: sqlite3.Connection, counts: dict[str, Counter],
) -> None:
    preflight = _preflight_path(golden_db)
    for method, path, body in _routes(preflight):
        for c in counts.values():
            c.n = 0
        _send(api_client, method, path, body)
        assert counts["build"].n == 1, f"{method} {path} built the dashboard {counts['build'].n}x"
        if path.startswith("/api/symbol/"):
            assert counts["refusals"].n == 1
        if path == "/api/whatif":
            assert counts["whatif_resolver"].n == 1, "試算 made more than one RateResolver"


@pytest.fixture
def cached_client(
    tmp_path_factory: pytest.TempPathFactory, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, counts: dict[str, Counter],
) -> Iterator[tuple[TestClient, str, CorpusDB]]:
    db = copy_corpus("golden", tmp_path_factory.getbasetemp() / TEMPLATE_DIR_NAME,
                     tmp_path / "golden.db")
    # The cache serves only the application's database: make the copy that database.
    monkeypatch.setenv("DB_PATH", str(db.path))
    get_settings.cache_clear()
    app = create_app()

    def conn_per_request() -> Iterator[sqlite3.Connection]:
        conn = connect_path(db.path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    app.dependency_overrides[get_conn] = conn_per_request
    app.dependency_overrides[get_now] = lambda: db.now
    app.dependency_overrides[get_reporting] = lambda: db.reporting
    setup = connect_path(db.path)
    try:
        preflight = _preflight_path(setup)
    finally:
        setup.close()
    try:
        yield TestClient(app), preflight, db
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_the_second_identical_request_builds_nothing(
    cached_client: tuple[TestClient, str, CorpusDB], counts: dict[str, Counter],
) -> None:
    client, preflight, _db = cached_client
    # The preflight is an ordinary POST (plan P2: it writes an action-log row), so its write
    # window keeps it uncached — the first test pins its one build.
    routes = [r for r in _routes(preflight) if r[1] != preflight]
    first = [_send(client, m, p, b) for m, p, b in routes]           # warm (may seed tables)
    for (method, path, body), warm in zip(routes, first, strict=True):
        for c in counts.values():
            c.n = 0
        again = _send(client, method, path, body)
        assert again == warm, f"{method} {path}: the cached answer differs"
        assert counts["build"].n == 0, f"{method} {path} rebuilt the dashboard"
        assert counts["refusals"].n == 0, f"{method} {path} replayed the ledger's refusals"
        if path == "/api/alerts":
            assert counts["assemble"].n == 0, "GET /api/alerts re-read its 400-day inputs"
    hits = result_cache.stats()
    assert hits["dashboard"].hits >= len(routes)
    assert hits["alerts"].hits >= 1
    assert hits["dividend_refusals"].hits >= 1


def test_a_prewarm_leaves_the_next_alerts_request_a_hit(
    cached_client: tuple[TestClient, str, CorpusDB], counts: dict[str, Counter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _preflight, db = cached_client               # DB_PATH already names the copy
    monkeypatch.setattr(deps, "get_now", lambda: db.now)
    _send(client, "GET", "/api/dashboard", None)         # warm-up: a first GET may seed
    result_cache.invalidate()
    alert_inputs.prewarm_today()
    assert counts["build"].n >= 1 and counts["assemble"].n >= 1
    for c in counts.values():
        c.n = 0
    _send(client, "GET", "/api/alerts", None)
    assert counts["assemble"].n == 0, "GET /api/alerts re-read its 400-day inputs"
    _send(client, "GET", "/api/dashboard", None)
    assert counts["build"].n == 0, "the pre-warmed dashboard was rebuilt"
