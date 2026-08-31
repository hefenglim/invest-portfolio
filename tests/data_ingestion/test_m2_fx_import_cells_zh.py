"""H-2: every OTHER cell of the fx CSV door still answered in CPython/enum English.

Wave 3 gave ``fx_import`` a zh date reader (``_date_cell``) and QA-07 gave it a zh
non-finite check, but the rest of ``_parse_row`` still ended in
``except (KeyError, ValueError, InvalidOperation) as exc: Issue(message=str(exc))`` — so
whatever Python happened to say went straight into the import preview's 原因 column, the one
column whose entire job is to tell the owner why a row was rejected. Measured on the real
builder before the fix::

    (no ``account`` header)   ->  'account'
    (no ``to_amount`` header) ->  'to_amount'
    from_ccy=GBP              ->  'GBP' is not a valid Currency
    from_amount=abc           ->  [<class 'decimal.ConversionSyntax'>]
    from_amount=(blank)       ->  [<class 'decimal.ConversionSyntax'>]
    from_amount=1,200         ->  [<class 'decimal.ConversionSyntax'>]

A CPython class name is not a reason, and none of them names a column the owner can go and
look at. The cash door next to it has said this in Chinese since E-1/G-1 through typed cell
readers (``cash_import._decimal`` / ``_currency`` / ``_date_cell`` + the 缺少必填欄位 arm);
this door had only two of the four, which is why the leak survived two waves of zh-TW work
on the same file.

The static guard (``tests/architecture/test_user_messages_are_zh_tw.py``) cannot see any of
it: it scans ``Issue(message=<literal>)`` and skips non-literals, and ``str(exc)`` is a call.
So the rule is enforced HERE, on the value, by driving the real importer — the posture
``test_r5_csv_parse_error_zh.py`` takes for the transaction door and
``test_m2_import_date_zh.py`` takes for the date cell.

**Only the WORDING changes — with ONE deliberate exception, pinned below.** The degradation
was already right (a hard ``parse_error``, no payload, so the row can never be committed and
never funds a sibling in the same batch — ``architecture.md``'s C3 property) and is pinned
here too, so a wording fix cannot cost a row. The exception is a lower-case currency
(``usd``), which this door REFUSED while the cash door accepted it; the two bulk doors must
not disagree about the same cell, and refusing it while listing ``USD`` among the supported
codes would have been a self-contradictory sentence.
"""

import re
import sqlite3
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.fx_import import FX_COLUMNS, build_fx_preview
from portfolio_dash.data_ingestion.preview import ImportPreview
from portfolio_dash.data_ingestion.store import insert_cash_movement
from portfolio_dash.data_ingestion.validate import CashMovementInput, CashPool, CashPoolFn
from portfolio_dash.shared.enums import Currency

_HEADER = ",".join(FX_COLUMNS) + "\n"
_ZERO = Decimal("0")
_CJK = re.compile(r"[一-鿿]")

#: One conversion the seeded pool covers — the control row every cell-level case carries, so
#: "one broken cell costs exactly one row" is asserted on the same file as the message.
_GOOD = "schwab,2026-01-05,TWD,320000,USD,10000\n"

#: Python internals that must never reach the 原因 column, whatever the cell holds. The last
#: three are this defect's own signature strings.
_LEAKS = (
    "<class",
    "decimal.",
    "ConversionSyntax",
    "InvalidOperation",
    "Traceback",
    "is not a valid",
    "KeyError",
)


def _rich_pool() -> CashPoolFn:
    """A probe reporting an unlimited pool — the balance guard is not this file's subject,
    and an unfunded row would otherwise mask the parse verdict under an overdraft issue."""

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
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 1),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("900000"))
    conn.commit()
    return conn


def _fx(conn: sqlite3.Connection, body: str, *, header: str = _HEADER) -> ImportPreview:
    return build_fx_preview(conn, header + body, pool=_rich_pool())


def _message(preview: ImportPreview, index: int = 0) -> str:
    row = preview.rows[index]
    assert [i.kind for i in row.issues] == ["parse_error"], row.issues
    assert row.has_hard_issue
    # No payload -> the row can never be committed, and never joins the funding batch.
    assert row.payload == {}
    return row.issues[0].message


