"""Contract tests for GET /api/accounts (spec 13.1) — self-contained app + in-memory DB."""

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.deps import get_conn
from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.api.routers import accounts
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts


@pytest.fixture
def client() -> Iterator[TestClient]:
    # check_same_thread=False: TestClient runs the endpoint on a worker thread
    # while this fixture (and the :memory: connection) lives on the test thread.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    seed_accounts(conn)
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(accounts.router, prefix="/api")
    app.dependency_overrides[get_conn] = lambda: conn
    enable_socket()
    c = TestClient(app)
    yield c
    disable_socket(allow_unix_socket=True)
    conn.close()


def test_get_accounts_ok(client: TestClient) -> None:
    resp = client.get("/api/accounts")
    assert resp.status_code == 200


def test_returns_four_accounts(client: TestClient) -> None:
    body = client.get("/api/accounts").json()
    assert len(body["accounts"]) == 4


def test_version_block_category_accounts(client: TestClient) -> None:
    body = client.get("/api/accounts").json()
    # seed_accounts does not record an "accounts" category in settings_meta,
    # so seeded_at is null (no fabricated timestamp).
    assert body["version"] == {"category": "accounts", "seeded_at": None}


def _by_id(body: dict[str, Any]) -> dict[str, Any]:
    accs = body["accounts"]
    assert isinstance(accs, list)
    return {a["account_id"]: a for a in accs}


def test_tw_broker_fields(client: TestClient) -> None:
    accs = _by_id(client.get("/api/accounts").json())
    tw = accs["tw_broker"]
    assert tw["div_model"] == "tw"
    assert tw["settlement_ccy"] == "TWD"
    assert tw["funding_ccy"] == "TWD"
    rules = tw["fee_rules"]
    assert rules["rate"] == "0.001425"
    assert rules["min_fee"] == "20"
    assert rules["round_int"] is True
    assert rules["tax_sell"] == "0.003"
    assert rules["tax_sell_etf"] == "0.001"


def test_moomoo_my_my_div_model_net(client: TestClient) -> None:
    accs = _by_id(client.get("/api/accounts").json())
    assert accs["moomoo_my_my"]["div_model"] == "net"


def test_schwab_div_model_drip(client: TestClient) -> None:
    accs = _by_id(client.get("/api/accounts").json())
    assert accs["schwab"]["div_model"] == "drip"


def test_account_shape_keys(client: TestClient) -> None:
    accs = _by_id(client.get("/api/accounts").json())
    moomoo_us = accs["moomoo_my_us"]
    assert set(moomoo_us) == {
        "account_id", "name", "broker", "settlement_ccy", "funding_ccy",
        "div_model", "fee_rules",
    }
    assert set(moomoo_us["fee_rules"]) == {
        "rate", "discount", "min_fee", "round_int", "tax_sell", "tax_sell_etf", "label",
    }
    # Money/rate fields are strings (decoupled from float).
    assert isinstance(moomoo_us["fee_rules"]["rate"], str)
    assert moomoo_us["div_model"] == "drip"
