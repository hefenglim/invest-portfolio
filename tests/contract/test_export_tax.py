import io
import zipfile

from fastapi.testclient import TestClient


def test_export_tax_package(api_client: TestClient) -> None:
    r = api_client.post("/api/export/tax-package", json={"year": 2026})
    assert r.status_code == 200
    assert "tax_package_2026.zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert set(zf.namelist()) == {
            "realized_gains_2026.csv", "dividends_2026.csv",
            "fx_realized_2026.csv", "summary.md"}
        divs = zf.read("dividends_2026.csv")[3:].decode("utf-8")
        assert divs.split("\r\n", 1)[0] == \
            "date,account_id,symbol,type,gross,withholding,net,ccy"
        assert ",2330," in divs and ",5000," in divs and ",TWD" in divs
        realized_hdr = zf.read("realized_gains_2026.csv")[3:].decode("utf-8").split("\r\n", 1)[0]
        assert realized_hdr.startswith(
            "sell_date,account_id,symbol,quote_ccy,shares_sold,proceeds_net")
        assert "reporting_realized" in realized_hdr and "rate_used" in realized_hdr
        summary = zf.read("summary.md").decode("utf-8")
        assert "TWD" in summary


def test_export_tax_writes_audit_row(api_client: TestClient, golden_db) -> None:
    api_client.post("/api/export/tax-package", json={"year": 2026})
    row = golden_db.execute(
        "SELECT * FROM job_runs WHERE job_id = 'export:tax_package'").fetchone()
    assert row is not None and row["status"] == "ok"


def test_export_tax_bad_year_400(api_client: TestClient) -> None:
    # App-wide convention: the global RequestValidationError handler downgrades the
    # default 422 to 400 with error.code == "validation_error" (see api/errors.py); every
    # sibling export/ledger bad-input test asserts the same.
    r = api_client.post("/api/export/tax-package", json={"year": 1800})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"
