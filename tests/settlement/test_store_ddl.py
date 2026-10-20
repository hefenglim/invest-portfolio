"""The settlement store's DDL, reset and calc version (PERF spec 2026-10-18 §6, PERF-S5)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from portfolio_dash.portfolio import settlement_store as store
from portfolio_dash.shared.enums import Currency
from tests.conftest import init_golden_base


def _golden_base() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_create_tables_builds_every_settle_table_and_its_singletons() -> None:
    conn = sqlite3.connect(":memory:")
    store.create_tables(conn)
    assert set(store.SETTLE_TABLES) <= _tables(conn)
    assert conn.execute("SELECT id, generation, settled_through FROM settle_meta").fetchall() == [
        (1, 0, None)
    ]
    assert conn.execute("SELECT id, dirty_from, seq FROM settle_dirty").fetchall() == [
        (1, None, 0)
    ]


def test_a_second_create_changes_nothing(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "s.db")
    store.create_tables(conn)
    version = conn.execute("PRAGMA schema_version").fetchone()[0]
    changes = conn.total_changes
    store.create_tables(conn)
    assert conn.execute("PRAGMA schema_version").fetchone()[0] == version
    assert conn.total_changes == changes


def test_the_singletons_refuse_a_second_row() -> None:
    conn = sqlite3.connect(":memory:")
    store.create_tables(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO settle_meta (id) VALUES (2)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO settle_dirty (id) VALUES (2)")


def test_the_pending_counter_is_unique_per_table_and_key() -> None:
    conn = sqlite3.connect(":memory:")
    store.create_tables(conn)
    row = ("prices", "AAPL", 1, "2026-01-02", "2026-01-02")
    sql = ("INSERT INTO settle_change_agg (table_name, key, changes, earliest, latest) "
           "VALUES (?, ?, ?, ?, ?)")
    conn.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, row)
    conn.execute("UPDATE settle_change_agg SET claimed_run_id = 7")
    conn.execute(sql, row)  # a claimed counter no longer occupies the pending slot


def test_drop_store_drops_the_triggers_first_so_ledger_writes_keep_working() -> None:
    conn = _golden_base()
    conn.execute(
        f"CREATE TRIGGER {store.TRIGGER_PREFIX}probe AFTER INSERT ON fx_conversions "
        "BEGIN UPDATE settle_dirty SET seq = seq + 1; END"
    )
    store.drop_store(conn)
    assert not set(store.SETTLE_TABLES) & _tables(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
    ).fetchone()[0] == 0
    conn.execute(
        "INSERT INTO fx_conversions (account_id, date, from_ccy, from_amount, to_ccy, to_amount)"
        " VALUES ('schwab', '2026-01-08', 'TWD', '32000', 'USD', '1000')"
    )


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def test_the_code_digest_follows_only_the_calc_packages(tmp_path: Path) -> None:
    base = {
        "portfolio/a.py": b"x = 1\n",
        "shared/b.py": b"y = 2\n",
        "api/c.py": b"z = 3\n",
    }
    one = store.code_digest(_tree(tmp_path / "one", base))
    assert store.code_digest(_tree(tmp_path / "two", base)) == one
    # a non-calc package, a non-.py file and a line-ending change move nothing
    assert store.code_digest(_tree(tmp_path / "three", {**base, "api/c.py": b"z = 4\n"})) == one
    assert store.code_digest(_tree(tmp_path / "four", {**base, "portfolio/n.md": b"#"})) == one
    assert store.code_digest(
        _tree(tmp_path / "five", {**base, "portfolio/a.py": b"x = 1\r\n"})
    ) == one
    # a calc package's code moves it, and so does a renamed file
    assert store.code_digest(
        _tree(tmp_path / "six", {**base, "portfolio/a.py": b"x = 2\n"})
    ) != one
    moved = {k: v for k, v in base.items() if k != "shared/b.py"} | {"shared/b2.py": b"y = 2\n"}
    assert store.code_digest(_tree(tmp_path / "seven", moved)) != one


def test_calc_version_varies_with_the_reporting_currency() -> None:
    assert store.calc_version(Currency.TWD) == store.calc_version(Currency.TWD)
    assert store.calc_version(Currency.TWD) != store.calc_version(Currency.USD)
    assert len(store.calc_version(Currency.TWD)) == 16


def test_the_ledger_symbol_indexes_exist() -> None:
    conn = _golden_base()
    names = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {
        "idx_transactions_symbol", "idx_dividends_symbol", "idx_opening_inventory_symbol",
        "idx_corporate_actions_from_symbol", "idx_corporate_actions_to_symbol",
        "idx_transactions_account_symbol", "idx_dividends_account_symbol",
    } <= names


def test_job_runs_carries_results_after_the_boot_migration() -> None:
    conn = _golden_base()
    assert "results" in {str(r[1]) for r in conn.execute("PRAGMA table_info(job_runs)")}
