"""R5 + R9 (pre-merge review round 2, 2026-10-04): the correction doors' orphan check is PER
DIVIDEND, and there is ONE definition of "a dividend with no position".

R5 — ``replay_guard._replay_block`` compared the stranded set per (account, symbol) KEY, so a
legacy orphan on the key masked a NEW one the mutation strands. Measured: legacy orphan D1
(2330, 2025-01-01), buy B1 (2025-03-01), valid D2 (2025-06-01) — deleting B1 orphans D2 and
answered 200 with no question. Now each dividend ROW is compared (by its id, through the
replay's own ``Book.orphan_dividends``), so D2 is refused while D1 stays correctable.

R9 — when a side cannot be replayed the guard falls back to a direct computation, and that
computation dated a dividend by its PAYMENT date and ignored EXCHANGE / SPINOFF destinations:
a third definition, disagreeing with the replay and with ``holdings.position_exists_by`` (the
door's own check). It now reads ``LedgerBundle.position_opened_by``, the same rule. Pinned
here through the public doors (with an unbookable legacy row forcing the fallback) and as a
four-way agreement over one ledger.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from portfolio_dash.api.replay_guard import _orphan_triples
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.holdings import position_exists_by
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_dividend,
    insert_transaction,
    list_dividends,
    load_ledger_bundle,
    upsert_instrument,
    upsert_opening,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory, init_golden_base

_SYMBOLS = ("2330", "2317", "2454", "2603", "9999")


def _instruments(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    for sym in _SYMBOLS:
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Electronics",
                                           name=sym, board="TWSE"))


def _buy(conn: sqlite3.Connection, symbol: str, day: date, qty: str = "1000") -> int:
    return insert_transaction(conn, account_id="tw_broker", symbol=symbol, side=Side.BUY,
                              quantity=Decimal(qty), price=Decimal("100"), fees=Decimal("0"),
                              tax=Decimal("0"), trade_date=day)


def _cash(conn: sqlite3.Connection, symbol: str, day: date, net: str = "100") -> int:
    return insert_dividend(conn, account_id="tw_broker", symbol=symbol, div_date=day,
                           div_type="CASH", gross=Decimal(net), withholding=Decimal("0"),
                           net=Decimal(net))


def _unbookable_legacy_row(conn: sqlite3.Connection) -> None:
    """A 配股 with no share count on an unrelated symbol: the replay refuses it on EVERY path,
    so neither side of a mutation can be replayed and the guard takes its fallback."""
    _buy(conn, "9999", date(2024, 6, 1))
    insert_dividend(conn, account_id="tw_broker", symbol="9999", div_date=date(2024, 7, 1),
                    div_type="STOCK", gross=Decimal("0"), withholding=Decimal("0"),
                    net=Decimal("0"))


# ------------------------------------------------------------------ R5: per dividend


def _r5(ids: dict[str, int]) -> Any:
    def seed(conn: sqlite3.Connection) -> None:
        _instruments(conn)
        ids["d1"] = _cash(conn, "2330", date(2025, 1, 1))        # legacy orphan
        ids["b1"] = _buy(conn, "2330", date(2025, 3, 1))
        ids["d2"] = _cash(conn, "2330", date(2025, 6, 1))        # valid while b1 stands
    return seed


def test_a_legacy_orphan_does_not_mask_a_new_one_on_delete(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    ids: dict[str, int] = {}
    client = dashboard_client_factory(_r5(ids))
    r = client.delete(f"/api/ledgers/transactions/{ids['b1']}")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "orphan_correction"
    assert "2330" in r.json()["error"]["message"]


def test_a_legacy_orphan_does_not_mask_a_new_one_on_edit(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Moving the buy after D2 strands D2 exactly like deleting it."""
    ids: dict[str, int] = {}
    client = dashboard_client_factory(_r5(ids))
    r = client.put(f"/api/ledgers/transactions/{ids['b1']}", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy", "date": "2025-07-01",
        "shares": "1000", "price": "100", "fee": "0", "tax": "0",
        "fee_overridden": True, "tax_overridden": True})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "orphan_correction"


