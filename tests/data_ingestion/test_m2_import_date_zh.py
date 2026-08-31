"""G-1: both bulk CSV doors rendered CPython's English date error in the 原因 column.

``cash_import._parse_row`` and ``fx_import._parse_row`` each build their row inside a ``try``
whose ``except (ValueError, InvalidOperation) as exc`` returns
``Issue(kind="parse_error", message=str(exc))``.  ``date.fromisoformat`` raises exactly that
exception, so whatever CPython happens to say went straight onto the owner's screen::

    2026-13-01  ->  month must be in 1..12
    2026/07/01  ->  Invalid isoformat string: '2026/07/01'
    01-07-2026  ->  Invalid isoformat string: '01-07-2026'
    (blank)     ->  Invalid isoformat string: ''

None of the four names the column, and the first one does not even mention a date.  The
transaction door has said this in Chinese since QA-23 (``csv_import._date_cell`` ->
``_NOT_A_DATE``); these two were the odd ones out.

The static guard (``tests/architecture/test_user_messages_are_zh_tw.py``) cannot see it — it
scans ``Issue(message=<literal>)`` and skips non-literals, and ``str(exc)`` is a call.  So the
rule is enforced HERE, on the value, by driving the real importers, the same posture
``test_r5_csv_parse_error_zh.py`` takes for the transaction door and
``test_m2_cash_import_non_finite.py`` takes for the non-finite cells.

Only the WORDING changes.  The degradation was already right — status ``error``, HTTP 200,
the broken row carries no payload so it can never be committed and never funds a sibling —
and that is pinned here too, so a wording fix cannot cost a row.
"""

import re
import sqlite3
from collections.abc import Sequence
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.cash_import import (
    CASH_MOVEMENT_COLUMNS,
    build_cash_movement_preview,
)
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.fx_import import FX_COLUMNS, build_fx_preview
from portfolio_dash.data_ingestion.preview import ImportPreview
from portfolio_dash.data_ingestion.validate import CashMovementInput, CashPool, CashPoolFn
from portfolio_dash.shared.enums import Currency

_CASH_HEADER = ",".join(CASH_MOVEMENT_COLUMNS) + "\n"
_FX_HEADER = ",".join(FX_COLUMNS) + "\n"
_ZERO = Decimal("0")
_CJK = re.compile(r"[一-鿿]")

#: The wording BOTH doors share for one unreadable date cell — one shape for one defect, so
#: the owner does not learn two vocabularies for the same broken column.  Asserted equal
#: across the doors in :func:`test_both_bulk_doors_say_the_same_thing_about_the_same_date`.
_BAD_FORMAT = "格式不正確，須為 YYYY-MM-DD"
_BLANK = "不可空白"

#: The four shapes a date cell actually arrives broken in, with the sentence each must get.
#: ``2026-13-01`` is the interesting one: ISO syntax, impossible value, and the ONLY case
#: whose English (``month must be in 1..12``) does not contain the offending text at all.
_CASES: list[tuple[str, str]] = [
    ("2026-13-01", _BAD_FORMAT),
    ("2026/07/01", _BAD_FORMAT),
    ("01-07-2026", _BAD_FORMAT),
    ("", _BLANK),
]

#: CPython internals that must never reach the 原因 column, whatever the cell holds.
_LEAKS = (
    "month must be",
    "day must be",
    "year must be",
    "Invalid isoformat",
    "isoformat",
    "ValueError",
    "<class",
)


def _rich_pool() -> CashPoolFn:
    """A probe reporting an unlimited pool.

    Neither the withdraw guard nor the 換匯 balance guard is this file's subject, and an
    unfunded row would otherwise mask the parse verdict under an overdraft issue.
    """

    def rich(
        account_id: str,
        ccy: Currency,
        *,
        include: Sequence[CashMovementInput] = (),
        exclude_id: int | None = None,
    ) -> CashPool:
        return CashPool(balance=Decimal("999999999"), low=_ZERO)

    return rich


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_accounts(conn)
    conn.commit()
    return conn


def _cash_row(on: str) -> str:
    return f"schwab,{on},DEPOSIT,TWD,400000,,\n"


def _fx_row(on: str) -> str:
    return f"schwab,{on},TWD,100000,USD,3000\n"


def _cash(conn: sqlite3.Connection, body: str) -> ImportPreview:
    return build_cash_movement_preview(conn, _CASH_HEADER + body, pool=_rich_pool())


def _fx(conn: sqlite3.Connection, body: str) -> ImportPreview:
    return build_fx_preview(conn, _FX_HEADER + body, pool=_rich_pool())


# --- the defect itself, on each door -------------------------------------------------


