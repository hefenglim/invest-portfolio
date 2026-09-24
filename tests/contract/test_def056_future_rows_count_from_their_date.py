"""DEF-056 (functional test manual B-17, owner ruling 2026-09-24, option A): every ledger row
counts from its OWN date — trades, openings, FX conversions (and, for the same reason, cash
movements and corporate actions) exactly like dividends since DEF-016.

Measured before the fix on the golden subset (valuation day 2026-06-11): one BUY of 2330
entered for 2026-07-01 put 2,000 shares into today's holding and moved 總報酬, the adjusted
cost and XIRR at once, while ``/api/cash`` (already cut at the valuation day, M5-06) did not
move — one trade, two answers. The ruling: nothing counts before its own date.

Pinned here BEHAVIOURALLY, through the API (never by reading source):

* before its date a future row leaves EVERY valuation surface byte-identical to the ledger
  without it — the WHOLE ``/api/dashboard`` payload (kpis, holdings, realized, allocation,
  returns, fx, trend …), the ``/api/cash`` balances, the drawer's position / 交易明細 /
  footer, 試算, the sell hints and the tax package;
* from its date on, the same row DOES count (the cut is a date, not a deletion);
* the ledger lists flag the row (``counts_from``) while — and only while — it is ahead, on
  the SERVER's clock.
"""

from __future__ import annotations

