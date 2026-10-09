"""QA-06: the tax package must DEGRADE on an oversold ledger, not 500 on it.

``OversellError`` subclasses ``Exception``, not ``ValueError`` (``portfolio/cost_basis.py``
says so in its own docstring), so ``export_tax_package``'s single ``except
UnbookableLedgerError`` — a ``ValueError`` subclass — never caught it. One acknowledged
undeclared oversell therefore took the tax package down with an internal error, while
``/api/dashboard``, ``/api/export/realized``, ``/api/export/holdings`` and
``/api/export/holdings-report`` all answered 200 on the very same ledger. 賣超 is a state
this app is explicitly designed to survive (``domain-ledger.md``: the basis is discarded,
no realized row is emitted, the position is flagged 待釐清) — surviving it everywhere but
here is the asymmetry, and a 500 tells the owner nothing about which row to fix.

``build_tax_package_zip`` stays STRICT on purpose (``allow_oversell`` defaults False): a tax
package must not silently omit a sale. The fix is the one ``strategy/whatif.py`` already
made at the identical seam — "degrade with the reason, not relax the strictness" — so the
wording and the ``issues`` detail shape are deliberately the drawer's, naming the offending
(account, symbol, date) rather than making the owner regex a sentence.
"""

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory

_CJK = "帳本中有賣超部位待釐清"

# The endpoints that already survive 賣超; the asymmetry is the finding, so they are asserted
# alongside the fix rather than trusted.
_SURVIVING = ("/api/export/realized", "/api/export/holdings", "/api/export/holdings-report")


def _instrument(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    upsert_prices(conn, [PriceRow(instrument="2330", market=Market.TW, as_of=date(2026, 6, 9),
                                  close=Decimal("450"), source="test")], fetched_at=GOLDEN_NOW)


def _seed_oversold(conn: sqlite3.Connection) -> None:
    """Buy 100 @ 500, then sell 300 @ 600 — an UNDECLARED oversell (``short_sale`` false)."""
    seed_accounts(conn)
    _instrument(conn)
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("500"),
                       fees=Decimal("71"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.SELL,
                       quantity=Decimal("300"), price=Decimal("600"),
                       fees=Decimal("256"), tax=Decimal("540"), trade_date=date(2026, 2, 5))
    conn.commit()


def _seed_clean(conn: sqlite3.Connection) -> None:
    """The control: 1,000 bought, 500 sold — a covered sale, nothing to refuse."""
    seed_accounts(conn)
    _instrument(conn)
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=Decimal("1000"), price=Decimal("500"),
                       fees=Decimal("712"), tax=Decimal("0"), trade_date=date(2026, 1, 5))
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.SELL,
                       quantity=Decimal("500"), price=Decimal("600"),
                       fees=Decimal("427"), tax=Decimal("900"), trade_date=date(2026, 2, 5))
    conn.commit()


def test_tax_package_on_an_oversold_ledger_is_a_422_naming_the_row(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_oversold)
    response = client.post("/api/export/tax-package", json={"year": 2026})
    assert response.status_code == 422, response.text

    err: dict[str, Any] = response.json()["error"]
    assert err["code"] == "oversold_position"
    assert _CJK in err["message"], err["message"]
    # The offending row is named, so the message is actionable without a ledger hunt.
    assert "tw_broker" in err["message"] and "2330" in err["message"]
    assert "2026-02-05" in err["message"]

    issue: dict[str, Any] = err["issues"][0]
    assert issue["sev"] == "error"
    assert issue["code"] == "oversold_position"
    assert issue["account_id"] == "tw_broker"
    assert issue["symbol"] == "2330"
    assert issue["trade_date"] == "2026-02-05"
    assert issue["field"] is None
    assert issue["text"]


def test_the_same_ledger_still_serves_every_endpoint_that_survives_oversell(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The asymmetry that made QA-06 a defect rather than a design choice."""
    client = dashboard_client_factory(_seed_oversold)
    assert client.get("/api/dashboard").status_code == 200
    for path in _SURVIVING:
        assert client.post(path, json={}).status_code == 200, path


def test_a_clean_ledger_still_gets_its_zip(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed_clean)
    response = client.post("/api/export/tax-package", json={"year": 2026})
    assert response.status_code == 200, response.text
    assert response.content[:2] == b"PK", "not a zip"
    assert "attachment" in response.headers["content-disposition"]
