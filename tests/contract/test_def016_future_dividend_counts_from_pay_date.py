"""DEF-016 (functional test manual C-02, owner ruling 2026-09-24): a dividend counts from its
PAY date — on every surface that replays the dividend ledger.

Measured on the demo ledger (R1): confirming 2330's 2026-10-08 payout (ex 2026-09-16) on
2026-09-23 moved ``kpis.xirr`` 10.224 → 10.234 — a cash inflow dated AFTER the XIRR's own
terminal value — and lowered the holding's adjusted cost, while ``/api/cash`` and the trend's
last ``net_invested`` (both already cut at the valuation day) did not move. One payment, two
answers. The ruling: 總報酬, the cash pool and XIRR all follow actual receipt.

Pinned here, BEHAVIOURALLY (through the API, never by reading source):

* before the pay date the ledger with the confirmed-but-unpaid row answers EXACTLY what the
  ledger without it answers, on every replaying surface — the dashboard (XIRR, 總報酬, the
  holding's adjusted cost, the received-dividend summary), the drawer, 試算, the input page's
  sell hint and the tax package;
* from the pay date on, the same row DOES count (so the cut is a date, not a deletion);
* a future DRIP adds no shares yet, and the drawer's share reconciliation still balances.
"""

from __future__ import annotations

import io
import sqlite3
import zipfile
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_dividend
from tests.conftest import GOLDEN_NOW, DashboardClientFactory, _seed_golden

D = Decimal
_PAY = date(2026, 7, 1)                      # after GOLDEN_NOW (2026-06-11)
_AFTER_PAY = datetime(2026, 7, 2, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))


def _with_future_cash_dividend(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=_PAY,
                    div_type="CASH", gross=D("18200"), withholding=D("0"), net=D("18200"),
                    ex_date=date(2026, 6, 5))


def _with_future_drip(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    insert_dividend(conn, account_id="schwab", symbol="AAPL", div_date=_PAY,
                    div_type="DRIP", gross=D("10"), withholding=D("3"), net=D("7"),
                    reinvest_shares=D("0.05"), reinvest_price=D("140"))


def _holding(dash: dict[str, Any], account: str, symbol: str) -> dict[str, Any]:
    return next(h for h in dash["holdings"]
                if h["account_id"] == account and h["symbol"] == symbol)


def _surfaces(client: TestClient) -> dict[str, Any]:
    """Every figure a replay of the dividend ledger feeds, read off the wire."""
    dash = client.get("/api/dashboard").json()
    h = _holding(dash, "tw_broker", "2330")
    drawer = client.get("/api/symbol/2330/detail").json()
    whatif = client.post("/api/whatif", json={
        "symbol": "2330", "side": "sell", "shares": "100", "price": "600",
        "account_id": "tw_broker"}).json()
    held = client.get("/api/input/holdings", params={"account": "tw_broker"}).json()
    hint = next(x for x in held["held"] if x["symbol"] == "2330")
    cash = client.get("/api/cash", params={"limit": 500}).json()
    return {
        "xirr": dash["kpis"]["xirr"],
        "total_return": dash["kpis"]["total_return"],
        "realized_total": dash["kpis"]["realized_total"],
        "unrealized_total": dash["kpis"]["unrealized_total"],
        "returns_twd": dash["returns"]["by_currency"]["TWD"],
        "adjusted_cost_total": h["adjusted_cost_total"],
        "adjusted_avg": h["adjusted_avg"],
        "dividend_portion": h["dividend_portion"],
        "dividends_received": dash["dividends"],
        "principal_fx_effect": dash["kpis"]["principal_fx_effect"],
        "drawer_adjusted_avg": drawer["cost_basis"]["adjusted_avg"],
        "whatif_old_adjusted_avg": whatif["old_adjusted_avg"],
        "whatif_realized": whatif["realized"],
        "sell_hint_adjusted_avg": hint["adjusted_avg"],
        "cash": cash["balances"],
    }


def _tax_dividend_lines(client: TestClient) -> list[str]:
    r = client.post("/api/export/tax-package", json={"year": 2026})
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        name = next(n for n in zf.namelist() if n.startswith("dividends"))
        text = zf.read(name).decode("utf-8-sig")
    return [ln for ln in text.splitlines()[1:] if ln.strip()]


def test_a_dividend_not_yet_paid_changes_nothing_on_any_replaying_surface(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    baseline = dashboard_client_factory(_seed_golden)
    pending = dashboard_client_factory(_with_future_cash_dividend)
    before, after = _surfaces(baseline), _surfaces(pending)
    differing = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    assert not differing, (
        "a dividend whose pay date is after the valuation day moved these figures "
        f"(baseline, with-unpaid-row): {differing}")
    assert _tax_dividend_lines(pending) == _tax_dividend_lines(baseline)


def test_from_its_pay_date_the_same_dividend_does_count(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The cut is a DATE, not a deletion: once the clock passes the pay date the row enters
    the adjusted cost (−18,200), the received summary and the XIRR flows."""
    baseline = dashboard_client_factory(_seed_golden, now=_AFTER_PAY)
    paid = dashboard_client_factory(_with_future_cash_dividend, now=_AFTER_PAY)
    b = _holding(baseline.get("/api/dashboard").json(), "tw_broker", "2330")
    dash = paid.get("/api/dashboard").json()
    p = _holding(dash, "tw_broker", "2330")
    assert D(b["adjusted_cost_total"]) - D(p["adjusted_cost_total"]) == D("18200")
    assert D(dash["dividends"]["total_by_currency"]["TWD"]) == D("5000") + D("18200")
    assert dash["kpis"]["xirr"] != baseline.get("/api/dashboard").json()["kpis"]["xirr"]
    assert len(_tax_dividend_lines(paid)) == len(_tax_dividend_lines(baseline)) + 1


def test_the_valuation_day_itself_is_the_first_day_it_counts(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """``<=`` and not ``<``: paid on the valuation day means received."""
    on_pay = datetime(2026, 7, 1, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    baseline = dashboard_client_factory(_seed_golden, now=on_pay)
    paid = dashboard_client_factory(_with_future_cash_dividend, now=on_pay)
    b = _holding(baseline.get("/api/dashboard").json(), "tw_broker", "2330")
    p = _holding(paid.get("/api/dashboard").json(), "tw_broker", "2330")
    assert D(b["adjusted_cost_total"]) - D(p["adjusted_cost_total"]) == D("18200")


def test_a_future_drip_adds_no_shares_yet_and_the_drawer_still_reconciles(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    baseline = dashboard_client_factory(_seed_golden)
    pending = dashboard_client_factory(_with_future_drip)
    b = _holding(baseline.get("/api/dashboard").json(), "schwab", "AAPL")
    p = _holding(pending.get("/api/dashboard").json(), "schwab", "AAPL")
    assert p["shares"] == b["shares"] == "10"
    detail = pending.get("/api/symbol/AAPL/detail").json()
    recon = detail["activity_reconcile"]["total"]
    assert recon["balances"] is True, recon
    assert recon["reinvest_shares"] == "0", recon
    assert GOLDEN_NOW.date() < _PAY
