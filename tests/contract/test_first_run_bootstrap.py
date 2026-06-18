"""First-run bootstrap completeness (fresh 0-byte DB).

The app lifespan must create EVERY table the running app reads AND seed the broker
accounts, so a brand-new install is usable out of the box. Regression guard for the
``no such table: prices`` first-run crash: the lifespan previously omitted
``create_pricing_tables`` / ``datasources_store.ensure_seeded`` / ``seed_accounts``, and
NO test exercised the lifespan (the rest of the suite seeds via the test harness
``init_golden_base``, not the real boot path) — so an empty DB looked fine until the
first holding hit the (missing) ``prices`` table.

These tests drive ``create_app()`` through its REAL lifespan against a throwaway DB.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.app import create_app
from portfolio_dash.data_ingestion.config_seed import DEFAULT_ACCOUNTS
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.shared.config import get_settings
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side


@pytest.fixture
def first_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Path]]:
    """``create_app()`` driven through its REAL lifespan against a fresh on-disk DB.

    Mirrors ``api_client``'s socket handling (TestClient's anyio portal needs the Windows
    loopback self-pipe); ``with TestClient(app)`` runs the lifespan = the first-run boot.
    """
    db = tmp_path / "fresh.db"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("PD_DISABLE_SCHEDULER", "1")  # no APScheduler thread in the test
    get_settings.cache_clear()
    enable_socket()
    try:
        with TestClient(create_app()) as client:
            yield client, db
    finally:
        disable_socket(allow_unix_socket=True)
        get_settings.cache_clear()


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_first_run_creates_pricing_and_datasource_tables(
    first_run: tuple[TestClient, Path],
) -> None:
    _client, db = first_run
    tables = _tables(db)
    # The running app reads these; first-run bootstrap must create them (not lazily).
    assert {"prices", "fx_rates", "data_sources"} <= tables, sorted(tables)


def test_first_run_seeds_the_default_accounts(first_run: tuple[TestClient, Path]) -> None:
    _client, db = first_run
    conn = sqlite3.connect(str(db))
    try:
        got = {r[0] for r in conn.execute("SELECT account_id FROM accounts")}
    finally:
        conn.close()
    # Seeded from the single canonical config (DEFAULT_ACCOUNTS) — adding a future account
    # there auto-seeds it on next launch (idempotent upsert).
    assert got == {a.account_id for a in DEFAULT_ACCOUNTS}


def test_first_run_dashboard_ok_with_a_holding(first_run: tuple[TestClient, Path]) -> None:
    """The ``no such table: prices`` regression: a single holding must NOT 500 the
    dashboard — it degrades to an honest 缺價 (null value) instead."""
    client, db = first_run
    conn = sqlite3.connect(str(db))
    try:
        upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                          quote_ccy=Currency.TWD, sector="Semiconductors", name="TSMC",
                          board="TWSE"))
        insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                           quantity=Decimal("1000"), price=Decimal("500"), fees=Decimal("0"),
                           tax=Decimal("0"), trade_date=date(2026, 1, 5))
        conn.commit()
    finally:
        conn.close()
    r = client.get("/api/dashboard")
    assert r.status_code == 200, r.text[:400]
    by_sym = {h["symbol"]: h for h in r.json()["holdings"]}
    assert by_sym["2330"]["market_value"] is None  # unpriced -> graceful, not a crash
