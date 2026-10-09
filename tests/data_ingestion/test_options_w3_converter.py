"""Options W3 — the Schwab converter's option rows (spec §8, OPT-D9 / OPT-D11 / OPT-D19).

Synthetic Schwab-format rows only (the real export is git-ignored and never read here). Every
option row shape: the four trades, an expiry (Schwab's ``Expired`` and TD's removal journal),
a journal-pair adjustment, a platform transfer, an assignment; ``known_positions``; the
``UnmappedRow`` reasons; and the converted ``cells`` committed through the ordinary CSV door,
whose cash ties to the statement's Amount.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.broker import schwab
from portfolio_dash.data_ingestion.broker.convert import convert, render_kind
from portfolio_dash.data_ingestion.broker.ir import RawEvent, UnmappedRow
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.csv_import import (
    build_transaction_preview,
    normalize_import_csv,
    write_transaction_row,
)
from portfolio_dash.data_ingestion.preview import commit_preview
from portfolio_dash.data_ingestion.store import list_transactions

_HEADER = '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
PUT = "XYZ261218P00050000"
C60 = "XYZ261218C00060000"
C20 = "XYZ261218C00020000"


def _parse(*rows: str, file: str = "s.csv") -> list[RawEvent]:
    return schwab.parse(_HEADER + "".join(r + "\n" for r in rows), source_file=file)


def _txn_cells(events: list[RawEvent], known: dict[str, Decimal] | None = None
               ) -> list[list[str]]:
    conv, _g, report = convert(events, "schwab", "USD", known_positions=known or {})
    assert report.ok, [i.detail for i in report.blocking]
    return conv.rows["transactions"]


_STO = '11/02/2026,Sell to Open,XYZ 12/18/2026 50.00 P,"SELL TO OPEN",2,$1.50,$1.31,$298.69'
_BTC = '11/20/2026,Buy to Close,XYZ 12/18/2026 50.00 P,"BUY TO CLOSE",2,$0.40,$1.30,-$81.30'


def test_the_four_trade_shapes_write_share_equivalents_on_the_contract() -> None:
    rows = _txn_cells(_parse(
        _STO, _BTC,
        '11/03/2026,Buy to Open,XYZ 12/18/2026 60.00 C,"BUY TO OPEN",1,$2.00,$0.65,-$200.65',
        '11/25/2026,Sell to Close,XYZ 12/18/2026 60.00 C,"SELL TO CLOSE",1,$3.00,$0.66,$299.34',
    ))
    by = {(r[1], r[2], r[3]): r for r in rows}
    sto = by[(PUT, "SELL", "2026-11-02")]
    assert (sto[4], sto[5], sto[6], sto[7], sto[9], sto[11]) == ("200", "1.50", "1.31", "0",
                                                                 "1", "")
    btc = by[(PUT, "BUY", "2026-11-20")]
    assert (btc[4], btc[5], btc[9]) == ("200", "0.40", "0")
    assert by[(C60, "BUY", "2026-11-03")][9] == "0"
    assert by[(C60, "SELL", "2026-11-25")][9] == "0"   # selling to CLOSE is not a short


def test_the_report_fields_carry_the_display_text_only() -> None:
    conv, _g, _r = convert(_parse(_STO), "schwab", "USD", known_positions={})
    (detail,) = conv.details["transactions"]
    assert detail.symbol == "XYZ 2026-12-18 50 Put"
    assert detail.shares == "2 口（200 股當量）"
    assert detail.cells[1] == PUT and detail.cells[4] == "200"


def test_an_expiry_is_a_price_zero_row_dated_the_expiry_after_that_days_trades() -> None:
    rows = _txn_cells(_parse(
        _STO,
        # Posted the Monday after the Friday expiry; two lots merged into one row.
        '12/21/2026,Expired,XYZ 12/18/2026 50.00 P,"OPTION EXPIRATION",1,,,$0.00',
        '12/21/2026,Expired,XYZ 12/18/2026 50.00 P,"OPTION EXPIRATION",1,,,$0.00',
        '12/18/2026,Buy,AAA,"AAA CORP",1,$10.00,,-$10.00',
    ))
    assert [(r[1], r[3]) for r in rows][-2:] == [("AAA", "2026-12-18"), (PUT, "2026-12-18")]
    exp = rows[-1]
    assert (exp[2], exp[4], exp[5], exp[6], exp[7], exp[9], exp[11]) == (
        "BUY", "200", "0", "0", "0", "0", "expired")


def test_tds_removal_journal_is_an_expiry_too() -> None:
    rows = _txn_cells(_parse(
        _STO,
        '12/18/2026,Journaled Shares,XYZ 12/18/2026 50.00 P,'
        '"TDA TRAN - REMOVAL OF OPTION DUE TO EXPIRATION (XYZ)",2,,,$0.00',
    ))
    assert rows[-1][11] == "expired" and rows[-1][2] == "BUY"


def test_an_expiry_with_no_position_needs_known_positions() -> None:
    events = _parse('12/18/2026,Expired,XYZ 12/18/2026 50.00 P,"OPTION EXPIRATION",2,,,$0.00')
    with pytest.raises(UnmappedRow) as exc:
        convert(events, "schwab", "USD", known_positions={})
    assert exc.value.reason == "option_expiry_no_opening"
    assert exc.value.line == "s.csv:2"
    rows = _txn_cells(events, {PUT: Decimal("-200")})
    assert (rows[0][2], rows[0][4], rows[0][11]) == ("BUY", "200", "expired")
    long_rows = _txn_cells(events, {PUT: Decimal("200")})
    assert long_rows[0][2] == "SELL"


def test_a_journal_pair_two_to_six_is_a_one_to_three_adjust() -> None:
    conv, _g, report = convert(_parse(
        '11/03/2026,Buy to Open,XYZ 12/18/2026 60.00 C,"BUY TO OPEN",2,$2.00,$1.30,-$401.30',
        '11/10/2026,Journaled Shares,XYZ 12/18/2026 60.00 C,'
        '"TDA TRAN - OPTION POSITION CHANGE (XYZ)",-2,,,$0.00',
        '11/10/2026,Journaled Shares,XYZ 12/18/2026 20.00 C,'
        '"TDA TRAN - OPTION POSITION CHANGE (XYZ)",6,,,$0.00',
    ), "schwab", "USD", known_positions={})
    assert report.ok, [i.detail for i in report.blocking]
    (action,) = conv.rows["corporate_actions"]
    assert action[:7] == ["schwab", "2026-11-10", "OPTION_ADJUST", C60, C20, "3", "1"]


def test_a_non_standard_pair_stops_the_run_with_its_reason() -> None:
    with pytest.raises(UnmappedRow) as exc:
        convert(_parse(
            '11/03/2026,Buy to Open,XYZ 12/18/2026 60.00 C,"BUY TO OPEN",2,$2.00,$1.30,'
            '-$401.30',
            '11/10/2026,Journaled Shares,XYZ 12/18/2026 60.00 C,'
            '"TDA TRAN - OPTION ODD SPLIT REORGANIZATION (XYZ)",-2,,,$0.00',
            '11/10/2026,Journaled Shares,XYZ 12/18/2026 24.00 C,'
            '"TDA TRAN - OPTION ODD SPLIT REORGANIZATION (XYZ)",5,,,$0.00',
        ), "schwab", "USD", known_positions={})
    assert exc.value.reason == "option_nonstandard_adjust"


def test_a_digit_root_contract_is_non_standard() -> None:
    with pytest.raises(UnmappedRow) as exc:
        convert(_parse(
            '11/03/2026,Buy to Open,XYZ1 12/18/2026 60.00 C,"BUY TO OPEN",1,$2.00,$0.65,'
            '-$200.65'), "schwab", "USD", known_positions={})
    assert exc.value.reason == "option_nonstandard_adjust"


def test_an_assignment_row_is_refused_by_the_adapter() -> None:
    with pytest.raises(UnmappedRow) as exc:
        _parse('12/18/2026,Assigned,XYZ 12/18/2026 50.00 P,"ASSIGNED",2,,,$0.00')
    assert exc.value.reason == "option_assigned_unmapped"


def test_an_uncancelled_option_transfer_is_dropped_with_a_reason() -> None:
    conv, _g, report = convert(_parse(
        '09/20/2026,Journaled Shares,XYZ 12/18/2026 50.00 P,'
        '"TDA TRAN - TRANSFER OF SECURITY OR OPTION IN (XYZ)",1,,,$0.00',
    ), "schwab", "USD", known_positions={})
    assert [d.why for d in conv.dropped] == ["option_transfer"]
    assert conv.rows["transactions"] == []
    assert "option_transfer_dropped" in {i.code for i in report.advisory}


def test_option_symbols_never_become_derived_openings() -> None:
    conv, _g, _r = convert(_parse(
        '11/20/2026,Buy to Close,XYZ 12/18/2026 50.00 P,"BUY TO CLOSE",2,$0.40,$1.30,'
        '-$81.30'), "schwab", "USD", known_positions={})
    assert conv.openings == {}


# --- the cells commit through the CSV door, and the cash ties ---------------------------------


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    seed_accounts(c)
    yield c
    c.close()


def test_the_converted_cells_commit_clean_and_tie_to_the_amount(
    conn: sqlite3.Connection,
) -> None:
    events = _parse(
        _STO,
        '12/21/2026,Expired,XYZ 12/18/2026 50.00 P,"OPTION EXPIRATION",2,,,$0.00',
    )
    conv, _g, report = convert(events, "schwab", "USD", known_positions={})
    assert report.ok
    text = render_kind("transactions", conv.rows["transactions"])
    preview = build_transaction_preview(conn, normalize_import_csv(text, "date").text)
    assert [r.has_hard_issue for r in preview.rows] == [False, False], [
        [i.message for i in r.issues] for r in preview.rows]
    commit_preview(conn, preview, accept={0, 1}, writer=write_transaction_row)
    sto, exp = list_transactions(conn, symbol=PUT)
    # The CSV door books quantity × price − fee: the statement's own Amount.
    assert sto.quantity * sto.price - sto.fees == Decimal("298.69")
    assert (exp.close_reason, exp.price, exp.fees) == ("expired", Decimal("0"), Decimal("0"))
