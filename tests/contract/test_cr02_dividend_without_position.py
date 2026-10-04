"""CR-02 (pre-merge review, 2026-10-04): a dividend for a position the account never held.

``build_book`` raised a bare ``ValueError`` ('dividend for unknown position') that
``build_dashboard`` did not catch, and ``validate_dividend`` — the ONE validator of DEF-061 —
never checked that a position exists. Measured: ``POST /api/import/commit`` kind=dividends with
``tw_broker,2330,2026-03-01,CASH,500`` for an account that never held 2330 answered 200 (written
1); ``GET /api/dashboard`` then raised → 500, and every surface built on ``build_dashboard`` went
down with it.

Both ends are fixed and both are pinned here:

* **the door** — every door that writes a dividend refuses it with a zh sentence
  (``dividend_without_position``), while a dividend paid AFTER a full close (owner ruling H2:
  booked as realized income) still writes;
* **the degradation** — a row already in the ledger never 500s: the replay refuses it on the
  BOOK (``Book.orphan_dividends``) and the dashboard, the drawer and XIRR say 待釐清, naming it.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_transaction,
    list_dividends,
    load_ledger_bundle,
    upsert_instrument,
)
from portfolio_dash.portfolio.cost_basis import UnbookableLedgerError, build_book
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory, init_golden_base

_DIV_HEADER = "account,symbol,date,type,gross\n"


def _base(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    upsert_instrument(conn, Instrument(symbol="2454", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="MediaTek", board="TWSE"))
    upsert_prices(conn, [
        PriceRow(instrument=s, market=Market.TW, as_of=date(2026, 6, 9),
                 close=Decimal("600"), source="test") for s in ("2330", "2454")
    ], fetched_at=GOLDEN_NOW)


def _buy(conn: sqlite3.Connection, symbol: str, qty: str, day: date,
         side: Side = Side.BUY) -> int:
    return insert_transaction(conn, account_id="tw_broker", symbol=symbol, side=side,
                              quantity=Decimal(qty), price=Decimal("500"), fees=Decimal("0"),
                              tax=Decimal("0"), trade_date=day)


def _held_2454(conn: sqlite3.Connection) -> None:
    """tw_broker holds 2454 — so the ledger is not empty — and has NEVER held 2330."""
    _base(conn)
    _buy(conn, "2454", "1000", date(2026, 1, 5))


def _preview_row(client: TestClient, csv_text: str) -> dict[str, Any]:
    r = client.post("/api/import/preview", json={"kind": "dividends", "csv_text": csv_text})
    assert r.status_code == 200, r.text
    row: dict[str, Any] = r.json()["rows"][0]
    return row


# ------------------------------------------------------------------ the door


def test_the_import_door_refuses_a_dividend_the_account_never_held(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_held_2454)
    csv_text = _DIV_HEADER + "tw_broker,2330,2026-03-01,CASH,500\n"
    row = _preview_row(client, csv_text)
    assert row["status"] == "error", row
    assert "dividend_without_position" in row["kinds"], row
    for part in ("2330", "2026-03-01", "{account:tw_broker}", "沒有"):
        assert part in row["reason"], (part, row["reason"])

    c = client.post("/api/import/commit", json={
        "kind": "dividends", "csv_text": csv_text, "ack_warnings": True})
    assert c.status_code == 200, c.text
    out = c.json()
    assert out["written"] == 0 and out["rejected"] == 1, out
    assert out["rejected_rows"][0]["kind"] == "dividend_without_position", out
    assert client.get("/api/dashboard").status_code == 200


def test_a_dividend_dated_before_the_first_buy_is_refused_too(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The position must exist ON OR BEFORE the dividend's effective date, not merely later."""
    client = dashboard_client_factory(_held_2454)
    row = _preview_row(client, _DIV_HEADER + "tw_broker,2454,2026-01-04,CASH,500\n")
    assert row["status"] == "error" and "dividend_without_position" in row["kinds"], row
    same_day = _preview_row(client, _DIV_HEADER + "tw_broker,2454,2026-01-05,CASH,500\n")
    assert "dividend_without_position" not in same_day["kinds"], same_day


