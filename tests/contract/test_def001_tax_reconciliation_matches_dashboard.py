"""DEF-001 (functional test manual I-03, 2026-09-23): the tax package's 對帳 line must BE the
dashboard's realized figure, and say where it parts from the filing line.

Measured on the demo ledger: ``summary.md``'s 「對帳用：績效基礎已實現」 printed TWD
36804.55597014925373134328360 while ``GET /api/dashboard`` returned
``returns.by_currency.TWD.realized`` = 42004.55597014925373134328360 — 5,200 apart, with no
sentence on either surface saying why. The 5,200 is a post-close cash dividend (2412, paid
2026-08-07 after the position was sold out): the replay books it as a ``kind="dividend"``
realized row (audit H2) so it reaches 總報酬, and ``export/tax.py`` filtered it out of BOTH
subtotals with one ``if r.kind not in ("sale", "short_cover"): continue``. For the filing
column that filter is right (the dividend is declared once, on the dividends sheet); for the
reconciliation column it is the defect — a line whose only job is "match the dashboard"
applied the filing definition instead of the dashboard's.

Pinned here, all as STRINGS (``Decimal("1") == Decimal("1.0")`` would hide a rendering drift):

* the 對帳 line == the dashboard's wire figure, byte for byte;
* the post-close dividend is named on the line beneath it (amount + symbol + date);
* the FILING line is unchanged — it still subtotals ``realized_original`` of sales only;
* a ledger with realized rows in ANOTHER year also reconciles: the dashboard figure is
  cumulative, the package is year-cut, and the gap is printed rather than left to guess.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import zipfile
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory

D = Decimal


def _tw(conn: sqlite3.Connection, symbol: str) -> None:
    upsert_instrument(conn, Instrument(symbol=symbol, market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Telecom",
                                       name=symbol, board="TWSE"))


def _buy(conn: sqlite3.Connection, symbol: str, qty: str, price: str, on: date) -> None:
    insert_transaction(conn, account_id="tw_broker", symbol=symbol, side=Side.BUY,
                       quantity=D(qty), price=D(price), fees=D("0"), tax=D("0"),
                       trade_date=on)


def _sell(conn: sqlite3.Connection, symbol: str, qty: str, price: str, on: date) -> None:
    insert_transaction(conn, account_id="tw_broker", symbol=symbol, side=Side.SELL,
                       quantity=D(qty), price=D(price), fees=D("0"), tax=D("0"),
                       trade_date=on)


def _cash_div(conn: sqlite3.Connection, symbol: str, net: str, on: date) -> None:
    insert_dividend(conn, account_id="tw_broker", symbol=symbol, div_date=on,
                    div_type="CASH", gross=D(net), withholding=D("0"), net=D(net))


def _seed_2026(conn: sqlite3.Connection) -> None:
    """Two TW positions, both realized in 2026, one of them paid AFTER it was sold out.

    2330 is priced so the weighted average repeats (704 / 7), which gives the realized
    figure the long 28-digit tail the owner's real ledger shows — a short round number
    would not exercise the string-for-string comparison at all.
    """
    seed_accounts(conn)
    _tw(conn, "2330")
    _tw(conn, "2412")
    _buy(conn, "2330", "3", "100", date(2026, 1, 5))
    _buy(conn, "2330", "4", "101", date(2026, 1, 6))
    _cash_div(conn, "2330", "7", date(2026, 3, 1))          # folds into adjusted cost
    _sell(conn, "2330", "5", "110", date(2026, 5, 20))
    _buy(conn, "2412", "1000", "120", date(2026, 2, 1))
    _sell(conn, "2412", "1000", "125", date(2026, 7, 1))
    _cash_div(conn, "2412", "5200.0", date(2026, 8, 7))     # ★ post-close dividend


def _seed_across_years(conn: sqlite3.Connection) -> None:
    _seed_2026(conn)
    _tw(conn, "1101")
    _buy(conn, "1101", "10", "40", date(2025, 3, 1))
    _sell(conn, "1101", "10", "43.3", date(2025, 11, 3))    # realized in 2025, not 2026


def _package(client: TestClient, year: int = 2026) -> zipfile.ZipFile:
    r = client.post("/api/export/tax-package", json={"year": year})
    assert r.status_code == 200, r.text
    return zipfile.ZipFile(io.BytesIO(r.content))


def _dashboard_realized(client: TestClient, ccy: str) -> str:
    r = client.get("/api/dashboard")
    assert r.status_code == 200, r.text
    return str(r.json()["returns"]["by_currency"][ccy]["realized"])


def _section(summary: str, heading: str) -> str:
    """The text of one ``## `` section, heading line excluded."""
    body = summary.split(heading, 1)[1]
    return body.split("\n## ", 1)[0]


