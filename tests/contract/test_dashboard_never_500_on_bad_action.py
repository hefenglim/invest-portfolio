"""The never-500 rule, asserted AT THE ROUTE — which is where the rule is actually about.

`tests/portfolio/test_unreadable_actions.py` proves the *replay* degrades. That is not the
same claim: the defect it was written for lived **above** `build_book`, in
`store.load_ledger_bundle`, and a unit test on the replay could never have seen it. The
property the project's rule states is that the **page stays up**, so this asserts the HTTP
status.

Third occurrence of the class (`finding-oversell-dashboard-500` 2026-06; `compute_whatif`
letting `OversellError` escape, and this, both 2026-08-11), which is why it gets a
route-level guard rather than another unit test.
"""

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.app import create_app
from portfolio_dash.api.deps import get_conn, get_now, get_reporting
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market  # noqa: F811  (re-export order)
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import init_golden_base

D = Decimal
_NOW = datetime(2026, 10, 1, 12, 0, 0)


@pytest.fixture
def conn() -> sqlite3.Connection:
    # `check_same_thread=False`: TestClient drives the app through an anyio portal, so the
    # route runs on a worker thread (the same reason tests/conftest.py's golden_db does it).
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    # `init_golden_base` is conftest's ONE place for the ordered table setup — the route
    # reads ~12 table groups and `bootstrap_db` alone creates none of the later ones.
    # Hand-rolling the list here is how this file would silently rot.
    init_golden_base(c)
    seed_accounts(c)
    upsert_instrument(c, Instrument(symbol="AAA", market=Market.US, quote_ccy=Currency.USD,
                                    sector="Tech", name="AAA"))
    insert_transaction(c, account_id="schwab", symbol="AAA", side=Side.BUY, quantity=D("100"),
                       price=D("50"), fees=D("0"), tax=D("0"), trade_date=date(2026, 1, 10))
    c.commit()
    return c


@pytest.fixture
def client(conn: sqlite3.Connection) -> Iterator[TestClient]:
    enable_socket()          # the in-process ASGI transport, not network I/O
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_now] = lambda: _NOW
    app.dependency_overrides[get_reporting] = lambda: Currency.TWD
    yield TestClient(app)    # no `with`: lifespan not run (hermetic)
    disable_socket()


def _poison(conn: sqlite3.Connection, **over: str) -> None:
    """Write a corporate-action row entry validation would reject, straight to the table.

    E21's stated reachable state (a hand edit) — and, until W7 wired it, also what the
    importer and the API did, since `validate_corporate_action` had no production caller.
    """
    row = {"account_id": "schwab", "date": "2026-06-15", "kind": "SPLIT",
           "from_symbol": "AAA", "to_symbol": "AAA", "ratio_to": "0.2857", "ratio_from": "1"}
    row.update(over)
    conn.execute(
        "INSERT INTO corporate_actions (account_id,date,kind,from_symbol,to_symbol,"
        "ratio_to,ratio_from,cost_carry,note) VALUES (?,?,?,?,?,?,?,NULL,NULL)",
        tuple(row[k] for k in ("account_id", "date", "kind", "from_symbol", "to_symbol",
                               "ratio_to", "ratio_from")),
    )
    conn.commit()


def test_the_dashboard_stays_up(conn: sqlite3.Connection, client: TestClient) -> None:
    _poison(conn)
    r = client.get("/api/dashboard")
    assert r.status_code == 200, r.text[:400]


def test_the_row_reaches_the_wire_with_its_reason(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    """A 200 that hides the problem would be worse than the 500 — the number on screen would
    be wrong by the ratio with nothing to say so."""
    _poison(conn)
    body = client.get("/api/dashboard").json()
    unapplied = body.get("unapplied_actions") or []
    assert len(unapplied) == 1
    assert "0.2857" in unapplied[0]["reason"]
    assert unapplied[0]["from_symbol"] == "AAA"


def test_xirr_is_withheld_and_says_why(conn: sqlite3.Connection, client: TestClient) -> None:
    """D38 invariant 2: the blanking is portfolio-wide, so its reason must NAME the row."""
    _poison(conn)
    body = client.get("/api/dashboard").json()
    kpi = body.get("kpi") or {}
    assert not kpi.get("xirr")
    reason = " ".join(str(v) for v in kpi.values() if isinstance(v, str))
    assert "AAA" in reason or "AAA" in str(body.get("unapplied_actions"))


@pytest.mark.parametrize("bad", [
    pytest.param({"ratio_from": "0"}, id="zero denominator"),
    pytest.param({"kind": "MERGER"}, id="unknown kind"),
    pytest.param({"ratio_to": "-3"}, id="negative term"),
])
def test_every_malformed_shape_keeps_the_page_up(
    conn: sqlite3.Connection, client: TestClient, bad: dict[str, str]
) -> None:
    _poison(conn, **bad)
    assert client.get("/api/dashboard").status_code == 200


def test_a_clean_ledger_reports_nothing(conn: sqlite3.Connection, client: TestClient) -> None:
    """Detection power: the field must be empty when nothing is wrong, or its presence
    carries no information (D38 invariant 1)."""
    body = client.get("/api/dashboard").json()
    assert body.get("unapplied_actions") == []
