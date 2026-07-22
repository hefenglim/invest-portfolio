"""Unit: the stored per-market quote order IS the chain default_registry walks.

Item 9 (2026-07-03): the settings-page order used to be display-only (per-account
chains nothing consumed). Now datasources_store.set_quote_order feeds
default_registry(conn), so the first-listed capable provider is asked first.
"""

import sqlite3
from collections.abc import Iterator

import pytest

from portfolio_dash.pricing import datasources_store
from portfolio_dash.pricing.defaults import default_registry
from portfolio_dash.pricing.enums import DataType
from portfolio_dash.pricing.refs import InstrumentRef
from portfolio_dash.shared.enums import Market


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    datasources_store.ensure_seeded(c)
    yield c
    c.close()


def test_default_order_when_no_override(conn: sqlite3.Connection) -> None:
    order = datasources_store.quote_order(conn)
    assert order[Market.TW] == ["twse", "tpex", "yfinance", "twstock"]


def test_seed_leaves_market_order_defaults_byte_identical(conn: sqlite3.Connection) -> None:
    """Seeding writes NO data_source_market_order rows, so quote_order() resolves to the
    byte-identical DEFAULT_PROVIDER_ORDER per-market chains (routing is untouched by seed)."""
    rows = conn.execute("SELECT COUNT(*) AS n FROM data_source_market_order").fetchone()["n"]
    assert rows == 0  # ensure_seeded() ran in the fixture; it must not seed market order
    order = datasources_store.quote_order(conn)
    assert order == {
        Market.US: ["yfinance", "stockprices_dev"],
        Market.TW: ["twse", "tpex", "yfinance", "twstock"],
        Market.MY: ["yfinance", "klsescreener", "malaysiastock"],
    }


def test_registry_walks_the_stored_override(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasources_store.set_quote_order(conn, Market.TW, ["yfinance", "twse"])
    registry = default_registry(conn)

    asked: list[str] = []

    def probe(name: str) -> object:
        def fetch(instruments: list[InstrumentRef]) -> list[object]:
            asked.append(name)
            return []  # empty -> fall through to the next provider

        return fetch

    for name, provider in registry._providers.items():  # noqa: SLF001 — test seam
        if provider.supports(DataType.QUOTE_LATEST, Market.TW):
            monkeypatch.setattr(provider, "fetch_quote_latest", probe(name))
    ref = InstrumentRef(symbol="2330", market=Market.TW, board="TWSE")
    registry.fetch_quote_latest([ref])
    assert asked[:2] == ["yfinance", "twse"]  # override order, not the default


def test_capable_ids_lists_market_supporters(conn: sqlite3.Connection) -> None:
    registry = default_registry(conn)
    tw = registry.capable_ids(DataType.QUOTE_LATEST, Market.TW)
    assert "twse" in tw and "tpex" in tw and "yfinance" in tw
    assert "klsescreener" not in tw  # MY-only source never offered for TW
