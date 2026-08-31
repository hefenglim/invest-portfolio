"""I-1 (door side) — every door answers 4xx over an uncomputable row, not just the dashboard.

Wave 3 planted ONE row by direct SQL (``quantity='1E+999999'``, which overflows the Decimal
context at ``ev.quantity * ev.price``) and measured all four doors:

    GET  /api/dashboard          422   (portfolio/dashboard.py re-typed the fault)
    POST /api/actions/recompute  500
    POST /api/export/tax-package 500
    POST /api/whatif             500

The three 500s were not three bugs. ``decimal.Overflow`` is an ``ArithmeticError`` but not a
``ValueError``, and it is a SIBLING of ``InvalidOperation`` rather than a subclass, so the
strict doors' ``except (UnbookableLedgerError, OversellError, InvalidOperation)`` never saw
it — while ``portfolio/dashboard.py`` happened to hold the only re-typing in the codebase.
The fix moves that re-typing into ``build_book`` itself, so the doors inherit it instead of
each having to remember it (three of four had not).

The row is reachable only by editing the database: manual/commit, ``PUT /api/ledgers/
transactions/{id}`` and the CSV import all validate. That is exactly why it belongs to the
never-500 rule rather than to input validation — the guarantee is that a corrupt ledger is
survivable and NAMED, on every surface, not that it is impossible.
"""

import re
import sqlite3
from typing import Any

from fastapi.testclient import TestClient

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")

_WHATIF_BODY = {"symbol": "2330", "side": "buy", "shares": "100", "price": "600",
                "account_id": "tw_broker"}


def _plant_overflow_row(conn: sqlite3.Connection) -> None:
    """A golden-ledger row whose ``quantity × price`` overflows the Decimal context."""
    conn.execute(
        "INSERT INTO transactions (account_id, symbol, side, quantity, price, fees, tax,"
        " trade_date) VALUES ('tw_broker','2330','BUY','1E+999999','600','0','0',"
        "'2026-02-01')")
    conn.commit()


def _client(api_client: TestClient) -> TestClient:
    """Server exceptions become responses, so a 500 is asserted as a STATUS rather than
    re-raised into the test as an error (which is how these three hid)."""
    return TestClient(api_client.app, raise_server_exceptions=False)


def _error(response: Any) -> dict[str, Any]:
    body: dict[str, Any] = response.json()["error"]
    return body


# --- the three doors that used to 500 ---------------------------------------------------

def test_recompute_answers_422_naming_the_row(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """重算 rebuilds every figure from the ledgers, so it is the door that MUST be loud."""
    _plant_overflow_row(golden_db)
    response = _client(api_client).post("/api/actions/recompute", json={})
    assert response.status_code == 422, f"HTTP {response.status_code}: {response.text[:300]}"
    err = _error(response)
    assert err["code"] == "unbookable_ledger"
    assert _CJK.search(str(err["message"])), err["message"]
    assert "2330" in str(err["message"]) and "2026-02-01" in str(err["message"])


def test_the_tax_package_answers_4xx_not_500(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The tax package's replay is strict on purpose (a package must not omit a sale), so it
    degrades with the REASON — the same posture QA-06 gave it for 賣超."""
    _plant_overflow_row(golden_db)
    response = _client(api_client).post("/api/export/tax-package", json={"year": 2026})
    assert response.status_code == 422, f"HTTP {response.status_code}: {response.text[:300]}"
    err = _error(response)
    assert err["code"] == "unbookable_ledger"
    assert _CJK.search(str(err["message"])), err["message"]


def test_whatif_answers_4xx_not_500(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The drawer posts 試算 on open, so one uncomputable row 500'd EVERY symbol's drawer —
    the same blast radius the 2026-08-11 oversell fix found at this seam."""
    _plant_overflow_row(golden_db)
    response = _client(api_client).post("/api/whatif", json=_WHATIF_BODY)
    assert 400 <= response.status_code < 500, (
        f"HTTP {response.status_code}: {response.text[:300]}")
    assert _CJK.search(str(_error(response)["message"])), response.text[:300]


def test_the_dashboard_still_answers_422(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The door that already worked: moving the re-typing into ``build_book`` must not cost
    the dashboard its degradation (it is the page the owner lives on)."""
    _plant_overflow_row(golden_db)
    response = _client(api_client).get("/api/dashboard")
    assert response.status_code == 422, f"HTTP {response.status_code}: {response.text[:300]}"
    assert _error(response)["code"] == "unbookable_ledger"


# --- control ----------------------------------------------------------------------------

def test_every_door_is_untouched_on_a_clean_ledger(api_client: TestClient) -> None:
    """The four doors, same session, no planted row: nothing about the golden answers moves."""
    client = _client(api_client)
    assert client.get("/api/dashboard").status_code == 200
    assert client.post("/api/actions/recompute", json={}).status_code == 200
    assert client.post("/api/whatif", json=_WHATIF_BODY).status_code == 200
    assert client.post("/api/export/tax-package", json={"year": 2026}).status_code == 200
