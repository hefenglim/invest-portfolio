"""S9 (pre-merge review round 3, 2026-10-04): the drawer's 「部位已結清」 rests on a server fact —
did the replay ever OPEN a position for this symbol — never on the shape of the activity rows.

R8 judged it from ``activity``: any row that is not a DRIP / 配股 counted as proof a position was
opened. But an ``action`` row can be a corporate action the replay REFUSED (it opened
nothing), so a symbol whose only rows were a refused EXCHANGE into it and an orphan 配股 still
read 「帳本有交易紀錄，部位已結清」. ``GET /api/symbol/{symbol}/detail`` now carries
``position_opened``, read off the same book ``build_dashboard`` built
(``Book.opened_positions`` — every position the replay created, whatever its share count).
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_dividend,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    for sym in ("2330", "2317", "2454", "2603"):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Electronics",
                                           name=sym, board="TWSE"))
    # 2317: a refused EXCHANGE into it (2454 was never held) + a 配股 nobody earned.
    insert_corporate_action(conn, account_id="tw_broker", action_date=date(2026, 3, 1),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="2454",
                            to_symbol="2317", ratio_to=Decimal("1"), ratio_from=Decimal("1"))
    insert_dividend(conn, account_id="tw_broker", symbol="2317", div_date=date(2026, 4, 1),
                    div_type="STOCK", gross=Decimal("0"), withholding=Decimal("0"),
                    net=Decimal("0"), reinvest_shares=Decimal("50"))
    # 2330: genuinely closed — bought and sold.
    for side in (Side.BUY, Side.SELL):
        insert_transaction(conn, account_id="tw_broker", symbol="2330", side=side,
                           quantity=Decimal("1000"), price=Decimal("500"), fees=Decimal("0"),
                           tax=Decimal("0"), trade_date=date(2026, 1, 5))
    # 2603: a zero-quantity row only — the replay skips it before opening anything.
    insert_transaction(conn, account_id="tw_broker", symbol="2603", side=Side.BUY,
                       quantity=Decimal("0"), price=Decimal("50"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 1, 5))


def test_position_opened_is_the_replays_answer(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    detail = {s: client.get(f"/api/symbol/{s}/detail").json() for s in ("2317", "2330", "2603")}
    assert detail["2317"]["position_opened"] is False       # a refused action opens nothing
    assert any(a["side"] == "action" for a in detail["2317"]["activity"])   # …yet it is listed
    assert detail["2330"]["position_opened"] is True        # closed: opened, then sold out
    assert detail["2603"]["position_opened"] is False       # a 0-share row opens nothing
