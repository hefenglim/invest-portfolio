"""K3 (owner ruling 2026-10-05): every reader that shows 待釐清 for a refused dividend sees one
whose carrier is FLAT — through its own door.

A dividend on a ticker an EXCHANGE vacated (E24) is refused, and on the dashboard path its
``unbookable_dividend`` flag goes on the live successor. When that successor is flat (sold out),
``build_book`` drops it from ``holdings`` and the flag went with it — the CR-11 class. Measured
before the fix on the ledger below (2454 bought, exchanged into 2317, 2317 sold out, then a
payout on 2454): the trend's last day was complete, 含匯兌總損益 was a number with no reason, and
neither drawer said anything. With the successor HELD, the trend did mark the days, but the
FX-complete reason said 「最新一日有標的缺價」 — a wrong diagnosis on a priced position.

The book now records the refusal (``Book.unbookable_dividends``) and the readers read it:
the trend's incomplete flag, the FX-complete reason (which names the row), the drawer of the
dividend's own symbol AND of the carrier, and the reconciliation footer's server fact
``gap_is_refused_dividends`` that K1's remedy choice rests on. The manual-entry preview does
NOT withhold on it — pinned at the end, with the reason.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW, DashboardClientFactory

D = Decimal
OLD, NEW = "2454", "2317"
PAY_DAY = date(2026, 4, 1)


def _base(conn: sqlite3.Connection, *, sell_successor: bool = True,
          div_type: str = "CASH") -> None:
    """2454 bought, exchanged 1:1 into 2317 (2317 sold out unless told otherwise), then a payout
    on 2454 — refused (E24). Prices cover every day a position is held, so no day of the trend
    is incomplete for want of a price: what marks a day here is the refusal and nothing else."""
    seed_accounts(conn)
    for sym in (OLD, NEW, "2330"):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Electronics",
                                           name=sym, board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol=OLD, side=Side.BUY,
                       quantity=D("1000"), price=D("500"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 10))
    insert_corporate_action(conn, account_id="tw_broker", action_date=date(2026, 2, 1),
                            kind=CorporateActionKind.EXCHANGE, from_symbol=OLD, to_symbol=NEW,
                            ratio_to=D("1"), ratio_from=D("1"))
    if sell_successor:
        insert_transaction(conn, account_id="tw_broker", symbol=NEW, side=Side.SELL,
                           quantity=D("1000"), price=D("520"), fees=D("0"), tax=D("0"),
                           trade_date=date(2026, 3, 1))
    reinvest = D("20") if div_type == "STOCK" else None
    net = D("0") if div_type == "STOCK" else D("3000")
    insert_dividend(conn, account_id="tw_broker", symbol=OLD, div_date=PAY_DAY,
                    div_type=div_type, gross=net, withholding=D("0"), net=net,
                    reinvest_shares=reinvest)
    upsert_prices(conn, [
        PriceRow(instrument=OLD, market=Market.TW, as_of=date(2026, 1, 9), close=D("500"),
                 source="test"),
        PriceRow(instrument=NEW, market=Market.TW, as_of=date(2026, 1, 9), close=D("500"),
                 source="test"),
        PriceRow(instrument=NEW, market=Market.TW, as_of=date(2026, 6, 9), close=D("530"),
                 source="test"),
    ], fetched_at=GOLDEN_NOW)


def _dashboard(client: Any) -> dict[str, Any]:
    r = client.get("/api/dashboard")
    assert r.status_code == 200, r.text
    return dict(r.json())


def _point(dash: dict[str, Any], day: str) -> dict[str, Any]:
    return next(p for p in dash["trend"]["points"] if p["date"] == day)


# --- the trend and the FX-complete reason ----------------------------------------------


def test_the_trend_marks_the_days_after_a_refused_dividend_on_a_flat_successor(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    dash = _dashboard(dashboard_client_factory(_base))
    assert dash["holdings"] == []                       # nothing held: no row carries a flag
    assert _point(dash, "2026-03-31")["incomplete"] is False      # date-scoped, not retroactive
    assert _point(dash, PAY_DAY.isoformat())["incomplete"] is True
    assert dash["trend"]["points"][-1]["incomplete"] is True


def test_the_fx_complete_reason_names_the_refused_dividend(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    kpis = _dashboard(dashboard_client_factory(_base))["kpis"]
    assert kpis["total_return_fx_complete"] is None
    reason = kpis["fx_complete_reason"]
    assert "股利無法入帳" in reason and OLD in reason and PAY_DAY.isoformat() in reason, reason


def test_a_held_successor_is_no_longer_diagnosed_as_a_missing_price(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The held shape already marked the trend — but the reason ladder had no rung for it and
    fell through to 「最新一日有標的缺價」 over a position priced the day before."""
    dash = _dashboard(dashboard_client_factory(lambda c: _base(c, sell_successor=False)))
    (row,) = dash["holdings"]
    assert row["symbol"] == NEW and row["unbookable_dividend"] is True
    assert row["market_price"] is not None
    reason = dash["kpis"]["fx_complete_reason"]
    assert "缺價" not in reason and "股利無法入帳" in reason, reason


