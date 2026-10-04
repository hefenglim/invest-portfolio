"""R4 (pre-merge review round 2, 2026-10-04): the manual-entry 草稿預覽 must not quote ordinary
position math on a 賣超 that netted back to exactly zero shares.

``_position_preview`` looked the position up in ``Book.holdings``, which drops a zero-share
position flag and all (CR-01). A held 賣超 has always been WITHHELD there — its totals and share
count disagree, so no average quoted on it is the ledger's — but the flat one read as "never
held" and the BUY branch printed a fresh-looking average. Measured: ledger buy 1,000 → sell
2,000 (undeclared) → buy 1,000; a manual BUY of 500 previewed 「— → 500」 at an ordinary
average, and once written the dashboard and the drawer flagged the position 賣超待釐清.

A preview mirrors the replay's branches (domain-ledger.md): the replay keeps this position
on the BOOK (``Book.oversold_positions``) and so does the preview's withholding.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory


def _ledger(trades: list[tuple[Side, str, date]]) -> Any:
    def seed(conn: sqlite3.Connection) -> None:
        seed_accounts(conn)
        upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Semiconductors",
                                           name="TSMC", board="TWSE"))
        for side, qty, day in trades:
            insert_transaction(conn, account_id="tw_broker", symbol="2330", side=side,
                               quantity=Decimal(qty), price=Decimal("500"), fees=Decimal("0"),
                               tax=Decimal("0"), trade_date=day)
    return seed


#: buy 1,000 → sell 2,000 (undeclared: the basis is discarded) → buy 1,000 → flat, still 賣超.
_FLAT_OVERSOLD = [(Side.BUY, "1000", date(2026, 1, 5)),
                  (Side.SELL, "2000", date(2026, 2, 1)),
                  (Side.BUY, "1000", date(2026, 3, 1))]


def _preview(client: Any, side: str, day: str, shares: str = "500") -> dict[str, Any]:
    r = client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2330", "side": side, "date": day,
        "shares": shares, "price": "600"})
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def test_a_buy_on_a_flat_oversold_position_is_withheld_like_a_held_one(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_ledger(_FLAT_OVERSOLD))
    assert _preview(client, "buy", "2026-06-01")["position_preview"] is None
    # …and what the preview declined to promise is what the ledger then says.
    r = client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2330", "side": "buy", "date": "2026-06-01",
        "shares": "500", "price": "600"})
    assert r.status_code == 201, r.text
    (held,) =[h for h in client.get("/api/dashboard").json()["holdings"]
               if h["symbol"] == "2330"]
    assert held["oversold"] is True


def test_a_sell_on_a_flat_oversold_position_is_withheld(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The SELL side reached the same answer before (no holding → 「not held」 → null); pinned
    so the two sides keep agreeing for one stated reason rather than two accidental ones."""
    client = dashboard_client_factory(_ledger(_FLAT_OVERSOLD))
    assert _preview(client, "sell", "2026-06-01", shares="100")["position_preview"] is None


def test_an_ordinarily_closed_position_still_previews_a_fresh_buy(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The control: buy 1,000 → sell 1,000 is flat WITHOUT a 賣超, so a new buy is a fresh
    position and the card shows its average — withholding is for 賣超 only."""
    client = dashboard_client_factory(_ledger([(Side.BUY, "1000", date(2026, 1, 5)),
                                               (Side.SELL, "1000", date(2026, 2, 1))]))
    pp = _preview(client, "buy", "2026-06-01")["position_preview"]
    assert pp is not None
    # 500 × 600 + the NT$427 commission (floor(300,000 × 0.1425%)), all-in, over 500 shares.
    assert (pp["new_shares"], pp["new_original_avg"], pp["old_shares"]) == (
        "500", "600.854", None), pp
