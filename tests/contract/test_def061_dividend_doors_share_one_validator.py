"""DEF-061 (owner ruling 2026-09-24, spec §14): the dividend correction door and the entry
doors run ONE validator — ``validate.validate_dividend(conn, inp, replacing=)``.

Measured on 4655845 (R3 decision ⑪): ``PUT /api/ledgers/dividends/{id}`` checked the type
string and amount conservation only, so an edit could store what the entry door refuses —
a NET row carrying a withholding, a DRIP / STOCK row without its share count (which breaks
every later rebuild), a type the account's dividend model does not book, a US cash dividend
with no withholding stated.

Parametrised over every dividend type × every check, through the REAL doors: the entry door
(``POST /api/import/preview``, the path the manual form, the CSV, the AI door and the broker
converter all commit through) and the correction door must report the same finding kinds in
the same order, the same leading sentence, and the same gate (hard → refused, soft → saved).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_dividend, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

_DAY = "2026-05-04"
_HEADER = "account,symbol,date,type,gross,withholding,net,reinvest_shares,reinvest_price"


@dataclass(frozen=True)
class Case:
    name: str
    account: str
    symbol: str
    type: str
    gross: str
    withholding: str | None = None
    net: str | None = None
    shares: str | None = None
    price: str | None = None
    expect: tuple[str, ...] = ()


#: One clean baseline per type, then every check that can fire for that type.
CASES = [
    # ---- clean: every type in the account whose model books it
    Case("cash-clean", "tw_broker", "2330", "CASH", "1000", "0", "1000"),
    Case("stock-clean", "tw_broker", "2330", "STOCK", "0", shares="50"),
    Case("drip-clean", "schwab", "AAPL", "DRIP", "100", "30", "70", "0.5", "140"),
    Case("net-clean", "moomoo_my", "1155", "NET", "100", net="100"),
    # ---- dividend_type_mismatch (soft): every type in a model that does not book it
    Case("cash-mismatch", "moomoo_my", "1155", "CASH", "100", "0", "100",
         expect=("dividend_type_mismatch",)),
    Case("stock-mismatch", "schwab", "AAPL", "STOCK", "0", shares="1",
         expect=("dividend_type_mismatch",)),
    Case("drip-mismatch", "tw_broker", "2330", "DRIP", "100", "30", "70", "1",
         expect=("dividend_type_mismatch",)),
    Case("net-mismatch", "tw_broker", "2330", "NET", "100", net="100",
         expect=("dividend_type_mismatch",)),
    # ---- us_cash_dividend_no_withholding (soft): a drip_us CASH row with none stated
    Case("cash-us-no-withholding", "schwab", "AAPL", "CASH", "100",
         expect=("us_cash_dividend_no_withholding",)),
    # ---- net_dividend_withholding (hard)
    Case("net-withholding", "moomoo_my", "1155", "NET", "100", "5", "95",
         expect=("net_dividend_withholding",)),
    # ---- dividend_amounts (hard): conservation / sign, for every type
    Case("cash-amounts", "tw_broker", "2330", "CASH", "100", "30", "90",
         expect=("dividend_amounts",)),
    Case("drip-amounts", "schwab", "AAPL", "DRIP", "100", "30", "90", "1",
         expect=("dividend_amounts",)),
    Case("net-amounts", "moomoo_my", "1155", "NET", "100", net="150",
         expect=("dividend_amounts",)),
    Case("stock-negative", "tw_broker", "2330", "STOCK", "-1", shares="5",
         expect=("dividend_amounts",)),
    # ---- reinvest_shares_required (hard): the share-adding types
    Case("drip-no-shares", "schwab", "AAPL", "DRIP", "100", "30", "70",
         expect=("reinvest_shares_required",)),
    Case("stock-no-shares", "tw_broker", "2330", "STOCK", "0",
         expect=("reinvest_shares_required",)),
]

#: The row each correction starts from: a CLEAN row of the same account + symbol, so the
#: correction door's legacy scoping (``replacing=``) has nothing of its own to carry.
_BASE_ROW = {
    ("tw_broker", "2330"): ("CASH", "10", "0", "10", None, None),
    ("schwab", "AAPL"): ("DRIP", "10", "3", "7", "0.05", "140"),
    ("moomoo_my", "1155"): ("NET", "10", "0", "10", None, None),
}


def _seed(conn: sqlite3.Connection, account: str, symbol: str) -> int:
    upsert_instrument(conn, Instrument(symbol="1155", market=Market.MY,
                                       quote_ccy=Currency.MYR, sector="Financials",
                                       name="Maybank"))
    kind, gross, wh, net, shares, price = _BASE_ROW[(account, symbol)]
    div_id = insert_dividend(
        conn, account_id=account, symbol=symbol, div_date=date(2026, 5, 1),
        div_type=kind, gross=Decimal(gross), withholding=Decimal(wh), net=Decimal(net),
        reinvest_shares=Decimal(shares) if shares else None,
        reinvest_price=Decimal(price) if price else None)
    conn.commit()
    return div_id


def _csv(c: Case) -> str:
    cells = [c.account, c.symbol, _DAY, c.type, c.gross, c.withholding or "", c.net or "",
             c.shares or "", c.price or ""]
    return f"{_HEADER}\n{','.join(cells)}\n"


def _edit_body(c: Case) -> dict[str, Any]:
    return {"account_id": c.account, "symbol": c.symbol, "date": _DAY, "type": c.type,
            "gross": c.gross, "withhold": c.withholding, "net": c.net,
            "reinvest_shares": c.shares, "reinvest_price": c.price, "ack_oversell": True}


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_the_entry_door_and_the_correction_door_report_the_same_findings(
    case: Case, api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    div_id = _seed(golden_db, case.account, case.symbol)

    entry = api_client.post("/api/import/preview",
                            json={"kind": "dividends", "csv_text": _csv(case)})
    assert entry.status_code == 200, entry.text
    row = entry.json()["rows"][0]
    entry_kinds = tuple(k for k in row["kinds"] if k != "alias_account")
    assert entry_kinds == case.expect, row

    before = dict(golden_db.execute("SELECT * FROM dividends WHERE id=?", (div_id,)).fetchone())
    edit = api_client.put(f"/api/ledgers/dividends/{div_id}", json=_edit_body(case))
    hard = row["status"] == "error"
    if hard:
        assert edit.status_code == 400, edit.text
        err = edit.json()["error"]
        issues = err["issues"]
        assert err["message"] == row["reason"]
        after = dict(golden_db.execute(
            "SELECT * FROM dividends WHERE id=?", (div_id,)).fetchone())
        assert after == before, "a refused correction still wrote the row"
    else:
        assert edit.status_code == 200, edit.text
        issues = edit.json()["issues"]
        if issues:
            assert issues[0]["text"] == row["reason"]
    assert tuple(i["code"] for i in issues) == case.expect, issues


def test_the_correction_door_stores_what_the_entry_door_would_store(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Same input, same row: a DRIP given a reinvest PRICE but no share count gets the share
    count the model derives at BOTH doors (the entry door always derived it; the correction
    door stored ``None`` and the next rebuild refused the row)."""
    div_id = _seed(golden_db, "schwab", "AAPL")
    body = {"account_id": "schwab", "symbol": "AAPL", "date": _DAY, "type": "DRIP",
            "gross": "100", "withhold": "30", "net": "70", "reinvest_shares": None,
            "reinvest_price": "140"}
    r = api_client.put(f"/api/ledgers/dividends/{div_id}", json=body)
    assert r.status_code == 200, r.text
    stored = golden_db.execute(
        "SELECT reinvest_shares FROM dividends WHERE id=?", (div_id,)).fetchone()
    assert Decimal(stored["reinvest_shares"]) == Decimal("70") / Decimal("140")


