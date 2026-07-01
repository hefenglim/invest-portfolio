import io
import json
import sqlite3
import zipfile

from fastapi.testclient import TestClient


def test_export_ledgers_zip_members(api_client: TestClient) -> None:
    r = api_client.post("/api/export/ledgers")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "ledgers_2026-06-11.zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
        assert {"transactions.csv", "dividends.csv", "fx_conversions.csv",
                "opening_inventory.csv", "fee_rules_snapshot.json",
                "manifest.json"} <= names
        tx = zf.read("transactions.csv")[3:].decode("utf-8")  # strip BOM
        assert tx.split("\r\n", 1)[0].startswith(
            "id,account_id,symbol,side,quantity,price,fees,tax,trade_date")
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["counts"]["transactions"] == 2
        assert manifest["counts"]["opening_inventory"] == 0
        assert manifest["as_of"] == "2026-06-11"
        fee = json.loads(zf.read("fee_rules_snapshot.json"))
        assert "tw" in fee and "schwab" in fee


def test_export_ledgers_writes_audit_row(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    api_client.post("/api/export/ledgers")
    row = golden_db.execute(
        "SELECT * FROM job_runs WHERE job_id = 'export:ledgers'"
    ).fetchone()
    assert row is not None and row["status"] == "ok"
