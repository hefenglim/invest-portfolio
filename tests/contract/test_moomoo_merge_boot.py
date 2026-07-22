"""Boot-lifecycle contract for the Moomoo merge (drives the REAL app boot sequence).

The merge runs inside ``create_app()``'s lifespan (bootstrap_db -> ensure_seeded -> S0-gated
pre_migrate snapshot + migrate -> seed_accounts), the same ordering as production. Booting a
legacy-shaped on-disk DB must leave exactly {tw_broker, schwab, moomoo_my}, relabel every
legacy ledger row, and drop EXACTLY ONE ``pre_migrate_`` snapshot; a second boot is a no-op
that takes no further snapshot.
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.app import create_app
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.shared.config import get_settings


def _seed_legacy_ondisk(db: Path) -> None:
    """Write a populated PRE-merge DB to *db*: the two legacy accounts + a few ledger rows
    across the tables the merge touches (ledger / side / TEXT-embedded fingerprint)."""
    conn = sqlite3.connect(str(db))
    try:
        bootstrap_db(conn)
        conn.executemany(
            "INSERT INTO accounts (account_id, name, broker, settlement_ccy, funding_ccy, "
            "fee_rule_set, dividend_model) VALUES (?,?,?,?,?,?,?)",
            [
                ("moomoo_my_us", "Moomoo MY (US)", "Moomoo MY", "USD", "MYR",
                 "moomoo_us", "drip_us"),
                ("moomoo_my_my", "Moomoo MY (MY)", "Moomoo MY", "MYR", "MYR",
                 "moomoo_my", "cash"),
            ],
        )
        conn.executemany(
            "INSERT INTO account_market_rules (account_id, market, fee_rule_set, "
            "dividend_model) VALUES (?,?,?,?)",
            [("moomoo_my_us", "US", "moomoo_us", "drip_us"),
             ("moomoo_my_my", "MY", "moomoo_my", "cash")],
        )
        conn.execute(
            "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax, "
            "trade_date) VALUES ('moomoo_my_us','AAPL','buy','10','100','1','0','2026-01-10')")
        conn.execute(
            "INSERT INTO cash_movements (account_id, date, kind, ccy, amount) "
            "VALUES ('moomoo_my_my','2026-01-05','deposit','MYR','5000')")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS data_source_fallbacks "
            "(account_id TEXT PRIMARY KEY, chain TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO data_source_fallbacks (account_id, chain) "
            "VALUES ('moomoo_my_us','a>b')")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pending_dividend_skips "
            "(fingerprint TEXT PRIMARY KEY, skipped_at TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO pending_dividend_skips (fingerprint, skipped_at) "
            "VALUES ('div:moomoo_my_us:AAPL:2026-03-01','2026-03-02T00:00:00')")
        conn.commit()
    finally:
        conn.close()


def _boot(db: Path) -> None:
    """Run create_app()'s real lifespan once against the DB at *db*."""
    with TestClient(create_app()):
        pass


@pytest.fixture
def legacy_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db = tmp_path / "prod_like.db"
    _seed_legacy_ondisk(db)
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("PD_DISABLE_SCHEDULER", "1")
    get_settings.cache_clear()
    enable_socket()
    try:
        yield db
    finally:
        disable_socket(allow_unix_socket=True)
        get_settings.cache_clear()


def _accounts(db: Path) -> set[str]:
    conn = sqlite3.connect(str(db))
    try:
        return {r[0] for r in conn.execute("SELECT account_id FROM accounts")}
    finally:
        conn.close()


def _snapshots(db: Path) -> list[Path]:
    return sorted((db.parent / "snapshots").glob("pre_migrate_*.db.gz"))


def test_boot_migrates_legacy_db_once(legacy_db: Path) -> None:
    _boot(legacy_db)

    # exactly the merged 3-account world remains
    assert _accounts(legacy_db) == {"tw_broker", "schwab", "moomoo_my"}

    conn = sqlite3.connect(str(legacy_db))
    try:
        # every legacy ledger row relabelled to moomoo_my
        assert conn.execute(
            "SELECT account_id FROM transactions").fetchone()[0] == "moomoo_my"
        assert conn.execute(
            "SELECT account_id FROM cash_movements").fetchone()[0] == "moomoo_my"
        assert conn.execute(
            "SELECT COUNT(*) FROM data_source_fallbacks "
            "WHERE account_id IN ('moomoo_my_us','moomoo_my_my')").fetchone()[0] == 0
        assert {r[0] for r in conn.execute(
            "SELECT fingerprint FROM pending_dividend_skips")} == {
            "div:moomoo_my:AAPL:2026-03-01"}
        # merged account's per-market bindings seeded
        bindings = {r[0] for r in conn.execute(
            "SELECT market FROM account_market_rules WHERE account_id='moomoo_my'")}
        assert bindings == {"US", "MY"}
    finally:
        conn.close()

    # EXACTLY one pre-migration snapshot was taken
    assert len(_snapshots(legacy_db)) == 1


def test_second_boot_is_a_noop_no_new_snapshot(legacy_db: Path) -> None:
    _boot(legacy_db)
    after_first = _snapshots(legacy_db)
    assert len(after_first) == 1

    _boot(legacy_db)  # legacy ids gone -> S0 gate false -> no migrate, no snapshot
    assert _accounts(legacy_db) == {"tw_broker", "schwab", "moomoo_my"}
    assert _snapshots(legacy_db) == after_first  # unchanged, still exactly one
