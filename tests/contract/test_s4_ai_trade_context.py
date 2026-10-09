"""S4 + S10 (pre-merge review round 3, 2026-10-04): re-judging an AI dividend draft against the
paste's trades takes a MINIMAL context — the ticked trades' account / symbol / date — and the
server turns it into the same ``pending_trades`` the first preview builds, without previewing
the trades again.

R3 sent every trade draft along (ticked or not, and all of them dropped when one was
malformed), and each debounced dividend edit re-ran the whole sibling-aware trade preview just
to rebuild that map. ``POST /api/input/ai/preview`` now accepts ``trade_context`` beside
``drafts``; the server remains the one owner of the rule (``agents._build_result`` →
``validate_dividend(pending_trades=)`` → ``holdings.position_exists_by``).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

_DIV = {"kind": "div", "account_id": "tw_broker", "symbol": "2454", "date": "2026-05-15",
        "type": "CASH", "gross": "1000"}


def _register(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="2454", market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="Semiconductors", name="MediaTek", board="TWSE"))
    conn.commit()


def _row(api_client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    r = api_client.post("/api/input/ai/preview", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert "transactions" not in out["previews"], out["previews"].keys()   # no trade preview
    (row,) = out["previews"]["dividends"]["rows"]
    result: dict[str, Any] = row
    return result


def test_a_ticked_buy_in_the_context_is_the_dividends_position(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _register(golden_db)
    row = _row(api_client, {"drafts": {"rows": [_DIV]}, "trade_context": [
        {"account_id": "tw_broker", "symbol": "2454", "date": "2026-05-01"}]})
    assert "dividend_without_position" not in row["kinds"], row


def test_a_context_dated_after_the_dividend_does_not_cover_it(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    _register(golden_db)
    row = _row(api_client, {"drafts": {"rows": [_DIV]}, "trade_context": [
        {"account_id": "tw_broker", "symbol": "2454", "date": "2026-05-20"}]})
    assert row["status"] == "error" and "dividend_without_position" in row["kinds"], row


def test_no_context_is_no_position(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """An unticked buy is simply not in the context — the dividend is judged on the ledger."""
    _register(golden_db)
    row = _row(api_client, {"drafts": {"rows": [_DIV]}, "trade_context": []})
    assert row["status"] == "error" and "dividend_without_position" in row["kinds"], row
