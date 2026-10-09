"""E-1: the cash CSV door answered a broken cell with pydantic's own English.

``_parse_row`` builds a :class:`CashMovementInput` inside a try and returns
``Issue(kind="parse_error", message=str(exc))`` for ``(ValueError, InvalidOperation)``. For a
non-finite amount (``NaN`` / ``Infinity`` / ``-Infinity`` / ``sNaN``) ``Decimal()`` constructs
perfectly well, so neither ``_decimal``'s ``InvalidOperation`` guard nor the arm above ever
saw a problem — pydantic did, and ``ValidationError`` is a ``ValueError`` subclass, so its
``str()`` landed in the issue verbatim::

    1 validation error for CashMovementInput
    amount
      Input should be a finite number [type=finite_number, input_value=Decimal('NaN'), ...]

That is what the owner's import-preview 原因 column rendered. The *degradation* was already
right — HTTP 200, one row rejected, siblings kept — which is exactly why this door was the
reference case when the fx door was fixed (``test_m2_fx_import_non_finite.py``, QA-07). Only
the wording was wrong, and the wording is the whole content of that column.

The static guard (``tests/architecture/test_user_messages_are_zh_tw.py``) cannot see it: it
scans ``Issue(message=<literal>)`` and skips non-literals, and ``str(exc)`` is a call. So the
rule is enforced HERE, on the value, by driving the real importer — the same posture
``test_r5_csv_parse_error_zh.py`` takes for the transaction door.
"""

import re
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
from portfolio_dash.data_ingestion.validate import CashMovementInput, CashPool, CashPoolFn
from portfolio_dash.shared.enums import Currency

_HEADER = ",".join(CASH_MOVEMENT_COLUMNS) + "\n"
_ZERO = Decimal("0")
_CJK = re.compile(r"[一-鿿]")

#: The shared wording with the fx door (``fx_import._parse_row``) — one shape for one defect
#: across both bulk doors, so the owner does not learn two vocabularies for one broken cell.
_MARKER = "必須是有限數字"

#: Pydantic / Python internals that must never reach the 原因 column, whatever the failure.
_LEAKS = (
    "validation error",
    "Input should be",
    "finite_number",
    "input_value",
    "CashMovementInput",
    "errors.pydantic.dev",
    "<class",
    "Decimal(",
)


def _rich_pool() -> CashPoolFn:
    """A probe reporting an unlimited pool.

    The withdraw guard is not this file's subject, and an unfunded row would otherwise mask
    the parse verdict under an overdraft issue.
    """

    def rich(
        account_id: str,
        ccy: Currency,
        *,
        include: Sequence[CashMovementInput] = (),
        exclude_id: int | None = None,
        as_of: date | None = None,
    ) -> CashPool:
        return CashPool(balance=Decimal("999999999"), low=_ZERO)

    return rich


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_accounts(conn)
    conn.commit()
    return conn


def _amount_row(text: str) -> str:
    return _HEADER + f"schwab,2026-01-05,DEPOSIT,USD,{text},,\n"


def _acq_row(text: str) -> str:
    """A USD credit into a TWD-funded account — the one shape that carries an acq cost."""
    return _HEADER + f"schwab,2026-01-05,DEPOSIT,USD,1000,{text},\n"


@pytest.mark.parametrize("text", ["NaN", "nan", "Infinity", "-Infinity", "sNaN"])
def test_a_non_finite_amount_is_a_zh_parse_error(
    seeded: sqlite3.Connection, text: str
) -> None:
    preview = build_cash_movement_preview(seeded, _amount_row(text), pool=_rich_pool())
    row = preview.rows[0]
    assert [i.kind for i in row.issues] == ["parse_error"], row.issues
    message = row.issues[0].message
    assert _CJK.search(message), message
    assert _MARKER in message, message
    assert row.has_hard_issue
    # No payload -> the row can never be committed, and never joins the funding batch.
    assert row.payload == {}


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_no_pydantic_english_reaches_the_owner(
    seeded: sqlite3.Connection, text: str
) -> None:
    """The defect itself: the 原因 column printed a pydantic report, not a sentence."""
    preview = build_cash_movement_preview(seeded, _amount_row(text), pool=_rich_pool())
    message = preview.rows[0].issues[0].message
    assert not any(leak in message for leak in _LEAKS), message


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_acq_home_amount_is_refused_the_same_way(
    seeded: sqlite3.Connection, text: str
) -> None:
    """``acq_home_amount`` is the second Decimal cell on this row, and F1 makes it real
    money: it is the home-currency COST of a foreign credit, so a broken one silently
    degrades ``covered_ratio`` for the whole portfolio if it is ever let through."""
    preview = build_cash_movement_preview(seeded, _acq_row(text), pool=_rich_pool())
    row = preview.rows[0]
    assert [i.kind for i in row.issues] == ["parse_error"], row.issues
    message = row.issues[0].message
    assert _CJK.search(message) and _MARKER in message, message
    assert not any(leak in message for leak in _LEAKS), message
    assert row.payload == {}


def test_the_message_names_the_column_and_echoes_the_cell(
    seeded: sqlite3.Connection,
) -> None:
    """A parse error names WHICH cell and WHAT was in it — otherwise the owner is told a
    number is wrong without being told which one, on a row they can no longer see."""
    amount = build_cash_movement_preview(
        seeded, _amount_row("NaN"), pool=_rich_pool()).rows[0].issues[0].message
    assert "金額" in amount and "amount" in amount and "NaN" in amount, amount

    acq = build_cash_movement_preview(
        seeded, _acq_row("Infinity"), pool=_rich_pool()).rows[0].issues[0].message
    assert "acq_home_amount" in acq and "Infinity" in acq, acq


