"""CR-08 (pre-merge review, 2026-10-04): the ledger 更正 door discloses an unknown ETF flag.

``markets-and-fees.md`` (AI-D40): "an unknown rate is disclosed, never defaulted in silence".
The manual door and the CSV door resolve the registry's three-state ETF flag through
``fees.resolve_etf_flag`` and raise ``etf_flag_unknown`` on a TW SELL whose tax the flag moves,
and they stamp ``fee_rule_snapshot["etf_flag"] = "unknown"`` so a later audit of the row can see
the rate was assumed. ``PUT /api/ledgers/transactions/{id}`` recomputed the fee/tax with
``is_etf=inst.is_etf`` and neither: a TW sell edited on an instrument nobody had answered was
taxed at the 現股 0.3% with no finding and a snapshot that reads as a fact.

Pinned through both doors of the edit (the modal's live preview and the PUT), against the
manual door's own answer for the same trade, plus the narrow scope (a BUY, an answered flag,
an edit that recomputes nothing).
"""

from __future__ import annotations

import json
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

D = Decimal
SYM = "00999"


def _seed(conn: sqlite3.Connection, ids: dict[str, int], *, unknown: bool = True) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol=SYM, market=Market.TW, quote_ccy=Currency.TWD,
                                       sector="ETF", name="某檔 ETF", board="TWSE",
                                       is_etf=False, etf_flag_unknown=unknown))
    insert_transaction(conn, account_id="tw_broker", symbol=SYM, side=Side.BUY,
                       quantity=D("1000"), price=D("50"), fees=D("71"), tax=D("0"),
                       trade_date=date(2026, 1, 5))
    ids["sell"] = insert_transaction(
        conn, account_id="tw_broker", symbol=SYM, side=Side.SELL, quantity=D("500"),
        price=D("60"), fees=D("42"), tax=D("90"), trade_date=date(2026, 2, 2))
    conns.append(conn)


conns: list[sqlite3.Connection] = []


def _edit(shares: str, *, side: str = "SELL") -> dict[str, Any]:
    """The edit modal's PUT once a core field moved and fee/tax were not typed by hand."""
    return {"account_id": "tw_broker", "symbol": SYM, "side": side, "date": "2026-02-02",
            "shares": shares, "price": "60", "fee": "42", "tax": "90",
            "fee_overridden": False, "tax_overridden": False}


def _codes(issues: list[dict[str, Any]]) -> list[str]:
    return [str(i["code"]) for i in issues]


def _snapshot(conn: sqlite3.Connection, txn_id: int) -> dict[str, str]:
    raw = conn.execute("SELECT fee_rule_snapshot FROM transactions WHERE id=?",
                       (txn_id,)).fetchone()[0]
    snap: dict[str, str] = json.loads(raw) if raw else {}
    return snap


def test_a_recomputed_tw_sell_edit_raises_etf_flag_unknown_like_the_entry_door(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    ids: dict[str, int] = {}
    client = dashboard_client_factory(lambda c: _seed(c, ids))
    conn = conns[-1]

    # The manual door's answer for the same trade — the reference this door must match.
    entry = client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": SYM, "side": "SELL", "date": "2026-02-02",
        "shares": "400", "price": "60"})
    assert "etf_flag_unknown" in _codes(entry.json()["issues"])

    # The edit modal's live preview (``replaces_txn_id``) already went through that door.
    preview = client.post("/api/input/manual/preview", json={
        "account_id": "tw_broker", "symbol": SYM, "side": "SELL", "date": "2026-02-02",
        "shares": "400", "price": "60", "replaces_txn_id": ids["sell"]})
    assert "etf_flag_unknown" in _codes(preview.json()["issues"])

    r = client.put(f"/api/ledgers/transactions/{ids['sell']}", json=_edit("400"))
    assert r.status_code == 200, r.json()
    body = r.json()
    assert "etf_flag_unknown" in _codes(body["issues"]), body["issues"]
    issue = next(i for i in body["issues"] if i["code"] == "etf_flag_unknown")
    assert issue["sev"] == "warn"
    assert issue["text"] == next(i["text"] for i in entry.json()["issues"]
                                 if i["code"] == "etf_flag_unknown")
    # The same number the entry door computes (the 現股 rate, assumed) — and said to be assumed.
    assert body["tax"] == entry.json()["tax"]
    assert _snapshot(conn, ids["sell"]).get("etf_flag") == "unknown"


def test_the_disclosure_stays_narrow(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """No noise: a BUY carries no TW tax; an edit that recomputes nothing keeps the stored
    tax; an answered flag has nothing to disclose."""
    ids: dict[str, int] = {}
    client = dashboard_client_factory(lambda c: _seed(c, ids))
    conn = conns[-1]
    # Nothing core moved → no recompute → the stored tax stands, nothing assumed.
    same = client.put(f"/api/ledgers/transactions/{ids['sell']}", json=_edit("500"))
    assert same.status_code == 200, same.json()
    assert "etf_flag_unknown" not in _codes(same.json()["issues"])
    assert "etf_flag" not in _snapshot(conn, ids["sell"])
    # Both figures typed by hand → nothing computed → nothing assumed.
    typed = client.put(f"/api/ledgers/transactions/{ids['sell']}", json={
        **_edit("450"), "fee_overridden": True, "tax_overridden": True})
    assert typed.status_code == 200, typed.json()
    assert "etf_flag_unknown" not in _codes(typed.json()["issues"])

    ids2: dict[str, int] = {}
    client2 = dashboard_client_factory(lambda c: _seed(c, ids2, unknown=False))
    answered = client2.put(f"/api/ledgers/transactions/{ids2['sell']}", json=_edit("400"))
    assert answered.status_code == 200, answered.json()
    assert "etf_flag_unknown" not in _codes(answered.json()["issues"])
    assert "etf_flag" not in _snapshot(conns[-1], ids2["sell"])


def test_a_buy_edit_stays_silent_but_its_snapshot_still_records_the_assumption(
    dashboard_client_factory: DashboardClientFactory,
) -> None:
    """The manual door's posture exactly: a BUY carries no TW transaction tax, so the flag
    moves nothing and no finding is raised — but the regime it was computed under still
    records that the flag was unanswered."""
    ids: dict[str, int] = {}
    client = dashboard_client_factory(lambda c: _seed(c, ids))
    conn = conns[-1]
    buy_id = conn.execute("SELECT id FROM transactions WHERE side='BUY'").fetchone()[0]
    r = client.put(f"/api/ledgers/transactions/{buy_id}", json={
        **_edit("900", side="BUY"), "date": "2026-01-05", "price": "50"})
    assert r.status_code == 200, r.json()
    assert "etf_flag_unknown" not in _codes(r.json()["issues"])
    assert _snapshot(conn, buy_id).get("etf_flag") == "unknown"