def _assert_zh(message: str) -> None:
    assert _CJK.search(message), message
    assert not any(leak in message for leak in _LEAKS), message


# --- a missing COLUMN names the column ------------------------------------------------

#: Every required column, and the file that arrives without it. A missing header is a
#: FILE-level mistake (every row is affected), which is why these carry no clean sibling.
_MISSING_COLUMN: list[tuple[str, str, str]] = [
    ("account", "date,from_ccy,from_amount,to_ccy,to_amount\n",
     "2026-01-05,TWD,320000,USD,10000\n"),
    ("date", "account,from_ccy,from_amount,to_ccy,to_amount\n",
     "schwab,TWD,320000,USD,10000\n"),
    ("from_ccy", "account,date,from_amount,to_ccy,to_amount\n",
     "schwab,2026-01-05,320000,USD,10000\n"),
    ("from_amount", "account,date,from_ccy,to_ccy,to_amount\n",
     "schwab,2026-01-05,TWD,USD,10000\n"),
    ("to_ccy", "account,date,from_ccy,from_amount,to_amount\n",
     "schwab,2026-01-05,TWD,320000,10000\n"),
    ("to_amount", "account,date,from_ccy,from_amount,to_ccy\n",
     "schwab,2026-01-05,TWD,320000,USD\n"),
]


@pytest.mark.parametrize(("column", "header", "body"), _MISSING_COLUMN)
def test_a_missing_required_column_names_the_column(
    seeded: sqlite3.Connection, column: str, header: str, body: str
) -> None:
    """「缺少必填欄位 to_amount」, not the bare quoted word ``'to_amount'``.

    The same sentence the cash and corporate-action doors already give, so the owner learns
    one vocabulary for one mistake whichever bulk door the file went through.
    """
    message = _message(_fx(seeded, body, header=header))
    _assert_zh(message)
    assert message == f"缺少必填欄位 {column}", message


def test_a_missing_column_is_reported_on_every_row_not_just_the_first(
    seeded: sqlite3.Connection,
) -> None:
    """A header is a property of the FILE, so there is no surviving sibling to keep — the
    per-row degradation this file pins elsewhere does not apply, and asserting it would
    have hidden that half the rows had silently become payload-less."""
    preview = _fx(
        seeded,
        "schwab,2026-01-05,TWD,320000,USD\nschwab,2026-02-05,TWD,10000,USD\n",
        header="account,date,from_ccy,from_amount,to_ccy\n",
    )
    assert len(preview.rows) == 2
    for idx in (0, 1):
        assert _message(preview, idx) == "缺少必填欄位 to_amount"


# --- a blank required cell is not the same mistake as a missing column -----------------

_BLANK_CELLS: list[tuple[str, str]] = [
    ("schwab,2026-01-05,,320000,USD,10000\n", "換出幣別（from_ccy）不可空白"),
    ("schwab,2026-01-05,TWD,,USD,10000\n", "換出金額（from_amount）不可空白"),
    ("schwab,2026-01-05,TWD,320000,,10000\n", "換入幣別（to_ccy）不可空白"),
    ("schwab,2026-01-05,TWD,320000,USD,\n", "換入金額（to_amount）不可空白"),
    ("schwab,,TWD,320000,USD,10000\n", "日期（date）不可空白"),
]


@pytest.mark.parametrize(("row", "expected"), _BLANK_CELLS)
def test_a_blank_required_cell_says_which_cell_is_blank(
    seeded: sqlite3.Connection, row: str, expected: str
) -> None:
    """"You forgot the column" and "this cell is empty on row 4" are different mistakes with
    different fixes — ``[<class 'decimal.ConversionSyntax'>]`` was neither."""
    message = _message(_fx(seeded, row))
    _assert_zh(message)
    assert message == expected, message


# --- an unreadable amount echoes the cell ---------------------------------------------

_BAD_AMOUNTS: list[tuple[str, str]] = [
    ("schwab,2026-01-05,TWD,abc,USD,10000\n",
     "換出金額（from_amount）格式不正確，目前是「abc」"),
    ("schwab,2026-01-05,TWD,320000,USD,abc\n",
     "換入金額（to_amount）格式不正確，目前是「abc」"),
    ('schwab,2026-01-05,TWD,"1,200",USD,40\n',
     "換出金額（from_amount）格式不正確，目前是「1,200」"),
    ("schwab,2026-01-05,TWD,US$320000,USD,10000\n",
     "換出金額（from_amount）格式不正確，目前是「US$320000」"),
]


