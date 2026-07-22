"""Legacy Moomoo account id -> ``moomoo_my`` alias at the CSV import seam (Batch B, F19).

A CSV authored before the merge may still name ``moomoo_my_us`` / ``moomoo_my_my``. Every
importer resolves it to ``moomoo_my`` (the row lands on the merged account) and attaches a
SOFT info issue (``account_alias``) — soft, so the row stays importable (a hard/non-confirmable
issue would block the commit). A current id passes through untouched.
"""

import sqlite3
from collections.abc import Iterator

import pytest

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.csv_import import build_transaction_preview
from portfolio_dash.data_ingestion.dividend_import import build_dividend_preview
from portfolio_dash.data_ingestion.fx_import import build_fx_preview
from portfolio_dash.data_ingestion.opening_import import build_opening_preview
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    seed_accounts(conn)
    create_pricing_tables(conn)  # fx_rates: the moomoo US-stamp fee path resolves USD/MYR
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Information Technology", name="Apple"))
    upsert_instrument(conn, Instrument(symbol="1155", market=Market.MY, quote_ccy=Currency.MYR,
                                       sector="Financials", name="Maybank"))
    conn.commit()
    yield conn


def _issue_kinds(preview: object) -> list[str]:
    return [i.kind for row in preview.rows for i in row.issues]  # type: ignore[attr-defined]


def test_transaction_legacy_account_aliased(seeded: sqlite3.Connection) -> None:
    csv = "account,symbol,side,date,shares,price\nmoomoo_my_us,AAPL,buy,2026-01-10,10,100\n"
    preview = build_transaction_preview(seeded, csv)
    (row,) = preview.rows
    assert row.payload["account_id"] == "moomoo_my"  # landed on the merged account
    alias = [i for i in row.issues if i.kind == "account_alias"]
    assert alias and alias[0].needs_confirm is True  # soft info notice
    assert "moomoo_my_us" in alias[0].message and "已合併" in alias[0].message
    assert "unknown_account" not in _issue_kinds(preview)
    assert not any(not i.needs_confirm for i in row.issues)  # nothing hard -> importable


def test_fx_legacy_account_aliased(seeded: sqlite3.Connection) -> None:
    csv = "account,date,from_ccy,from_amount,to_ccy,to_amount\n" \
          "moomoo_my_us,2026-01-06,MYR,4400,USD,1000\n"
    (row,) = build_fx_preview(seeded, csv).rows
    assert row.payload["account_id"] == "moomoo_my"
    assert any(i.kind == "account_alias" for i in row.issues)
    assert not any(not i.needs_confirm for i in row.issues)


def test_dividend_legacy_account_aliased(seeded: sqlite3.Connection) -> None:
    csv = "account,symbol,date,type,gross\nmoomoo_my_my,1155,2026-04-05,NET,50\n"
    (row,) = build_dividend_preview(seeded, csv).rows
    assert row.payload["account_id"] == "moomoo_my"
    assert any(i.kind == "account_alias" for i in row.issues)
    assert "unknown_account" not in _issue_kinds(build_dividend_preview(seeded, csv))


def test_opening_legacy_account_aliased(seeded: sqlite3.Connection) -> None:
    csv = "account,symbol,shares,original_cost_total,build_date\n" \
          "moomoo_my_my,1155,200,1800,2026-01-02\n"
    (row,) = build_opening_preview(seeded, csv).rows
    assert row.payload["account_id"] == "moomoo_my"
    assert any(i.kind == "account_alias" for i in row.issues)


def test_current_account_id_passthrough_no_alias(seeded: sqlite3.Connection) -> None:
    csv = "account,symbol,side,date,shares,price\nschwab,AAPL,buy,2026-01-10,10,100\n"
    (row,) = build_transaction_preview(seeded, csv).rows
    assert row.payload["account_id"] == "schwab"
    assert not any(i.kind == "account_alias" for i in row.issues)
