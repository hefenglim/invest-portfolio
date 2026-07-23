"""Contract: 待確認退款（折讓款） API (Wave B, FE-D1).

Golden clock GOLDEN_NOW = 2026-06-11. Seeds a fee-bearing tw_broker trade in 2026-05 so
that month is PENDING; drives the inbox endpoints. Confirm books a cash-pool CREDIT
(movement kind ``rebate``) with an EDITABLE amount — the estimate is never money of record
(FE-D1). Endpoints are ungated in guest mode, matching the dividend-inbox siblings.
"""

import sqlite3
from datetime import date
from decimal import Decimal
from typing import cast

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.shared.models.enums import Side

_TW = "tw_broker"


def _seed_may_trade(conn: sqlite3.Connection, fee: str = "142") -> None:
    insert_transaction(
        conn, account_id=_TW, symbol="2330", side=Side.BUY, quantity=Decimal("1000"),
        price=Decimal("500"), fees=Decimal(fee), tax=Decimal("0"),
        trade_date=date(2026, 5, 5))


def _rows(api_client: TestClient) -> list[dict[str, object]]:
    r = api_client.get("/api/rebates")
    assert r.status_code == 200
    return list(r.json()["rows"])


def _balance(api_client: TestClient, account_id: str, ccy: str) -> str | None:
    body = api_client.get("/api/cash").json()
    row = next((b for b in body["balances"]
                if b["account_id"] == account_id and b["ccy"] == ccy), None)
    return row["amount"] if row else None


