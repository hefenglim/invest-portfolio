"""CR-09 (pre-merge review, 2026-10-04): a dividend correction owns its ex-date.

``PUT /api/ledgers/dividends/{id}`` always carried ``existing.ex_date`` and ``update_dividend``
never wrote the column, while the edit dialog could neither show nor change it. Yet the
ex-date is load-bearing: a 配股 counts from it (``Dividend.effective_date`` — replay order,
valuation cuts, the 「未來日期」 badge). An inbox-confirmed CASH row carrying ex 2026-03-10,
corrected to STOCK or re-keyed to another symbol, silently moved its effective date onto that
unrelated day.

Now: the dialog shows and sends ``ex_date`` (absent from the body = keep, for older callers;
``null`` = clear); every door refuses an ex-date after the payment date
(``ex_date_after_pay_date``, hard); and an edit that KEEPS the stored ex-date across a symbol
change, or across a type / payment-date change of a STOCK row, asks the owner to confirm it
(``ex_date_carried_over``, soft — the preview shows it, the dialog gates 儲存 on it).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory

D = Decimal
PAY = "2026-04-05"
EX = "2026-03-10"


def _seed(conn: sqlite3.Connection, ids: dict[str, int]) -> None:
    seed_accounts(conn)
    for sym, name in (("2330", "TSMC"), ("2317", "鴻海")):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW, quote_ccy=Currency.TWD,
                                           sector="Semiconductors", name=name, board="TWSE"))
        insert_transaction(conn, account_id="tw_broker", symbol=sym, side=Side.BUY,
                           quantity=D("1000"), price=D("100"), fees=D("0"), tax=D("0"),
                           trade_date=date(2026, 1, 5))
    # What the dividend inbox writes: the payment date as `date`, the provider's ex-date.
    ids["div"] = insert_dividend(
        conn, account_id="tw_broker", symbol="2330", div_date=date.fromisoformat(PAY),
        div_type="CASH", gross=D("2500"), withholding=D("0"), net=D("2500"),
        ex_date=date.fromisoformat(EX))


def _client(factory: DashboardClientFactory) -> tuple[TestClient, int]:
    ids: dict[str, int] = {}
    client = factory(lambda c: _seed(c, ids))
    return client, ids["div"]


def _body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"account_id": "tw_broker", "symbol": "2330", "date": PAY,
                            "type": "cash", "gross": "2500", "withhold": "0", "net": "2500"}
    body.update(over)
    return body


def _stored_ex(client: TestClient) -> str | None:
    rows = client.get("/api/ledgers/dividends").json()["rows"]
    ex: str | None = rows[0]["ex_date"]
    return ex


def _codes(payload: dict[str, Any]) -> list[str]:
    issues = payload.get("issues") or payload.get("error", {}).get("issues") or []
    return [str(i["code"]) for i in issues]


def _preview(client: TestClient, div_id: int, **over: Any) -> list[str]:
    r = client.post("/api/ledgers/dividends/preview",
                    json={**_body(**over), "replaces_div_id": div_id})
    assert r.status_code == 200, r.json()
    return _codes(r.json())


def test_the_correction_can_set_clear_and_keep_the_ex_date(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client, div_id = _client(dashboard_client_factory)
    r = client.put(f"/api/ledgers/dividends/{div_id}", json=_body(ex_date="2026-03-12"))
    assert r.status_code == 200, r.json()
    assert _stored_ex(client) == "2026-03-12"                    # set
    r = client.put(f"/api/ledgers/dividends/{div_id}", json=_body(net="2400", gross="2400"))
    assert r.status_code == 200, r.json()
    assert _stored_ex(client) == "2026-03-12"                    # absent = keep (old callers)
    r = client.put(f"/api/ledgers/dividends/{div_id}", json=_body(ex_date=None))
    assert r.status_code == 200, r.json()
    assert _stored_ex(client) is None                            # null = clear


def test_an_ex_date_after_the_payment_date_is_refused_on_every_door(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client, div_id = _client(dashboard_client_factory)
    assert "ex_date_after_pay_date" in _preview(client, div_id, ex_date="2026-04-10")
    r = client.put(f"/api/ledgers/dividends/{div_id}", json=_body(ex_date="2026-04-10"))
    assert r.status_code == 400, r.json()
    assert "ex_date_after_pay_date" in _codes(r.json())
    assert _stored_ex(client) == EX
    # An edit that moves the PAYMENT date before the kept ex-date invalidates it too.
    r = client.put(f"/api/ledgers/dividends/{div_id}", json=_body(date="2026-03-01"))
    assert r.status_code == 400, r.json()
    assert "ex_date_after_pay_date" in _codes(r.json())
    # The same day on both is a legitimate shape (a source that knows one date).
    assert "ex_date_after_pay_date" not in _preview(client, div_id, ex_date=PAY)
    # The entry doors share the one validator (DEF-061): the CSV door refuses it too.
    csv_text = ("account,symbol,date,type,gross,ex_date\n"
                "tw_broker,2330,2026-04-05,CASH,500,2026-04-10\n")
    p = client.post("/api/import/preview", json={"kind": "dividends", "csv_text": csv_text})
    assert p.status_code == 200, p.json()
    assert "ex_date_after_pay_date" in json.dumps(p.json(), ensure_ascii=False)


def test_rekeying_to_another_symbol_asks_about_the_carried_ex_date(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client, div_id = _client(dashboard_client_factory)
    assert "ex_date_carried_over" in _preview(client, div_id, symbol="2317")
    # The dialog prefills the field, so an echoed value is still a carried one.
    assert "ex_date_carried_over" in _preview(client, div_id, symbol="2317", ex_date=EX)
    # Stating another date — or clearing it — answers the question.
    assert "ex_date_carried_over" not in _preview(client, div_id, symbol="2317",
                                                  ex_date="2026-03-20")
    assert "ex_date_carried_over" not in _preview(client, div_id, symbol="2317", ex_date=None)
    r = client.put(f"/api/ledgers/dividends/{div_id}", json=_body(symbol="2317"))
    assert r.status_code == 200, r.json()
    assert "ex_date_carried_over" in _codes(r.json())          # said in the save's answer too


def test_a_stock_row_asks_when_its_type_or_payment_date_moves(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client, div_id = _client(dashboard_client_factory)
    stock = {"type": "stock", "gross": "0", "withhold": "0", "net": "0",
             "reinvest_shares": "50"}
    # CASH → STOCK: the kept ex-date becomes the day the 配股 counts from.
    r = client.post("/api/ledgers/dividends/preview",
                    json={**_body(**stock), "replaces_div_id": div_id})
    warn = next(i for i in r.json()["issues"] if i["code"] == "ex_date_carried_over")
    assert warn["sev"] == "warn"
    assert EX in warn["text"] and "起計入" in warn["text"]
    r = client.put(f"/api/ledgers/dividends/{div_id}", json=_body(**stock))
    assert r.status_code == 200, r.json()
    # Now a STOCK row: moving only its payment date asks again; touching nothing does not.
    assert "ex_date_carried_over" in _preview(client, div_id, **stock, date="2026-04-08")
    assert "ex_date_carried_over" not in _preview(client, div_id, **stock)


def test_the_question_stays_narrow(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """No noise where nothing an ex-date governs moved: an account change (the date is the
    security's), a CASH row's payment date (nothing reads its ex-date), an amount fix."""
    client, div_id = _client(dashboard_client_factory)
    assert "ex_date_carried_over" not in _preview(client, div_id, date="2026-04-08")
    assert "ex_date_carried_over" not in _preview(client, div_id, net="2400", gross="2400")
    assert "ex_date_carried_over" not in _preview(client, div_id, account_id="tw_broker")
