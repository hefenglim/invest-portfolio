"""Options W3 — the broker-import route around the converter (spec §8, OPT-D19).

``known_positions`` is read from the LEDGER (the day before the request's earliest row) and
injected into the pure converter, so an expiry whose opening trade arrived in an earlier
request still gets its side; an option row the converter cannot express is a 422 carrying
``reason`` and ``line`` — raised inside ``convert`` as much as inside the parse.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side

_HEADER = '"Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"\n'
PUT = "XYZ261218P00050000"
_EXPIRED = '12/21/2026,Expired,XYZ 12/18/2026 50.00 P,"OPTION EXPIRATION",2,,,$0.00'


def _convert(client: TestClient, *rows: str) -> Any:
    return client.post("/api/broker/convert", json={
        "account": "schwab", "broker": "schwab", "currency": "USD",
        "exports": [{"name": "s.csv", "text": _HEADER + "".join(r + "\n" for r in rows)}]})


def test_an_expiry_alone_reads_the_ledgers_position(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    register_option_contract(golden_db, PUT)
    insert_transaction(golden_db, account_id="schwab", symbol=PUT, side=Side.SELL,
                       quantity=Decimal("200"), price=Decimal("1.5"), fees=Decimal("1.31"),
                       tax=Decimal("0"), trade_date=date(2026, 5, 4), short_sale=True)
    r = _convert(api_client, _EXPIRED)
    assert r.status_code == 200, r.text
    (row,) = r.json()["rows"]["transactions"]
    assert (row["cells"][1], row["cells"][2], row["cells"][4], row["cells"][11]) == (
        PUT, "BUY", "200", "expired")


def test_an_expiry_with_no_position_anywhere_is_a_422_with_reason_and_line(
    api_client: TestClient,
) -> None:
    r = _convert(api_client, _EXPIRED)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert (err["code"], err["reason"], err["line"]) == (
        "broker_row_unmapped", "option_expiry_no_opening", "s.csv:2")


def test_an_assignment_row_is_a_422_from_the_parse(api_client: TestClient) -> None:
    r = _convert(api_client, '12/18/2026,Assigned,XYZ 12/18/2026 50.00 P,"ASSIGNED",2,,,$0.00')
    assert r.status_code == 422
    assert r.json()["error"]["reason"] == "option_assigned_unmapped"


def test_a_generic_unmapped_row_carries_a_null_reason(api_client: TestClient) -> None:
    r = _convert(api_client, '12/18/2026,Never Seen,AAA,"MYSTERY",1,,,$0.00')
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["reason"] is None and err["line"] == "s.csv:2"


def test_an_overlapping_request_converts_from_its_own_rows(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The opening trade already imported AND in this file: the ledger position is read the
    day BEFORE the file's earliest row (zero), so the file's own sell to open drives the
    expiry — nothing is counted twice. The re-imported sell is the CSV door's duplicate."""
    register_option_contract(golden_db, PUT)
    insert_transaction(golden_db, account_id="schwab", symbol=PUT, side=Side.SELL,
                       quantity=Decimal("200"), price=Decimal("1.5"), fees=Decimal("1.31"),
                       tax=Decimal("0"), trade_date=date(2026, 11, 2), short_sale=True)
    r = _convert(api_client,
                 '11/02/2026,Sell to Open,XYZ 12/18/2026 50.00 P,"SELL TO OPEN",2,$1.50,$1.31,'
                 '$298.69', _EXPIRED)
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]["transactions"]
    assert [(x["cells"][2], x["cells"][4], x["cells"][11]) for x in rows] == [
        ("SELL", "200", ""), ("BUY", "200", "expired")]