def _line_value(section: str, ccy: str) -> str:
    """The value on the FIRST ``- CCY: value`` line of *section* (the section's headline)."""
    for line in section.splitlines():
        if line.startswith(f"- {ccy}: "):
            return line[len(f"- {ccy}: "):].split("（", 1)[0]
    raise AssertionError(f"no '- {ccy}:' line in:\n{section}")


_RECON = "## 對帳用：績效基礎已實現"
_FILING = "## Realized gains — 申報用"


def test_the_reconciliation_line_is_the_dashboard_figure_byte_for_byte(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_2026)
    with _package(client) as zf:
        summary = zf.read("summary.md").decode("utf-8")
    dashboard = _dashboard_realized(client, "TWD")
    assert _line_value(_section(summary, _RECON), "TWD") == dashboard, summary


def test_the_post_close_dividend_is_named_under_the_reconciliation_line(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_2026)
    with _package(client) as zf:
        summary = zf.read("summary.md").decode("utf-8")
    recon = _section(summary, _RECON)
    assert "結清後配息" in recon and "不列入申報" in recon, recon
    # Amount, symbol and payment date: enough to find the row on the dividends sheet.
    assert "- TWD: 5200.0（2412 2026-08-07）" in recon, recon


def test_the_filing_line_is_untouched_by_the_fix(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The ruling's constraint: the FILING subtotal must not move. It is Σ realized_original
    over SALES only — the post-close dividend is income, declared on the dividends sheet."""
    client = dashboard_client_factory(_seed_2026)
    with _package(client) as zf:
        summary = zf.read("summary.md").decode("utf-8")
        text = zf.read("realized_gains_2026.csv")[3:].decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert {r["symbol"] for r in rows} == {"2330", "2412"}   # no dividend row on this sheet
    filed = sum((D(r["realized_original"]) for r in rows), D("0"))
    assert _line_value(_section(summary, _FILING), "TWD") == str(filed)
    # And it is a DIFFERENT number from the reconciliation line — that difference is the
    # whole point of printing both.
    assert str(filed) != _line_value(_section(summary, _RECON), "TWD")


def test_a_ledger_spanning_years_still_reconciles_and_says_how(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The dashboard's realized is CUMULATIVE; the package is year-cut. With a 2025 sale in
    the ledger the 2026 line cannot equal the dashboard, so the gap is printed and the
    cumulative total is printed beside it — and THAT one equals the dashboard exactly."""
    client = dashboard_client_factory(_seed_across_years)
    with _package(client) as zf:
        summary = zf.read("summary.md").decode("utf-8")
    recon = _section(summary, _RECON)
    dashboard = _dashboard_realized(client, "TWD")
    assert _line_value(recon, "TWD") != dashboard       # the year slice alone cannot match
    assert "本年度以外" in recon, recon
    assert f"- TWD: {dashboard}（= 儀表板" in recon, recon


def test_a_single_year_ledger_prints_no_cross_year_lines(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_2026)
    with _package(client) as zf:
        summary = zf.read("summary.md").decode("utf-8")
    assert "本年度以外" not in summary

