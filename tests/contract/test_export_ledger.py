"""Contract tests for POST /api/export/ledger (single-ledger reconciliation CSV)."""

from fastapi.testclient import TestClient


def test_export_ledger_transactions(api_client: TestClient) -> None:
    r = api_client.post("/api/export/ledger", json={"kind": "transactions"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "ledger_transactions_all_all.csv" in r.headers["content-disposition"]
    assert r.content[:3] == b"\xef\xbb\xbf"
    text = r.content[3:].decode("utf-8")
    header = text.split("\r\n", 1)[0]
    assert header.startswith("id,account_id,symbol,side,quantity,price,fees,tax,trade_date")
    # golden 2330 BUY 1000 @ 500 at source precision (raw DB TEXT).
    assert "2330,BUY,1000,500," in text


def test_export_ledger_fx_kind_maps_table(api_client: TestClient) -> None:
    r = api_client.post("/api/export/ledger", json={"kind": "fx"})
    assert r.status_code == 200
    text = r.content[3:].decode("utf-8")
    assert text.split("\r\n", 1)[0].startswith(
        "id,account_id,date,from_ccy,from_amount,to_ccy,to_amount")
    # golden schwab TWD 32000 -> USD 1000 conversion.
    assert "schwab,2026-01-08,TWD,32000,USD,1000" in text


def test_export_ledger_date_range_filters(api_client: TestClient) -> None:
    # 2330 buy 2026-01-05, AAPL buy 2026-01-10; from=2026-01-08 drops the 2330 row.
    r = api_client.post("/api/export/ledger",
                        json={"kind": "transactions", "from": "2026-01-08"})
    assert r.status_code == 200
    assert "ledger_transactions_2026-01-08_all.csv" in r.headers["content-disposition"]
    text = r.content[3:].decode("utf-8")
    assert "AAPL" in text and "2330" not in text


def test_export_ledger_invalid_kind_400(api_client: TestClient) -> None:
    r = api_client.post("/api/export/ledger", json={"kind": "bogus"})
    assert r.status_code == 400
    assert r.json()["error"]["field"] == "kind"


def test_export_ledger_bad_range_400(api_client: TestClient) -> None:
    r = api_client.post("/api/export/ledger",
                        json={"kind": "transactions", "from": "2026-12-31", "to": "2026-01-01"})
    assert r.status_code == 400
    assert r.json()["error"]["field"] == "from"


def test_export_ledger_missing_kind_400(api_client: TestClient) -> None:
    # kind is required — the app's RequestValidationError handler answers 400 field=kind.
    r = api_client.post("/api/export/ledger", json={})
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "validation_error" and body["field"] == "kind"
