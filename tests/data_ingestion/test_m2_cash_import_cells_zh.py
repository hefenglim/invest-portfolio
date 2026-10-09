"""J-2 / J-3: the cash CSV door's last two imprecise sentences.

Both are message-precision defects in the 原因 column — the one column whose entire job is to
tell the owner which cell to go and fix — and both are the cash door lagging behind the fx
door, which got its typed readers in wave 4 (H-2).

* **J-2** — ``_decimal`` read its cell with ``.get(column, "")``, so a file whose header has
  no ``amount`` column at all was reported as 「金額（amount）不可空白」: a BLANK CELL. The two
  mistakes have different fixes ("add the column to the file" vs "fill in row 4"), which is
  exactly the argument this module's own :func:`cash_import._date_cell` docstring makes for
  reading the date by SUBSCRIPT — the date was simply the only cell that got it. The other
  required columns (``date``) land on ``_parse_row``'s 缺少必填欄位 arm through ``KeyError``;
  ``amount`` did not. ``acq_home_amount`` is OPTIONAL and must keep tolerating an absent
  column, so the two readers deliberately do NOT converge.

* **J-3** — ``_currency`` upper-cased the cell *before* validating and then echoed the
  upper-cased text, so a file containing ``gbp`` was answered 「幣別（ccy）無法辨識：GBP」 — a
  code the owner never typed, in a sentence telling them to go and find it in the file.
  Accepting ``usd`` is unchanged (the fx door was widened to match it in H-2); only what the
  rejection QUOTES moves.

Both are asserted against the fx door's wording as well as on their own, because two bulk
doors answering the same broken cell differently is the divergence ``architecture.md``'s C3
seam exists to prevent — and the fx door is the one that is already right.

**K-1 / K-2 (appended) — the two J left on the table**, both reported in J's own gap list:

* **K-1** — ``_currency`` was the last required cell still read with ``.get("ccy", "")``, so
  J-2's defect survived one column over: a file with no ``ccy`` column was answered
  「幣別（ccy）不可空白」. Same fix, same argument, and the fx door already says
  ``缺少必填欄位 from_ccy`` for the equivalent file.
* **K-2** — ``validate_cash_movement``'s ``unknown_account`` message interpolated the id into
  ``f"帳戶 {id} 不存在"``, so a BLANK account cell rendered ``帳戶  不存在``: two spaces, no
  name, and a sentence claiming an account the owner never typed does not exist. The
  validator is the seam BOTH cash doors share (``api/routers/cash.py::movement_guard`` and
  this importer), so the manual form is pinned here too — that is the point of the seam.
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
from portfolio_dash.data_ingestion.fx_import import FX_COLUMNS, build_fx_preview
from portfolio_dash.data_ingestion.preview import ImportPreview
from portfolio_dash.data_ingestion.validate import (
    CashMovementInput,
    CashPool,
    CashPoolFn,
    validate_cash_movement,
)
from portfolio_dash.shared.enums import Currency

_HEADER = ",".join(CASH_MOVEMENT_COLUMNS) + "\n"
_ZERO = Decimal("0")
_CJK = re.compile(r"[一-鿿]")

#: The supported set is DERIVED from the enum in both doors, so a fourth currency cannot
#: leave this file asserting three.
_SUPPORTED = "／".join(c.value for c in Currency)

#: Python / pydantic internals that must never reach the 原因 column.
_LEAKS = ("KeyError", "is not a valid", "<class", "Traceback", "ValueError")


def _rich_pool() -> CashPoolFn:
    """An unlimited pool probe: the withdraw guard is not this file's subject.

    Every row below is a DEPOSIT, but an unfunded ledger would still let a future overdraft
    finding mask the parse verdict under a second issue.
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


def _built(conn: sqlite3.Connection, csv_text: str) -> ImportPreview:
    return build_cash_movement_preview(conn, csv_text, pool=_rich_pool())


def _message(preview: ImportPreview, index: int = 0) -> str:
    issues = preview.rows[index].issues
    assert issues, f"row {index} produced no issue at all: {preview.rows[index]}"
    return issues[0].message


