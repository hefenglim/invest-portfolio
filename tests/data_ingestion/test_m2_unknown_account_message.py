"""L-1: one owner for the unknown/blank account sentence, across every bulk door.

Five importers built the same finding independently as ``f"帳戶 {account_id} 不存在"``. Wave 7
(K-2) repaired exactly one of them — ``validate_cash_movement`` — so a BLANK ``account`` cell
was answered 「帳戶不可空白」 by the cash door and 「帳戶  不存在」 (two spaces, no name) by the
other four: a sentence asserting that an account the owner never typed does not exist, printed
in the one column whose entire job is to say which cell to go and fix.

That is the cross-door divergence ``architecture.md``'s C3 seam exists to prevent, arrived at
from the opposite direction — not two guards disagreeing, but one guard's wording forked five
ways. The repair is a single :func:`validate.unknown_account_issue` that every site calls, so
the next change to this sentence cannot reach four doors and miss the fifth.

**What deliberately does NOT move.** The issue ``kind`` stays ``unknown_account`` everywhere
(``api/wire.py`` and ``api/routers/cash.py`` map it to the ``account_id`` field), the finding
stays HARD, and a NAMED unknown id keeps 「帳戶 {id} 不存在」 byte-identically — this splits one
sentence in two, it does not reword the half that was already right.

**The transaction door is measured here too, and is a different shape.** ``csv_import._cell``
rejects a blank ``account`` cell as a ``parse_error`` 「必填欄位不可空白（欄位 account）」 before
``validate_transaction`` is reached, so the CSV door never rendered the doubled space and is
pinned UNCHANGED below. Its validator (``validate.py``'s first ``unknown_account`` site) did,
and is reachable from the manual 手動輸入 form, whose ``ManualBody.account_id`` carries no
length constraint — so it calls the shared helper as well.
"""

import re
import sqlite3
from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

import pytest

from portfolio_dash.data_ingestion.cash_import import (
    CASH_MOVEMENT_COLUMNS,
    build_cash_movement_preview,
)
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.corporate_action_import import (
    CORPORATE_ACTION_COLUMNS,
    build_corporate_action_preview,
)
from portfolio_dash.data_ingestion.csv_import import (
    TRANSACTION_COLUMNS,
    build_transaction_preview,
)
from portfolio_dash.data_ingestion.dividend_import import (
    DIVIDEND_COLUMNS,
    build_dividend_preview,
)
from portfolio_dash.data_ingestion.fx_import import FX_COLUMNS, build_fx_preview
from portfolio_dash.data_ingestion.opening_import import (
    OPENING_COLUMNS,
    build_opening_preview,
)
from portfolio_dash.data_ingestion.preview import ImportPreview
from portfolio_dash.data_ingestion.validate import (
    CashMovementInput,
    CashPool,
    CashPoolFn,
    TxnInput,
    validate_transaction,
)
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.enums import Side

_ZERO = Decimal("0")

#: The unknown-account sentence with an INTERPOLATED id. After the repair exactly one
#: production site may match it; before it, six did.
_INTERPOLATED = re.compile(r"帳戶 \{[^}]+\} 不存在")

_BLANK_SENTENCE = "帳戶不可空白"
_NAMED_SENTENCE = "帳戶 zz_unknown 不存在"


