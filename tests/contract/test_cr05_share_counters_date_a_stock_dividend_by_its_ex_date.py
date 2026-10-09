"""CR-05 (pre-merge review, 2026-10-04): every share counter dates a 配股 by its EX-date.

R6 moved a STOCK dividend with a known ex-date onto that ex-date in the replay
(``Dividend.effective_date``), in ``LedgerBundle``'s valuation cuts and in the trend. The
second, independent share path in ``data_ingestion/holdings.py`` — the walker
(``_Walk._flows_for``) and the naive sum (``_shares_until``) — kept reading the PAYMENT date.
Between the two dates the sell guard and the replay disagreed:

    opening 1,000 · STOCK +100 (ex 03-10, paid 04-05) · SELL 1,100 on 03-20

``build_book`` holds 1,100 on 03-20 and books the sale normally, while the manual preview
raised ``sell_exceeds_holdings`` (「賣出 1100 股，超過 2026-03-20 當日持有的 1000 股」) and asked
the owner to accept a discarded cost basis for a sale that was covered.

Pinned through the public door (``POST /api/input/manual/preview``) on BOTH share paths: the
naive one (no corporate action on the symbol — D38's short-circuit) and the walker (a SPLIT on
the symbol puts every count through ``_Walk``), plus the edit door that reuses the same guard.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.holdings import (
    current_shares,
    holds_position,
    shares_through,
)
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_dividend,
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import Transaction
from tests.conftest import DashboardClientFactory

D = Decimal
EX_DAY = date(2026, 3, 10)
PAY_DAY = date(2026, 4, 5)
SELL_DAY = date(2026, 3, 20)        # between the ex-date and the payment date


def _seed(conn: sqlite3.Connection, *, split: bool = False) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="TSMC", board="TWSE"))
    upsert_opening(conn, account_id="tw_broker", symbol="2330", shares=D("1000"),
                   original_cost_total=D("500000"), build_date=date(2026, 1, 2))
    stock = D("100")
    if split:
        # A 2-for-1 BEFORE the dividend: every count of 2330 now goes through the walker
        # (``_Walk``), not through the naive short-circuit.
        insert_corporate_action(conn, account_id="tw_broker", action_date=date(2026, 1, 20),
                                kind=CorporateActionKind.SPLIT, from_symbol="2330",
                                to_symbol="2330", ratio_to=D("2"), ratio_from=D("1"))
        stock = D("200")
    insert_dividend(conn, account_id="tw_broker", symbol="2330", div_date=PAY_DAY,
                    div_type="STOCK", gross=D("0"), withholding=D("0"), net=D("0"),
                    reinvest_shares=stock, ex_date=EX_DAY)
    conn.commit()


def _sell_body(shares: str) -> dict[str, Any]:
    return {"account_id": "tw_broker", "symbol": "2330", "side": "SELL",
            "date": SELL_DAY.isoformat(), "shares": shares, "price": "600"}


def _codes(resp: Any) -> list[str]:
    return [str(i.get("code")) for i in resp.json().get("issues", [])]


def _replay_holds_on_sell_day(conn: sqlite3.Connection, shares: str) -> Decimal:
    """What the REPLAY books for the same sale — the authority the guard must agree with."""
    bundle = load_ledger_bundle(conn)
    with_sell = dataclasses.replace(bundle, transactions=[*bundle.transactions, Transaction(
        account_id="tw_broker", symbol="2330", side=Side.SELL, quantity=D(shares),
        price=D("600"), fees=D("0"), tax=D("0"), trade_date=SELL_DAY)])
    book = build_book(with_sell)          # strict: raises OversellError if it were uncovered
    sold = sum((r.shares_sold for r in book.realized.rows if r.symbol == "2330"), D("0"))
    return sold


def test_naive_path_the_sell_between_ex_and_pay_is_covered(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    conns: list[sqlite3.Connection] = []

    def seed(conn: sqlite3.Connection) -> None:
        _seed(conn)
        conns.append(conn)

    client = dashboard_client_factory(seed)
    conn = conns[0]
    assert _replay_holds_on_sell_day(conn, "1100") == D("1100")    # the replay books it
    assert shares_through(conn, "tw_broker", "2330", on=SELL_DAY) == D("1100")
    resp = client.post("/api/input/manual/preview", json=_sell_body("1100"))
    assert resp.status_code == 200, resp.json()
    assert "sell_exceeds_holdings" not in _codes(resp), resp.json()["issues"]
    # …and the guard still bites one share past what the replay holds.
    over = client.post("/api/input/manual/preview", json=_sell_body("1101"))
    assert "sell_exceeds_holdings" in _codes(over)


def test_walker_path_the_sell_between_ex_and_pay_is_covered(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    conns: list[sqlite3.Connection] = []

    def seed(conn: sqlite3.Connection) -> None:
        _seed(conn, split=True)
        conns.append(conn)

    client = dashboard_client_factory(seed)
    conn = conns[0]
    assert _replay_holds_on_sell_day(conn, "2200") == D("2200")
    assert shares_through(conn, "tw_broker", "2330", on=SELL_DAY) == D("2200")
    resp = client.post("/api/input/manual/preview", json=_sell_body("2200"))
    assert resp.status_code == 200, resp.json()
    assert "sell_exceeds_holdings" not in _codes(resp), resp.json()["issues"]
    over = client.post("/api/input/manual/preview", json=_sell_body("2201"))
    assert "sell_exceeds_holdings" in _codes(over)


def test_the_counts_follow_the_ex_date_on_every_day_of_the_window(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Day by day, both share paths against the replay's own cut: before the ex-date the 配股
    does not count, from the ex-date it does — whatever the payment date says."""
    for split, base, stock in ((False, D("1000"), D("100")), (True, D("2000"), D("200"))):
        conns: list[sqlite3.Connection] = []

        def seed(conn: sqlite3.Connection, split: bool = split,
                 sink: list[sqlite3.Connection] = conns) -> None:
            _seed(conn, split=split)
            sink.append(conn)

        dashboard_client_factory(seed)
        conn = conns[0]
        for day, expected in ((date(2026, 3, 9), base), (EX_DAY, base + stock),
                              (SELL_DAY, base + stock), (PAY_DAY, base + stock)):
            got = shares_through(conn, "tw_broker", "2330", on=day)
            book = build_book(load_ledger_bundle(conn).valued_as_of(day))
            replay = next(h.shares for h in book.holdings if h.symbol == "2330")
            assert got == replay == expected, (split, day, got, replay)
        assert current_shares(conn, "tw_broker", "2330") == base + stock


