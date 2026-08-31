"""QA-07: ``Decimal("NaN")`` parses, so the fx CSV door's parse guard never fired.

``_parse_row`` wraps its construction in ``except (KeyError, ValueError, InvalidOperation)``
— but ``Decimal("NaN")`` and ``Decimal("Infinity")`` are perfectly legal constructions, so
the row was returned as PARSED. ``_structural_issues`` then evaluated
``parsed.from_amount <= _ZERO``, and a Decimal NaN comparison raises ``InvalidOperation``
(unlike a float NaN, which compares False) — from outside every try in the module. The
importer therefore died mid-file: ``POST /api/import/preview`` answered HTTP 500 and a file
holding one clean conversion plus one NaN row lost BOTH.

``Infinity`` did not crash but was worse in its own way: it flowed all the way into the
balance guard and produced 「換出金額 Infinity TWD 超過…可用餘額 0」 — a sentence that reads
as an ordinary overdraft rather than as a broken cell.

The cash door next to it (``cash_import.py``) degrades cleanly on the identical input
because its pydantic input model validates INSIDE the try. This door now rejects a
non-finite amount as a hard ``parse_error`` for the same reason: a row that cannot be parsed
is a row with no payload, so it can never be committed and — the ``architecture.md`` C3
property — can never fund a sibling in the same batch either.
"""

import sqlite3
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.fx_import import FX_COLUMNS, build_fx_preview
from portfolio_dash.data_ingestion.store import insert_cash_movement
from portfolio_dash.data_ingestion.validate import CashMovementInput, CashPool, CashPoolFn
from portfolio_dash.shared.enums import Currency

_HEADER = ",".join(FX_COLUMNS) + "\n"
_ZERO = Decimal("0")
_CJK = "必須是有限數字"


def _rich_pool() -> CashPoolFn:
    """A probe reporting an unlimited pool — the balance guard is not this file's subject,
    and an unfunded row would otherwise mask the parse verdict under an overdraft issue."""
    def rich(
        account_id: str, ccy: Currency, *,
        include: Sequence[CashMovementInput] = (), exclude_id: int | None = None,
    ) -> CashPool:
        return CashPool(balance=Decimal("999999999"), low=_ZERO)

    return rich


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_accounts(conn)
    insert_cash_movement(conn, account_id="schwab", move_date=date(2026, 1, 1),
                         kind="DEPOSIT", ccy=Currency.TWD, amount=Decimal("400000"))
    conn.commit()
    return conn


@pytest.mark.parametrize("text", ["NaN", "nan", "Infinity", "-Infinity", "sNaN"])
def test_a_non_finite_from_amount_is_a_parse_error_not_an_exception(
    seeded: sqlite3.Connection, text: str
) -> None:
    preview = build_fx_preview(
        seeded, _HEADER + f"schwab,2026-01-05,TWD,{text},USD,1000\n", pool=_rich_pool())
    row = preview.rows[0]
    assert [i.kind for i in row.issues] == ["parse_error"], row.issues
    assert _CJK in row.issues[0].message, row.issues[0].message
    assert row.has_hard_issue
    # No payload -> the row can never be committed, and never joins the funding batch.
    assert row.payload == {}


@pytest.mark.parametrize("text", ["NaN", "Infinity"])
def test_a_non_finite_to_amount_is_refused_the_same_way(
    seeded: sqlite3.Connection, text: str
) -> None:
    preview = build_fx_preview(
        seeded, _HEADER + f"schwab,2026-01-05,TWD,320000,USD,{text}\n", pool=_rich_pool())
    row = preview.rows[0]
    assert [i.kind for i in row.issues] == ["parse_error"], row.issues
    assert _CJK in row.issues[0].message
    assert row.payload == {}


def test_the_message_echoes_the_cell_the_owner_typed(seeded: sqlite3.Connection) -> None:
    """A parse error names WHICH leg and WHAT was in it — otherwise the owner is told a
    number is wrong without being told which one."""
    preview = build_fx_preview(
        seeded, _HEADER + "schwab,2026-01-05,TWD,NaN,USD,1000\n", pool=_rich_pool())
    message = preview.rows[0].issues[0].message
    assert "換出金額" in message and "NaN" in message, message

    to_leg = build_fx_preview(
        seeded, _HEADER + "schwab,2026-01-05,TWD,320000,USD,Infinity\n", pool=_rich_pool())
    assert "換入金額" in to_leg.rows[0].issues[0].message


def test_one_broken_row_does_not_take_its_clean_sibling_down(
    seeded: sqlite3.Connection,
) -> None:
    """The QA reproduction: a two-row file where row 0 is a real conversion the pool covers.

    Before the fix the whole preview raised, so the clean row was lost along with the
    broken one — the worst outcome available to an import preview, which exists precisely
    to show a per-row verdict.
    """
    csv_text = _HEADER + (
        "schwab,2026-01-05,TWD,320000,USD,10000\n"
        "schwab,2026-02-05,TWD,NaN,USD,1000\n"
    )
    preview = build_fx_preview(seeded, csv_text, pool=_rich_pool())
    assert len(preview.rows) == 2
    assert preview.rows[0].issues == [], preview.rows[0].issues
    assert preview.rows[0].payload["from_amount"] == "320000"
    assert preview.rows[1].issues[0].kind == "parse_error"