def _rich_pool() -> CashPoolFn:
    """An unlimited pool probe — the withdraw guard is not this file's subject.

    Every cash/fx row below is funded by construction, so an unfunded ledger could only add a
    second finding that masks the account verdict this file is measuring.
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


class _Door(NamedTuple):
    """One bulk import door: its canonical header, its builder, and one clean row."""

    name: str
    columns: list[str]
    build: Callable[[sqlite3.Connection, str], ImportPreview]
    cells: dict[str, str]


_DOORS: list[_Door] = [
    _Door(
        "cash",
        CASH_MOVEMENT_COLUMNS,
        lambda conn, text: build_cash_movement_preview(conn, text, pool=_rich_pool()),
        {"date": "2026-01-05", "kind": "DEPOSIT", "ccy": "TWD", "amount": "400000"},
    ),
    _Door(
        "corporate_actions",
        CORPORATE_ACTION_COLUMNS,
        build_corporate_action_preview,
        {"date": "2026-01-05", "kind": "SPLIT", "from_symbol": "AAPL",
         "to_symbol": "AAPL", "ratio_to": "2", "ratio_from": "1"},
    ),
    _Door(
        "fx",
        FX_COLUMNS,
        lambda conn, text: build_fx_preview(conn, text, pool=_rich_pool()),
        {"date": "2026-01-05", "from_ccy": "TWD", "from_amount": "100000",
         "to_ccy": "USD", "to_amount": "3000"},
    ),
    _Door(
        "dividends",
        DIVIDEND_COLUMNS,
        build_dividend_preview,
        {"symbol": "AAPL", "date": "2026-01-05", "type": "CASH", "gross": "100"},
    ),
    _Door(
        "openings",
        OPENING_COLUMNS,
        build_opening_preview,
        {"symbol": "AAPL", "shares": "10", "original_cost_total": "1000",
         "build_date": "2026-01-05"},
    ),
]

_DOOR_IDS = [d.name for d in _DOORS]


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_accounts(conn)
    conn.commit()
    return conn


def _csv(door: _Door, account: str) -> str:
    """*door*'s canonical header plus one row whose only defect is its ``account`` cell."""
    values = dict(door.cells)
    values["account"] = account
    body = ",".join(values.get(column, "") for column in door.columns)
    return ",".join(door.columns) + "\n" + body + "\n"


def _account_issue(preview: ImportPreview) -> tuple[str, str, bool]:
    """The row's FIRST finding as ``(kind, message, needs_confirm)``.

    Deliberately the first and not a search: ``api/routers/input_center._preview_wire`` puts
    ``issues[0].message`` into the wire's ``reason``, so a finding that is not first is not
    what the owner reads.
    """
    issues = preview.rows[0].issues
    assert issues, f"the row produced no issue at all: {preview.rows[0]}"
    first = issues[0]
    return first.kind, first.message, first.needs_confirm


# --- L-1: a blank account cell, at all five doors ---------------------------------------


@pytest.mark.parametrize("door", _DOORS, ids=_DOOR_IDS)
def test_a_blank_account_cell_names_the_blank(
    seeded: sqlite3.Connection, door: _Door
) -> None:
    """What the owner saw at four of the five doors: ``帳戶  不存在`` — two spaces where the
    account name should be, and no cell named."""
    kind, message, needs_confirm = _account_issue(door.build(seeded, _csv(door, "")))
    assert kind == "unknown_account", (door.name, kind)
    assert message == _BLANK_SENTENCE, (door.name, message)
    assert "  " not in message, (door.name, repr(message))
    assert needs_confirm is False, door.name
    assert door.build(seeded, _csv(door, "")).rows[0].has_hard_issue, door.name


@pytest.mark.parametrize("door", _DOORS, ids=_DOOR_IDS)
def test_a_whitespace_only_account_cell_is_blank_too(
    seeded: sqlite3.Connection, door: _Door
) -> None:
    """Every CSV door strips its cells, so ``"   "`` must read exactly as ``""`` — the
    predicate is ``.strip()``, not ``== ""``, for the manual doors that do not strip."""
    _kind, message, _confirm = _account_issue(door.build(seeded, _csv(door, "   ")))
    assert message == _BLANK_SENTENCE, (door.name, message)
    assert "  " not in message, (door.name, repr(message))


@pytest.mark.parametrize("door", _DOORS, ids=_DOOR_IDS)
def test_a_named_unknown_account_is_byte_identical(
    seeded: sqlite3.Connection, door: _Door
) -> None:
    """The half that was already right stays byte-for-byte unchanged at every door — this
    repair splits one sentence in two, it does not reword both."""
    kind, message, needs_confirm = _account_issue(
        door.build(seeded, _csv(door, "zz_unknown")))
    assert kind == "unknown_account", (door.name, kind)
    assert message == _NAMED_SENTENCE, (door.name, message)
    assert needs_confirm is False, door.name


