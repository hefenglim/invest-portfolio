import csv
import io
import sqlite3
import zipfile
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_fx_conversion,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.export.tax import build_tax_package_zip
from portfolio_dash.portfolio.cost_basis import UnbookableLedgerError
from portfolio_dash.pricing.results import FxRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_fx
from portfolio_dash.shared.corporate_actions import CorporateActionKind
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


# --- Unapplied corporate actions: the tax package REFUSES rather than being quietly wrong.


def _add_unapplied_action(conn: sqlite3.Connection) -> None:
    """A SPLIT dated BEFORE the position it targets was ever opened (E1).

    Realistic rather than exotic: the golden 2330 buy is 2026-01-05, and a back-dated or
    mistyped action row is precisely the kind of ledger slip corporate-action entry invites.
    The replay cannot apply it, so every later share count is in the wrong denomination.
    """
    insert_corporate_action(conn, account_id="tw_broker", action_date=date(2025, 6, 1),
                            kind=CorporateActionKind.SPLIT, from_symbol="2330",
                            to_symbol="2330", ratio_to=Decimal("3"),
                            ratio_from=Decimal("1"))
    conn.commit()


def test_tax_package_refuses_a_ledger_with_an_unapplied_corporate_action(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The tax package is the one consumer where being QUIETLY WRONG is worse than failing.

    ``build_tax_package_zip`` calls ``build_book`` on the STRICT path (no ``allow_oversell``),
    so an action the replay cannot apply raises ``UnbookableLedgerError`` instead of
    producing ``Book.unapplied_actions`` — and the realized-gains sheet is never written from
    share counts in the wrong denomination. This test pins that posture end to end: 422 with
    the zh reason, not a 200 with a plausible-looking CSV, and not a 500.

    (The dashboard makes the opposite trade deliberately: it degrades and flags, because a
    blank dashboard helps nobody. A tax filing is not a dashboard.)
    """
    ok = api_client.post("/api/export/tax-package", json={"year": 2026})
    assert ok.status_code == 200, "precondition: the golden ledger exports cleanly"

    _add_unapplied_action(golden_db)

    r = api_client.post("/api/export/tax-package", json={"year": 2026})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "unbookable_ledger"
    assert "2330" in err["message"] and "2025-06-01" in err["message"]


def test_the_strict_export_path_raises_rather_than_recording_unapplied_actions(
    golden_db: sqlite3.Connection,
) -> None:
    """The property the consumer audit relies on: on ``allow_oversell=False`` a refusal is an
    EXCEPTION, never a quietly-populated ``Book.unapplied_actions``. So ``export/tax.py`` (and
    ``strategy/whatif.py``, and 重算) need no new check — the book they receive is either
    fully applied or does not exist."""
    _add_unapplied_action(golden_db)
    with pytest.raises(UnbookableLedgerError):
        build_tax_package_zip(golden_db, now=_NOW, year=2026, reporting=Currency.TWD)