def test_a_dividend_paid_after_a_full_close_still_writes(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Owner ruling H2 (2026-07-26): flat on the payment date is ordinary — the payout is
    realized income. "Never held by its date" is a different fact from "closed before it"."""
    def seed(conn: sqlite3.Connection) -> None:
        _base(conn)
        _buy(conn, "2330", "1000", date(2026, 1, 5))
        _buy(conn, "2330", "1000", date(2026, 2, 1), side=Side.SELL)
    client = dashboard_client_factory(seed)
    csv_text = _DIV_HEADER + "tw_broker,2330,2026-03-01,CASH,500\n"
    assert "dividend_without_position" not in _preview_row(client, csv_text)["kinds"]
    c = client.post("/api/import/commit", json={
        "kind": "dividends", "csv_text": csv_text, "ack_warnings": True})
    assert c.status_code == 200 and c.json()["written"] == 1, c.text
    realized = client.get("/api/dashboard").json()["realized"]["rows"]
    assert any(r["kind"] == "dividend" and r["realized"] == "500" for r in realized), realized


def test_the_edit_door_refuses_moving_a_dividend_before_the_position(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """Golden: tw_broker 2330 bought 2026-01-05, CASH dividend 2026-03-01."""
    (div,) = list_dividends(golden_db)
    body = {"account_id": "tw_broker", "symbol": "2330", "date": "2026-01-01", "type": "cash",
            "gross": "5000", "withhold": "0", "net": "5000"}
    pv = api_client.post("/api/ledgers/dividends/preview",
                         json={**body, "replaces_div_id": div.id})
    assert pv.status_code == 200
    assert any(i["code"] == "dividend_without_position" for i in pv.json()["issues"]), pv.json()
    r = api_client.put(f"/api/ledgers/dividends/{div.id}", json=body)
    assert r.status_code == 400, r.text
    assert any(i["code"] == "dividend_without_position" for i in r.json()["error"]["issues"])
    assert list_dividends(golden_db)[0].date == date(2026, 3, 1)


def test_the_ai_door_counts_a_buy_in_the_same_paste_as_the_position(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The AI door commits its kinds in order (交易 → 股利 → 資金), so a dividend whose buy is in
    the SAME paste is not refused at preview; without that buy it is."""
    client = dashboard_client_factory(_held_2454)
    buy = {"kind": "txn", "account_id": "tw_broker", "symbol": "2330", "side": "BUY",
           "date": "2026-02-02", "shares": "1000", "price": "500"}
    div = {"kind": "div", "account_id": "tw_broker", "symbol": "2330", "date": "2026-03-01",
           "type": "CASH", "gross": "500"}
    both = client.post("/api/input/ai/preview", json={"drafts": {"rows": [buy, div]}})
    assert both.status_code == 200, both.text
    (row,) = both.json()["previews"]["dividends"]["rows"]
    assert "dividend_without_position" not in row["kinds"], row

    alone = client.post("/api/input/ai/preview", json={"drafts": {"rows": [div]}})
    (row,) = alone.json()["previews"]["dividends"]["rows"]
    assert row["status"] == "error" and "dividend_without_position" in row["kinds"], row


# ------------------------------------------------------------------ the degradation


def _orphan_in_ledger(conn: sqlite3.Connection) -> None:
    """A row that predates the door's check (or reached the table some other way)."""
    _held_2454(conn)
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=date(2026, 3, 1),
                    div_type="CASH", gross=Decimal("500"), withholding=Decimal("0"),
                    net=Decimal("500"))


def test_a_stored_orphan_never_500s_and_is_named_as_pending(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_orphan_in_ledger)
    dash = client.get("/api/dashboard")
    assert dash.status_code == 200, dash.text
    body = dash.json()
    assert body["kpis"]["xirr"] is None
    reason = body["freshness"]["xirr_unavailable_reason"]
    for part in ("股利", "2330", "2026-03-01", "{account:tw_broker}", "待釐清"):
        assert part in reason, (part, reason)
    assert "股利" in (body["kpis"]["fx_complete_reason"] or ""), body["kpis"]
    # The orphan is not booked anywhere — never as income, never as a ghost holding.
    assert not any(h["symbol"] == "2330" for h in body["holdings"])
    assert not any(r["symbol"] == "2330" for r in body["realized"]["rows"])

    detail = client.get("/api/symbol/2330/detail")
    assert detail.status_code == 200, detail.text
    (line,) = detail.json()["ledger_issues"]["orphan_dividends"]
    assert line["account_id"] == "tw_broker" and line["date"] == "2026-03-01"
    assert "沒有對應持倉" in line["reason"], line


def test_the_replay_refuses_an_orphan_on_the_book_or_raises_typed_when_strict() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    _orphan_in_ledger(conn)
    bundle = load_ledger_bundle(conn)
    book = build_book(bundle, allow_oversell=True)
    (orphan,) = book.orphan_dividends
    assert (orphan.account_id, orphan.symbol, orphan.date) == ("tw_broker", "2330",
                                                                date(2026, 3, 1))
    assert orphan in book.refused_dividends            # so XIRR excludes it too
    assert [h.symbol for h in book.holdings] == ["2454"]
    with pytest.raises(UnbookableLedgerError, match="2330") as exc:
        build_book(bundle)
    assert "沒有" in str(exc.value)
    conn.close()