@pytest.mark.parametrize(("row", "expected"), _BAD_AMOUNTS)
def test_an_unreadable_amount_names_the_leg_and_echoes_the_cell(
    seeded: sqlite3.Connection, row: str, expected: str
) -> None:
    """``1,200`` is not a hypothetical: Excel writes thousands separators into a template the
    moment the owner formats the column, and ``[<class 'decimal.ConversionSyntax'>]`` did not
    say which of the two amount columns had it, let alone what was in it."""
    message = _message(_fx(seeded, row))
    _assert_zh(message)
    assert message == expected, message


# --- an unknown currency names the cell, the text, and the supported set ---------------


@pytest.mark.parametrize("text", ["GBP", "gbp", "TWDD", "台幣"])
@pytest.mark.parametrize(("column", "label"),
                         [("from_ccy", "換出幣別"), ("to_ccy", "換入幣別")])
def test_an_unknown_currency_is_explained_in_chinese(
    seeded: sqlite3.Connection, text: str, column: str, label: str
) -> None:
    """The supported list is DERIVED from the enum, here and in the door, so adding a fourth
    currency cannot leave the sentence advertising three."""
    row = (f"schwab,2026-01-05,{text},320000,USD,10000\n" if column == "from_ccy"
           else f"schwab,2026-01-05,TWD,320000,{text},10000\n")
    message = _message(_fx(seeded, row))
    _assert_zh(message)
    assert message.startswith(f"{label}（{column}）"), message
    # The RAW cell, not a normalised one: it is what the owner has to go and fix.
    assert text in message, message
    for supported in Currency:
        assert supported.value in message, message


def test_a_lower_case_currency_is_accepted_exactly_as_the_cash_door_accepts_it(
    seeded: sqlite3.Connection,
) -> None:
    """⚠ The ONE acceptance change in this repair, and it is a parity fix.

    ``cash_import._currency`` upper-cases before the enum lookup, so ``usd`` imports through
    the cash door; this door passed the cell to ``Currency()`` verbatim and answered
    ``'usd' is not a valid Currency``. Two bulk doors disagreeing about the same cell is the
    exact class ``architecture.md``'s C3 seam exists to prevent, and the alternative — a zh
    rejection — would have read 「換出幣別（from_ccy）無法辨識：usd（僅支援 TWD／USD／MYR）」,
    a sentence that refuses a code while listing it as supported.
    """
    preview = _fx(seeded, "schwab,2026-01-05,twd,320000,usd,10000\n")
    assert preview.rows[0].issues == [], preview.rows[0].issues
    assert preview.rows[0].payload["from_ccy"] == "TWD"
    assert preview.rows[0].payload["to_ccy"] == "USD"


# --- what must NOT change --------------------------------------------------------------


def test_one_broken_cell_costs_exactly_one_row(seeded: sqlite3.Connection) -> None:
    """The degradation, pinned so a wording fix cannot cost a row.

    Expected to pass BEFORE the fix as well: a per-row rejection with no payload was always
    right; only the sentence in the rejected row was wrong.
    """
    preview = _fx(seeded, _GOOD + (
        "schwab,2026-02-05,TWD,abc,USD,1000\n"
        "schwab,2026-03-05,GBP,1000,USD,300\n"
        "schwab,2026-04-05,TWD,,USD,1000\n"
    ))
    assert len(preview.rows) == 4
    assert [r.has_hard_issue for r in preview.rows] == [False, True, True, True]
    assert preview.rows[0].issues == [], preview.rows[0].issues
    assert preview.rows[0].payload["from_amount"] == "320000"
    for idx in (1, 2, 3):
        assert preview.rows[idx].payload == {}


