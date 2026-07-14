"""Unit tests: date-aware pool timeline (C3 running-min) + opening-kind credit (C4)."""

from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.store import StoredCashMovement
from portfolio_dash.portfolio.cash import (
    cash_balances,
    pool_lines,
    running_min,
    running_statement,
)
from portfolio_dash.shared.enums import Currency


def _mv(d: str, kind: str, amt: str, mid: int = 0) -> StoredCashMovement:
    return StoredCashMovement(id=mid, account_id="acc", date=date.fromisoformat(d),
                              kind=kind, ccy=Currency.USD, amount=Decimal(amt), note=None)


def _lines(movements: list[StoredCashMovement]) -> list:
    return pool_lines("acc", Currency.USD, movements, [], [], [], {})


def test_running_min_catches_backdated_dip() -> None:
    # deposit AFTER the withdrawal: end aggregate is +200 (passes an end-check), but the
    # pool dipped to -500 in between -> the date-aware running-min catches it (audit C3).
    movements = [_mv("2026-02-01", "WITHDRAW", "500"), _mv("2026-03-01", "DEPOSIT", "700")]
    assert running_min(_lines(movements)) == Decimal("-500")


def test_running_min_non_negative_when_funded_first() -> None:
    movements = [_mv("2026-01-01", "DEPOSIT", "700"), _mv("2026-02-01", "WITHDRAW", "500")]
    assert running_min(_lines(movements)) == Decimal("0")


def test_opening_kind_is_credit_in_balance_and_lines() -> None:
    movements = [_mv("2026-01-01", "OPENING", "1000")]
    bal = cash_balances(movements, [], [], [], {})
    assert bal[("acc", Currency.USD)] == Decimal("1000")  # deposit-like credit
    lines = _lines(movements)
    assert lines[0].kind == "opening" and lines[0].delta == Decimal("1000")


def test_running_statement_annotates_balance() -> None:
    movements = [_mv("2026-01-01", "OPENING", "1000"), _mv("2026-02-01", "WITHDRAW", "300")]
    stmt = running_statement(_lines(movements))
    assert [str(b) for _ln, b in stmt] == ["1000", "700"]
