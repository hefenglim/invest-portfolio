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


def test_returns_three_accounts(client: TestClient) -> None:
    body = client.get("/api/accounts").json()
    # Batch B: the two legacy Moomoo accounts merged into one dual-market moomoo_my.
    assert len(body["accounts"]) == 3


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


def test_moomoo_my_market_div_models(client: TestClient) -> None:
    # The merged account's scalar div_model is the US pair (drip); its per-market bundle
    # carries US -> drip and MY -> net (single-tier), so each market books correctly.
    accs = _by_id(client.get("/api/accounts").json())
    moomoo = accs["moomoo_my"]
    assert moomoo["div_model"] == "drip"
    assert moomoo["markets"]["US"]["div_model"] == "drip"
    assert moomoo["markets"]["MY"]["div_model"] == "net"


def test_schwab_div_model_drip(client: TestClient) -> None:
    accs = _by_id(client.get("/api/accounts").json())
    assert accs["schwab"]["div_model"] == "drip"


def test_account_shape_keys(client: TestClient) -> None:
    accs = _by_id(client.get("/api/accounts").json())
    moomoo = accs["moomoo_my"]
    # Batch B (Wave 3): the account object GAINS an additive per-market ``markets`` bundle
    # (deliberate, documented contract extension); the legacy scalar fields stay byte-identical.
    assert set(moomoo) == {
        "account_id", "name", "broker", "settlement_ccy", "funding_ccy",
        "div_model", "fee_rules", "markets",
    }
    assert set(moomoo["fee_rules"]) == {
        "rate", "discount", "min_fee", "round_int", "tax_sell", "tax_sell_etf", "label",
    }
    # Money/rate fields are strings (decoupled from float).
    assert isinstance(moomoo["fee_rules"]["rate"], str)
    assert moomoo["div_model"] == "drip"  # scalar mirrors the US pair (settlement USD)


def test_markets_bundle_for_synthetic_dual_market_account() -> None:
    # A future merged Moomoo account bound to BOTH US (drip) and MY (net): the wire exposes
    # one ``markets`` entry per bound market with that market's fee_rules + div_model, so the
    # frontend can book each market's dividends under the right model (F01) and show the right
    # fee schedule. Built inline (the four seeded accounts are all single-market).
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    seed_accounts(conn)
    conn.execute(
        "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
        "fee_rule_set, dividend_model) VALUES (?,?,?,?,?,?,?)",
        ("moomoo_merged", "Moomoo MY", "Moomoo MY", "USD", "MYR", "moomoo_us", "drip_us"),
    )
    conn.executemany(
        "INSERT INTO account_market_rules (account_id, market, fee_rule_set, dividend_model) "
        "VALUES (?,?,?,?)",
        [("moomoo_merged", "US", "moomoo_us", "drip_us"),
         ("moomoo_merged", "MY", "moomoo_my", "cash")],
    )
    conn.commit()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(accounts.router, prefix="/api")
    app.dependency_overrides[get_conn] = lambda: conn
    enable_socket()
    try:
        accs = _by_id(TestClient(app).get("/api/accounts").json())
    finally:
        disable_socket(allow_unix_socket=True)
        conn.close()
    merged = accs["moomoo_merged"]
    assert set(merged["markets"]) == {"US", "MY"}
    assert merged["markets"]["US"]["div_model"] == "drip"   # US Schwab/Moomoo -> DRIP
    assert merged["markets"]["MY"]["div_model"] == "net"    # MY single-tier -> net
    # per-market fee schedules differ (this is exactly why rules bind per market).
    assert "SEC/TAF" in merged["markets"]["US"]["fee_rules"]["label"]
    assert "印花" in merged["markets"]["MY"]["fee_rules"]["label"]
    # legacy scalar div_model still present (mirrors accounts.dividend_model).
    assert merged["div_model"] == "drip"


def test_markets_bundle_mirrors_scalars_for_single_market_accounts(
    client: TestClient,
) -> None:
    # The single-market accounts (tw_broker, schwab) carry exactly ONE ``markets`` entry, keyed
    # by their market value, whose fee_rules + div_model equal the legacy scalar fields.
    accs = _by_id(client.get("/api/accounts").json())
    expected_market = {"tw_broker": "TW", "schwab": "US"}
    for aid, market in expected_market.items():
        a = accs[aid]
        markets = a["markets"]
        assert set(markets) == {market}
        bundle = markets[market]
        assert set(bundle) == {"fee_rules", "div_model"}
        assert bundle["div_model"] == a["div_model"]          # mirrors the scalar
        assert bundle["fee_rules"] == a["fee_rules"]          # same fee-rule shape + values
        assert set(bundle["fee_rules"]) == set(a["fee_rules"].keys())
    # The merged Moomoo account is dual-market: exactly the two bound markets.
    assert set(accs["moomoo_my"]["markets"]) == {"US", "MY"}