def test_a_broken_row_still_funds_no_sibling(seeded: sqlite3.Connection) -> None:
    """``architecture.md`` C3 / QA-01: the batch is the set of rows that will be WRITTEN.

    A row rejected here carries no payload and is excluded from ``batch``, so it can never
    cover a sibling's conversion. Driven with the REAL pool arithmetic shape (a probe that
    counts the injected siblings) rather than the unlimited one, because that is the only
    way the property is observable.
    """
    seen: list[int] = []

    def counting(
        account_id: str,
        ccy: Currency,
        *,
        include: Sequence[CashMovementInput] = (),
        exclude_id: int | None = None,
    ) -> CashPool:
        seen.append(len(include))
        return CashPool(balance=Decimal("900000"), low=_ZERO)

    preview = build_fx_preview(
        seeded,
        _HEADER + "schwab,2026-01-05,TWD,abc,USD,10000\n" + _GOOD,
        pool=counting,
    )
    assert preview.rows[0].payload == {}
    assert preview.rows[1].issues == [], preview.rows[1].issues
    # The only row priced is the clean one (``before`` then ``after``), and ``before`` sees
    # NO siblings: the broken row contributed neither a debit nor a credit to the batch. The
    # 2 in ``after`` is the clean row's OWN two legs (:func:`fx_legs`), not a sibling.
    assert seen == [0, 2], seen


def test_a_clean_file_is_untouched(seeded: sqlite3.Connection) -> None:
    """Control: the readers must not start rejecting rows that always worked, and the
    payload must be byte-identical to what the door wrote before."""
    preview = _fx(seeded, _GOOD)
    assert preview.rows[0].issues == [], preview.rows[0].issues
    assert preview.rows[0].payload == {
        "account_id": "schwab",
        "date": "2026-01-05",
        "from_ccy": "TWD",
        "from_amount": "320000",
        "to_ccy": "USD",
        "to_amount": "10000",
    }
    # Surrounding whitespace is stripped before the readers see it (the builder strips, and
    # the readers strip again so a direct caller gets the same answer).
    spaced = _fx(seeded, "schwab, 2026-01-05 , TWD , 320000 , USD , 10000 \n")
    assert spaced.rows[0].issues == [], spaced.rows[0].issues
    assert spaced.rows[0].payload["from_amount"] == "320000"


def test_the_non_finite_check_is_unchanged(seeded: sqlite3.Connection) -> None:
    """QA-07's wording, byte for byte — a finite-number cell is a DIFFERENT finding from an
    unreadable one, and its sentence is pinned by ``test_m2_fx_import_non_finite.py``."""
    message = _message(_fx(seeded, "schwab,2026-01-05,TWD,NaN,USD,10000\n"))
    assert message == "換出金額必須是有限數字，目前是「NaN」", message


def test_the_legacy_account_alias_still_resolves(seeded: sqlite3.Connection) -> None:
    """Batch B's alias, unchanged: the account cell is still read through
    ``alias_import_account``, and its SOFT notice is still the row's only issue."""
    # MYR->USD, not TWD->USD: ``moomoo_my`` settles USD and is funded in MYR, so a TWD leg
    # would add a (correct) ``ccy_not_allowed`` finding and hide what this test asserts.
    preview = _fx(seeded, "moomoo_my_us,2026-01-05,MYR,32000,USD,7000\n")
    kinds = [i.kind for i in preview.rows[0].issues]
    assert kinds == ["account_alias"], kinds
    assert not preview.rows[0].has_hard_issue
    assert preview.rows[0].payload["account_id"] == "moomoo_my"


def test_no_message_this_door_can_emit_is_ascii_only(seeded: sqlite3.Connection) -> None:
    """The whole file at once — every 原因 the preview hands back must be readable by the
    owner, not just the one under test. One English cell among five Chinese ones is exactly
    how this leak survived two waves of zh-TW work on this module."""
    preview = _fx(seeded, _GOOD + (
        "schwab,2026-02-05,GBP,320000,USD,10000\n"
        "schwab,2026-03-05,TWD,abc,USD,10000\n"
        "schwab,2026-04-05,TWD,320000,USD,\n"
        "schwab,2026-05-05,TWD,320000,USD,NaN\n"
        "schwab,2026/06/05,TWD,320000,USD,10000\n"
    ))
    messages = [i.message for r in preview.rows for i in r.issues]
    assert len(messages) == 5, messages
    for message in messages:
        _assert_zh(message)