def _cash_csv(*, columns: list[str], rows: int = 1, **cells: str) -> str:
    """A cash CSV restricted to *columns*, repeated *rows* times.

    Cell values default to one clean deposit, so exactly one thing is wrong per file.
    """
    values = {"account": "schwab", "date": "2026-01-05", "kind": "DEPOSIT",
              "ccy": "TWD", "amount": "400000", "acq_home_amount": "", "note": ""}
    values.update(cells)
    body = ",".join(values[c] for c in columns) + "\n"
    return ",".join(columns) + "\n" + body * rows


# --- J-2: a missing COLUMN is not a blank CELL -----------------------------------------


def test_a_missing_amount_column_names_the_missing_column(
    seeded: sqlite3.Connection,
) -> None:
    """The defect: 「金額（amount）不可空白」 sent the owner looking for an empty cell in a file
    that has no such column, which is the one thing they cannot find."""
    columns = [c for c in CASH_MOVEMENT_COLUMNS if c != "amount"]
    preview = _built(seeded, _cash_csv(columns=columns))
    row = preview.rows[0]
    assert [i.kind for i in row.issues] == ["parse_error"], row.issues
    assert _message(preview) == "缺少必填欄位 amount", _message(preview)
    assert row.has_hard_issue and row.payload == {}


def test_a_blank_amount_cell_still_says_the_cell_is_blank(
    seeded: sqlite3.Connection,
) -> None:
    """Counter-evidence that J-2 is a SPLIT, not a rename: with the column present, the
    blank cell keeps the sentence it has always had (also pinned by
    ``test_cash_import.py::test_unparseable_cells_are_hard_parse_errors``)."""
    preview = _built(seeded, _cash_csv(columns=CASH_MOVEMENT_COLUMNS, amount=""))
    assert [i.kind for i in preview.rows[0].issues] == ["parse_error"]
    assert _message(preview) == "金額（amount）不可空白", _message(preview)


def test_the_two_amount_mistakes_do_not_share_a_sentence(
    seeded: sqlite3.Connection,
) -> None:
    """Stated as its own property: whatever the two sentences are, they must differ — that
    is the entire content of the repair."""
    missing = _message(_built(seeded, _cash_csv(
        columns=[c for c in CASH_MOVEMENT_COLUMNS if c != "amount"])))
    blank = _message(_built(seeded, _cash_csv(columns=CASH_MOVEMENT_COLUMNS, amount="")))
    assert missing != blank, (missing, blank)
    for message in (missing, blank):
        assert _CJK.search(message), message
        assert not any(leak in message for leak in _LEAKS), message


def test_a_missing_amount_column_is_a_file_level_mistake(
    seeded: sqlite3.Connection,
) -> None:
    """There is no clean sibling to keep: every row of the file is missing the same column,
    and each one says so rather than echoing ``KeyError``'s bare quoted word."""
    preview = _built(seeded, _cash_csv(
        columns=[c for c in CASH_MOVEMENT_COLUMNS if c != "amount"], rows=3))
    assert len(preview.rows) == 3
    for row in preview.rows:
        assert [i.kind for i in row.issues] == ["parse_error"], row.issues
        assert row.issues[0].message == "缺少必填欄位 amount", row.issues[0].message
        assert row.payload == {}


def test_a_missing_date_column_is_unchanged(seeded: sqlite3.Connection) -> None:
    """The reader that already did this must keep doing it — and must keep WINNING when both
    columns are absent, because ``_parse_row`` evaluates the date first."""
    only_date = _built(seeded, _cash_csv(
        columns=[c for c in CASH_MOVEMENT_COLUMNS if c != "date"]))
    assert _message(only_date) == "缺少必填欄位 date", _message(only_date)
    both = _built(seeded, _cash_csv(
        columns=[c for c in CASH_MOVEMENT_COLUMNS if c not in ("date", "amount")]))
    assert _message(both) == "缺少必填欄位 date", _message(both)


# --- J-2: the OPTIONAL column must stay optional ---------------------------------------


def test_an_absent_acq_home_amount_column_still_previews(
    seeded: sqlite3.Connection,
) -> None:
    """``acq_home_amount`` is optional (F1), so ``_optional_decimal`` keeps ``.get()``: a file
    without the column is the ordinary shape, not a mistake. Reading it by subscript would
    reject every hand-made cash file in existence."""
    columns = [c for c in CASH_MOVEMENT_COLUMNS if c != "acq_home_amount"]
    preview = _built(seeded, _cash_csv(columns=columns))
    row = preview.rows[0]
    assert row.issues == [], row.issues
    assert row.payload["amount"] == "400000"
    assert "acq_home_amount" not in row.payload