def test_the_edit_door_reuses_the_same_dating(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The ledger 更正 door runs the same guard (DEF-042): a stored sell of 500 moved to 1,100
    on the same day between ex and pay is covered, and saves without a 賣超 ack."""
    ids: dict[str, int] = {}

    def seed(conn: sqlite3.Connection) -> None:
        _seed(conn)
        ids["sell"] = insert_transaction(
            conn, account_id="tw_broker", symbol="2330", side=Side.SELL, quantity=D("500"),
            price=D("600"), fees=D("0"), tax=D("0"), trade_date=SELL_DAY)

    client = dashboard_client_factory(seed)
    body = {"account_id": "tw_broker", "symbol": "2330", "side": "SELL",
            "date": SELL_DAY.isoformat(), "shares": "1100", "price": "600", "fee": "0",
            "tax": "0", "fee_overridden": True, "tax_overridden": True}
    r = client.put(f"/api/ledgers/transactions/{ids['sell']}", json=body)
    assert r.status_code == 200, r.json()
    assert "sell_exceeds_holdings" not in [i["code"] for i in r.json()["issues"]]


def test_holds_position_visits_the_ex_date(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """``holds_position`` samples the count on every later date it can CHANGE — a 配股's
    ex-date is one of them. Here the position is non-zero ONLY between the ex-date and a
    later sale, so a predicate that samples the payment date instead answers 「not held」."""
    conns: list[sqlite3.Connection] = []

    def seed(conn: sqlite3.Connection) -> None:
        seed_accounts(conn)
        upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Semiconductors",
                                           name="TSMC", board="TWSE"))
        insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.BUY,
                           quantity=D("1000"), price=D("500"), fees=D("0"), tax=D("0"),
                           trade_date=date(2026, 1, 5))
        insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.SELL,
                           quantity=D("1000"), price=D("600"), fees=D("0"), tax=D("0"),
                           trade_date=date(2026, 6, 1))
        insert_dividend(conn, account_id="tw_broker", symbol="2330",
                        div_date=date(2026, 7, 15), div_type="STOCK", gross=D("0"),
                        withholding=D("0"), net=D("0"), reinvest_shares=D("100"),
                        ex_date=date(2026, 6, 20))
        insert_transaction(conn, account_id="tw_broker", symbol="2330", side=Side.SELL,
                           quantity=D("100"), price=D("600"), fees=D("0"), tax=D("0"),
                           trade_date=date(2026, 7, 1))
        conns.append(conn)

    dashboard_client_factory(seed)
    conn = conns[0]
    assert shares_through(conn, "tw_broker", "2330", on=date(2026, 6, 25)) == D("100")
    assert holds_position(conn, "2330", today=date(2026, 6, 11)) is True
