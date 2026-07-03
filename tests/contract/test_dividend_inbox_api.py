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


def test_us_symbols_not_detected_v1(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # AAPL is held (schwab) but v1 scope is TW cash only (DRIP needs broker data).
    upsert_dividend_events(golden_db, [DividendEvent(
        instrument="AAPL", market=Market.US, ex_date=ddate(2026, 5, 10),
        cash_amount=Decimal("0.25"), currency=Currency.USD, source="yfinance")],
        fetched_at=_NOW)
    assert all(x["symbol"] != "AAPL" for x in _rows(api_client))