def test_every_door_answers_a_blank_account_with_the_same_sentence(
    seeded: sqlite3.Connection,
) -> None:
    """The point of the repair, stated as one assertion: five doors, ONE sentence.

    The parametrized cases above would still pass if a future edit gave each door its own
    (identical) literal; this one fails the moment any two of them disagree, which is the
    state the tree was actually in.
    """
    answers = {door.name: _account_issue(door.build(seeded, _csv(door, "")))[1]
               for door in _DOORS}
    assert len(set(answers.values())) == 1, answers


def test_the_sentence_has_exactly_one_owner_in_data_ingestion() -> None:
    """The structural half: within ``data_ingestion`` the interpolated sentence may appear at
    exactly ONE site, the shared helper.

    Behaviour tests cannot see a copy that no door happens to reach today, and a copy nobody
    reaches is exactly how the four doors drifted from the cash one — they were identical when
    they were written.

    ⚠ Scoped to ``data_ingestion`` on purpose. ``portfolio_dash/api/routers/`` builds the same
    zh sentence by hand at six ``error_body`` sites (broker_import ×1, cash ×3, input_center
    ×1, ledgers ×1) for its own 400/404 envelopes. Those are a different layer with a
    different carrier (``error_body(code, message, field=…)``, not ``Issue``), they were
    outside this repair's allowlist, and they are reported as a gap rather than silently
    swept in here — a test that fails for work nobody was asked to do is a broken window.
    """
    root = Path(__file__).resolve().parents[2] / "portfolio_dash" / "data_ingestion"
    owners = {
        path.relative_to(root).as_posix(): len(_INTERPOLATED.findall(text))
        for path in sorted(root.rglob("*.py"))
        if (text := path.read_text(encoding="utf-8")) and _INTERPOLATED.search(text)
    }
    assert sum(owners.values()) == 1, owners
    assert list(owners) == ["validate.py"], owners


# --- the transaction door: a different shape, pinned so it cannot be swept in ------------


def test_the_transaction_csv_door_still_rejects_a_blank_cell_as_a_parse_error(
    seeded: sqlite3.Connection,
) -> None:
    """Counter-evidence. The transaction CSV door never rendered the doubled space because
    ``csv_import._cell`` refuses a blank required cell first, naming the column. That verdict
    is unchanged — this repair must not pull a sixth door into the ``unknown_account``
    vocabulary."""
    values = {"account": "", "symbol": "AAPL", "side": "BUY", "date": "2026-01-05",
              "shares": "10", "price": "100"}
    text = (",".join(TRANSACTION_COLUMNS) + "\n"
            + ",".join(values.get(c, "") for c in TRANSACTION_COLUMNS) + "\n")
    kind, message, _confirm = _account_issue(build_transaction_preview(seeded, text))
    assert kind == "parse_error", kind
    assert message == "必填欄位不可空白（欄位 account）", message


@pytest.mark.parametrize(
    ("account_id", "expected"),
    [("", _BLANK_SENTENCE), ("   ", _BLANK_SENTENCE), ("zz_unknown", _NAMED_SENTENCE)],
)
def test_the_transaction_validator_says_the_same_thing(
    seeded: sqlite3.Connection, account_id: str, expected: str
) -> None:
    """``validate_transaction`` is the sixth copy of the sentence and the one the MANUAL form
    runs (``ManualBody.account_id`` has no length constraint, so a blank reaches it). Measured
    before the repair: ``帳戶  不存在`` for ``""`` and ``帳戶     不存在`` for ``"   "`` — the
    form does not strip, so the whitespace case is worse there than at any CSV door."""
    inp = TxnInput(account_id=account_id, symbol="AAPL", side=Side.BUY,
                   quantity=Decimal("10"), price=Decimal("100"),
                   trade_date=date(2026, 1, 5))
    issues = [i for i in validate_transaction(seeded, inp) if i.kind == "unknown_account"]
    assert [i.message for i in issues] == [expected], issues
    assert issues[0].needs_confirm is False, issues
