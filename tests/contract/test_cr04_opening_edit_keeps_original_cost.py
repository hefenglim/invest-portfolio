"""CR-04 (pre-merge review, 2026-10-04): an opening correction never rewrites the original cost
from an average.

The 期初 edit modal sent the legacy ``avg`` field — the server's own ``total / shares``, a
28-digit quotient — and never sent ``total``, so ``edit_opening`` stored ``total = avg × shares``.
Measured: an opening of 3 shares and 100,000 with only its build date edited came back with
``original_cost_total`` 99999.99999999999999999999999, and changing the share count alone scaled
the original cost with it. CLAUDE.md invariant 7: original cost is never overwritten.

The rule now (domain-ledger.md: the total is the authority; project the totals, divide on read):

* ``total`` (原始總成本) is the only figure this door stores as cost;
* omitted = the stored total is KEPT — a 股數 or 建檔日 correction leaves the original cost
  exactly as it was, and the average follows on read;
* ``avg`` is refused, loudly, in zh: a door that derived the total from it is the defect.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import get_opening, upsert_instrument, upsert_opening
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import DashboardClientFactory

_PATH = "/api/ledgers/openings/tw_broker/2330"


def _seed(conns: list[sqlite3.Connection]) -> Any:
    def seed(conn: sqlite3.Connection) -> None:
        seed_accounts(conn)
        upsert_instrument(conn, Instrument(symbol="2330", market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Semiconductors",
                                           name="TSMC", board="TWSE"))
        upsert_opening(conn, account_id="tw_broker", symbol="2330", shares=Decimal("3"),
                       original_cost_total=Decimal("100000"), build_date=date(2026, 1, 2))
        conns.append(conn)
    return seed


def _row(client: TestClient) -> dict[str, Any]:
    (row,) = client.get("/api/ledgers/openings").json()["rows"]
    out: dict[str, Any] = row
    return out


def test_a_date_only_edit_keeps_the_original_cost_exactly(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    conns: list[sqlite3.Connection] = []
    client = dashboard_client_factory(_seed(conns))
    r = client.put(_PATH, json={"shares": "3", "date": "2026-01-03"})
    assert r.status_code == 200, r.text
    stored = get_opening(conns[0], "tw_broker", "2330")
    assert stored is not None
    assert stored.original_cost_total == Decimal("100000")
    assert str(stored.original_cost_total) == "100000"          # not 99999.999…, not 1.00000E+5
    assert stored.build_date == date(2026, 1, 3)
    assert _row(client)["total"] == "100000"


def test_a_shares_only_edit_keeps_the_total_and_the_average_follows_on_read(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    conns: list[sqlite3.Connection] = []
    client = dashboard_client_factory(_seed(conns))
    r = client.put(_PATH, json={"shares": "4", "date": "2026-01-02"})
    assert r.status_code == 200, r.text
    row = _row(client)
    assert (row["shares"], row["total"], row["avg"]) == ("4", "100000", "25000")


def test_an_explicit_total_is_the_one_way_to_change_the_cost(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    conns: list[sqlite3.Connection] = []
    client = dashboard_client_factory(_seed(conns))
    r = client.put(_PATH, json={"shares": "3", "total": "90000", "date": "2026-01-02"})
    assert r.status_code == 200, r.text
    assert _row(client)["total"] == "90000"


def test_a_blank_total_keeps_the_stored_one(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """R7 (round 2): the refusal sentence promises 「或不填，保留原值」, and the dialog sends the
    field's value — so a CLEARED 原始總成本 arrives as ``""``. It was a 400 (Pydantic's
    decimal_parsing); blank now means "not stated", exactly like an omitted key."""
    conns: list[sqlite3.Connection] = []
    client = dashboard_client_factory(_seed(conns))
    for blank in ("", "   "):
        r = client.put(_PATH, json={"shares": "3", "total": blank, "date": "2026-01-04"})
        assert r.status_code == 200, (blank, r.text)
        assert _row(client)["total"] == "100000"
    assert _row(client)["date"] == "2026-01-04"


def test_the_legacy_avg_field_is_refused_and_nothing_is_written(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """Exactly what the old modal sent: the server's own quotient, echoed back."""
    conns: list[sqlite3.Connection] = []
    client = dashboard_client_factory(_seed(conns))
    before = _row(client)
    r = client.put(_PATH, json={"shares": before["shares"], "avg": before["avg"],
                                "date": "2026-01-03"})
    assert r.status_code == 400, r.text
    err = r.json()["error"]
    assert err["code"] == "validation_error" and err.get("field") == "avg"
    assert "原始總成本" in err["message"] and "均價" in err["message"], err
    stored = get_opening(conns[0], "tw_broker", "2330")
    assert stored is not None
    assert stored.original_cost_total == Decimal("100000")
    assert stored.build_date == date(2026, 1, 2)