def test_xirr_is_unchanged_by_the_record(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """XIRR has never been withheld for a refused short / vacated dividend — it excludes the
    payout and computes (review R2 ⑤). K3 is about the flag surviving, not about what it gates."""
    dash = _dashboard(dashboard_client_factory(_base))
    reason = dash["freshness"].get("xirr_unavailable_reason") or ""
    assert "股利" not in reason, reason


# --- the drawer ------------------------------------------------------------------------


def _detail(client: Any, symbol: str) -> dict[str, Any]:
    r = client.get(f"/api/symbol/{symbol}/detail")
    assert r.status_code == 200, r.text
    return dict(r.json())


def test_both_drawers_name_the_refused_dividend(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_base)
    (own,) = _detail(client, OLD)["ledger_issues"]["unbookable_dividends"]
    assert (own["role"], own["cause"], own["symbol"], own["carrier_symbol"]) == (
        "own", "vacated", OLD, NEW)
    assert (own["account_id"], own["date"], own["reinvest"]) == (
        "tw_broker", PAY_DAY.isoformat(), False)
    assert f"換股為 {NEW}" in own["reason"]
    (carried,) = _detail(client, NEW)["ledger_issues"]["unbookable_dividends"]
    assert (carried["role"], carried["symbol"]) == ("carrier", OLD)
    # A symbol the refusal does not touch lists nothing.
    assert _detail(client, "2330")["ledger_issues"]["unbookable_dividends"] == []


def test_the_footer_says_when_its_whole_gap_is_refused_dividends(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """A refused 配股 adds 20 shares on the ledger's share path that the book does not hold, so
    2454's footer is red by exactly those 20 — the server says so, and K1's drawer offers the
    dividend's remedy instead of 補登公司行動 on that fact, never on a reading of the rows."""
    client = dashboard_client_factory(lambda c: _base(c, div_type="STOCK"))
    detail = _detail(client, OLD)
    (row,) = detail["ledger_issues"]["unbookable_dividends"]
    assert row["reinvest"] is True
    total = detail["activity_reconcile"]["total"]
    assert total["balances"] is False and total["diff_shares"] == "20"
    assert total["refused_reinvest_shares"] == "20"
    assert total["gap_is_refused_dividends"] is True
    acct = detail["activity_reconcile"]["by_account"]["tw_broker"]
    assert acct["gap_is_refused_dividends"] is True
    # The carrier's own footer is green and owns no refused shares.
    new_total = _detail(client, NEW)["activity_reconcile"]["total"]
    assert new_total["balances"] is True
    assert new_total["refused_reinvest_shares"] == "0"
    assert new_total["gap_is_refused_dividends"] is False


def test_an_orphan_reinvest_dividend_is_the_whole_gap_too(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        seed_accounts(conn)
        upsert_instrument(conn, Instrument(symbol=NEW, market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Electronics",
                                           name=NEW, board="TWSE"))
        insert_dividend(conn, account_id="tw_broker", symbol=NEW, div_date=PAY_DAY,
                        div_type="STOCK", gross=D("0"), withholding=D("0"), net=D("0"),
                        reinvest_shares=D("50"))
    total = _detail(dashboard_client_factory(seed), NEW)["activity_reconcile"]["total"]
    assert (total["balances"], total["refused_reinvest_shares"],
            total["gap_is_refused_dividends"]) == (False, "50", True)


def test_a_refused_corporate_action_is_not_a_dividend_gap(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The control: door 2's own red footer (a 賣超 carrying a SPLIT the replay refuses, E3)
    owns no refused dividend, so its gap is NOT explained by one."""
    def seed(conn: sqlite3.Connection) -> None:
        seed_accounts(conn)
        upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Electronics",
                                           name="2330", board="TWSE"))
        for side, qty, day in ((Side.SELL, "100", date(2026, 1, 5)),
                               (Side.BUY, "300", date(2026, 2, 5))):
            insert_transaction(conn, account_id="tw_broker", symbol="2330", side=side,
                               quantity=D(qty), price=D("500"), fees=D("0"), tax=D("0"),
                               trade_date=day)
        insert_corporate_action(conn, account_id="tw_broker", action_date=date(2026, 3, 1),
                                kind=CorporateActionKind.SPLIT, from_symbol="2330",
                                to_symbol="2330", ratio_to=D("2"), ratio_from=D("1"))
    total = _detail(dashboard_client_factory(seed), "2330")["activity_reconcile"]["total"]
    assert total["balances"] is False
    assert (total["refused_reinvest_shares"], total["gap_is_refused_dividends"]) == ("0", False)


# --- the manual-entry preview: deliberately NOT withheld -------------------------------


def test_the_preview_projects_a_buy_on_the_flat_carrier_as_the_replay_books_it(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """A refused dividend does not make the carrier's numbers undefined: its shares are right
    and its totals are exactly what the replay holds — the refusal happens identically in the
    book before the draft and in the book with it. So the preview projects (unlike a 賣超 or a
    refused corporate action, whose share count or basis is undefined), and what it projects is
    what the written row books. A pin, not a fix: it passes before and after K3."""
    client = dashboard_client_factory(_base)
    body = {"account_id": "tw_broker", "symbol": NEW, "side": "buy", "date": "2026-06-01",
            "shares": "100", "price": "600", "short_sale": False}
    r = client.post("/api/input/manual/preview", json=body)
    assert r.status_code == 200, r.text
    preview = r.json()["position_preview"]
    assert preview is not None and preview["new_shares"] == "100"
    r = client.post("/api/input/manual/commit", json=body)
    assert r.status_code == 201, r.text
    (row,) = _dashboard(client)["holdings"]
    assert row["symbol"] == NEW and row["shares"] == "100"
    assert row["unbookable_dividend"] is True           # the sticky flag, on the reopened row
    assert row["original_avg"] == preview["new_original_avg"]
    assert row["adjusted_avg"] == preview["new_adjusted_avg"]
