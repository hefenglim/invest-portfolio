"""PERF plan P2: the declared read-only POSTs are mounted, unlogged, and write nothing.

``api/read_only.py`` is the one table the result cache and the change actor consult; an entry
that wrote a row would let a cached answer outlive its own write, and one that is logged
writes an action-log row (which is why the insight preflight is NOT an entry).
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from portfolio_dash.api import action_log
from portfolio_dash.api.app import create_app
from portfolio_dash.api.read_only import READ_ONLY_POSTS, is_read_only

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
