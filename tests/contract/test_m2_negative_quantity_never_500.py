"""QA-12 (API side) — a negative-quantity row must never produce a 500, on any door.

The row is only reachable by editing the database (manual/commit -> 400,
``PUT /api/ledgers/transactions/{id}`` -> 400, CSV import -> per-row error), so it is
inserted here by direct SQL, exactly as ``evidence/s8_negqty.py`` does.

Two doors, ONE answer (I-3, 2026-08-29 — this file's second version):

* ``POST /api/actions/recompute`` (重算, STRICT) — 422 ``unbookable_ledger`` carrying the
  replay's own sentence, through the catch that has been in ``api/routers/actions.py`` since
  the dividend-on-short refusal;
* ``GET /api/dashboard`` (``allow_oversell=True``) — **also** 422 ``unbookable_ledger``, and
  the same sentence. Wave 1 skipped the row here instead, because at that time an
  ``UnbookableLedgerError`` escaping ``build_dashboard`` reached ``api/errors.py``'s catch-all
  as a **500** (measured), and a silent skip beat taking the page down. Wave 3 registered the
  handler for that error class, so 422 is now available — and 422 with the row named beats a
  skip the dashboard has no channel to report (``Book`` carries a refusal list for corporate
  actions and for dividends, but none for a transaction).

Never-500 means "never an internal error", NOT "always 200": the rule is that the owner is
told which row to fix, in their own language, on whichever surface they were using.
"""

import re
import sqlite3
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def _plant_negative_sell(conn: sqlite3.Connection) -> None:
    """A -100 SELL of the golden 2330 position, on 2026-02-01 (before its dividend)."""
    conn.execute(
        "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax,"
        " trade_date) VALUES ('tw_broker','2330','SELL','-100','600','0','0','2026-02-01')")
    conn.commit()


def _no_raise(api_client: TestClient) -> TestClient:
    """Server exceptions become responses, so a 500 is asserted as a STATUS rather than
    re-raised into the test as an error."""
    return TestClient(api_client.app, raise_server_exceptions=False)


def test_the_dashboard_answers_422_naming_the_row_never_500(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """★ The half wave 1 could not deliver. Booking the -100 SELL literally gave 1,100 shares
    / 550,000 / 545,000 on this exact fixture (the golden 2330 buy is 1,000 @500 with no fees
    -> 500,000 original, less a 5,000 cash dividend -> 495,000 adjusted); skipping it gave the
    right numbers with no way to say a row had been dropped. Refusing says so."""
    _plant_negative_sell(golden_db)
    response = _no_raise(api_client).get("/api/dashboard")
    assert response.status_code == 422, f"HTTP {response.status_code}: {response.text[:300]}"
    err: dict[str, Any] = response.json()["error"]
    assert err["code"] == "unbookable_ledger"
    assert "2330" in str(err["message"]) and "2026-02-01" in str(err["message"])
    assert _CJK.search(str(err["message"])), err["message"]


def test_recompute_refuses_with_the_same_envelope(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """重算 rebuilds every figure from the ledgers, so it must not run over a row the replay
    cannot book — and it must not describe that row differently from the dashboard."""
    _plant_negative_sell(golden_db)
    r = api_client.post("/api/actions/recompute", json={})
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "unbookable_ledger"
    assert "2330" in err["message"] and "2026-02-01" in err["message"]
    assert _CJK.search(str(err["message"])), err["message"]


def test_the_two_doors_say_exactly_the_same_thing(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """One owner (``build_book``) writes the sentence, so neither door can drift from it."""
    _plant_negative_sell(golden_db)
    client = _no_raise(api_client)
    dashboard = client.get("/api/dashboard").json()["error"]["message"]
    recompute = client.post("/api/actions/recompute", json={}).json()["error"]["message"]
    assert dashboard == recompute


def test_a_clean_ledger_recomputes_and_values_exactly_as_before(
    api_client: TestClient,
) -> None:
    """Control: with no negative row, both doors are untouched — 1,000 shares, 500,000
    original, 495,000 adjusted after the golden 5,000 cash dividend."""
    assert api_client.post("/api/actions/recompute", json={}).status_code == 200
    r = api_client.get("/api/dashboard")
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}"
    (holding,) = [h for h in r.json()["holdings"] if h["symbol"] == "2330"]
    assert Decimal(str(holding["shares"])) == Decimal("1000")
    assert Decimal(str(holding["original_cost_total"])) == Decimal("500000")
    assert Decimal(str(holding["adjusted_cost_total"])) == Decimal("495000")