def test_a_blank_acq_home_amount_cell_still_previews(seeded: sqlite3.Connection) -> None:
    preview = _built(seeded, _cash_csv(columns=CASH_MOVEMENT_COLUMNS, acq_home_amount=""))
    assert preview.rows[0].issues == [], preview.rows[0].issues
    assert "acq_home_amount" not in preview.rows[0].payload


def test_a_supplied_acq_home_amount_is_unaffected(seeded: sqlite3.Connection) -> None:
    """A USD credit into the TWD-funded account — the one shape that carries a cost (F1)."""
    preview = _built(seeded, _cash_csv(
        columns=CASH_MOVEMENT_COLUMNS, ccy="USD", amount="1000", acq_home_amount="32500"))
    assert preview.rows[0].issues == [], preview.rows[0].issues
    assert preview.rows[0].payload["acq_home_amount"] == "32500"


# --- J-3: the rejection quotes what the owner typed ------------------------------------


def test_an_unknown_lower_case_currency_is_quoted_as_typed(
    seeded: sqlite3.Connection,
) -> None:
    """The defect: 「無法辨識：GBP」 for a file that says ``gbp``. The owner then searches a
    spreadsheet for a string that is not in it."""
    preview = _built(seeded, _cash_csv(columns=CASH_MOVEMENT_COLUMNS, ccy="gbp"))
    message = _message(preview)
    assert message == f"幣別（ccy）無法辨識：gbp（僅支援 {_SUPPORTED}）", message
    assert "GBP" not in message, message
    assert preview.rows[0].has_hard_issue and preview.rows[0].payload == {}


def test_an_unknown_upper_case_currency_is_unchanged(seeded: sqlite3.Connection) -> None:
    """The already-correct half: an upper-case code is echoed exactly as before, so the
    repair cannot be a rename of the whole sentence."""
    message = _message(_built(seeded, _cash_csv(columns=CASH_MOVEMENT_COLUMNS, ccy="XXX")))
    assert message == f"幣別（ccy）無法辨識：XXX（僅支援 {_SUPPORTED}）", message


def test_a_mixed_case_unknown_currency_is_quoted_as_typed(
    seeded: sqlite3.Connection,
) -> None:
    message = _message(_built(seeded, _cash_csv(columns=CASH_MOVEMENT_COLUMNS, ccy="Gbp")))
    assert "：Gbp（" in message, message


@pytest.mark.parametrize("text", ["usd", "Usd", "uSd", "USD"])
def test_a_valid_currency_is_still_ACCEPTED_in_any_case(
    seeded: sqlite3.Connection, text: str
) -> None:
    """Counter-evidence that J-3 is a wording change only: what the door ACCEPTS does not
    move, and the payload keeps the canonical upper-case code."""
    preview = _built(seeded, _cash_csv(
        columns=CASH_MOVEMENT_COLUMNS, ccy=text, amount="1000"))
    assert preview.rows[0].issues == [], preview.rows[0].issues
    assert preview.rows[0].payload["ccy"] == "USD"


def test_a_blank_currency_cell_is_unchanged(seeded: sqlite3.Connection) -> None:
    message = _message(_built(seeded, _cash_csv(columns=CASH_MOVEMENT_COLUMNS, ccy="")))
    assert message == "幣別（ccy）不可空白", message


# --- both doors say the same thing about the same cell ---------------------------------


def _fx_csv(*, columns: list[str], **cells: str) -> str:
    values = {"account": "schwab", "date": "2026-01-05", "from_ccy": "TWD",
              "from_amount": "100000", "to_ccy": "USD", "to_amount": "3000"}
    values.update(cells)
    return ",".join(columns) + "\n" + ",".join(values[c] for c in columns) + "\n"