def test_one_broken_row_does_not_take_its_clean_sibling_down(
    seeded: sqlite3.Connection,
) -> None:
    """A preview is a PER-ROW verdict; one unreadable cell costs exactly one row."""
    csv_text = _HEADER + (
        "schwab,2026-01-05,DEPOSIT,TWD,400000,,\n"
        "schwab,2026-02-05,DEPOSIT,TWD,NaN,,\n"
    )
    preview = build_cash_movement_preview(seeded, csv_text, pool=_rich_pool())
    assert len(preview.rows) == 2
    good, bad = preview.rows
    assert good.issues == [], good.issues
    assert good.payload["amount"] == "400000"
    assert [i.kind for i in bad.issues] == ["parse_error"], bad.issues
    assert bad.payload == {}


def test_a_model_validation_error_is_translated_too(
    seeded: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: no ``ValidationError`` may reach the owner as ``str(exc)`` again.

    With the two numeric cells guarded, today's ``CashMovementInput`` has no other field that
    can reject a row this parser builds — so the arm is exercised by tightening the model the
    way a future field constraint would. That is the regression being prevented: the *next*
    constraint added to this model must not re-open the English leak, and a test that only
    covered the two guarded cells would not notice.
    """
    from pydantic import Field

    from portfolio_dash.data_ingestion import cash_import

    class _Stricter(CashMovementInput):
        note: str | None = Field(default=None, max_length=3)

    monkeypatch.setattr(cash_import, "CashMovementInput", _Stricter)
    preview = build_cash_movement_preview(
        seeded,
        _HEADER + "schwab,2026-01-05,DEPOSIT,TWD,400000,,a rather long note\n",
        pool=_rich_pool(),
    )
    row = preview.rows[0]
    assert [i.kind for i in row.issues] == ["parse_error"], row.issues
    message = row.issues[0].message
    assert _CJK.search(message), message
    assert "note" in message, message           # names the field that was refused
    assert not any(leak in message for leak in _LEAKS), message
    assert row.payload == {}


def test_a_finite_cash_row_is_untouched(seeded: sqlite3.Connection) -> None:
    """Counter-evidence that the guard is not "always reject": the ordinary row still parses,
    including a legitimate ``acq_home_amount`` and an exponent-notation amount."""
    preview = build_cash_movement_preview(
        seeded,
        _HEADER + (
            "schwab,2026-01-05,DEPOSIT,TWD,400000,,\n"
            "schwab,2026-01-06,DEPOSIT,USD,1000,32500,wire\n"
            "schwab,2026-01-07,DEPOSIT,TWD,1E+3,,\n"
        ),
        pool=_rich_pool(),
    )
    assert [i.kind for r in preview.rows for i in r.issues] == []
    assert preview.rows[0].payload["amount"] == "400000"
    assert preview.rows[1].payload["acq_home_amount"] == "32500"
    # Exponent notation is FINITE and must survive; the wire keeps the stored form.
    assert Decimal(preview.rows[2].payload["amount"]) == Decimal("1000")


def test_the_neighbouring_parse_errors_do_not_move(
    seeded: sqlite3.Connection,
) -> None:
    """The other failures in ``_parse_row`` are not this repair's subject.

    ⚠ → ✅ ``date.fromisoformat`` raised an ENGLISH ``ValueError`` (``"month must be in
    1..12"``) which ``_parse_row`` rendered verbatim — a SEPARATE English leak of the same
    class as the one fixed here, reported to the orchestrator rather than fixed, because
    this repair was scoped to the non-finite cells and to ``ValidationError``. The date row
    was therefore pinned on its KIND only, so this test would neither certify the leak nor
    break when somebody closed it.

    **G-1 closed it** (``cash_import._date_cell``), so the placeholder is now a real
    assertion: the date row is held to the same CJK standard as its neighbours. The full
    case — all four broken shapes, both bulk doors, and the wording held identical between
    them — lives in ``tests/data_ingestion/test_m2_import_date_zh.py``.
    """
    preview = build_cash_movement_preview(
        seeded,
        _HEADER + (
            "schwab,2026-13-05,DEPOSIT,TWD,400000,,\n"
            "schwab,2026-01-05,DEPOSIT,XXX,400000,,\n"
            "schwab,2026-01-05,DEPOSIT,TWD,,,\n"
        ),
        pool=_rich_pool(),
    )
    kinds = [i.kind for r in preview.rows for i in r.issues]
    assert kinds == ["parse_error"] * 3, kinds
    messages = [i.message for r in preview.rows for i in r.issues]
    assert messages[0] == (
        "日期（date）格式不正確，須為 YYYY-MM-DD，目前是「2026-13-05」"), messages[0]
    assert "幣別（ccy）無法辨識：XXX" in messages[1], messages[1]
    assert "金額（amount）不可空白" in messages[2], messages[2]
    for message in messages:
        assert _CJK.search(message), message
        assert "month must be" not in message and "isoformat" not in message, message


def test_a_zero_amount_is_still_the_validator_s_finding_not_a_parse_error(
    seeded: sqlite3.Connection,
) -> None:
    """``0`` is finite, so it must reach ``validate_cash_movement`` exactly as before — the
    new guard rejects unreadable cells, never merely unwelcome numbers."""
    preview = build_cash_movement_preview(
        seeded, _amount_row("0"), pool=_rich_pool())
    issues = preview.rows[0].issues
    assert [i.kind for i in issues] == ["non_positive_amount"], issues
