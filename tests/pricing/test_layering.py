"""Architecture-boundary tests for the pricing layer (#2).

architecture.md: ``pricing`` and ``data_ingestion`` are sibling lower layers; pricing
must NOT import data_ingestion (a cross-peer layering violation). ``datasources_store``
previously imported ``data_ingestion.config_seed.DEFAULT_ACCOUNTS``; it now iterates its
own local ``_ACCOUNT_MARKET`` map. These tests lock the boundary and pin the per-account
fallback-chain seeding to its pre-change values (byte-equivalent regression guard).
"""

import ast
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

import portfolio_dash.pricing as pricing_pkg
from portfolio_dash.pricing import datasources_store as store

# Per-account fallback chains as they were seeded BEFORE the #2 refactor. Iterating
# the local _ACCOUNT_MARKET must reproduce these exactly (no behavior change).
_EXPECTED_CHAINS: dict[str, list[str]] = {
    "tw_broker": ["twse", "tpex", "yfinance", "twstock"],
    "schwab": ["yfinance", "stockprices_dev"],
    "moomoo_my_us": ["yfinance", "stockprices_dev"],
    "moomoo_my_my": ["yfinance", "klsescreener", "malaysiastock"],
}


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


def test_seed_writes_expected_per_account_chains(ds_conn: sqlite3.Connection) -> None:
    """seed() persists the same per-account fallback chains as before the refactor."""
    store.seed(ds_conn)
    rows = ds_conn.execute(
        "SELECT account_id, chain FROM data_source_fallbacks"
    ).fetchall()
    seeded = {r["account_id"]: json.loads(r["chain"]) for r in rows}
    assert seeded == _EXPECTED_CHAINS


def test_account_chains_from_seeded_table(ds_conn: sqlite3.Connection) -> None:
    """account_chains() reads the persisted chains unchanged after seeding."""
    store.seed(ds_conn)
    assert store.account_chains(ds_conn) == _EXPECTED_CHAINS


def test_account_chains_empty_table_falls_back_to_market_defaults(
    ds_conn: sqlite3.Connection,
) -> None:
    """With no rows persisted, account_chains() returns the same hardcoded defaults."""
    # Tables created but not seeded -> empty data_source_fallbacks.
    assert store.account_chains(ds_conn) == _EXPECTED_CHAINS
