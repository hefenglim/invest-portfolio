"""R1 / QA-01: the batch handed to the withdraw guard is the set of rows that will be WRITTEN.

``batch = [inp for ... if inp is not None]`` was every row that PARSED. Two different kinds
of row are in that set and in no ledger afterwards:

* a row the same preview REJECTS (a hard issue — never written), and
* a row the caller DESELECTS on the commit (``select``, F-03's row indices).

Either one funded a withdrawal that was then written alone, so the bulk door booked an
overdraft the manual form answers 422 for. E1a is preserved: a withdrawal is still validated
against its siblings *that are being written*, so a first import into a fresh ledger works.
"""

import sqlite3
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.cash_import import (
    CASH_MOVEMENT_COLUMNS,
    build_cash_movement_preview,
)
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.preview import ImportPreview
from portfolio_dash.data_ingestion.store import insert_cash_movement
from portfolio_dash.data_ingestion.validate import CashMovementInput, CashPool, CashPoolFn
from portfolio_dash.portfolio.cash import cash_balances, pool_lines, running_min
from portfolio_dash.shared.enums import Currency

_HEADER = ",".join(CASH_MOVEMENT_COLUMNS) + "\n"
_ZERO = Decimal("0")


def _pool_fn(conn: sqlite3.Connection) -> CashPoolFn:
    """The REAL arithmetic, bound the way ``api`` binds it (see ``test_cash_import.py``)."""
    from portfolio_dash.data_ingestion.store import (
        StoredCashMovement,
        list_cash_movements,
        list_dividends,
        list_fx_conversions,
        list_instruments,
        list_transactions,
    )

    movements = list_cash_movements(conn)
    fx = list_fx_conversions(conn)
    txns = list_transactions(conn)
    divs = list_dividends(conn)
    insts = {i.symbol: i for i in list_instruments(conn)}

    def probe(
        account_id: str,
        ccy: Currency,
        *,
        include: Sequence[CashMovementInput] = (),
        exclude_id: int | None = None,
    ) -> CashPool:
        rows = [m for m in movements if m.id != exclude_id]
        rows.extend(
            StoredCashMovement(id=0, account_id=m.account_id, date=m.date,
                               kind=m.kind.upper(), ccy=m.ccy, amount=m.amount, note=m.note)
            for m in include
        )
        return CashPool(
            balance=cash_balances(rows, fx, txns, divs, insts).get((account_id, ccy), _ZERO),
            low=running_min(pool_lines(account_id, ccy, rows, fx, txns, divs, insts)),
        )

    return probe


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_accounts(conn)
    conn.commit()
    return conn


def _built(
    conn: sqlite3.Connection, csv_text: str, *, select: list[int] | None = None
) -> ImportPreview:
    return build_cash_movement_preview(conn, csv_text, pool=_pool_fn(conn), select=select)


_FUNDED_WITHDRAW = _HEADER + (
    "tw_broker,2026-01-01,DEPOSIT,TWD,100000,,\n"
    "tw_broker,2026-02-01,WITHDRAW,TWD,60000,,\n"
)


def test_a_deselected_deposit_does_not_fund_the_withdrawal(
    seeded: sqlite3.Connection,
) -> None:
    preview = _built(seeded, _FUNDED_WITHDRAW, select=[1])
    issue = preview.rows[1].issues[0]
    assert issue.kind == "withdraw_insufficient_balance"
    assert "出金" in issue.message


def test_both_rows_selected_is_the_self_funding_file_that_must_still_import(
    seeded: sqlite3.Connection,
) -> None:
    """E1a: the deposit that funds the withdrawal IS being written, so it counts."""
    preview = _built(seeded, _FUNDED_WITHDRAW, select=[0, 1])
    assert preview.rows[0].issues == [] and preview.rows[1].issues == []


def test_no_selection_means_every_row(seeded: sqlite3.Connection) -> None:
    """``select=None`` is the preview door and every non-selecting caller; unchanged."""
    preview = _built(seeded, _FUNDED_WITHDRAW)
    assert preview.rows[0].issues == [] and preview.rows[1].issues == []


def test_a_rejected_deposit_does_not_fund_the_withdrawal(
    seeded: sqlite3.Connection,
) -> None:
    """No ``select`` needed: the deposit carries a negative acquisition cost, so the SAME
    preview refuses it (``acq_cost_not_positive``) and it will never reach the ledger."""
    preview = _built(seeded, _HEADER + (
        "schwab,2026-01-01,DEPOSIT,USD,10000,-1,\n"
        "schwab,2026-02-01,WITHDRAW,USD,6000,,\n"
    ))
    assert preview.rows[0].issues[0].kind == "acq_cost_not_positive"
    hard = [i for i in preview.rows[1].issues if not i.needs_confirm]
    assert hard and hard[0].kind == "withdraw_insufficient_balance"


def test_an_unparseable_deposit_does_not_fund_the_withdrawal(
    seeded: sqlite3.Connection,
) -> None:
    """The one case the old code already got right (``inp is None``) — pinned so the fix
    that widens the rule cannot narrow this one by accident."""
    preview = _built(seeded, _HEADER + (
        "tw_broker,2026-01-01,DEPOSIT,TWD,not-a-number,,\n"
        "tw_broker,2026-02-01,WITHDRAW,TWD,60000,,\n"
    ))
    assert preview.rows[0].issues[0].kind == "parse_error"
    assert preview.rows[1].issues[0].kind == "withdraw_insufficient_balance"


def test_a_stored_deposit_still_funds_a_deselected_files_withdrawal(
    seeded: sqlite3.Connection,
) -> None:
    """The batch narrows; the stored LEDGER does not. A withdrawal covered by money already
    in the pool is unaffected by any selection."""
    insert_cash_movement(seeded, account_id="tw_broker", move_date=date(2026, 1, 1),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("100000"))
    seeded.commit()
    preview = _built(seeded, _HEADER + "tw_broker,2026-02-01,WITHDRAW,TWD,60000,,\n",
                     select=[])
    assert preview.rows[0].issues == []


def test_two_withdrawals_that_only_jointly_overdraft_are_both_still_caught(
    seeded: sqlite3.Connection,
) -> None:
    """The property the file-wide batch exists for, unchanged: each sees the other."""
    insert_cash_movement(seeded, account_id="tw_broker", move_date=date(2026, 1, 1),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("100000"))
    seeded.commit()
    preview = _built(seeded, _HEADER + (
        "tw_broker,2026-02-01,WITHDRAW,TWD,60000,,\n"
        "tw_broker,2026-02-02,WITHDRAW,TWD,60000,,\n"
    ))
    assert preview.rows[0].issues[0].kind == "withdraw_insufficient_balance"
    assert preview.rows[1].issues[0].kind == "withdraw_insufficient_balance"
