"""FIX-A3 (QA BUG-05): the dividend door's parse failures are vetted zh sentences, and a
NET row may carry the §6.3 number of record — the net — alone.

**Part (a) — message hygiene (H-2).** ``build_dividend_preview``'s parse block answered
``Issue(message=str(exc))``, so the 原因 column — whose entire job is to tell the owner why
a row was rejected — rendered, verbatim::

    (blank gross)   ->  [<class 'decimal.ConversionSyntax'>]
    2026/06/16      ->  Invalid isoformat string: '2026/06/16'
    type "bogus"    ->  unknown dividend type 'bogus'
    (no gross col)  ->  'gross'

The static zh guard cannot see it (it scans ``Issue(message=<literal>)`` and ``str(exc)``
is a call), so the rule is enforced here on the VALUES, the posture of
``test_m2_cash_import_cells_zh`` / ``test_m2_fx_import_cells_zh`` — the doors whose
``_CellError`` pattern the dividend door now shares.

**Part (b) — §6.3 conformance.** Malaysia's single-tier system "records the **net amount
received**", yet the door hard-required ``gross`` — so the manual's own number of record
could not be imported alone (the QA repro row). A ``NET`` row may now supply ``net`` only
(``gross := net``; gross ≡ net by definition under single-tier), and a NET row claiming a
non-zero withholding is refused: ``apply_dividend_model`` would happily book the TW shape
for it, which is exactly why the contradiction is stopped at the door before it reaches the
one amounts engine.
"""

import re
import sqlite3
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.dividend_import import (
    DIVIDEND_COLUMNS,
    build_dividend_preview,
    write_dividend_row,
)
from portfolio_dash.data_ingestion.preview import ImportPreview, commit_preview
from portfolio_dash.data_ingestion.store import list_dividends, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

_HEADER = ",".join(DIVIDEND_COLUMNS) + "\n"
_CJK = re.compile(r"[一-鿿]")

#: Internals that must never reach the 原因 column, whatever the cell holds.
_LEAKS = (
    "<class", "ConversionSyntax", "InvalidOperation",
    "Invalid isoformat", "isoformat", "month must be",
    "unknown dividend type", "ValueError", "KeyError",
)


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    seed_accounts(conn)
    # The QA repro symbol: an MY-market instrument on the merged dual-market account, so
    # the (moomoo_my, MY) binding resolves to the "cash" model whose accepted type is NET.
    upsert_instrument(conn, Instrument(symbol="5225", market=Market.MY,
                                       quote_ccy=Currency.MYR, sector="Industrials",
                                       name="5225"))
    # Registered so the one CLEAN tw row below (blank ex_date) asserts a truly empty
    # issue list rather than a soft symbol_unresolved that has nothing to do with A3.
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Semiconductors",
                                       name="TSMC"))
    conn.commit()
    return conn


def _preview(conn: sqlite3.Connection, row: str) -> ImportPreview:
    return build_dividend_preview(conn, _HEADER + row + "\n")


def _messages(preview: ImportPreview) -> list[str]:
    return [i.message for r in preview.rows for i in r.issues]


def _assert_vetted(preview: ImportPreview) -> None:
    """Every finding is a zh sentence and none of it is a Python internal."""
    messages = _messages(preview)
    assert messages, "expected at least one finding"
    for message in messages:
        assert _CJK.search(message), f"not a zh sentence: {message!r}"
        for leak in _LEAKS:
            assert leak not in message, f"internal leaked to the 原因 column: {message!r}"


# --- part (b): the QA repro row — §6.3's number of record imports alone ---------------


