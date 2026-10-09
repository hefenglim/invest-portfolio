"""F-02 counter-evidence: an oversell ANYWHERE must not silently blank EVERY symbol's 試算.

``compute_whatif`` builds the book strictly (``allow_oversell=False``) on purpose — a 試算
must not quote a price off a book whose basis was discarded. The strictness is right; what
was missing is the other half of the repair this seam already recorded for itself. The
comment in ``whatif.py`` says the 2026-08-11 fix was "to degrade with the reason, not to
relax the strictness", and only the degrade-from-500-to-400 half ever shipped.

The reason was computed, put into the exception's message, and then discarded twice:

* the router answered ``field="account_id"`` — a field that has nothing to do with the
  problem and is not even wrong about the same symbol;
* the drawer's ``.catch`` ignored the message entirely and printed a bare 「試算暫不可用」.

Blast radius, measured by the 2026-08-27 sweep: ONE unresolved position in ONE account
blanks the drawer for every symbol, in every account, in every market — and no screen ever
says which position to go and fix. Meanwhile the KPI band's XIRR, looking at the same book,
says 「帳本中有賣超部位待釐清」 out loud. One app, two standards.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side

_HEALTHY = {"symbol": "2330", "side": "sell", "shares": "100", "price": "600",
            "account_id": "tw_broker"}


def _oversell_aapl(conn: sqlite3.Connection) -> None:
    """schwab holds 10 AAPL; sell 50 of them, undeclared. A different account, a different
    market, a different currency from the symbol the drawer is about to ask about."""
    insert_transaction(conn, account_id="schwab", symbol="AAPL", side=Side.SELL,
                       quantity=Decimal("50"), price=Decimal("130"), fees=Decimal("0"),
                       tax=Decimal("0"), trade_date=date(2026, 5, 4))
    conn.commit()


def test_a_healthy_symbol_simulates_before_the_oversell(api_client: TestClient) -> None:
    """The control arm: without it, a 400 below could mean the request was simply wrong."""
    assert api_client.post("/api/whatif", json=_HEALTHY).status_code == 200


def test_an_oversell_in_another_account_names_the_blocking_position(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _oversell_aapl(golden_db)
    r = api_client.post("/api/whatif", json=_HEALTHY)
    assert r.status_code == 400
    err = r.json()["error"]
    assert "AAPL" in err["message"], f"the message does not name the blocker: {err['message']}"
    assert "schwab" in err["message"], f"the message does not name the account: {err['message']}"


def test_the_blocking_position_is_machine_readable_not_only_prose(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """So a UI can offer a link to the offending row instead of parsing a sentence."""
    _oversell_aapl(golden_db)
    err = api_client.post("/api/whatif", json=_HEALTHY).json()["error"]
    issues = err.get("issues") or []
    blocking = [i for i in issues if i.get("code") == "oversold_position"]
    assert blocking, f"no structured blocking issue: {issues}"
    assert blocking[0]["symbol"] == "AAPL"
    assert blocking[0]["account_id"] == "schwab"


def test_the_error_no_longer_blames_an_unrelated_field(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """``field`` marks the input the user must change. The account_id they sent is fine."""
    _oversell_aapl(golden_db)
    err = api_client.post("/api/whatif", json=_HEALTHY).json()["error"]
    assert err.get("field") != "account_id"


def test_a_genuinely_unheld_symbol_still_blames_account_id(api_client: TestClient) -> None:
    """The OTHER WhatIfError — "cannot infer account" — really is about account_id, and the
    fix above must not blanket-strip the field from a case where it is correct."""
    r = api_client.post("/api/whatif", json={
        "symbol": "NVDA", "side": "buy", "shares": "1", "price": "100"})
    assert r.status_code == 400
    assert r.json()["error"].get("field") == "account_id"