def test_both_bulk_doors_answer_a_missing_amount_column_identically(
    seeded: sqlite3.Connection,
) -> None:
    """``缺少必填欄位 <column>`` — one sentence, one shape, whichever door the file went
    through. The fx door has said this since H-2 because it reads every required cell by
    subscript; the cash door said it for ``date`` only."""
    cash = _message(_built(seeded, _cash_csv(
        columns=[c for c in CASH_MOVEMENT_COLUMNS if c != "amount"])))
    fx = build_fx_preview(
        seeded,
        _fx_csv(columns=[c for c in FX_COLUMNS if c != "from_amount"]),
        pool=_rich_pool(),
    ).rows[0].issues[0].message
    assert cash == "缺少必填欄位 amount", cash
    assert fx == "缺少必填欄位 from_amount", fx


def test_both_bulk_doors_quote_an_unknown_currency_as_typed(
    seeded: sqlite3.Connection,
) -> None:
    """The label differs (幣別 vs 換出幣別 — the fx row has two currency cells and must name
    which), but the finding, the echo and the supported set are identical."""
    cash = _message(_built(seeded, _cash_csv(columns=CASH_MOVEMENT_COLUMNS, ccy="gbp")))
    fx = build_fx_preview(
        seeded, _fx_csv(columns=FX_COLUMNS, from_ccy="gbp"), pool=_rich_pool(),
    ).rows[0].issues[0].message
    tail = f"無法辨識：gbp（僅支援 {_SUPPORTED}）"
    assert cash.endswith(tail), cash
    assert fx.endswith(tail), fx


# --- K-1: the LAST required cell that was read with ``.get()`` -------------------------


def test_a_missing_ccy_column_names_the_missing_column(seeded: sqlite3.Connection) -> None:
    """J-2 for the other required cell: ``_currency`` read ``ccy`` with ``.get("ccy", "")``,
    so a file with NO ``ccy`` column at all was answered 「幣別（ccy）不可空白」 — a blank cell
    in a column the file does not have. J fixed ``amount`` and reported this one as the same
    class, for the same reason, one column over."""
    columns = [c for c in CASH_MOVEMENT_COLUMNS if c != "ccy"]
    preview = _built(seeded, _cash_csv(columns=columns))
    row = preview.rows[0]
    assert [i.kind for i in row.issues] == ["parse_error"], row.issues
    assert _message(preview) == "缺少必填欄位 ccy", _message(preview)
    assert row.has_hard_issue and row.payload == {}


def test_the_two_ccy_mistakes_do_not_share_a_sentence(seeded: sqlite3.Connection) -> None:
    """The entire content of K-1, as a property: "you forgot the column" and "row 4's cell is
    empty" are different mistakes with different fixes, so they must not be one sentence."""
    missing = _message(_built(seeded, _cash_csv(
        columns=[c for c in CASH_MOVEMENT_COLUMNS if c != "ccy"])))
    blank = _message(_built(seeded, _cash_csv(columns=CASH_MOVEMENT_COLUMNS, ccy="")))
    assert missing != blank, (missing, blank)
    assert blank == "幣別（ccy）不可空白", blank
    for message in (missing, blank):
        assert _CJK.search(message), message
        assert not any(leak in message for leak in _LEAKS), message


def test_a_missing_ccy_column_is_a_file_level_mistake(seeded: sqlite3.Connection) -> None:
    """Every row is missing the same column, so every row says so — there is no clean sibling
    to preserve, exactly as for ``amount``."""
    preview = _built(seeded, _cash_csv(
        columns=[c for c in CASH_MOVEMENT_COLUMNS if c != "ccy"], rows=3))
    assert len(preview.rows) == 3
    for row in preview.rows:
        assert [i.kind for i in row.issues] == ["parse_error"], row.issues
        assert row.issues[0].message == "缺少必填欄位 ccy", row.issues[0].message
        assert row.payload == {}


def test_a_blank_ccy_cell_keeps_its_clean_sibling(seeded: sqlite3.Connection) -> None:
    """The row-level half: with the column present, one empty cell rejects ONE row and the
    file's other rows still preview — the degradation this door has always had."""
    preview = _built(seeded, _HEADER
                     + "schwab,2026-01-05,DEPOSIT,,400000,,\n"
                     + "schwab,2026-01-06,DEPOSIT,TWD,400000,,\n")
    assert _message(preview, 0) == "幣別（ccy）不可空白", _message(preview, 0)
    assert preview.rows[0].has_hard_issue and preview.rows[0].payload == {}
    assert preview.rows[1].issues == [], preview.rows[1].issues
    assert preview.rows[1].payload["ccy"] == "TWD"