def test_the_legacy_orphan_itself_stays_correctable(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Per ROW, not per value: editing D1's amount leaves D1 the orphan it already was — the
    mutation strands nothing new, so the replay guard does not stand in its way (the H3 rule)."""
    ids: dict[str, int] = {}
    client = dashboard_client_factory(_r5(ids))
    r = client.put(f"/api/ledgers/dividends/{ids['d1']}", json={
        "account_id": "tw_broker", "symbol": "2330", "date": "2025-01-01", "type": "cash",
        "gross": "120", "withhold": "0", "net": "120"})
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ R9: the fallback agrees


def test_the_fallback_dates_a_stock_dividend_by_its_ex_date(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """A 配股 is booked on its EX-date. Moving the buy between the ex-date and the payment date
    strands it in the replay; the fallback used the payment date and let the edit through."""
    ids: dict[str, int] = {}

    def seed(conn: sqlite3.Connection) -> None:
        _instruments(conn)
        _unbookable_legacy_row(conn)
        ids["buy"] = _buy(conn, "2603", date(2025, 5, 10))
        insert_dividend(conn, account_id="tw_broker", symbol="2603", div_date=date(2025, 6, 1),
                        div_type="STOCK", gross=Decimal("0"), withholding=Decimal("0"),
                        net=Decimal("0"), reinvest_shares=Decimal("50"),
                        ex_date=date(2025, 5, 12))
    client = dashboard_client_factory(seed)
    r = client.put(f"/api/ledgers/transactions/{ids['buy']}", json={
        "account_id": "tw_broker", "symbol": "2603", "side": "buy", "date": "2025-05-20",
        "shares": "1000", "price": "100", "fee": "0", "tax": "0",
        "fee_overridden": True, "tax_overridden": True})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "orphan_correction"


def test_the_fallback_counts_an_exchange_as_the_position(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """2317 exists from an EXCHANGE out of 2330. Deleting a later, unrelated 2317 buy strands
    nothing — the fallback ignored the exchange and refused the delete."""
    ids: dict[str, int] = {}

    def seed(conn: sqlite3.Connection) -> None:
        _instruments(conn)
        _unbookable_legacy_row(conn)
        _buy(conn, "2330", date(2025, 3, 1))
        insert_corporate_action(conn, account_id="tw_broker", action_date=date(2025, 4, 1),
                                kind=CorporateActionKind.EXCHANGE, from_symbol="2330",
                                to_symbol="2317", ratio_to=Decimal("1"),
                                ratio_from=Decimal("1"))
        ids["later"] = _buy(conn, "2317", date(2025, 4, 15), qty="10")
        _cash(conn, "2317", date(2025, 5, 1))
    client = dashboard_client_factory(seed)
    r = client.delete(f"/api/ledgers/transactions/{ids['later']}")
    assert r.status_code == 200, r.text


def test_replay_fallback_and_door_check_agree_on_every_dividend() -> None:
    """ONE definition, four readers: the replay (``Book.orphan_dividends``), the guard's
    fallback, the bundle predicate and the door's SQL check (``position_exists_by``)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    _instruments(conn)
    upsert_opening(conn, account_id="tw_broker", symbol="2454", shares=Decimal("100"),
                   original_cost_total=Decimal("10000"), build_date=date(2025, 2, 1))
    _cash(conn, "2454", date(2025, 1, 15))                # before the opening → orphan
    _cash(conn, "2454", date(2025, 2, 1))                 # the opening's own day → booked
    _buy(conn, "2330", date(2025, 3, 1))
    insert_corporate_action(conn, account_id="tw_broker", action_date=date(2025, 4, 1),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="2330",
                            to_symbol="2317", ratio_to=Decimal("1"), ratio_from=Decimal("1"))
    _cash(conn, "2317", date(2025, 3, 31))                # the day before the exchange → orphan
    _cash(conn, "2317", date(2025, 4, 1))                 # the exchange's own day → booked
    _buy(conn, "2603", date(2025, 5, 10))
    for ex, pay in ((date(2025, 5, 5), date(2025, 6, 1)),     # ex before the buy → orphan
                    (date(2025, 5, 12), date(2025, 6, 1))):   # ex after the buy → booked
        insert_dividend(conn, account_id="tw_broker", symbol="2603", div_date=pay,
                        div_type="STOCK", gross=Decimal("0"), withholding=Decimal("0"),
                        net=Decimal("0"), reinvest_shares=Decimal("5"), ex_date=ex)
    # S8 (round 3): an UNREADABLE action row (a non-positive ratio) and an action whose source
    # symbol is UNREGISTERED. The replay applies neither — the first never becomes an event,
    # the second is dropped with every row naming an unregistered symbol — so neither opens a
    # position, in any of the four readers.
    for to_symbol in ("2609", "2610"):
        upsert_instrument(conn, Instrument(symbol=to_symbol, market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Shipping",
                                           name=to_symbol, board="TWSE"))
    _buy(conn, "9999", date(2025, 6, 1))
    insert_corporate_action(conn, account_id="tw_broker", action_date=date(2025, 7, 1),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="9999",
                            to_symbol="2609", ratio_to=Decimal("0"), ratio_from=Decimal("1"))
    _cash(conn, "2609", date(2025, 7, 15))                # unreadable exchange → orphan
    insert_corporate_action(conn, account_id="tw_broker", action_date=date(2025, 7, 1),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="NOPE",
                            to_symbol="2610", ratio_to=Decimal("1"), ratio_from=Decimal("1"))
    _cash(conn, "2610", date(2025, 7, 15))                # unregistered source → orphan
    bundle = load_ledger_bundle(conn).without_unregistered()   # as every replay reads it
    book = build_book(bundle, allow_oversell=True)
    from_replay = _orphan_triples(bundle, book)
    from_fallback = _orphan_triples(bundle, None)
    assert from_replay == from_fallback
    assert {(s, d) for _a, s, d in from_replay} == {
        ("2454", date(2025, 1, 15)), ("2317", date(2025, 3, 31)), ("2603", date(2025, 5, 5)),
        ("2609", date(2025, 7, 15)), ("2610", date(2025, 7, 15))}
    for dv in bundle.dividends:
        triple = (dv.account_id, dv.symbol, dv.effective_date)
        opened = bundle.position_opened_by(dv.account_id, dv.symbol, dv.effective_date)
        door = position_exists_by(conn, dv.account_id, dv.symbol, dv.effective_date)
        assert (triple not in from_replay) == opened == door, (triple, opened, door)
    assert len(list_dividends(conn)) == 8
    conn.close()


def test_the_one_stated_difference_a_readable_action_the_replay_refuses() -> None:
    """Where the two forms deliberately differ from the replay, and agree with each other: an
    EXCHANGE that is readable and registered but REFUSED (its source was never held) counts as
    opening its destination in both forms, while the replay opens nothing — the dividend door
    therefore lets the dividend in, and the replay refuses it on the book (``Book.orphan_
    dividends``), named on the dashboard. Pinned so the difference stays the stated one."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    _instruments(conn)
    insert_corporate_action(conn, account_id="tw_broker", action_date=date(2025, 4, 1),
                            kind=CorporateActionKind.EXCHANGE, from_symbol="2330",
                            to_symbol="2317", ratio_to=Decimal("1"), ratio_from=Decimal("1"))
    _cash(conn, "2317", date(2025, 5, 1))
    bundle = load_ledger_bundle(conn).without_unregistered()
    (dv,) = bundle.dividends
    assert bundle.position_opened_by(dv.account_id, dv.symbol, dv.effective_date) is True
    assert position_exists_by(conn, dv.account_id, dv.symbol, dv.effective_date) is True
    book = build_book(bundle, allow_oversell=True)
    assert [d.symbol for d in book.orphan_dividends] == ["2317"]
    assert [u.from_symbol for u in book.unapplied_actions] == ["2330"]
    conn.close()
