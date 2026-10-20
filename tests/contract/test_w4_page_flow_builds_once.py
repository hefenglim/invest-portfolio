"""PERF plan W4 / spec §18 "per-page call-count ceilings": one page flow builds the dashboard at
most ONCE, and a reload of the same state builds it not at all.

The 首頁 asks, on load and on its toggles, for the dashboard, the alerts, 月度成績 (the month
closes and, for 年, the year closes), 績效比較 (`/api/performance/twr`), the cash pools and the
settlement chip's status. Each of those reads the dashboard build (directly, or through the
alerts, the live current period, or the TWR overlay); before the result cache each one paid for
a full build of its own. The app here is the real one (lifespan, settlement store on, inline),
over golden in a FILE that is the configured database — the only database the cache serves.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.app import create_app
from portfolio_dash.portfolio import dashboard
from portfolio_dash.shared import result_cache
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.db import connect_path
from tests.conftest import _seed_golden, init_golden_base

#: The 首頁's reads, in the order the page asks (web/app.js, web/charts.js, web/settlement.js).
PAGE_FLOW: tuple[tuple[str, dict[str, str]], ...] = (
    ("/api/dashboard", {}),
    ("/api/alerts", {}),
    ("/api/settlement/status", {}),
    ("/api/snapshots", {"limit": "12"}),
    ("/api/settlement/periods", {"kind": "Y", "limit": "20"}),
    ("/api/performance/twr", {"benchmark": "0050", "window": "1y"}),
    ("/api/cash", {}),
)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    path = tmp_path / "ledger.db"
    conn = connect_path(path)
    init_golden_base(conn)
    _seed_golden(conn)
    conn.commit()
    conn.close()
    monkeypatch.setenv("DB_PATH", str(path))
    get_settings.cache_clear()
    result_cache.reset()
    enable_socket()  # the TestClient portal's event loop uses a socket pair on Windows
    try:
        with TestClient(create_app()) as c:  # the real lifespan: boot, store, boot run
            yield c
    finally:
        disable_socket(allow_unix_socket=True)
        result_cache.reset()
        get_settings.cache_clear()


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    count = [0]
    real = dashboard._build_dashboard

    def counted(*args: object, **kwargs: object) -> object:
        count[0] += 1
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(dashboard, "_build_dashboard", counted)
    return count


def _walk(client: TestClient) -> None:
    for route, params in PAGE_FLOW:
        response = client.get(route, params=params)
        assert response.status_code == 200, (route, response.text[:200])


def test_one_page_flow_builds_the_dashboard_at_most_once(
    client: TestClient, builds: list[int],
) -> None:
    result_cache.reset()  # whatever the boot pre-built: start the flow cold
    _walk(client)
    assert builds[0] <= 1, f"the 首頁 flow built the dashboard {builds[0]} times"


def test_a_reload_of_the_same_state_builds_nothing(
    client: TestClient, builds: list[int],
) -> None:
    _walk(client)
    first = builds[0]
    _walk(client)
    assert builds[0] == first, "a reload of an unchanged ledger rebuilt the dashboard"
