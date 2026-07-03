import csv
import io
import sqlite3
import zipfile
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_fx_conversion,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.export.tax import build_tax_package_zip
from portfolio_dash.pricing.results import FxRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_fx
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side

_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


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


def test_export_tax_no_job_runs_audit(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """2026-07-03: exports audit via 系統操作記錄, no job_runs rows (user decision)."""
    api_client.post("/api/export/tax-package", json={"year": 2026})
    row = golden_db.execute(
        "SELECT * FROM job_runs WHERE job_id = 'export:tax_package'").fetchone()
    assert row is None


def test_export_tax_bad_year_400(api_client: TestClient) -> None:
    # App-wide convention: the global RequestValidationError handler downgrades the
    # default 422 to 400 with error.code == "validation_error" (see api/errors.py); every
    # sibling export/ledger bad-input test asserts the same.
    r = api_client.post("/api/export/tax-package", json={"year": 1800})
    assert r.status_code == 400 and r.json()["error"]["code"] == "validation_error"


# --- Money-path coverage: the golden DB has no sells/reconversions, so the realized-gains
# reporting-conversion and FX-realized paths are exercised here via a purpose-built DB. ---


def _db_with_sells(*, with_rate: bool) -> sqlite3.Connection:
    """In-memory DB seeded (real write paths) with a US sell and an FX reconversion.

    Schwab: settlement USD / funding TWD (FX-exposed). AAPL BUY 10@100 then SELL 4@130
    in 2026 -> realized 120 USD. With ``with_rate`` a USD/TWD rate is stored on the sell
    date so the reporting conversion + rate_used populate; without it both blank.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    create_pricing_tables(conn)
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("10"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.SELL,
                       quantity=Decimal("4"), price=Decimal("130"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 5, 20))
    # FX acquisition (TWD->USD, avg 32) + reconversion (USD->TWD): realized FX = 17000 - 500*32.
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 8),
                         from_ccy=Currency.TWD, from_amount=Decimal("32000"),
                         to_ccy=Currency.USD, to_amount=Decimal("1000"))
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 6, 1),
                         from_ccy=Currency.USD, from_amount=Decimal("500"),
                         to_ccy=Currency.TWD, to_amount=Decimal("17000"))
    if with_rate:
        upsert_fx(conn, [FxRow(base=Currency.USD, quote=Currency.TWD,
                               as_of=date(2026, 5, 20), rate=Decimal("33"), source="test")],
                  fetched_at=_NOW)
    conn.commit()
    return conn


def _data_row(zf: zipfile.ZipFile, name: str) -> dict[str, str]:
    rows = list(csv.reader(io.StringIO(zf.read(name)[3:].decode("utf-8"))))
    header, data = rows[0], rows[1]
    return dict(zip(header, data, strict=True))


def test_tax_realized_reporting_conversion_and_fx() -> None:
    conn = _db_with_sells(with_rate=True)
    art = build_tax_package_zip(conn, now=_NOW, year=2026, reporting=Currency.TWD)
    with zipfile.ZipFile(io.BytesIO(art.content)) as zf:
        gain = _data_row(zf, "realized_gains_2026.csv")
        assert gain["symbol"] == "AAPL" and gain["quote_ccy"] == "USD"
        assert Decimal(gain["realized"]) == Decimal("120")
        assert Decimal(gain["rate_used"]) == Decimal("33")
        # reporting_realized = native realized * trade-date rate (no fabrication).
        assert Decimal(gain["reporting_realized"]) == Decimal("120") * Decimal("33")
        fx = _data_row(zf, "fx_realized_2026.csv")
        assert fx["home_ccy"] == "TWD" and fx["foreign_ccy"] == "USD"
        assert Decimal(fx["rate_used"]) == Decimal("32")
        assert Decimal(fx["realized"]) == Decimal("1000")
        summary = zf.read("summary.md").decode("utf-8")
        assert "- USD:" in summary   # realized-gains subtotal, per-currency
        assert "- TWD:" in summary   # realized-FX subtotal, per-currency (never summed)
    conn.close()


def test_tax_realized_blank_when_no_trade_date_rate() -> None:
    conn = _db_with_sells(with_rate=False)
    art = build_tax_package_zip(conn, now=_NOW, year=2026, reporting=Currency.TWD)
    with zipfile.ZipFile(io.BytesIO(art.content)) as zf:
        gain = _data_row(zf, "realized_gains_2026.csv")
        # Native realized still computed; reporting columns blank, never fabricated.
        assert Decimal(gain["realized"]) == Decimal("120")
        assert gain["rate_used"] == ""
        assert gain["reporting_realized"] == ""
    conn.close()
