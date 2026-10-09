"""The cash-kind table and the two calculation modules that must read it.

The tests that matter here are the ones that FAILED before ``shared/cash_kinds.py`` existed:
a debit kind other than ``WITHDRAW`` used to increase the balance and count as a foreign
acquisition, in two modules that do not import each other and therefore could not be fixed
in one place.
"""

from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.validate import CASH_MOVEMENT_KINDS
from portfolio_dash.forex import pools
from portfolio_dash.portfolio import cash as portfolio_cash
from portfolio_dash.shared.cash_kinds import (
    CASH_KIND_VALUES,
    DEBIT_KINDS,
    CashKind,
    canonical_kind,
    is_debit,
    is_fx_acquisition,
    movement_sign,
)
from portfolio_dash.shared.enums import Currency


class _Movement:
    """Satisfies both ``portfolio.cash._MovementRow`` and ``forex.pools.MovementRow``."""

    def __init__(
        self,
        kind: str,
        amount: str,
        *,
        ccy: Currency = Currency.USD,
        acq_home_amount: str | None = None,
        account_id: str = "schwab",
    ) -> None:
        self.account_id = account_id
        self.date = date(2026, 8, 13)
        self.kind = kind
        self.ccy = ccy
        self.amount = Decimal(amount)
        self.note: str | None = None
        self.acq_home_amount = (
            Decimal(acq_home_amount) if acq_home_amount is not None else None
        )


# --------------------------------------------------------------------------- the table


def test_every_kind_has_a_spec() -> None:
    """No kind may fall through to the unknown fallback."""
    for kind in CashKind:
        # Reaching the fallback would make both of these silently True.
        assert is_debit(kind.value) is not None
        assert is_fx_acquisition(kind.value) is not None
    assert CASH_KIND_VALUES == {k.value for k in CashKind}


@pytest.mark.parametrize(
    ("kind", "credit", "acquisition"),
    [
        ("DEPOSIT", True, True),
        ("OPENING", True, True),
        ("REBATE", True, True),
        ("WITHDRAW", False, False),
        ("INTEREST", True, False),
        ("INTEREST_EXPENSE", False, False),
        ("BROKER_FEE", False, False),
    ],
)
def test_two_axes_are_independent(kind: str, credit: bool, acquisition: bool) -> None:
    """``INTEREST`` is the row that proves one boolean was never enough: a CREDIT that is
    NOT an acquisition. A single flag cannot express it."""
    assert is_debit(kind) is not credit
    assert is_fx_acquisition(kind) is acquisition
    assert movement_sign(kind) == (Decimal("1") if credit else Decimal("-1"))


def test_a_debit_is_never_an_acquisition() -> None:
    """domain-ledger.md N1 — a disposal changes neither the average nor the coverage."""
    for kind in CashKind:
        if is_debit(kind.value):
            assert not is_fx_acquisition(kind.value)


def test_kind_is_normalized_before_lookup() -> None:
    """``portfolio/cash.py`` used to compare the RAW string, so a wire-cased ``'withdraw'``
    was signed as a credit and the overdraft guard watched the pool go UP
    (``validate.py::_pool_row`` exists because of exactly that)."""
    assert canonical_kind("  withdraw ") == "WITHDRAW"
    for raw in ("withdraw", "Withdraw", " WITHDRAW "):
        assert is_debit(raw)
        assert movement_sign(raw) == Decimal("-1")


def test_debit_kinds_set_matches_the_predicate() -> None:
    assert DEBIT_KINDS == {k.value for k in CashKind if is_debit(k.value)}


def test_registration_points_agree() -> None:
    """THE guard. A kind added to the table but not to the write-path allowed set is
    unreachable; one added to the allowed set but not the table gets the unknown
    fallback's sign — a silently mis-signed pool. Neither may ship."""
    assert CASH_MOVEMENT_KINDS == CASH_KIND_VALUES


# ------------------------------------------------------ the two calculation modules


def test_broker_fee_reduces_the_cash_balance() -> None:
    """Under the old ``kind == "WITHDRAW"`` predicate this returned +100: a fee that ADDED
    money to the pool."""
    balances = portfolio_cash.cash_balances(
        [_Movement("BROKER_FEE", "100")], [], [], [], {}
    )
    assert balances[("schwab", Currency.USD)] == Decimal("-100")


def test_margin_interest_reduces_the_cash_balance() -> None:
    balances = portfolio_cash.cash_balances(
        [_Movement("INTEREST_EXPENSE", "40")], [], [], [], {}
    )
    assert balances[("schwab", Currency.USD)] == Decimal("-40")


def test_interest_earned_increases_the_cash_balance() -> None:
    balances = portfolio_cash.cash_balances([_Movement("INTEREST", "7")], [], [], [], {})
    assert balances[("schwab", Currency.USD)] == Decimal("7")


def test_foreign_cash_balance_signs_a_fee_as_a_debit() -> None:
    """The SECOND module. ``forex/pools.py`` carried its own copy of the predicate, so the
    same fee was wrong here too — and this one feeds 換匯損益."""
    balance = pools.foreign_cash_balance(
        [], [], [], {}, Currency.USD, movements=[_Movement("BROKER_FEE", "100")]
    )
    assert balance == Decimal("-100")


def test_interest_does_not_dilute_the_coverage_ratio() -> None:
    """A USD pool funded entirely by a basis-known deposit that then earns interest is still
    FULLY covered. Counting interest as an unbased acquisition would report
    ``covered_ratio < 1`` and flag the whole foreign exposure (F3: cash *and* stocks) as
    basis-incomplete — a false alarm on every real account."""
    basis = pools.acquisition_basis(
        [],
        [
            _Movement("DEPOSIT", "1000", acq_home_amount="32000"),
            _Movement("INTEREST", "7"),
        ],
        Currency.TWD,
        Currency.USD,
    )
    assert basis.covered_ratio == Decimal("1")
    assert basis.acquired_without_basis == Decimal("0")


def test_a_fee_is_not_an_acquisition() -> None:
    """A debit must not enter the acquisition denominator at all — under the old predicate
    it entered as an UNBASED one, pushing covered_ratio down."""
    basis = pools.acquisition_basis(
        [],
        [
            _Movement("DEPOSIT", "1000", acq_home_amount="32000"),
            _Movement("BROKER_FEE", "100"),
        ],
        Currency.TWD,
        Currency.USD,
    )
    assert basis.acquired_without_basis == Decimal("0")
    assert basis.acquired_with_basis == Decimal("1000")


def test_an_unbased_deposit_still_dilutes_the_coverage() -> None:
    """The behaviour the two tests above must NOT have broken (F2)."""
    basis = pools.acquisition_basis(
        [],
        [
            _Movement("DEPOSIT", "1000", acq_home_amount="32000"),
            _Movement("DEPOSIT", "1000"),
        ],
        Currency.TWD,
        Currency.USD,
    )
    assert basis.covered_ratio == Decimal("0.5")