def test_a_legacy_rows_own_hard_condition_stays_correctable(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """``replacing=`` (the DEF-042 rule): a stored NET row that already carries a withholding
    (booked before §6.3's door refused it) can still have its DATE corrected — the finding is
    the row's own, not the edit's — while an edit that changes the offending figure is
    refused like a new entry."""
    upsert_instrument(golden_db, Instrument(symbol="1155", market=Market.MY,
                                            quote_ccy=Currency.MYR, sector="Financials",
                                            name="Maybank"))
    div_id = insert_dividend(
        golden_db, account_id="moomoo_my", symbol="1155", div_date=date(2026, 5, 1),
        div_type="NET", gross=Decimal("100"), withholding=Decimal("5"), net=Decimal("95"))
    golden_db.commit()
    body = {"account_id": "moomoo_my", "symbol": "1155", "date": "2026-05-02",
            "type": "NET", "gross": "100", "withhold": "5", "net": "95"}
    assert api_client.put(f"/api/ledgers/dividends/{div_id}", json=body).status_code == 200
    body["withhold"] = "6"
    body["net"] = "94"
    r = api_client.put(f"/api/ledgers/dividends/{div_id}", json=body)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["issues"][0]["code"] == "net_dividend_withholding"


def test_the_inbox_confirm_door_only_ever_writes_what_the_validator_accepts(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The one dividend door that does not CALL the validator — 配息確認入帳 — is whitelisted
    because it builds every row from the account's own model (type, withholding, net, share
    count) and refuses a DRIP with no reinvest price (``confirmable``). Pinned rather than
    asserted: every kind it books (TW cash, TW 配股, US DRIP, MY NET) passes
    ``validate_dividend`` with no finding at all."""
    from datetime import UTC, datetime

    from portfolio_dash.data_ingestion.store import insert_transaction, list_dividends
    from portfolio_dash.data_ingestion.validate import DividendInput, validate_dividend
    from portfolio_dash.pricing.results import DividendEvent, PriceRow
    from portfolio_dash.pricing.store import upsert_dividend_events, upsert_prices
    from portfolio_dash.shared.models.enums import Side

    now = datetime(2026, 6, 11, tzinfo=UTC)
    upsert_instrument(golden_db, Instrument(symbol="1155", market=Market.MY,
                                            quote_ccy=Currency.MYR, sector="Financials",
                                            name="Maybank", board=".KL"))
    insert_transaction(golden_db, account_id="moomoo_my", symbol="1155", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("9"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 2, 1))
    upsert_prices(golden_db, [PriceRow(instrument="AAPL", market=Market.US,
                                       as_of=date(2026, 5, 8), close=Decimal("100"),
                                       source="test")], fetched_at=now)
    upsert_dividend_events(golden_db, [
        DividendEvent(instrument="2330", market=Market.TW, ex_date=date(2026, 5, 20),
                      cash_amount=Decimal("2.75"), stock_amount=Decimal("2"),
                      currency=Currency.TWD, source="finmind"),
        DividendEvent(instrument="AAPL", market=Market.US, ex_date=date(2026, 5, 10),
                      cash_amount=Decimal("0.25"), currency=Currency.USD, source="yfinance"),
        DividendEvent(instrument="1155", market=Market.MY, ex_date=date(2026, 4, 15),
                      cash_amount=Decimal("0.30"), currency=Currency.MYR, source="yfinance"),
    ], fetched_at=now)
    golden_db.commit()
    before = {d.id for d in list_dividends(golden_db)}
    items = api_client.get("/api/dividend-inbox").json()["rows"]
    assert {i["kind"] for i in items} >= {"cash", "stock", "drip", "net"}, items
    r = api_client.post("/api/dividend-inbox/confirm",
                        json={"fingerprints": [i["fingerprint"] for i in items]})
    assert r.status_code == 200 and r.json()["written"] == 4, r.text
    booked = [d for d in list_dividends(golden_db) if d.id not in before]
    assert {d.type for d in booked} == {"CASH", "STOCK", "DRIP", "NET"}
    for d in booked:
        findings = validate_dividend(golden_db, DividendInput(
            account_id=d.account_id, symbol=d.symbol, div_date=d.date, type=d.type,
            gross=d.gross, withholding=d.withholding, net=d.net,
            reinvest_shares=d.reinvest_shares, reinvest_price=d.reinvest_price,
            ex_date=d.ex_date))
        assert findings == [], (d, findings)
