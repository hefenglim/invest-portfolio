"""Tests for opening_inventory store functions and CSV import."""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.opening_import import (
    build_opening_preview,
    write_opening_row,
)
from portfolio_dash.data_ingestion.preview import commit_preview
from portfolio_dash.data_ingestion.store import list_opening, upsert_instrument, upsert_opening
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument


def _setup(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(
        conn,
        Instrument(
            symbol="2330",
            market=Market.TW,
            quote_ccy=Currency.TWD,
            sector="Tech",
            name="台積電",
        ),
    )


def test_store_upsert_roundtrip_and_total_computed(conn: sqlite3.Connection) -> None:
    upsert_opening(
        conn,
        account_id="tw_broker",
        symbol="2330",
        shares=Decimal("1000"),
        original_avg_cost=Decimal("500"),
        original_cost_total=Decimal("500000"),
        build_date=date(2025, 1, 1),
    )
    upsert_opening(
        conn,
        account_id="tw_broker",
        symbol="2330",
        shares=Decimal("1000"),
        original_avg_cost=Decimal("500"),
        original_cost_total=Decimal("500000"),
        build_date=date(2025, 1, 1),
    )  # idempotent (PK account+symbol)
    rows = list_opening(conn, account_id="tw_broker")
    assert len(rows) == 1 and rows[0].original_cost_total == Decimal("500000")


def test_csv_preview_computes_total_when_omitted_and_commits(conn: sqlite3.Connection) -> None:
    _setup(conn)
    csv = (
        "account,symbol,shares,original_avg_cost,build_date\n"
        "tw_broker,2330,1000,500,2025-01-01\n"
    )
    p = build_opening_preview(conn, csv)
    assert p.rows[0].issues == []  # valid
    summary = commit_preview(conn, p, accept={0}, writer=write_opening_row)
    assert len(summary.written) == 1
    rows = list_opening(conn, account_id="tw_broker")
    assert rows[0].original_cost_total == Decimal("500000")  # 500*1000 computed


def test_csv_unknown_account_hard_blocks(conn: sqlite3.Connection) -> None:
    _setup(conn)
    csv = (
        "account,symbol,shares,original_avg_cost,build_date\n"
        "nope,2330,1000,500,2025-01-01\n"
    )
    p = build_opening_preview(conn, csv)
    assert p.rows[0].has_hard_issue
    summary = commit_preview(conn, p, accept={0}, writer=write_opening_row)
    assert summary.written == [] and 0 in summary.skipped