import io
import sqlite3
import zipfile
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import (
    insert_cash_movement,
    insert_corporate_action,
    insert_dividend,
    insert_fx_conversion,
    insert_transaction,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory, _seed_golden

D = Decimal
_TPE = ZoneInfo("Asia/Taipei")
_FUTURE = date(2026, 7, 1)                              # after GOLDEN_NOW (2026-06-11)
_AFTER = datetime(2026, 7, 2, 14, 30, tzinfo=_TPE)
_ON_THE_DAY = datetime(2026, 7, 1, 9, 0, tzinfo=_TPE)

Seed = Callable[[sqlite3.Connection], None]


def _future_buy(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                       quantity=D("1000"), price=D("600"), fees=D("855"), tax=D("0"),
                       trade_date=_FUTURE)


def _future_sell(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.SELL,
                       quantity=D("100"), price=D("650"), fees=D("92"), tax=D("195"),
                       trade_date=_FUTURE)


def _future_opening(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    upsert_instrument(conn, Instrument(symbol="2317", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Electronics",
                                       name="Hon Hai", board="TWSE"))
    upsert_prices(conn, [PriceRow(instrument="2317", market=Market.TW, as_of=date(2026, 6, 9),
                                  close=D("150"), source="test")], fetched_at=GOLDEN_NOW)
    upsert_opening(conn, account_id="tw_broker", symbol="2317", shares=D("1000"),
                   original_cost_total=D("120000"), build_date=_FUTURE)


def _future_fx(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    insert_fx_conversion(conn, account_id="schwab", date=_FUTURE, from_ccy=Currency.TWD,
                         from_amount=D("33500"), to_ccy=Currency.USD, to_amount=D("1000"))


def _future_reconversion(conn: sqlite3.Connection) -> None:
    """USD back to TWD next week: a DISPOSAL of the pool — realized FX once it happens."""
    _seed_golden(conn)
    insert_fx_conversion(conn, account_id="schwab", date=_FUTURE, from_ccy=Currency.USD,
                         from_amount=D("500"), to_ccy=Currency.TWD, to_amount=D("16750"))


def _future_split(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    insert_corporate_action(conn, account_id="schwab", action_date=_FUTURE,
                            kind=CorporateActionKind.SPLIT, from_symbol="AAPL",
                            to_symbol="AAPL", ratio_to=D("2"), ratio_from=D("1"))


def _future_fee(conn: sqlite3.Connection) -> None:
    _seed_golden(conn)
    insert_cash_movement(conn, account_id="schwab", move_date=_FUTURE, kind="BROKER_FEE",
                         ccy=Currency.USD, amount=D("25"))


def _future_drip(conn: sqlite3.Connection) -> None:
    """DEF-016's own case, re-pinned on EVERY surface: its guard read the sell hint's average
    but not its share count, and ``/input/holdings`` took ``shares`` from a SECOND share path
    (``current_shares``, every date) — measured on 4655845: 可賣股數 10.05 beside a dashboard
    holding of 10, for a DRIP paying next month."""
    _seed_golden(conn)
    insert_dividend(conn, account_id="schwab", symbol="AAPL", div_date=_FUTURE,
                    div_type="DRIP", gross=D("10"), withholding=D("3"), net=D("7"),
                    reinvest_shares=D("0.05"), reinvest_price=D("140"))


_FUTURE_ROWS: dict[str, Seed] = {
    "drip": _future_drip,
    "buy": _future_buy, "sell": _future_sell, "opening": _future_opening,
    "fx": _future_fx, "fx_back": _future_reconversion, "split": _future_split,
    "cash_fee": _future_fee,
}


def _tax_package(client: TestClient) -> dict[str, str]:
    r = client.post("/api/export/tax-package", json={"year": 2026})
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        return {n: zf.read(n).decode("utf-8-sig") for n in sorted(zf.namelist())}


def _drawer(client: TestClient, symbol: str) -> dict[str, Any]:
    detail = client.get(f"/api/symbol/{symbol}/detail").json()
    # `trade_events` / `dividend_events` are the ledger's markers — they list every row by
    # design, like the ledger page does. Everything that describes the POSITION is compared.
    return {k: detail[k] for k in ("cost_basis", "position", "position_accounts",
                                   "activity", "activity_reconcile", "realized_rows")}


def _valuations(client: TestClient) -> dict[str, Any]:
    """Every surface that values the portfolio "as at a day", read off the wire."""
    cash = client.get("/api/cash", params={"limit": 500}).json()
    whatif = client.post("/api/whatif", json={
        "symbol": "2330", "side": "sell", "shares": "100", "price": "600",
        "account_id": "tw_broker"}).json()
    return {
        "dashboard": client.get("/api/dashboard").json(),
        "cash.balances": cash["balances"],
        "cash.reporting_total": cash["reporting_total"],
        "cash.negative_pools": cash["negative_pools"],
        "drawer.2330": _drawer(client, "2330"),
        "drawer.AAPL": _drawer(client, "AAPL"),
        "whatif": whatif,
        "sell_hints.tw_broker": client.get(
            "/api/input/holdings", params={"account": "tw_broker"}).json(),
        "sell_hints.schwab": client.get(
            "/api/input/holdings", params={"account": "schwab"}).json(),
        "tax_package": _tax_package(client),
    }


@pytest.mark.parametrize("kind", sorted(_FUTURE_ROWS))
def test_a_row_dated_after_today_changes_no_valuation_surface(
    kind: str, dashboard_client_factory: DashboardClientFactory,
) -> None:
    before = _valuations(dashboard_client_factory(_seed_golden))
    after = _valuations(dashboard_client_factory(_FUTURE_ROWS[kind]))
    differing = sorted(k for k in before if before[k] != after[k])
    assert not differing, (
        f"a {kind} dated {_FUTURE} (after the valuation day {GOLDEN_NOW.date()}) already "
        f"moved these surfaces: {differing}")


def _holding(dash: dict[str, Any], account: str, symbol: str) -> dict[str, Any] | None:
    return next((h for h in dash["holdings"]
                 if h["account_id"] == account and h["symbol"] == symbol), None)


def test_from_its_date_the_future_buy_does_count(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The cut is a DATE, not a deletion — and ``<=``: on its own date the row counts."""
    for now in (_ON_THE_DAY, _AFTER):
        base = dashboard_client_factory(_seed_golden, now=now)
        bought = dashboard_client_factory(_future_buy, now=now)
        b = base.get("/api/dashboard").json()
        dash = bought.get("/api/dashboard").json()
        h = _holding(dash, "tw_broker", "2330")
        assert h is not None and h["shares"] == "2000", now
        assert D(h["original_cost_total"]) == D("500000") + D("600855"), now
        assert dash["kpis"]["xirr"] != b["kpis"]["xirr"], now
        hint = bought.get("/api/input/holdings", params={"account": "tw_broker"}).json()
        assert next(x for x in hint["held"] if x["symbol"] == "2330")["shares"] == "2000"
        recon = bought.get("/api/symbol/2330/detail").json()["activity_reconcile"]["total"]
        assert recon["buy_shares"] == "2000" and recon["balances"] is True, recon


def test_from_its_date_the_future_sell_is_realized_and_taxed(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    base = dashboard_client_factory(_seed_golden, now=_AFTER)
    sold = dashboard_client_factory(_future_sell, now=_AFTER)
    rows = sold.get("/api/dashboard").json()["realized"]["rows"]
    assert [r["sell_date"] for r in rows if r["kind"] == "sale"] == [_FUTURE.isoformat()]
    gains = next(v for k, v in _tax_package(sold).items() if k.startswith("realized_gains"))
    base_gains = next(v for k, v in _tax_package(base).items()
                      if k.startswith("realized_gains"))
    assert len(gains.splitlines()) == len(base_gains.splitlines()) + 1


def test_from_its_date_the_future_opening_is_held(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    dash = dashboard_client_factory(_future_opening, now=_AFTER).get("/api/dashboard").json()
    h = _holding(dash, "tw_broker", "2317")
    assert h is not None and h["shares"] == "1000" and h["original_cost_total"] == "120000"


def test_from_its_date_the_future_conversion_funds_the_pool(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    def usd(client: TestClient) -> Decimal:
        bal = client.get("/api/cash", params={"limit": 500}).json()["balances"]
        return D(next(b["amount"] for b in bal
                      if b["account_id"] == "schwab" and b["ccy"] == "USD"))

    base = dashboard_client_factory(_seed_golden, now=_AFTER)
    converted = dashboard_client_factory(_future_fx, now=_AFTER)
    assert usd(converted) - usd(base) == D("1000")
    b_fx = base.get("/api/dashboard").json()["fx"]
    c_fx = converted.get("/api/dashboard").json()["fx"]
    assert b_fx != c_fx            # the pool's average and exposure now include it


def test_from_its_date_the_future_reconversion_is_realized_fx_in_the_tax_package(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    def fx_lines(client: TestClient) -> list[str]:
        sheet = next(v for k, v in _tax_package(client).items() if k.startswith("fx_realized"))
        return [ln for ln in sheet.splitlines()[1:] if ln.strip()]

    assert fx_lines(dashboard_client_factory(_seed_golden, now=_AFTER)) == []
    lines = fx_lines(dashboard_client_factory(_future_reconversion, now=_AFTER))
    assert len(lines) == 1 and lines[0].startswith(_FUTURE.isoformat()), lines


def test_from_its_date_the_future_split_redenominates(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Before its date a 2-for-1 must NOT double today's shares — every stored price is still
    pre-split, so applying it early doubled the position's market value (measured: AAPL
    10 → 20 shares at the unsplit 120 USD). From its date both sides are post-split."""
    dash = dashboard_client_factory(_future_split, now=_AFTER).get("/api/dashboard").json()
    h = _holding(dash, "schwab", "AAPL")
    assert h is not None and h["shares"] == "20"
    assert D(h["market_price"]) == D("60")           # the carried-forward close, re-expressed


def test_from_its_date_the_future_fee_is_an_xirr_flow(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    b = dashboard_client_factory(_seed_golden, now=_AFTER).get("/api/dashboard").json()
    f = dashboard_client_factory(_future_fee, now=_AFTER).get("/api/dashboard").json()
    assert f["kpis"]["xirr"] != b["kpis"]["xirr"]
    assert f["kpis"]["trading_financing_cost"] != b["kpis"]["trading_financing_cost"]


# --- the ledger rows say so ------------------------------------------------------------


def _all_future(conn: sqlite3.Connection) -> None:
    """One future row in each of the six ledgers the lists serve, beside the past ones."""
    _future_buy(conn)
    upsert_instrument(conn, Instrument(symbol="2317", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Electronics",
                                       name="Hon Hai", board="TWSE"))
    upsert_opening(conn, account_id="tw_broker", symbol="2317", shares=D("1000"),
                   original_cost_total=D("120000"), build_date=_FUTURE)
    insert_fx_conversion(conn, account_id="schwab", date=_FUTURE, from_ccy=Currency.TWD,
                         from_amount=D("33500"), to_ccy=Currency.USD, to_amount=D("1000"))
    insert_cash_movement(conn, account_id="schwab", move_date=_FUTURE, kind="BROKER_FEE",
                         ccy=Currency.USD, amount=D("25"))
    # A cash dividend paying in the future counts from its PAY date …
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=_FUTURE,
                    div_type="CASH", gross=D("18200"), withholding=D("0"), net=D("18200"),
                    ex_date=date(2026, 6, 5))
    # … a 配股 from its EX-date (R6) — already past here, so it counts today: no flag.
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=_FUTURE,
                    div_type="STOCK", gross=D("0"), withholding=D("0"), net=D("0"),
                    reinvest_shares=D("50"), ex_date=date(2026, 6, 10))


def _by_date(row: tuple[str, str | None]) -> tuple[str, str]:
    return row[0], row[1] or ""


def _flags(client: TestClient) -> dict[str, list[tuple[str, str | None]]]:
    out: dict[str, list[tuple[str, str | None]]] = {}
    for kind in ("transactions", "dividends", "fx", "cash", "openings"):
        rows = client.get(f"/api/ledgers/{kind}", params={"limit": 500}).json()["rows"]
        out[kind] = sorted(((r["date"], r["counts_from"]) for r in rows), key=_by_date)
    rows = client.get("/api/cash", params={"limit": 500}).json()["movements"]["rows"]
    out["cash_page"] = sorted(((r["date"], r["counts_from"]) for r in rows), key=_by_date)
    return out


def test_the_ledger_rows_carry_counts_from_while_and_only_while_they_are_ahead(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    ahead = _flags(dashboard_client_factory(_all_future))
    f = _FUTURE.isoformat()
    assert ahead["transactions"] == [("2026-01-05", None), ("2026-01-10", None), (f, f)]
    assert ahead["dividends"] == [("2026-03-01", None), (f, None), (f, f)]
    assert ahead["fx"] == [("2026-01-08", None), (f, f)]
    assert ahead["cash"] == [(f, f)]
    assert ahead["cash_page"] == [(f, f)]
    assert ahead["openings"] == [(f, f)]
    # On its own date it counts, so the flag is gone everywhere — the SERVER's clock decides.
    today = _flags(dashboard_client_factory(_all_future, now=_ON_THE_DAY))
    assert all(cf is None for rows in today.values() for _d, cf in rows), today
