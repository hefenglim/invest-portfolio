"""The tax package and 換匯損益 must price the SAME reconversion identically (QA-02/R8).

``export/tax.py`` says so in its own comment — "Reading a different average here would make
the tax package disagree with 換匯損益 on the same reconversion" — and it stopped being true
the moment the dashboard's realized-FX rate was date-bounded (manual §8.2, 「回換前 avg_rate」)
while this call site kept the ALL-TIME average. Measured before the fix: **2,500 vs 10,000**
for one 2026-02-20 reconversion.

``tests/contract/test_export_tax.py`` cannot see this class of bug: every fixture there dates
its acquisition BEFORE its reconversion, so the bounded and unbounded averages coincide. The
discriminating ingredient — and the only thing that makes this file worth having — is an
acquisition dated **after** the reconversion it must not reprice.
"""

import csv
import io
import sqlite3
import zipfile
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_fx_conversion,
    list_cash_movements,
    list_fx_conversions,
)
from portfolio_dash.export.tax import build_tax_package_zip
from portfolio_dash.forex.fx_pnl import realized_fx_rows_as_of
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.ledger import FXConversion

_NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))

#: The February disposal could only have disposed of USD acquired in January at 32.00.
_AS_AT_FEBRUARY = Decimal("10000")  # 170000 - 5000 * 32
#: What the all-time average produced once March @ 35.00 existed: avg (670000/20000) = 33.5.
_RESTATED_BY_MARCH = Decimal("2500")  # 170000 - 5000 * 33.5


def _db_with_a_later_acquisition() -> sqlite3.Connection:
    """Schwab (home TWD / foreign USD): acquire, reconvert, then acquire again HIGHER.

    Ordering is the entire point. The March acquisition is entered after the February
    reconversion has already been reported, so any figure that moves is a figure the owner
    had already read.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    create_pricing_tables(conn)
    seed_accounts(conn)
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 10),
                         from_ccy=Currency.TWD, from_amount=Decimal("320000"),
                         to_ccy=Currency.USD, to_amount=Decimal("10000"))  # rate 32.00
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 2, 20),
                         from_ccy=Currency.USD, from_amount=Decimal("5000"),
                         to_ccy=Currency.TWD, to_amount=Decimal("170000"))
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 3, 15),
                         from_ccy=Currency.TWD, from_amount=Decimal("350000"),
                         to_ccy=Currency.USD, to_amount=Decimal("10000"))  # rate 35.00
    conn.commit()
    return conn


def _fx_rows_from_package(conn: sqlite3.Connection) -> list[dict[str, str]]:
    art = build_tax_package_zip(conn, now=_NOW, year=2026, reporting=Currency.TWD)
    with zipfile.ZipFile(io.BytesIO(art.content)) as zf:
        text = zf.read("fx_realized_2026.csv")[3:].decode("utf-8")  # strip the BOM
    rows = list(csv.DictReader(io.StringIO(text)))
    return rows


def test_the_tax_package_prices_a_reconversion_as_at_its_own_date() -> None:
    conn = _db_with_a_later_acquisition()
    rows = _fx_rows_from_package(conn)

    assert len(rows) == 1, "one reconversion in 2026"
    row = rows[0]
    assert row["date"] == "2026-02-20"
    assert Decimal(row["realized"]) == _AS_AT_FEBRUARY
    assert Decimal(row["rate_used"]) == Decimal("32")
    # The pre-fix figure, named so a regression is unmistakable rather than merely numeric.
    assert Decimal(row["realized"]) != _RESTATED_BY_MARCH


def test_the_tax_package_and_the_dashboard_report_the_SAME_row() -> None:
    """The load-bearing assertion: two surfaces, one answer, byte for byte.

    Compared as strings, not as Decimals — ``Decimal("32") == Decimal("32.00")`` is True but
    the two render differently in a CSV the owner files with, and this project's wire rule is
    about the canonical STRING.
    """
    conn = _db_with_a_later_acquisition()
    # Stored rows -> the domain model, exactly as export/tax.py:70 does, so this test
    # compares the two surfaces over the same types the app actually passes around.
    convs = [FXConversion(account_id=s.account_id, date=s.date, from_ccy=s.from_ccy,
                          from_amount=s.from_amount, to_ccy=s.to_ccy,
                          to_amount=s.to_amount)
             for s in list_fx_conversions(conn) if s.account_id == "schwab"]
    moves = [m for m in list_cash_movements(conn) if m.account_id == "schwab"]

    dashboard = realized_fx_rows_as_of(convs, moves, Currency.TWD, Currency.USD)
    package = _fx_rows_from_package(conn)

    assert len(dashboard) == len(package) == 1
    assert package[0]["rate_used"] == str(dashboard[0].rate_used)
    assert package[0]["realized"] == str(dashboard[0].realized)
    assert package[0]["foreign_sold"] == str(dashboard[0].foreign_sold)
    assert package[0]["home_received"] == str(dashboard[0].home_received)


def test_entering_the_later_acquisition_does_not_move_the_filed_figure() -> None:
    """The owner's real complaint, stated as a test: a filed number must not change.

    Builds the package with the March acquisition absent, files it, then adds March and
    rebuilds. Both packages must carry the identical February row.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    bootstrap_db(conn)
    create_pricing_tables(conn)
    seed_accounts(conn)
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 1, 10),
                         from_ccy=Currency.TWD, from_amount=Decimal("320000"),
                         to_ccy=Currency.USD, to_amount=Decimal("10000"))
    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 2, 20),
                         from_ccy=Currency.USD, from_amount=Decimal("5000"),
                         to_ccy=Currency.TWD, to_amount=Decimal("170000"))
    conn.commit()
    filed = _fx_rows_from_package(conn)

    insert_fx_conversion(conn, account_id="schwab", date=date(2026, 3, 15),
                         from_ccy=Currency.TWD, from_amount=Decimal("350000"),
                         to_ccy=Currency.USD, to_amount=Decimal("10000"))
    conn.commit()
    after = _fx_rows_from_package(conn)

    assert filed == after
