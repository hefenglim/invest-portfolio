"""Contract: 待確認匯入 (FinMind dividend inbox, 2026-07-03 R4 item 1).

Golden subset: tw_broker holds 2330 (BUY 1000 @ 2026-01-05) and one CASH ledger
dividend dated 2026-03-01. Tests seed dividend EVENTS directly (the pricing
store's table the providers write) and drive the inbox endpoints.
"""

import sqlite3
from datetime import UTC, datetime
from datetime import date as ddate
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.pricing.results import DividendEvent
from portfolio_dash.pricing.store import upsert_dividend_events
from portfolio_dash.shared.enums import Currency, Market

_NOW = datetime(2026, 6, 11, tzinfo=UTC)


def _event(ex: str, amount: str, symbol: str = "2330") -> DividendEvent:
    return DividendEvent(
        instrument=symbol, market=Market.TW, ex_date=ddate.fromisoformat(ex),
        cash_amount=Decimal(amount), currency=Currency.TWD, source="finmind")


def _seed_events(conn: sqlite3.Connection, *events: DividendEvent) -> None:
    upsert_dividend_events(conn, list(events), fetched_at=_NOW)


def _rows(api_client: TestClient) -> list[dict[str, object]]:
    r = api_client.get("/api/dividend-inbox")
    assert r.status_code == 200
    return list(r.json()["rows"])


