import sqlite3
from datetime import date
from decimal import Decimal

# The pool arithmetic the fx preview now takes as a REQUIRED argument (QA-09). Bound from the
# router, the layer that owns the injection, exactly as ``scripts/verify_corporate_actions.py``
# binds it — re-deriving it here would make this file a second owner of the binding.
from portfolio_dash.api.routers.cash import cash_pool_fn
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.fx_import import build_fx_preview, write_fx_row
from portfolio_dash.data_ingestion.preview import commit_preview
from portfolio_dash.data_ingestion.store import (
    insert_cash_movement,
    insert_fx_conversion,
    list_fx_conversions,
)
from portfolio_dash.shared.enums import Currency


def test_store_roundtrip_and_implied_rate(conn: sqlite3.Connection) -> None:
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 1),
                         from_ccy=Currency.TWD, from_amount=Decimal("320000"),
                         to_ccy=Currency.USD, to_amount=Decimal("10000"))
    rows = list_fx_conversions(conn, account_id="schwab")
    assert len(rows) == 1
    assert rows[0].from_ccy is Currency.TWD and rows[0].to_ccy is Currency.USD
    assert rows[0].implied_rate == Decimal("32")  # 320000 / 10000 (home per foreign)


def test_implied_rate_is_none_when_nothing_was_received(conn: sqlite3.Connection) -> None:
    """QA-10 — a ``to_amount`` of 0 has no implied rate, so the property answers None.

    ``from_amount / to_amount`` raised ``decimal.DivisionByZero`` and took ``GET
    /api/ledgers/fx`` down with a 500 (the whole換匯 tab, over one row). Every write door
    already refuses a zero leg, so this is only reachable by a hand-edited database —
    which is exactly the shape ``StoredOpening.original_avg`` in the same module already
    guards defensively. Written by direct SQL for that reason: the doors will not produce it.
    """
    conn.execute(
        "INSERT INTO fx_conversions (account_id, date, from_ccy, from_amount, to_ccy,"
        " to_amount) VALUES ('schwab','2026-01-05','TWD','320000','USD','0')")
    conn.commit()
    (row,) = list_fx_conversions(conn, account_id="schwab")
    assert row.implied_rate is None


def test_csv_preview_and_commit(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    # The pool must actually hold the TWD being sold: since QA-09 this door runs the same
    # HARD no-overdraft rule (FU-D34) ``POST /api/cash/fx`` runs, so an unfunded conversion is
    # refused rather than written. Fund it the way the owner does — a deposit, first.
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 1),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("320000"))
    csv = ("account,date,from_ccy,from_amount,to_ccy,to_amount\n"
           "schwab,2026-01-01,TWD,320000,USD,10000\n")
    p = build_fx_preview(conn, csv, pool=cash_pool_fn(conn))
    assert p.rows[0].issues == []
    summary = commit_preview(conn, p, accept={0}, writer=write_fx_row)
    assert len(summary.written) == 1
    assert len(list_fx_conversions(conn, account_id="schwab")) == 1


def test_csv_hard_blocks(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    csv = ("account,date,from_ccy,from_amount,to_ccy,to_amount\n"
           "nope,2026-01-01,TWD,320000,TWD,10000\n")  # unknown account + same ccy
    p = build_fx_preview(conn, csv, pool=cash_pool_fn(conn))
    kinds = {i.kind for i in p.rows[0].issues}
    assert "unknown_account" in kinds and "same_currency" in kinds
    assert p.rows[0].has_hard_issue
