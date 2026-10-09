"""M8-01: the 持倉 CSV must serialize Decimals through the ONE canonical wire form.

``shared/wire.py::decimal_str`` is documented as fixed-point, full source precision,
**NEVER scientific notation** — and ``GET /api/dashboard`` emits every holding Decimal
through it. The export is a reconciliation-grade artifact: a user diffs its cells against
the API/screen as STRINGS, so a cell that says ``0E-24`` where the API says
``0.000000000000000000000000`` breaks the comparison even though the two Decimals are
equal.

The assertions below are parametrised over **every Decimal column of the row**, not the
one column the defect was reported in (``dividend_portion``): ``str(Decimal)`` switches to
exponent form purely on the value's exponent, so pinning a single column would let the next
value whose exponent lands outside the observed range slip through unnoticed.
"""

import csv
import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory

# Every Decimal-valued column of the holdings CSV. `reporting_ccy_value` is the one the
# builder derives itself (market value converted into the reporting currency); the other
# twelve are HoldingRow fields the dashboard payload carries under the SAME key.
_DECIMAL_COLUMNS = (
    "shares", "original_avg", "adjusted_avg", "original_cost_total",
    "adjusted_cost_total", "market_price", "market_value", "unrealized_pnl",
    "capital_gain", "dividend_portion", "payback_ratio", "weight",
    "reporting_ccy_value",
)
_API_SHARED_COLUMNS = tuple(c for c in _DECIMAL_COLUMNS if c != "reporting_ccy_value")


def _csv_rows(client: TestClient) -> dict[tuple[str, str], dict[str, str]]:
    """POST the holdings export and index its data rows by (account_id, symbol)."""
    r = client.post("/api/export/holdings")
    assert r.status_code == 200
    text = r.content[3:].decode("utf-8")  # strip the UTF-8 BOM
    lines = [ln for ln in text.split("\r\n") if ln and not ln.startswith("#")]
    reader = csv.DictReader(lines)
    return {(row["account_id"], row["symbol"]): row for row in reader}


def _api_rows(client: TestClient) -> dict[tuple[str, str], dict[str, object]]:
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    return {(h["account_id"], h["symbol"]): h for h in r.json()["holdings"]}


def _assert_wire_form(
    csv_rows: dict[tuple[str, str], dict[str, str]],
    api_rows: dict[tuple[str, str], dict[str, object]],
) -> None:
    assert csv_rows and set(csv_rows) == set(api_rows)
    for key, row in csv_rows.items():
        for col in _DECIMAL_COLUMNS:
            cell = row[col]
            assert "E" not in cell and "e" not in cell, (
                f"{key} {col}: scientific notation {cell!r} in a Decimal cell — "
                "decimal_str() is fixed-point, never exponent form"
            )
        for col in _API_SHARED_COLUMNS:
            expected = api_rows[key][col]
            assert row[col] == ("" if expected is None else expected), (
                f"{key} {col}: CSV {row[col]!r} != /api/dashboard {expected!r}"
            )


def _seed_repeating_average(conn: sqlite3.Connection) -> None:
    """A holding whose weighted average is a NON-TERMINATING division.

    3 shares bought all-in for 301 USD (buy-side fees are part of cost basis) →
    ``average = 301 / 3`` = 100.333…, carried to the Decimal context's 28 significant
    digits. Selling one share removes that repeating average from BOTH the original and
    the adjusted total, so with no dividend in play ``original_total - adjusted_total``
    is an exact zero carrying the *inherited exponent* — ``Decimal("0E-25")`` — which is
    precisely the shape (``0E-24`` / ``0E-7`` on the QA fixture) that ``str()`` renders in
    exponent form and ``decimal_str()`` does not.
    """
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                                       sector="Tech", name="Apple"))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=Decimal("3"), price=Decimal("100"),
                       fees=Decimal("1"), tax=Decimal("0"), trade_date=date(2026, 1, 10))
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.SELL,
                       quantity=Decimal("1"), price=Decimal("110"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 2, 10))
    upsert_prices(conn, [
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=Decimal("120"), source="test"),
    ], fetched_at=GOLDEN_NOW)
    upsert_fx(conn, [
        FxRow(base=Currency.USD, quote=Currency.TWD, as_of=date(2026, 6, 9),
              rate=Decimal("33"), source="test"),
    ], fetched_at=GOLDEN_NOW)


def test_holdings_csv_decimal_cells_are_the_canonical_wire_form(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """M8-01 regression: a deep-exponent value must reach the CSV fixed-point, and as the
    byte-identical string ``/api/dashboard`` serves for the same field."""
    client = dashboard_client_factory(_seed_repeating_average)
    api_rows = _api_rows(client)

    # Guard the fixture itself: if the replay ever stops producing an exponent-form value
    # here, this test would keep passing while testing nothing. Fail loudly instead.
    assert any(
        "E" in str(Decimal(str(row[col])))
        for row in api_rows.values()
        for col in _API_SHARED_COLUMNS
        if row[col] is not None
    ), "fixture no longer produces a value whose str() is scientific notation"

    _assert_wire_form(_csv_rows(client), api_rows)


def test_holdings_csv_decimal_cells_match_dashboard_on_the_golden_set(
    api_client: TestClient,
) -> None:
    """The same contract over the golden ledger — the cells that were already correct stay
    byte-identical to the API (the fix changes exponent-form values ONLY)."""
    _assert_wire_form(_csv_rows(api_client), _api_rows(api_client))
