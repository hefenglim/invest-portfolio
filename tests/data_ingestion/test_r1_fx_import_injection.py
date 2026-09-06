"""R1 / QA-09: the fx CSV door's pool probe is REQUIRED, exactly as the cash door's is.

``architecture.md``'s injection rule has three obligations, and the first one is that the
injected parameter has **no default** — "D39 rejected injection from ``api/app.py`` because a
*missed* registration degrades silently, and a **required** argument is exactly the
difference: forgetting it is a mypy error and a ``TypeError``, not a quiet loss of the
guard". ``build_cash_movement_preview`` obeys it; ``build_fx_preview`` had no probe at all.

The parity assertion (the CSV door refuses what ``POST /api/cash/fx`` refuses) lives in
``tests/api/test_r1_import_door_parity.py``; this file pins the SEAM, so a future edit that
"helpfully" gives the probe a default fails here rather than shipping a silently weaker door.
"""

import sqlite3
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.fx_import import FX_COLUMNS, build_fx_preview
from portfolio_dash.data_ingestion.store import insert_cash_movement
from portfolio_dash.data_ingestion.validate import CashMovementInput, CashPool, CashPoolFn
from portfolio_dash.portfolio.cash import (
    cash_balances,
    pool_lines,
    running_min,
)
from portfolio_dash.shared.enums import Currency

_HEADER = ",".join(FX_COLUMNS) + "\n"
_ZERO = Decimal("0")


def _pool_fn(conn: sqlite3.Connection) -> CashPoolFn:
    """The REAL arithmetic, bound the way ``api`` binds it — built here from
    ``portfolio/cash.py`` directly so this unit test does not depend on the router."""
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
        as_of: date | None = None,
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
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 1),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("100000"))
    conn.commit()
    return conn


def test_the_probe_is_required(seeded: sqlite3.Connection) -> None:
    """No default, on purpose: forgetting to bind it must be loud, not silently permissive.

    mypy catches it statically; this catches a future edit that adds a default.
    """
    with pytest.raises(TypeError):
        build_fx_preview(seeded, _HEADER)  # type: ignore[call-arg]


def test_a_stub_probe_reporting_a_rich_pool_lets_the_overdraft_through(
    seeded: sqlite3.Connection,
) -> None:
    """Counter-evidence that the verdict comes FROM the injected arithmetic.

    A probe that reports an unlimited pool passes the same conversion the real one refuses.
    If this test and the next one agreed, the guard would not be reading the ledger at all.
    """
    def rich(
        account_id: str, ccy: Currency, *,
        include: Sequence[CashMovementInput] = (), exclude_id: int | None = None,
        as_of: date | None = None,
    ) -> CashPool:
        return CashPool(balance=Decimal("999999999"), low=_ZERO)

    preview = build_fx_preview(
        seeded, _HEADER + "schwab,2026-02-01,TWD,320000,USD,10000\n", pool=rich)
    assert preview.rows[0].issues == []


def test_the_real_probe_refuses_the_same_conversion(seeded: sqlite3.Connection) -> None:
    preview = build_fx_preview(
        seeded, _HEADER + "schwab,2026-02-01,TWD,320000,USD,10000\n", pool=_pool_fn(seeded))
    issue = preview.rows[0].issues[0]
    assert issue.kind == "fx_insufficient_balance"
    assert "可用餘額" in issue.message


def test_the_row_that_will_not_be_written_does_not_fund_its_sibling(
    seeded: sqlite3.Connection,
) -> None:
    """QA-01's invariant, applied to the door being fixed one over: ``select`` restricts the
    batch to the rows that will actually be COMMITTED, so a deselected sibling funds nothing.
    """
    csv_text = _HEADER + (
        "schwab,2026-01-05,TWD,32000,USD,1000\n"
        "schwab,2026-01-06,USD,1000,TWD,32100\n"
    )
    both = build_fx_preview(seeded, csv_text, pool=_pool_fn(seeded))
    assert both.rows[0].issues == [] and both.rows[1].issues == []

    second_only = build_fx_preview(seeded, csv_text, pool=_pool_fn(seeded), select=[1])
    assert second_only.rows[1].issues[0].kind == "fx_insufficient_balance"


def test_a_structurally_invalid_sibling_does_not_fund_anything_either(
    seeded: sqlite3.Connection,
) -> None:
    """The half that needs no ``select``: a row the same preview REJECTS is not a row that
    will be written, so it must not be in the batch the guard reasons over."""
    csv_text = _HEADER + (
        # Same currency on both legs -> hard ``same_currency``; never written.
        "schwab,2026-01-05,USD,1000,USD,1000\n"
        "schwab,2026-01-06,USD,1000,TWD,32100\n"
    )
    preview = build_fx_preview(seeded, csv_text, pool=_pool_fn(seeded))
    assert preview.rows[0].has_hard_issue
    assert preview.rows[1].issues[0].kind == "fx_insufficient_balance"


def test_the_existing_hard_issues_are_unchanged(seeded: sqlite3.Connection) -> None:
    """The four rejections the door already had must survive the new ones being added."""
    csv_text = _HEADER + (
        "nope,2026-01-05,TWD,1000,USD,30\n"
        "schwab,2026-01-05,TWD,0,USD,30\n"
        "schwab,2026-01-05,TWD,1000,TWD,1000\n"
        "schwab,not-a-date,TWD,1000,USD,30\n"
    )
    preview = build_fx_preview(seeded, csv_text, pool=_pool_fn(seeded))
    kinds = [r.issues[0].kind for r in preview.rows]
    assert kinds == ["unknown_account", "non_positive_amount", "same_currency", "parse_error"]


def test_the_payload_shape_is_unchanged(seeded: sqlite3.Connection) -> None:
    """The writer reads these keys; the guard is additive and must not touch them."""
    preview = build_fx_preview(
        seeded, _HEADER + "schwab,2026-01-05,TWD,32000,USD,1000\n", pool=_pool_fn(seeded))
    payload: dict[str, Any] = preview.rows[0].payload
    assert payload == {
        "account_id": "schwab", "date": "2026-01-05",
        "from_ccy": "TWD", "from_amount": "32000",
        "to_ccy": "USD", "to_amount": "1000",
    }