def test_the_qa_repro_net_only_row_imports_cleanly(seeded: sqlite3.Connection) -> None:
    """★ ``moomoo_my,5225,2026-06-16,NET`` with a BLANK gross and net=120 — the §6.3 shape
    — books gross = net = 120, withholding 0, with no finding at all."""
    preview = _preview(seeded, "moomoo_my,5225,2026-06-16,NET,,,120,,")
    (row,) = preview.rows
    assert row.issues == []
    assert (row.payload["gross"], row.payload["withholding"], row.payload["net"]) \
        == ("120", "0", "120")
    summary = commit_preview(seeded, preview, accept={0}, writer=write_dividend_row)
    assert len(summary.written) == 1
    stored = list_dividends(seeded, account_id="moomoo_my")[-1]
    assert stored.type == "NET"
    assert stored.gross == Decimal("120")
    assert stored.withholding == Decimal("0")
    assert stored.net == Decimal("120")


def test_a_gross_only_net_row_still_books_net_equals_gross(
    seeded: sqlite3.Connection,
) -> None:
    """The pre-existing shape (gross supplied, net derived) is untouched — the completion
    is additive, not a replacement."""
    preview = _preview(seeded, "moomoo_my,5225,2026-06-16,NET,120,,,,")
    (row,) = preview.rows
    assert row.issues == []
    assert (row.payload["gross"], row.payload["net"]) == ("120", "120")


def test_a_net_row_with_neither_amount_is_refused_in_zh(
    seeded: sqlite3.Connection,
) -> None:
    preview = _preview(seeded, "moomoo_my,5225,2026-06-16,NET,,,,,")
    assert preview.rows[0].has_hard_issue
    _assert_vetted(preview)
    assert "net" in _messages(preview)[0]


def test_a_net_row_claiming_withholding_is_refused(seeded: sqlite3.Connection) -> None:
    """Single-tier has no withholding: gross ≡ net, so a non-zero withholding means the
    row is mis-typed — hard, with a sentence that names the repair (CASH)."""
    preview = _preview(seeded, "moomoo_my,5225,2026-06-16,NET,120,10,110,,")
    (row,) = preview.rows
    kinds = {i.kind for i in row.issues}
    assert "net_dividend_withholding" in kinds
    assert row.has_hard_issue
    _assert_vetted(preview)
    message = next(i.message for i in row.issues if i.kind == "net_dividend_withholding")
    assert "withholding" in message and "CASH" in message


def test_an_explicit_zero_withholding_on_net_is_not_refused(
    seeded: sqlite3.Connection,
) -> None:
    """``0`` states exactly what the model derives — refusing it would punish precision."""
    preview = _preview(seeded, "moomoo_my,5225,2026-06-16,NET,,0,120,,")
    assert preview.rows[0].issues == []


def test_check_amounts_still_guards_an_inconsistent_net_row(
    seeded: sqlite3.Connection,
) -> None:
    """Both amounts present and impossible (net > gross): the conservation gate (audit M5)
    keeps firing — the completion did not relax it."""
    preview = _preview(seeded, "moomoo_my,5225,2026-06-16,NET,100,,120,,")
    (row,) = preview.rows
    assert {i.kind for i in row.issues} == {"dividend_amounts"}
    assert row.has_hard_issue


def test_the_net_completion_is_scoped_to_net_alone(seeded: sqlite3.Connection) -> None:
    """A CASH row with a blank gross and a filled net stays refused: only the single-tier
    type has "gross ≡ net" as a definition rather than as an assumption."""
    preview = _preview(seeded, "tw_broker,2330,2026-06-16,CASH,,,120,,")
    assert preview.rows[0].has_hard_issue
    _assert_vetted(preview)
    assert "gross" in _messages(preview)[0]


# --- part (a): every parse failure is a specific zh sentence naming the column --------


def test_blank_gross_on_a_cash_row_names_the_column_not_a_python_class(
    seeded: sqlite3.Connection,
) -> None:
    """The headline leak: ``[<class 'decimal.ConversionSyntax'>]`` for a blank gross."""
    preview = _preview(seeded, "tw_broker,2330,2026-06-16,CASH,,,,,")
    assert preview.rows[0].has_hard_issue
    _assert_vetted(preview)
    assert "gross" in _messages(preview)[0] and "不可空白" in _messages(preview)[0]


