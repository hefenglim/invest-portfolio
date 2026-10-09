"""R8 / QA-12 (second half): a decimal fault must reach the degradation channel.

QA-12's CAUSE — a zero-quantity transaction dividing by zero shares — is fixed in
``build_book`` itself. This file covers the CLASS. ``decimal.InvalidOperation`` is an
``ArithmeticError``, **not** a ``ValueError``, so it slipped past the entire
``except (ValueError, KeyError)`` degradation family that ``UnbookableLedgerError`` was
deliberately made a ``ValueError`` in order to reach — and ``portfolio/dashboard.py``'s
``build_book(bundle, allow_oversell=True)`` had no guard at all, against a standing rule
that reads "never-500 at EVERY build_book call site".

The fault is injected rather than provoked, on purpose: provoking it needs a ledger row the
importer now rejects and the replay now skips, so a cause-based test would pass for the wrong
reason the day someone widens the guard. What is pinned here is the CHANNEL.
"""

import sqlite3
from datetime import datetime
from decimal import InvalidOperation
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.portfolio import dashboard as dashboard_mod
from portfolio_dash.portfolio.cost_basis import UnbookableLedgerError
from portfolio_dash.shared.enums import Currency

_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def _boom(*_a: object, **_k: object) -> None:
    raise InvalidOperation("[<class 'decimal.DivisionUndefined'>]")


def test_the_dashboard_retypes_a_decimal_fault_as_a_ledger_problem(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dashboard_mod, "build_book", _boom)
    with pytest.raises(UnbookableLedgerError) as exc:
        dashboard_mod.build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    # The owner is told which ledger to open, in their own language — not handed a traceback.
    assert any("一" <= ch <= "鿿" for ch in str(exc.value)), str(exc.value)
    # And it lands on the ValueError channel every other degradation site already watches.
    assert isinstance(exc.value, ValueError)


def test_the_raw_arithmetic_error_no_longer_escapes(
    golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative half: ``InvalidOperation`` itself must not reach the caller.

    Without this, a handler that catches ``UnbookableLedgerError`` would still be bypassed —
    which is precisely how this escaped for as long as it did.
    """
    monkeypatch.setattr(dashboard_mod, "build_book", _boom)
    try:
        dashboard_mod.build_dashboard(golden_db, now=_NOW, reporting=Currency.TWD)
    except UnbookableLedgerError:
        pass
    except InvalidOperation:  # pragma: no cover - the regression this file exists to catch
        pytest.fail("InvalidOperation escaped build_dashboard: the never-500 rule is broken")
