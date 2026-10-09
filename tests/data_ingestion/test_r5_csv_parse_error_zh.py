"""R5 / QA-23: an unparseable CSV cell must explain itself, not print Python internals.

``build_transaction_preview`` captured every parse failure as ``Issue(message=str(exc))``.
For a malformed number ``str(InvalidOperation(...))`` is literally
``[<class 'decimal.ConversionSyntax'>]`` — and that string is what the import preview's
原因 column shows the owner, a column whose only job is to say why the row was rejected.
A missing column rendered as ``'shares'`` and a bad date as
``Invalid isoformat string: '2026/01/05'``: three flavours of the same defect, all English,
one of them naming a CPython class.

The static guard (``tests/architecture/test_user_messages_are_zh_tw.py``) cannot see any of
this: it scans ``Issue(message=<literal>)`` and skips non-literals, and ``str(exc)`` is a
call. So the rule is enforced here on the VALUE, by driving the real importer.
"""

import re
import sqlite3

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.csv_import import build_transaction_preview

_CJK = re.compile(r"[一-鿿]")

#: The header the downloadable template ships (see ``TRANSACTION_COLUMNS``).
_HEADER = "account,symbol,side,date,shares,price,fee,tax,daytrade,short_sale,note\n"

#: Python internals that must never reach the 原因 column, whatever the failure.
_LEAKS = ("<class", "decimal.", "ConversionSyntax", "isoformat", "Traceback", "Decimal(")


def _parse_reasons(conn: sqlite3.Connection, body: str) -> list[str]:
    preview = build_transaction_preview(conn, _HEADER + body)
    return [i.message for r in preview.rows for i in r.issues if i.kind == "parse_error"]


def test_a_thousands_separator_does_not_print_a_python_class(
    conn: sqlite3.Connection,
) -> None:
    """``1,200`` in the shares column — the QA-23 reproduction, verbatim."""
    seed_accounts(conn)
    reasons = _parse_reasons(
        conn, 'tw_broker,2330,BUY,2026-01-05,"1,200",500,0,0,,,\n')
    assert len(reasons) == 1, reasons
    reason = reasons[0]
    assert "[<class 'decimal.ConversionSyntax'>]" not in reason
    assert not any(leak in reason for leak in _LEAKS), reason
    assert _CJK.search(reason), reason
    # It must name the offending column AND the value the owner typed, or the owner has
    # to guess which of eleven columns broke on a row they can no longer see.
    assert "shares" in reason and "1,200" in reason, reason


def test_a_missing_required_column_names_the_column_in_chinese(
    conn: sqlite3.Connection,
) -> None:
    """``str(KeyError('shares'))`` is ``"'shares'"`` — a quoted word, no sentence at all."""
    seed_accounts(conn)
    preview = build_transaction_preview(
        conn,
        "account,symbol,side,date,price\ntw_broker,2330,BUY,2026-01-05,500\n",
    )
    reasons = [i.message for r in preview.rows for i in r.issues if i.kind == "parse_error"]
    assert len(reasons) == 1, reasons
    assert _CJK.search(reasons[0]), reasons[0]
    assert "shares" in reasons[0], reasons[0]


def test_a_bad_date_and_a_bad_side_are_explained_in_chinese(
    conn: sqlite3.Connection,
) -> None:
    seed_accounts(conn)
    bad_date = _parse_reasons(conn, "tw_broker,2330,BUY,2026/01/05,100,500,0,0,,,\n")
    bad_side = _parse_reasons(conn, "tw_broker,2330,PURCHASE,2026-01-05,100,500,0,0,,,\n")
    for reasons, column, value in ((bad_date, "date", "2026/01/05"),
                                   (bad_side, "side", "PURCHASE")):
        assert len(reasons) == 1, reasons
        assert not any(leak in reasons[0] for leak in _LEAKS), reasons[0]
        assert _CJK.search(reasons[0]), reasons[0]
        assert column in reasons[0] and value in reasons[0], reasons[0]


def test_a_good_row_still_parses(conn: sqlite3.Connection) -> None:
    """Control: the message change must not turn a valid row into a rejected one."""
    seed_accounts(conn)
    preview = build_transaction_preview(
        conn, _HEADER + "tw_broker,2330,BUY,2026-01-05,1000,500,0,0,,,\n")
    assert [i.kind for r in preview.rows for i in r.issues if i.kind == "parse_error"] == []
    assert preview.rows[0].payload["quantity"] == "1000"
