"""S1 + S5 (pre-merge review round 3, 2026-10-04): the manual 草稿預覽 withholds position math
over EVERY 待釐清 source the replay has for the position — for every branch, on every book.

R4 read ``Book.holdings`` and ``Book.oversold_positions``, so a FLAT position carrying the sticky
``unbookable_action`` flag was in neither, and only the BUY branch re-checked the replayed
result. Measured: buy 1,000 → sell 1,000 → a 2:1 SPLIT on the now-flat position (refused, so
the position is flagged); a DECLARED short sell of 100 @600 previewed ``remain_shares -100,
new_original_avg 597.35, short_opened 100`` while the same draft as a BUY was withheld — and once
written, the dashboard showed 2330 at −100 flagged 待釐清.

The replay now records every position it opened, with its two 待釐清 flags, whatever its share
count (``Book.opened_positions``); the preview asks that ONE record on today's book, on the
trade-date book, and on the book with the draft (where only an action flag counts — a 賣超 there
is the draft's own undeclared oversell, which the SELL branch projects as the replay books it).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory


def _base(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    for sym in ("2330", "2317", "2454"):
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Electronics",
                                           name=sym, board="TWSE"))


def _tx(conn: sqlite3.Connection, side: Side, qty: str, day: date,
        symbol: str = "2330") -> None:
    insert_transaction(conn, account_id="tw_broker", symbol=symbol, side=side,
                       quantity=Decimal(qty), price=Decimal("500"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=day)


def _split(conn: sqlite3.Connection, day: date, *, ratio_to: str = "2",
           kind: CorporateActionKind = CorporateActionKind.SPLIT, to_symbol: str = "2330",
           from_symbol: str = "2330") -> None:
    insert_corporate_action(conn, account_id="tw_broker", action_date=day, kind=kind,
                            from_symbol=from_symbol, to_symbol=to_symbol,
                            ratio_to=Decimal(ratio_to), ratio_from=Decimal("1"))


def _flat_oversold(conn: sqlite3.Connection) -> None:
    _tx(conn, Side.BUY, "1000", date(2026, 1, 5))
    _tx(conn, Side.SELL, "2000", date(2026, 2, 1))
    _tx(conn, Side.BUY, "1000", date(2026, 3, 1))


def _flat_action(conn: sqlite3.Connection) -> None:
    """The review's probe: the split lands on a flat position, is refused, flags it."""
    _tx(conn, Side.BUY, "1000", date(2026, 1, 5))
    _tx(conn, Side.SELL, "1000", date(2026, 2, 1))
    _split(conn, date(2026, 3, 1))


def _held_oversold(conn: sqlite3.Connection) -> None:
    _flat_oversold(conn)
    _tx(conn, Side.BUY, "500", date(2026, 4, 1))


def _held_action(conn: sqlite3.Connection) -> None:
    _flat_action(conn)
    _tx(conn, Side.BUY, "500", date(2026, 4, 1))


_SOURCES: dict[str, Callable[[sqlite3.Connection], None]] = {
    "flat-oversold": _flat_oversold, "flat-unapplied-action": _flat_action,
    "held-oversold": _held_oversold, "held-unapplied-action": _held_action,
}

#: (label, side, shares, short_sale). On a flat source "sell-100" is an undeclared oversell;
#: on a held one (500 shares) it is an ORDINARY sell and "sell-1000" the oversell.
_DRAFTS = [
    ("buy", "buy", "100", False),
    ("declared-short", "sell", "100", True),
    ("sell-100", "sell", "100", False),
    ("sell-1000", "sell", "1000", False),
]


def _preview(client: Any, side: str, shares: str, short: bool,
             symbol: str = "2330") -> Any:
    r = client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": symbol, "side": side, "date": "2026-06-01",
        "shares": shares, "price": "600", "short_sale": short})
    assert r.status_code == 200, r.text
    return r.json()["position_preview"]


@pytest.mark.parametrize("source", list(_SOURCES))
@pytest.mark.parametrize(("label", "side", "shares", "short"), _DRAFTS,
                         ids=[d[0] for d in _DRAFTS])
def test_every_shape_is_withheld_over_every_unclear_source(
    dashboard_client_factory: DashboardClientFactory,
    source: str, label: str, side: str, shares: str, short: bool,
) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        _base(conn)
        _SOURCES[source](conn)
    client = dashboard_client_factory(seed)
    assert _preview(client, side, shares, short) is None, (source, label)


def test_the_probe_is_flagged_by_the_ledger_once_written(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """What the preview declines to project is what the dashboard then marks 待釐清."""
    def seed(conn: sqlite3.Connection) -> None:
        _base(conn)
        _flat_action(conn)
    client = dashboard_client_factory(seed)
    r = client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "sell", "date": "2026-06-01",
        "shares": "100", "price": "600", "short_sale": True})
    assert r.status_code == 201, r.text
    (row,) = [h for h in client.get("/api/dashboard").json()["holdings"]
              if h["symbol"] == "2330"]
    assert row["shares"] == "-100" and row["unbookable_action"] is True, row


def test_a_position_the_draft_creates_under_an_unreadable_action_is_withheld(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The book WITH the draft: an unreadable action row (a non-positive ratio) flags every
    position named on it that exists when the replay ends — including the one this draft
    opens. Neither today's book nor the trade-date book holds that position yet."""
    def seed(conn: sqlite3.Connection) -> None:
        _base(conn)
        _split(conn, date(2026, 3, 1), ratio_to="0", kind=CorporateActionKind.EXCHANGE,
               from_symbol="2317", to_symbol="2454")
    client = dashboard_client_factory(seed)
    for side, short in (("buy", False), ("sell", True)):
        assert _preview(client, side, "100", short, symbol="2317") is None, side


def test_a_clean_position_still_projects_every_shape(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The control: held 1,000 with nothing 待釐清 — every shape projects."""
    def seed(conn: sqlite3.Connection) -> None:
        _base(conn)
        _tx(conn, Side.BUY, "1000", date(2026, 1, 5))
    client = dashboard_client_factory(seed)
    for _label, side, shares, short in _DRAFTS:
        assert _preview(client, side, shares, short) is not None, _label