def test_detects_event_after_acquisition(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_events(golden_db, _event("2026-05-20", "2.75"))
    rows = _rows(api_client)
    hit = next((x for x in rows if x["symbol"] == "2330"), None)
    assert hit is not None
    assert hit["account_id"] == "tw_broker"
    assert hit["per_share"] == "2.75"
    assert hit["shares_held"] == "1000"      # golden buy of 1000 before the ex-date
    assert hit["est_gross"] == "2750.00"     # 2.75 × 1000
    assert hit["source"] == "finmind"


def test_event_before_acquisition_excluded(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_events(golden_db, _event("2025-12-15", "3"))  # before the 2026-01-05 buy
    assert all(x["ex_date"] != "2025-12-15" for x in _rows(api_client))


def test_event_near_ledger_row_suppressed(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # golden ledger has a CASH dividend dated 2026-03-01; an event ex 2026-02-20
    # falls inside the ±45-day match window -> already recorded.
    _seed_events(golden_db, _event("2026-02-20", "5"))
    assert all(x["ex_date"] != "2026-02-20" for x in _rows(api_client))


def test_confirm_writes_ledger_and_clears(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_events(golden_db, _event("2026-05-20", "2.75"))
    fp = _rows(api_client)[0]["fingerprint"]
    r = api_client.post("/api/dividend-inbox/confirm", json={"fingerprints": [fp]})
    assert r.status_code == 200 and r.json()["written"] == 1
    # ledger row landed: CASH, gross = net = 2750.00, no withholding
    divs = api_client.get("/api/ledgers/dividends",
                          params={"symbol": "2330", "limit": 500}).json()["rows"]
    row = next(x for x in divs if x["date"] == "2026-05-20")
    assert row["type"] == "cash" and row["gross"] == "2750.00"
    assert row["net"] == "2750.00" and row["withhold"] == "0"
    # the item disappears (ledger-match guard) and the dashboard stays healthy
    assert all(x["fingerprint"] != fp for x in _rows(api_client))
    assert api_client.get("/api/dashboard").status_code == 200
    # double-confirm is a no-op (server recomputes; nothing matches anymore)
    r2 = api_client.post("/api/dividend-inbox/confirm", json={"fingerprints": [fp]})
    assert r2.status_code == 200 and r2.json()["written"] == 0


def test_skip_persists(api_client: TestClient, golden_db: sqlite3.Connection) -> None:
    _seed_events(golden_db, _event("2026-05-20", "2.75"))
    fp = _rows(api_client)[0]["fingerprint"]
    r = api_client.post("/api/dividend-inbox/skip", json={"fingerprints": [fp]})
    assert r.status_code == 200 and r.json()["skipped"] == 1
    assert all(x["fingerprint"] != fp for x in _rows(api_client))


def test_bulk_confirm_multiple_events(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_events(golden_db, _event("2026-04-20", "1.5"), _event("2026-06-01", "2"))
    fps = [x["fingerprint"] for x in _rows(api_client) if x["symbol"] == "2330"]
    assert len(fps) == 2
    r = api_client.post("/api/dividend-inbox/confirm", json={"fingerprints": fps})
    assert r.status_code == 200 and r.json()["written"] == 2


# --- R5 expansion: US DRIP (estimated) / MY NET / TW stock -----------------------


def _us_event(ex: str = "2026-05-10", amount: str = "0.25") -> DividendEvent:
    return DividendEvent(
        instrument="AAPL", market=Market.US, ex_date=ddate.fromisoformat(ex),
        cash_amount=Decimal(amount), currency=Currency.USD, source="yfinance")


def test_us_drip_without_stored_price_not_confirmable(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """DRIP needs a reinvest-price estimate; no stored close near the pay/ex date
    -> the item shows but CANNOT be confirmed (缺再投資價, honest guard)."""
    upsert_dividend_events(golden_db, [_us_event()], fetched_at=_NOW)
    rows = _rows(api_client)
    hit = next((x for x in rows if x["symbol"] == "AAPL"), None)
    assert hit is not None and hit["kind"] == "drip"
    assert hit["confirmable"] is False and "缺再投資價" in (hit["note"] or "")
    r = api_client.post("/api/dividend-inbox/confirm",
                        json={"fingerprints": [hit["fingerprint"]]})
    assert r.status_code == 200 and r.json()["written"] == 0  # cannot force through


def test_us_drip_with_price_books_estimated_reinvest(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    from portfolio_dash.pricing.results import PriceRow
    from portfolio_dash.pricing.store import upsert_prices

    upsert_dividend_events(golden_db, [_us_event()], fetched_at=_NOW)
    upsert_prices(golden_db, [PriceRow(instrument="AAPL", market=Market.US,
                                       as_of=ddate(2026, 5, 8), close=Decimal("100"),
                                       source="test")], fetched_at=_NOW)
    hit = next(x for x in _rows(api_client) if x["symbol"] == "AAPL")
    # schwab holds 10 AAPL from 2026-01-10: gross 2.50, withhold 0.75, net 1.75
    assert hit["kind"] == "drip" and hit["confirmable"] is True
    assert hit["est_gross"] == "2.50" and hit["est_withhold"] == "0.7500"
    assert hit["est_net"] == "1.7500" and hit["est_reinvest_price"] == "100"
    r = api_client.post("/api/dividend-inbox/confirm",
                        json={"fingerprints": [hit["fingerprint"]]})
    assert r.status_code == 200 and r.json()["written"] == 1
    divs = api_client.get("/api/ledgers/dividends",
                          params={"symbol": "AAPL", "limit": 500}).json()["rows"]
    row = next(x for x in divs if x["type"] == "drip")
    assert row["withhold"] == "0.7500" and row["reinvest_shares"] == "0.0175"
    assert api_client.get("/api/dashboard").status_code == 200


def test_my_net_dividend_books_and_dashboard_survives(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """MY single-tier: NET row. Regression for the core bug found in R5: NET used
    to fall into the shares-branch of build_book and CRASH every rebuild."""
    from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
    from portfolio_dash.shared.models.assets import Instrument
    from portfolio_dash.shared.models.enums import Side

    upsert_instrument(golden_db, Instrument(
        symbol="1155", market=Market.MY, quote_ccy=Currency.MYR,
        sector="Banking", name="Maybank", board=".KL"))
    insert_transaction(golden_db, account_id="moomoo_my_my", symbol="1155",
                       side=Side.BUY, quantity=Decimal("1000"), price=Decimal("9"),
                       fees=Decimal("0"), tax=Decimal("0"),
                       trade_date=ddate(2026, 2, 1))
    upsert_dividend_events(golden_db, [DividendEvent(
        instrument="1155", market=Market.MY, ex_date=ddate(2026, 4, 15),
        cash_amount=Decimal("0.30"), currency=Currency.MYR, source="yfinance")],
        fetched_at=_NOW)
    hit = next(x for x in _rows(api_client) if x["symbol"] == "1155")
    assert hit["kind"] == "net" and hit["est_gross"] == "300.00"
    r = api_client.post("/api/dividend-inbox/confirm",
                        json={"fingerprints": [hit["fingerprint"]]})
    assert r.status_code == 200 and r.json()["written"] == 1
    divs = api_client.get("/api/ledgers/dividends",
                          params={"symbol": "1155", "limit": 500}).json()["rows"]
    assert any(x["type"] == "net" and x["net"] == "300.00" for x in divs)
    # THE regression assertion: rebuild paths survive a NET row.
    assert api_client.get("/api/dashboard").status_code == 200
    r2 = api_client.post("/api/actions/recompute")
    assert r2.status_code == 200


def test_tw_stock_dividend_item_books_added_shares(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """配股: 股票股利 2 元（面額 10）→ 每股 0.2 股; 1000 held → +200 $0-cost shares."""
    upsert_dividend_events(golden_db, [DividendEvent(
        instrument="2330", market=Market.TW, ex_date=ddate(2026, 5, 20),
        cash_amount=None, stock_amount=Decimal("2"), currency=Currency.TWD,
        source="finmind")], fetched_at=_NOW)
    hit = next(x for x in _rows(api_client)
               if x["symbol"] == "2330" and x["kind"] == "stock")
    assert hit["est_reinvest_shares"] == "200"
    r = api_client.post("/api/dividend-inbox/confirm",
                        json={"fingerprints": [hit["fingerprint"]]})
    assert r.status_code == 200 and r.json()["written"] == 1
    dash = api_client.get("/api/dashboard").json()
    h2330 = next(h for h in dash["holdings"] if h["symbol"] == "2330")
    assert h2330["shares"] == "1200"  # 1000 + 200 zero-cost shares


def test_same_event_cash_and_stock_are_independent_items(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    upsert_dividend_events(golden_db, [DividendEvent(
        instrument="2330", market=Market.TW, ex_date=ddate(2026, 5, 20),
        cash_amount=Decimal("3"), stock_amount=Decimal("1"), currency=Currency.TWD,
        source="finmind")], fetched_at=_NOW)
    rows = [x for x in _rows(api_client) if x["ex_date"] == "2026-05-20"]
    kinds = {x["kind"] for x in rows}
    assert kinds == {"cash", "stock"}
    fps = {x["fingerprint"] for x in rows}
    assert len(fps) == 2  # distinct fingerprints (:stock suffix)
