"""Architecture-boundary tests for the pricing layer (#2).

architecture.md: ``pricing`` and ``data_ingestion`` are sibling lower layers; pricing
must NOT import data_ingestion (a cross-peer layering violation). ``datasources_store``
does not import ``data_ingestion.config_seed.DEFAULT_ACCOUNTS``; it owns its per-market
quote routing outright. These tests lock that boundary and pin the current seeding
reality: quote routing is per-MARKET, and the legacy per-account ``data_source_fallbacks``
table is retained but never seeded (zero rows on a fresh DB).
"""

import ast
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

import portfolio_dash.pricing as pricing_pkg
from portfolio_dash.pricing import datasources_store as store


def _pricing_sources() -> list[Path]:
    pkg_dir = Path(pricing_pkg.__file__).parent
    return sorted(pkg_dir.rglob("*.py"))


def test_no_data_ingestion_import_under_pricing() -> None:
    """No source under pricing/ may import portfolio_dash.data_ingestion (architecture.md)."""
    offenders: list[str] = []
    for path in _pricing_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module == "portfolio_dash.data_ingestion" or node.module.startswith(
                    "portfolio_dash.data_ingestion."
                ):
                    offenders.append(f"{path.name}: from {node.module} import ...")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "portfolio_dash.data_ingestion" or alias.name.startswith(
                        "portfolio_dash.data_ingestion."
                    ):
                        offenders.append(f"{path.name}: import {alias.name}")
    assert offenders == [], f"pricing imports data_ingestion: {offenders}"


@pytest.fixture
def ds_conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.create_tables(c)
    yield c
    c.close()


def test_seed_writes_no_per_account_fallback_rows(ds_conn: sqlite3.Connection) -> None:
    """seed() no longer seeds the legacy per-account fallback table (quote routing is
    per-MARKET). A freshly seeded DB therefore carries zero ``data_source_fallbacks`` rows."""
    store.seed(ds_conn)
    n = ds_conn.execute("SELECT COUNT(*) AS n FROM data_source_fallbacks").fetchone()["n"]
    assert n == 0


def test_fallbacks_table_still_created(ds_conn: sqlite3.Connection) -> None:
    """The legacy table is retained (deferred debt) even though it is never seeded/read,
    so the db-stats registry entry keeps resolving and existing rows are never dropped."""
    row = ds_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='data_source_fallbacks'"
    ).fetchone()
    assert row is not None
