"""DEF-048 (functional test manual B-17, owner ruling 2026-09-24): the manual-trade draft shows
the position ON THE TRADE DATE, not today's.

Measured on the demo site (R2): 台灣券商 2884 buy 100 @ 30 dated 2025-01-02 — the ledger's
earliest 2884 row is 2026-01-05, so on 2025-01-02 nothing was held — drafted
「持股 100 → 200、原始均價 93.20 → 61.70」: TODAY's holding on the left and the whole-ledger end
state on the right. Once written, the ledger replays by date and books 0 → 100 on that day, so
the card described a position the trade never meets.

The ruling: replay the ledger to the trade date (the draft is appended LAST, the same-day
write-order rule) and show that position before and after the draft. Pinned through the real
door (``POST /api/input/manual/preview``, real fee engine), and against the replay that the
written row actually produces.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    load_ledger_bundle,
    upsert_instrument,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import DashboardClientFactory

D = Decimal


def _seed(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2884", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Financials", name="玉山金", board="TWSE"))
    insert_transaction(conn, account_id="tw_broker", symbol="2884", side=Side.BUY,
                       quantity=D("100"), price=D("93.2"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 5))


def _seed_closed_since(conn: sqlite3.Connection) -> None:
    """Held 1,000 from January; the whole position was sold in May. Nothing is held today."""
    _seed(conn)
    insert_transaction(conn, account_id="tw_broker", symbol="2884", side=Side.BUY,
                       quantity=D("900"), price=D("93.2"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 6))
    insert_transaction(conn, account_id="tw_broker", symbol="2884", side=Side.SELL,
                       quantity=D("1000"), price=D("95"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 5, 4))


def _preview(client: TestClient, **over: str) -> dict[str, Any]:
    body = {"account_id": "tw_broker", "symbol": "2884", "side": "buy",
            "date": "2025-01-02", "shares": "100", "price": "30"}
    body.update(over)
    r = client.post("/api/input/manual/preview", json=body)
    assert r.status_code == 200, r.text
    pp = r.json()["position_preview"]
    assert pp is not None, r.json()
    return dict(pp)


def test_the_reported_backfill_reads_nothing_held_then_and_100_after(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    client = dashboard_client_factory(_seed)
    pp = _preview(client)
    assert pp["old_shares"] is None and pp["old_original_avg"] is None, (
        f"nothing was held on 2025-01-02, the card showed {pp['old_shares']!r}")
    assert pp["new_shares"] == "100"
    fee = D(client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": "2884", "side": "buy", "date": "2025-01-02",
        "shares": "100", "price": "30"}).json()["fee"])
    assert D(str(pp["new_original_avg"])) == (D("3000") + fee) / D("100")
    assert pp["as_of"] == "2025-01-02" and pp["backdated"] is True


def test_a_draft_after_everything_is_unchanged_and_not_backdated(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Counter-evidence: the happy path (today's trade) keeps TODAY's 100 → 200."""
    client = dashboard_client_factory(_seed)
    pp = _preview(client, date="2026-06-11")
    assert pp["old_shares"] == "100" and pp["new_shares"] == "200"
    assert pp["backdated"] is False


def test_a_backdated_sell_of_a_position_closed_since_is_projected_on_its_own_date(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The same class, other gate: the SELL arm bailed out on "not held TODAY", so a sell
    back-dated into a position that existed then (1,000 held in March) got no card at all."""
    client = dashboard_client_factory(_seed_closed_since)
    pp = _preview(client, side="sell", date="2026-03-02", shares="200", price="94")
    assert pp["kind"] == "sell"
    assert pp["old_shares"] == "1000" and pp["remain_shares"] == "800"
    assert pp["oversell"] is False and pp["realized_pnl"] is not None


def test_the_card_is_what_the_written_row_replays_to_on_that_date(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """「與寫入後的結果一致」: write the drafted row, replay the ledger through its own trade,
    and the holding there IS the card's new side — shares and both averages."""
    client = dashboard_client_factory(_seed)
    pp = _preview(client)
    r = client.post("/api/input/manual/commit", json={
        "account_id": "tw_broker", "symbol": "2884", "side": "buy", "date": "2025-01-02",
        "shares": "100", "price": "30"})
    assert r.status_code == 201, r.text
    conn = _conn_of(client)
    bundle = load_ledger_bundle(conn)
    day = date(2025, 1, 2)
    cut = replace(bundle, transactions=[t for t in bundle.transactions if t.trade_date <= day],
                  dividends=[d for d in bundle.dividends if d.effective_date < day],
                  opening=[o for o in bundle.opening if o.build_date <= day],
                  actions=[a for a in bundle.actions if a.date <= day])
    held = next(h for h in build_book(cut, allow_oversell=True).holdings
                if h.symbol == "2884")
    assert D(str(pp["new_shares"])) == held.shares
    assert D(str(pp["new_original_avg"])) == held.original_avg
    assert D(str(pp["new_adjusted_avg"])) == held.adjusted_avg


def _conn_of(client: TestClient) -> sqlite3.Connection:
    from portfolio_dash.api.deps import get_conn
    override = client.app.dependency_overrides[get_conn]  # type: ignore[attr-defined]
    conn = override()
    assert isinstance(conn, sqlite3.Connection)
    return conn
