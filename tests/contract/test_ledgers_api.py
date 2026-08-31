import sqlite3

from fastapi.testclient import TestClient


def test_transactions_shape_lowercase_side_and_total_sign(api_client: TestClient) -> None:
    r = api_client.get("/api/ledgers/transactions")
    assert r.status_code == 200
    body = r.json()
    assert body["total_count"] == 2
    rows = {(x["symbol"], x["account_id"]): x for x in body["rows"]}
    tx = rows[("2330", "tw_broker")]
    assert tx["side"] == "buy"
    assert tx["account"] == "TW Broker"
    assert tx["shares"] == "1000" and tx["price"] == "500"
    assert tx["total"] == "-500000"
    assert tx["ccy"] == "TWD"
    assert "fee_snapshot" in tx


def test_transactions_filter_and_pagination(api_client: TestClient) -> None:
    r = api_client.get("/api/ledgers/transactions", params={"account_id": "schwab"})
    body = r.json()
    assert body["total_count"] == 1 and body["rows"][0]["symbol"] == "AAPL"
    r2 = api_client.get("/api/ledgers/transactions", params={"limit": 1, "offset": 0})
    assert len(r2.json()["rows"]) == 1 and r2.json()["total_count"] == 2


def test_transactions_bad_date_range_400(api_client: TestClient) -> None:
    r = api_client.get("/api/ledgers/transactions",
                       params={"from": "2026-12-01", "to": "2026-01-01"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"


def test_dividends_lowercase_type(api_client: TestClient) -> None:
    body = api_client.get("/api/ledgers/dividends").json()
    assert body["total_count"] == 1
    d = body["rows"][0]
    assert d["type"] == "cash" and d["symbol"] == "2330"
    assert d["net"] == "5000" and d["account"] == "TW Broker" and d["ccy"] == "TWD"


def test_fx_rows(api_client: TestClient) -> None:
    body = api_client.get("/api/ledgers/fx").json()
    assert body["total_count"] == 1
    fx = body["rows"][0]
    assert fx["from_ccy"] == "TWD" and fx["from_amt"] == "32000"
    assert fx["to_ccy"] == "USD" and fx["to_amt"] == "1000"
    assert fx["implied_rate"] == "32" and fx["account"] == "Charles Schwab"


def test_fx_row_with_nothing_received_degrades_instead_of_500(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """QA-10 — one hand-edited row must not take the whole 換匯 ledger down.

    ``implied_rate = from_amount / to_amount`` raised ``decimal.DivisionByZero`` straight
    through to the generic 500 handler. Reachable only by editing the database (every write
    door refuses a zero leg), which is why the guard is `None` and not a fabricated number:
    ``f.rate(null)`` in ``web/format.js`` already renders 「—」.
    """
    golden_db.execute(
        "INSERT INTO fx_conversions (account_id, date, from_ccy, from_amount, to_ccy,"
        " to_amount) VALUES ('schwab','2026-01-05','TWD','320000','USD','0')")
    golden_db.commit()
    r = api_client.get("/api/ledgers/fx")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_count"] == 2
    broken = [x for x in body["rows"] if x["to_amt"] == "0"]
    assert len(broken) == 1 and broken[0]["implied_rate"] is None
    # The healthy row beside it is untouched.
    assert [x["implied_rate"] for x in body["rows"] if x["to_amt"] == "1000"] == ["32"]


def test_openings_empty_and_shape(api_client: TestClient) -> None:
    body = api_client.get("/api/ledgers/openings").json()
    assert body["total_count"] == 0 and body["rows"] == []