def test_list_shape_and_decimal_strings(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_may_trade(golden_db)
    body = api_client.get("/api/rebates").json()
    # owner #1 added the accruing field (current / not-yet-due forecast) to the envelope.
    assert set(body.keys()) == {"rows", "total_count", "accruing", "skipped"}
    hit = next(r for r in body["rows"] if r["account_id"] == _TW and r["month"] == "2026-05")
    assert hit["account_name"] == "TW Broker"
    assert hit["trade_count"] == 1
    # money/forecast are Decimal STRINGS: fee_total 142, expected floor(142×0.77)=109
    assert hit["fee_total"] == "142" and hit["expected"] == "109"
    assert hit["ccy"] == "TWD"
    # FU-D6: the month carries a per-trade breakdown that sums to its own totals
    assert len(hit["trades"]) == 1
    # the only seeded trade (2026-05) is PENDING under GOLDEN_NOW=2026-06 → nothing accruing.
    assert body["total_count"] >= 1 and body["skipped"] == [] and body["accruing"] == []


def test_trades_breakdown_shape_and_sum_invariants(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """FU-D6: rows carry a per-trade breakdown ordered by trade_date; Σ == month totals."""
    insert_transaction(
        golden_db, account_id=_TW, symbol="2330", side=Side.BUY, quantity=Decimal("1000"),
        price=Decimal("500"), fees=Decimal("142"), tax=Decimal("0"),
        trade_date=date(2026, 5, 5))
    insert_transaction(
        golden_db, account_id=_TW, symbol="2330", side=Side.SELL, quantity=Decimal("1000"),
        price=Decimal("520"), fees=Decimal("156"), tax=Decimal("0"),
        trade_date=date(2026, 5, 20))
    hit = next(r for r in _rows(api_client)
               if r["account_id"] == _TW and r["month"] == "2026-05")
    trades = cast(list[dict[str, str]], hit["trades"])
    assert len(trades) == 2
    for t in trades:
        assert set(t.keys()) == {"trade_date", "symbol", "name", "side", "fee", "expected"}
    # ordered by trade_date ascending
    assert [t["trade_date"] for t in trades] == ["2026-05-05", "2026-05-20"]
    # instrument display name resolved once (2330 -> TSMC); side is the enum's wire value
    assert trades[0]["name"] == "TSMC" and trades[0]["symbol"] == "2330"
    assert trades[0]["side"] == "BUY" and trades[1]["side"] == "SELL"
    # money is Decimal STRINGS; per-trade floor(fee×0.77): 142->109, 156->120
    assert trades[0]["fee"] == "142" and trades[0]["expected"] == "109"
    assert trades[1]["fee"] == "156" and trades[1]["expected"] == "120"
    # INVARIANT: Σ trade.fee == month fee_total; Σ trade.expected == month expected
    assert sum((Decimal(t["fee"]) for t in trades), Decimal("0")) == Decimal(str(hit["fee_total"]))
    assert (sum((Decimal(t["expected"]) for t in trades), Decimal("0"))
            == Decimal(str(hit["expected"])))
    assert hit["fee_total"] == "298" and hit["expected"] == "229"


def test_trade_name_falls_back_to_symbol(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """A traded symbol with no instrument row shows the symbol itself as the name."""
    insert_transaction(
        golden_db, account_id=_TW, symbol="9999", side=Side.BUY, quantity=Decimal("100"),
        price=Decimal("10"), fees=Decimal("100"), tax=Decimal("0"),
        trade_date=date(2026, 5, 5))
    hit = next(r for r in _rows(api_client)
               if r["account_id"] == _TW and r["month"] == "2026-05")
    trades = cast(list[dict[str, str]], hit["trades"])
    t = next(x for x in trades if x["symbol"] == "9999")
    assert t["name"] == "9999"


def test_count_endpoint(api_client: TestClient, golden_db: sqlite3.Connection) -> None:
    assert api_client.get("/api/rebates/count").json()["count"] == 0
    _seed_may_trade(golden_db)
    assert api_client.get("/api/rebates/count").json()["count"] == 1


def test_confirm_books_editable_credit_and_suppresses(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_may_trade(golden_db)  # estimate = 109
    before = _balance(api_client, _TW, "TWD")
    # ACTUAL wins: confirm a DIFFERENT amount than the 109 estimate (the estimate is a prefill).
    r = api_client.post("/api/rebates/confirm",
                        json={"account_id": _TW, "month": "2026-05", "amount": "150"})
    assert r.status_code == 200
    resp = r.json()
    assert resp["month"] == "2026-05" and resp["amount"] == "150" and resp["ccy"] == "TWD"
    # a rebate cash movement landed (credit), note carries the deterministic month tag
    movements = api_client.get("/api/cash").json()["movements"]["rows"]
    mv = next(m for m in movements if m["id"] == resp["id"])
    assert mv["kind"] == "rebate" and mv["amount"] == "150"
    assert mv["note"] == "2026-05 折讓款" and mv["ccy"] == "TWD"
    # the pool is credited by the ACTUAL amount (deposit-like), not the estimate
    after = Decimal(_balance(api_client, _TW, "TWD") or "0")
    assert after == Decimal(before or "0") + Decimal("150")
    # the month leaves the inbox (suppressed by its own movement tag) — self-healing
    assert all(x["month"] != "2026-05" for x in _rows(api_client) if x["account_id"] == _TW)
    # double-confirm is now a no-op (month no longer pending) -> 400
    r2 = api_client.post("/api/rebates/confirm",
                         json={"account_id": _TW, "month": "2026-05", "amount": "150"})
    assert r2.status_code == 400 and r2.json()["error"]["field"] == "month"


def test_accruing_month_listed_but_not_confirmable(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """owner #1: a CURRENT-month trade surfaces as an accruing (未到期) forecast, NOT a
    pending/confirmable item — it is not counted in the badge and confirming it is rejected
    (the refund is not yet due). GOLDEN_NOW=2026-06 → a 2026-06 trade is the current month."""
    insert_transaction(
        golden_db, account_id=_TW, symbol="2330", side=Side.BUY, quantity=Decimal("1000"),
        price=Decimal("500"), fees=Decimal("142"), tax=Decimal("0"),
        trade_date=date(2026, 6, 5))
    body = api_client.get("/api/rebates").json()
    acc = next(a for a in body["accruing"]
               if a["account_id"] == _TW and a["month"] == "2026-06")
    # accruing carries the SAME forecast shape (Decimal strings + per-trade breakdown)
    assert acc["expected"] == "109" and acc["trade_count"] == 1 and len(acc["trades"]) == 1
    assert all(r["month"] != "2026-06" for r in body["rows"])  # not in the pending list
    # accruing is NOT counted in the pending sidebar badge
    assert api_client.get("/api/rebates/count").json()["count"] == 0
    # confirming a not-yet-due month is rejected (未到次月)
    r = api_client.post("/api/rebates/confirm",
                        json={"account_id": _TW, "month": "2026-06", "amount": "109"})
    assert r.status_code == 400 and r.json()["error"]["field"] == "month"


def test_confirmed_month_not_double_booked_after_note_edit(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """F2d/F12 double-credit guard: once a month is booked, editing the confirmed rebate
    movement's (free-text) note must NOT re-surface it as pending — the structural date key
    keeps it suppressed, so it can never be confirmed (and credited) a second time."""
    _seed_may_trade(golden_db)  # 2026-05 pending, estimate 109
    r = api_client.post("/api/rebates/confirm",
                        json={"account_id": _TW, "month": "2026-05", "amount": "109"})
    assert r.status_code == 200
    move_id = r.json()["id"]
    booked_balance = _balance(api_client, _TW, "TWD")  # includes the single 109 credit
    # Edit ONLY the note of the confirmed rebate movement (the old double-credit vector). The
    # movement stays kind=rebate dated 2026-06-01 (the refund month); the note tag is broken.
    # ack_negative: the golden tw_broker pool is negative from the un-funded buy, so any edit
    # trips the date-aware negative_cash ack — the owner would confirm it (irrelevant to the
    # suppression we are proving).
    e = api_client.put(f"/api/cash/movements/{move_id}", json={
        "account_id": _TW, "date": "2026-06-01", "kind": "rebate",
        "ccy": "TWD", "amount": "109", "note": "some other note", "ack_negative": True})
    assert e.status_code == 200
    # the month is STILL suppressed (structural key: movement dated in 2026-06 → month 2026-05)
    assert all(x["month"] != "2026-05" for x in _rows(api_client) if x["account_id"] == _TW)
    # a re-confirm is rejected → no second credit
    r2 = api_client.post("/api/rebates/confirm",
                         json={"account_id": _TW, "month": "2026-05", "amount": "109"})
    assert r2.status_code == 400 and r2.json()["error"]["field"] == "month"
    # the pool was credited EXACTLY ONCE (balance unchanged by the note edit + failed confirm)
    assert _balance(api_client, _TW, "TWD") == booked_balance


def test_rebate_movement_kind_and_date_are_locked(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """F2d residual guard: a booked 折讓款 movement may have its amount (and note) corrected, but
    its kind and date are LOCKED — changing the date re-anchors the structural suppression month
    and changing the kind drops it from the confirmed set, either of which would let the month be
    confirmed (and credited) a second time. The backend PUT rejects both with 400."""
    _seed_may_trade(golden_db)  # 2026-05 pending, estimate 109
    r = api_client.post("/api/rebates/confirm",
                        json={"account_id": _TW, "month": "2026-05", "amount": "109"})
    assert r.status_code == 200
    move_id = r.json()["id"]
    base = {"account_id": _TW, "ccy": "TWD", "amount": "109", "ack_negative": True}
    # date change (would re-anchor 2026-05's suppression to another month) -> 400
    d = api_client.put(f"/api/cash/movements/{move_id}",
                       json={**base, "date": "2026-07-01", "kind": "rebate", "note": None})
    assert d.status_code == 400 and d.json()["error"]["field"] == "kind"
    # kind change (would drop it from the confirmed set) -> 400
    k = api_client.put(f"/api/cash/movements/{move_id}",
                       json={**base, "date": "2026-06-01", "kind": "deposit", "note": None})
    assert k.status_code == 400
    # amount-only correction (same kind + date) -> allowed
    ok = api_client.put(f"/api/cash/movements/{move_id}",
                        json={**base, "date": "2026-06-01", "kind": "rebate",
                              "amount": "100", "note": None})
    assert ok.status_code == 200


def test_confirm_400s(api_client: TestClient, golden_db: sqlite3.Connection) -> None:
    _seed_may_trade(golden_db)
    # unknown / non-rebate account (schwab rebates at 0)
    for aid in ("ghost", "schwab"):
        r = api_client.post("/api/rebates/confirm",
                            json={"account_id": aid, "month": "2026-05", "amount": "10"})
        assert r.status_code == 400 and r.json()["error"]["field"] == "account_id"
    # a month that is not pending (no trades there)
    r = api_client.post("/api/rebates/confirm",
                        json={"account_id": _TW, "month": "2020-01", "amount": "10"})
    assert r.status_code == 400 and r.json()["error"]["field"] == "month"
    # non-positive amount on an otherwise-valid pending month
    r = api_client.post("/api/rebates/confirm",
                        json={"account_id": _TW, "month": "2026-05", "amount": "0"})
    assert r.status_code == 400 and r.json()["error"]["field"] == "amount"


def test_skip_unskip_resurfaces(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_may_trade(golden_db)
    s = api_client.post("/api/rebates/skip", json={"account_id": _TW, "month": "2026-05"})
    assert s.status_code == 200 and s.json()["skipped"] == 1
    body = api_client.get("/api/rebates").json()
    assert all(x["month"] != "2026-05" for x in body["rows"])
    sk = next(x for x in body["skipped"] if x["month"] == "2026-05")
    assert sk["account_id"] == _TW and sk["detail"]["expected"] == "109"
    # skipped detail nests the same per-trade breakdown shape (FU-D6)
    assert len(sk["detail"]["trades"]) == 1 and sk["detail"]["trades"][0]["fee"] == "142"
    u = api_client.post("/api/rebates/unskip", json={"account_id": _TW, "month": "2026-05"})
    assert u.status_code == 200 and u.json()["unskipped"] == 1
    assert any(x["month"] == "2026-05" for x in _rows(api_client))


def test_guest_parity_with_inbox_siblings(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Golden DB is guest mode: GET/confirm/skip/unskip all answer without an auth gate,
    exactly like /api/dividend-inbox/*. (No 401/403 on any rebate endpoint.)"""
    _seed_may_trade(golden_db)
    for method, path, body in (
        ("GET", "/api/rebates", None),
        ("GET", "/api/rebates/count", None),
        ("POST", "/api/rebates/skip", {"account_id": _TW, "month": "2026-05"}),
        ("POST", "/api/rebates/unskip", {"account_id": _TW, "month": "2026-05"}),
    ):
        r = (api_client.get(path) if method == "GET"
             else api_client.post(path, json=body))
        assert r.status_code == 200, (path, r.status_code)