def test_a_present_ccy_column_still_accepts_a_lower_case_code(
    seeded: sqlite3.Connection,
) -> None:
    """Counter-evidence that K-1 changes the reader, not the vocabulary: J-3's acceptance and
    its raw-text echo are both untouched by reading the cell by subscript."""
    ok = _built(seeded, _cash_csv(columns=CASH_MOVEMENT_COLUMNS, ccy="usd", amount="1000"))
    assert ok.rows[0].issues == [], ok.rows[0].issues
    assert ok.rows[0].payload["ccy"] == "USD"
    bad = _message(_built(seeded, _cash_csv(columns=CASH_MOVEMENT_COLUMNS, ccy="gbp")))
    assert bad == f"幣別（ccy）無法辨識：gbp（僅支援 {_SUPPORTED}）", bad


def test_both_bulk_doors_answer_a_missing_currency_column_identically(
    seeded: sqlite3.Connection,
) -> None:
    """``缺少必填欄位 <column>`` for a currency column too, whichever door the file went
    through. The fx door has answered this since H-2 because it reads every required cell by
    subscript; the cash door's ``ccy`` was the one cell left behind."""
    cash = _message(_built(seeded, _cash_csv(
        columns=[c for c in CASH_MOVEMENT_COLUMNS if c != "ccy"])))
    fx = build_fx_preview(
        seeded,
        _fx_csv(columns=[c for c in FX_COLUMNS if c != "from_ccy"]),
        pool=_rich_pool(),
    ).rows[0].issues[0].message
    assert cash == "缺少必填欄位 ccy", cash
    assert fx == "缺少必填欄位 from_ccy", fx


# --- K-2: a BLANK account cell is not an unknown account id ---------------------------


def test_a_blank_account_cell_says_the_account_is_blank(
    seeded: sqlite3.Connection,
) -> None:
    """The defect: ``f"帳戶 {inp.account_id} 不存在"`` rendered ``帳戶  不存在`` — two spaces
    and no name — telling the owner an account they never typed does not exist, instead of
    that the cell is empty."""
    preview = _built(seeded, _cash_csv(columns=CASH_MOVEMENT_COLUMNS, account=""))
    row = preview.rows[0]
    assert [i.kind for i in row.issues] == ["unknown_account"], row.issues
    message = _message(preview)
    assert message == "帳戶不可空白", message
    assert "  " not in message, repr(message)
    assert row.has_hard_issue


def test_an_unknown_account_id_is_byte_identical(seeded: sqlite3.Connection) -> None:
    """The other half: a NAMED unknown account keeps today's sentence exactly, including the
    single spaces around the id (also pinned at the manual door by
    ``tests/contract/test_cash_movement_guard_contract.py``)."""
    preview = _built(seeded, _cash_csv(columns=CASH_MOVEMENT_COLUMNS, account="zz_unknown"))
    assert [i.kind for i in preview.rows[0].issues] == ["unknown_account"]
    assert _message(preview) == "帳戶 zz_unknown 不存在", _message(preview)


def test_a_whitespace_only_account_is_treated_as_blank(
    seeded: sqlite3.Connection,
) -> None:
    """Straight at the shared validator, because the CSV door strips every cell before it
    gets here — the MANUAL door does not, so ``"  "`` is a shape only this call can reach.
    Whitespace is a blank cell, not an account named with two spaces."""
    issues = validate_cash_movement(
        seeded,
        CashMovementInput(account_id="  ", date=date(2026, 1, 5), kind="DEPOSIT",
                          ccy=Currency.TWD, amount=Decimal("100")),
        pool=_rich_pool(),
    )
    assert [i.kind for i in issues] == ["unknown_account"], issues
    assert issues[0].message == "帳戶不可空白", issues[0].message


def test_no_account_message_carries_a_doubled_space(seeded: sqlite3.Connection) -> None:
    """Stated as a property over both shapes, so a future edit cannot re-introduce the gap by
    interpolating an empty id into a sentence that reads correctly only when it is filled."""
    for account in ("", "   ", "zz_unknown"):
        message = _message(_built(
            seeded, _cash_csv(columns=CASH_MOVEMENT_COLUMNS, account=account)))
        assert "  " not in message, (account, repr(message))
        assert _CJK.search(message), message
        assert not any(leak in message for leak in _LEAKS), message
