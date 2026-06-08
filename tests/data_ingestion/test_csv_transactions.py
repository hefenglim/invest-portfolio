import sqlite3
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.csv_import import (
    build_transaction_preview,
    write_transaction_row,
)
from portfolio_dash.data_ingestion.preview import commit_preview
from portfolio_dash.data_ingestion.store import list_transactions, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

_CSV = (
    "account,symbol,side,date,shares,price\n"
    "tw_broker,2330,BUY,2026-06-01,1000,600\n"
    "tw_broker,2330,SELL,2026-06-02,2000,610\n"
    "nope,2330,BUY,2026-06-03,100,600\n"
)


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


def test_preview_builds_rows_with_autocomputed_fee_and_issues(
    conn: sqlite3.Connection,
) -> None:
    _setup(conn)
    p = build_transaction_preview(conn, _CSV)
    assert len(p.rows) == 3
    assert p.rows[0].fee == Decimal("855")  # auto-computed TW buy
    assert any(i.kind == "sell_exceeds_holdings" for i in p.rows[1].issues)  # soft
    assert any(i.kind == "unknown_account" for i in p.rows[2].issues)  # hard


def test_commit_writes_only_accepted_non_hard_rows(conn: sqlite3.Connection) -> None:
    _setup(conn)
    p = build_transaction_preview(conn, _CSV)
    summary = commit_preview(conn, p, accept={0, 1, 2}, writer=write_transaction_row)
    # row0 buy written; row1 sell soft-issue accepted -> written; row2 hard -> skipped
    assert len(summary.written) == 2 and 2 in summary.skipped
    assert len(list_transactions(conn, account_id="tw_broker")) == 2


def test_blank_fee_autofilled_provided_fee_kept(conn: sqlite3.Connection) -> None:
    _setup(conn)
    csv = (
        "account,symbol,side,date,shares,price,fee\n"
        "tw_broker,2330,BUY,2026-06-01,1000,600,10\n"
    )
    p = build_transaction_preview(conn, csv)
    assert p.rows[0].fee == Decimal("10")  # provided fee preserved