def test_an_unparseable_gross_echoes_the_cell(seeded: sqlite3.Connection) -> None:
    preview = _preview(seeded, "tw_broker,2330,2026-06-16,CASH,1@2,,,,")
    _assert_vetted(preview)
    assert "1@2" in _messages(preview)[0]


def test_a_malformed_date_gets_the_shared_zh_sentence(seeded: sqlite3.Connection) -> None:
    """Word for word the cash/fx doors' date sentence — one broken date cell says one
    thing whichever bulk door it arrived through."""
    preview = _preview(seeded, "tw_broker,2330,2026/06/16,CASH,50,,,,")
    _assert_vetted(preview)
    message = _messages(preview)[0]
    assert "格式不正確，須為 YYYY-MM-DD" in message
    assert "2026/06/16" in message and "date" in message


def test_a_malformed_ex_date_is_loud_and_vetted(seeded: sqlite3.Connection) -> None:
    """A malformed ``ex_date`` has always rejected the row (only the wording leaked);
    a blank one is still simply None — R6's replays-as-before rule."""
    bad = _preview(seeded, "tw_broker,2330,2026-06-16,CASH,50,,,,,2026/07/01")
    _assert_vetted(bad)
    assert "ex_date" in _messages(bad)[0]
    blank = _preview(seeded, "tw_broker,2330,2026-06-16,CASH,50,,,,,")
    assert blank.rows[0].issues == []


def test_an_unknown_type_echoes_what_was_typed_and_the_supported_set(
    seeded: sqlite3.Connection,
) -> None:
    preview = _preview(seeded, "tw_broker,2330,2026-06-16,bogus,50,,,,")
    _assert_vetted(preview)
    message = _messages(preview)[0]
    assert "bogus" in message
    for supported in ("CASH", "STOCK", "DRIP", "NET"):
        assert supported in message


def test_a_missing_required_column_is_named(seeded: sqlite3.Connection) -> None:
    preview = build_dividend_preview(
        seeded, "account,symbol,date,type\ntw_broker,2330,2026-06-16,CASH\n")
    _assert_vetted(preview)
    assert "缺少必填欄位 gross" in _messages(preview)[0]


def test_an_unparseable_optional_cell_is_loud_not_silently_dropped(
    seeded: sqlite3.Connection,
) -> None:
    """The old ``_opt_decimal`` returned None for a typo'd ``net``, silently booking
    ``net = gross − 0`` instead of the statement's figure. A money cell is either read or
    refused with a sentence — never half-read (H-2)."""
    preview = _preview(seeded, "tw_broker,2330,2026-06-16,CASH,50,,1O0,,")
    assert preview.rows[0].has_hard_issue
    _assert_vetted(preview)
    assert "1O0" in _messages(preview)[0] and "net" in _messages(preview)[0]


def test_a_non_finite_numeric_cell_is_refused_not_a_500(
    seeded: sqlite3.Connection,
) -> None:
    """``Decimal("NaN")`` constructs, and ``check_amounts``' comparison then raises
    ``InvalidOperation`` OUTSIDE the parse arm — the preview endpoint 500'd on one broken
    cell. Now the finite guard answers with the doors' shared sentence."""
    preview = _preview(seeded, "tw_broker,2330,2026-06-16,CASH,50,NaN,,,")
    _assert_vetted(preview)
    assert "必須是有限數字" in _messages(preview)[0]


def test_a_broken_row_still_keeps_its_siblings(seeded: sqlite3.Connection) -> None:
    """The degradation was already right and must stay right: one bad row, one rejected
    row, and the clean sibling imports untouched."""
    preview = build_dividend_preview(
        seeded,
        _HEADER
        + "tw_broker,2330,2026-06-16,CASH,,,,,\n"
        + "moomoo_my,5225,2026-06-16,NET,,,120,,\n",
    )
    assert preview.rows[0].has_hard_issue
    assert preview.rows[1].issues == []
    summary = commit_preview(seeded, preview, accept={0, 1}, writer=write_dividend_row)
    assert len(summary.written) == 1 and len(summary.rejected) == 1
