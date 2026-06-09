import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.fx_import import build_fx_preview, write_fx_row
from portfolio_dash.data_ingestion.preview import commit_preview
from portfolio_dash.data_ingestion.store import insert_fx_conversion, list_fx_conversions
from portfolio_dash.shared.enums import Currency


def test_store_roundtrip_and_implied_rate(conn: sqlite3.Connection) -> None:
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 1),
                         from_ccy=Currency.TWD, from_amount=Decimal("320000"),
                         to_ccy=Currency.USD, to_amount=Decimal("10000"))
    rows = list_fx_conversions(conn, account_id="schwab")
    assert len(rows) == 1
    assert rows[0].from_ccy is Currency.TWD and rows[0].to_ccy is Currency.USD
    assert rows[0].implied_rate == Decimal("32")  # 320000 / 10000 (home per foreign)


def test_csv_preview_and_commit(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    csv = ("account,date,from_ccy,from_amount,to_ccy,to_amount\n"
           "schwab,2026-01-01,TWD,320000,USD,10000\n")
    p = build_fx_preview(conn, csv)
    assert p.rows[0].issues == []
    summary = commit_preview(conn, p, accept={0}, writer=write_fx_row)
    assert len(summary.written) == 1
    assert len(list_fx_conversions(conn, account_id="schwab")) == 1


def test_csv_hard_blocks(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    csv = ("account,date,from_ccy,from_amount,to_ccy,to_amount\n"
           "nope,2026-01-01,TWD,320000,TWD,10000\n")  # unknown account + same ccy
    p = build_fx_preview(conn, csv)
    kinds = {i.kind for i in p.rows[0].issues}
    assert "unknown_account" in kinds and "same_currency" in kinds
    assert p.rows[0].has_hard_issue