@pytest.mark.parametrize(("text", "marker"), _CASES)
def test_the_cash_door_explains_a_broken_date_in_chinese(
    seeded: sqlite3.Connection, text: str, marker: str
) -> None:
    preview = _cash(seeded, _cash_row(text))
    row = preview.rows[0]
    assert [i.kind for i in row.issues] == ["parse_error"], row.issues
    message = row.issues[0].message
    assert _CJK.search(message), message
    assert marker in message, message
    assert not any(leak in message for leak in _LEAKS), message
    assert row.has_hard_issue
    # No payload -> the row can never be committed, and never joins the funding batch.
    assert row.payload == {}


@pytest.mark.parametrize(("text", "marker"), _CASES)
def test_the_fx_door_explains_a_broken_date_in_chinese(
    seeded: sqlite3.Connection, text: str, marker: str
) -> None:
    preview = _fx(seeded, _fx_row(text))
    row = preview.rows[0]
    assert [i.kind for i in row.issues] == ["parse_error"], row.issues
    message = row.issues[0].message
    assert _CJK.search(message), message
    assert marker in message, message
    assert not any(leak in message for leak in _LEAKS), message
    assert row.has_hard_issue
    assert row.payload == {}


@pytest.mark.parametrize("text", ["2026-13-01", "2026/07/01", "01-07-2026"])
def test_the_message_names_the_column_and_echoes_the_cell(
    seeded: sqlite3.Connection, text: str
) -> None:
    """A parse error names WHICH cell and WHAT was in it.

    ``month must be in 1..12`` did neither — it does not contain ``2026-13-01``, so on a
    12-row file the owner was told a month was wrong somewhere and left to find it.
    """
    for message in (
        _cash(seeded, _cash_row(text)).rows[0].issues[0].message,
        _fx(seeded, _fx_row(text)).rows[0].issues[0].message,
    ):
        assert "date" in message, message
        assert f"「{text}」" in message, message


def test_both_bulk_doors_say_the_same_thing_about_the_same_date(
    seeded: sqlite3.Connection,
) -> None:
    """Byte-identical, not merely both-Chinese.

    The two doors hold one private helper each (``csv_import`` owns the transaction door's
    and cannot be imported from without dragging its ``_CellError`` type across), so the only
    thing standing between them and drift is this assertion.
    """
    for text, _marker in _CASES:
        cash = _cash(seeded, _cash_row(text)).rows[0].issues[0].message
        fx = _fx(seeded, _fx_row(text)).rows[0].issues[0].message
        assert cash == fx, (text, cash, fx)


# --- the degradation, unchanged ------------------------------------------------------


def test_one_broken_date_does_not_take_its_clean_sibling_down(
    seeded: sqlite3.Connection,
) -> None:
    """Pinned so a wording fix cannot cost a row — expected to pass BEFORE the fix too."""
    cash = _cash(seeded, "".join(_cash_row(t) for t, _ in _CASES) + _cash_row("2026-01-05"))
    fx = _fx(seeded, "".join(_fx_row(t) for t, _ in _CASES) + _fx_row("2026-01-05"))
    for preview in (cash, fx):
        assert len(preview.rows) == 5, preview.rows
        assert [r.has_hard_issue for r in preview.rows] == [True] * 4 + [False]
        assert preview.rows[4].issues == [], preview.rows[4].issues


def test_a_valid_date_is_untouched(seeded: sqlite3.Connection) -> None:
    """Control: the helper must not start rejecting dates that always worked."""
    assert _cash(seeded, _cash_row("2026-01-05")).rows[0].issues == []
    assert _fx(seeded, _fx_row("2026-01-05")).rows[0].issues == []
    # Leading/trailing whitespace is stripped by the readers before ``_parse_row`` sees it.
    assert _cash(seeded, _cash_row(" 2026-01-05 ")).rows[0].issues == []
    assert _fx(seeded, _fx_row(" 2026-01-05 ")).rows[0].issues == []


def test_a_missing_date_COLUMN_still_says_the_column_is_missing(
    seeded: sqlite3.Connection,
) -> None:
    """A different owner mistake from a broken cell, and it already had its own zh sentence.

    ``_parse_row`` reads ``raw["date"]`` by subscript so an ABSENT header still raises
    ``KeyError`` into the 缺少必填欄位 arm.  Reading it with ``.get()`` would have collapsed
    "you forgot the column" into "this cell is blank" — a regression the fix must not make.
    """
    preview = build_cash_movement_preview(
        seeded,
        "account,kind,ccy,amount\nschwab,DEPOSIT,TWD,400000\n",
        pool=_rich_pool(),
    )
    message = preview.rows[0].issues[0].message
    assert message == "缺少必填欄位 date", message
