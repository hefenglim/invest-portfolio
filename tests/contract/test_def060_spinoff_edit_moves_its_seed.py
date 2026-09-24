"""DEF-060 (owner ruling 2026-09-24, spec §13): editing a SPINOFF moves the child's seed price
with it — under DEF-040's rules on BOTH ends.

Measured on 4655845 (R3 decision ⑩): PUT /api/ledgers/corporate-actions/{id} with a new date
left the seed on the OLD day — a hand-typed price on a day the ledger no longer creates the
child, i.e. an orphan (and an orphan carries the seed signature, which is exactly what made
DEF-040's R3 fix unsafe). The ruling: move it when the old seed is still intact; when the new
day already has a real quote, do not write (DEF-040) and say so; when the old seed is no longer
intact, leave it and say why.

The same rule family covers the other two things an edit can do to the seed: a new CHILD
symbol (the seed follows the child) and a typed PRICE (``to_symbol_price`` on the PUT, which
until now was dropped in silence — D48b's own failure mode), plus a kind change away from
SPINOFF (the action no longer creates a child, so its seed leaves as on delete).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from tests.conftest import GOLDEN_NOW
from tests.contract.test_def040_spinoff_delete_takes_its_seed_price import (
    _BASE,
    _DAY,
    _listed,
    _seed_parent,
)

_NEW_DAY = date(2026, 3, 20)


def _row(conn: sqlite3.Connection, day: date, symbol: str = "CHLD") -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM prices WHERE instrument=? AND as_of_date=?",
                       (symbol, day.isoformat())).fetchone()
    return None if row is None else dict(row)


def _body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "account_id": "schwab", "date": _DAY.isoformat(), "kind": "SPINOFF",
        "from_symbol": "PARN", "to_symbol": "CHLD", "ratio_to": "1", "ratio_from": "2",
        "cost_carry": "0.2", "ack_warnings": True}
    body.update(over)
    return body


def _save(client: TestClient, price: str | None = "50") -> int:
    r = client.post(_BASE, json=_body(**({"to_symbol_price": price} if price else {})))
    assert r.status_code == 201, r.text
    return int(r.json()["ids"][0])


def _edit(client: TestClient, action: int, **over: Any) -> dict[str, Any]:
    r = client.put(f"{_BASE}/{action}", json=_body(**over))
    assert r.status_code == 200, r.text
    return dict(r.json())


def _quote(conn: sqlite3.Connection, day: date, close: str = "48.70") -> None:
    upsert_prices(conn, [PriceRow(instrument="CHLD", market=Market.US, as_of=day,
                                  close=Decimal(close), source="yfinance")],
                  fetched_at=GOLDEN_NOW)
    conn.commit()


def _is_seed_on(row: dict[str, Any] | None, day: date) -> bool:
    if row is None or row["source"] != "manual":
        return False
    stamp = datetime.fromisoformat(row["fetched_at"])
    return stamp.date() == day and (stamp.hour, stamp.minute, stamp.second) == (0, 0, 0)


def test_a_date_edit_moves_an_intact_seed_and_the_delete_then_takes_it_from_there(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    action = _save(api_client)
    out = _edit(api_client, action, date=_NEW_DAY.isoformat())
    assert _row(golden_db, _DAY) is None, "the seed stayed on the old day"
    moved = _row(golden_db, _NEW_DAY)
    assert _is_seed_on(moved, _NEW_DAY) and moved is not None and moved["close"] == "50"
    move = out["child_price_move"]
    assert move["moved"] is True and move["to"]["date"] == _NEW_DAY.isoformat(), move
    assert "2026-03-16" in move["message"] and "2026-03-20" in move["message"]
    assert out["child_priced"] == "CHLD" and out["child_price_skipped"] is None

    promise = _listed(api_client, action)["child_price_restore"]
    assert promise["restorable"] is True and promise["date"] == _NEW_DAY.isoformat()
    assert api_client.delete(f"{_BASE}/{action}").json()["child_price_removed"] is True
    assert _row(golden_db, _NEW_DAY) is None and _row(golden_db, _DAY) is None


def test_a_date_edit_onto_a_quoted_day_writes_nothing_there_and_says_so(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    action = _save(api_client)
    _quote(golden_db, _NEW_DAY)
    quote = _row(golden_db, _NEW_DAY)
    out = _edit(api_client, action, date=_NEW_DAY.isoformat())
    assert _row(golden_db, _NEW_DAY) == quote, "the move overwrote the new day's quote"
    assert _row(golden_db, _DAY) is None, "the seed stayed behind on a day the action left"
    assert out["child_priced"] is None
    assert out["child_price_skipped"]["reason"] == (
        "CHLD 在 2026-03-20 已有正式報價 48.70（來源 yfinance），起始價未寫入")
    assert out["child_price_move"]["moved"] is False
    # The action now owns nothing: its delete promises and takes nothing.
    assert _listed(api_client, action)["child_price_restore"] is None
    assert api_client.delete(f"{_BASE}/{action}").json()["child_price_removed"] is None
    assert _row(golden_db, _NEW_DAY) == quote


def test_a_seed_a_quote_replaced_is_not_moved_and_the_reason_is_given(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    action = _save(api_client)
    _quote(golden_db, _DAY)
    quote = _row(golden_db, _DAY)
    out = _edit(api_client, action, date=_NEW_DAY.isoformat())
    assert _row(golden_db, _DAY) == quote
    assert _row(golden_db, _NEW_DAY) is None
    move = out["child_price_move"]
    assert move["moved"] is False and "正式報價" in move["message"], move
    assert api_client.delete(f"{_BASE}/{action}").json()["child_price_removed"] is None
    assert _row(golden_db, _DAY) == quote


def test_a_date_edit_of_a_spinoff_that_wrote_no_seed_moves_nothing(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    action = _save(api_client, price=None)
    out = _edit(api_client, action, date=_NEW_DAY.isoformat())
    assert out["child_price_move"] is None and out["child_priced"] is None
    assert _row(golden_db, _DAY) is None and _row(golden_db, _NEW_DAY) is None


def test_a_new_child_symbol_takes_the_seed_with_it(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    upsert_instrument(golden_db, Instrument(symbol="CHL2", market=Market.US,
                                            quote_ccy=Currency.USD, sector="Tech",
                                            name="Child2"))
    golden_db.commit()
    action = _save(api_client)
    out = _edit(api_client, action, to_symbol="CHL2")
    assert _row(golden_db, _DAY) is None
    assert _is_seed_on(_row(golden_db, _DAY, "CHL2"), _DAY)
    assert out["child_price_move"]["moved"] is True
    assert api_client.delete(f"{_BASE}/{action}").json()["child_price_removed"] is True
    assert _row(golden_db, _DAY, "CHL2") is None


def test_a_typed_price_on_the_edit_replaces_the_actions_own_seed(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    action = _save(api_client)
    out = _edit(api_client, action, to_symbol_price="55")
    row = _row(golden_db, _DAY)
    assert _is_seed_on(row, _DAY) and row is not None and row["close"] == "55"
    assert out["child_priced"] == "CHLD"
    assert api_client.delete(f"{_BASE}/{action}").json()["child_price_removed"] is True
    assert _row(golden_db, _DAY) is None


def test_a_typed_price_on_the_edit_never_overwrites_a_quote(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    action = _save(api_client, price=None)
    _quote(golden_db, _DAY)
    quote = _row(golden_db, _DAY)
    out = _edit(api_client, action, to_symbol_price="55")
    assert _row(golden_db, _DAY) == quote
    assert out["child_priced"] is None and "起始價未寫入" in out["child_price_skipped"]["reason"]


def test_a_typed_price_on_an_edit_to_another_kind_is_refused_loudly(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    action = _save(api_client)
    r = api_client.put(f"{_BASE}/{action}", json=_body(
        kind="SPLIT", to_symbol="PARN", ratio_to="2", ratio_from="1", cost_carry=None,
        to_symbol_price="55"))
    assert r.status_code == 400 and "僅適用於分拆" in r.text, r.text


def test_an_edit_away_from_spinoff_takes_the_seed_as_a_delete_would(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    action = _save(api_client)
    out = _edit(api_client, action, kind="SPLIT", to_symbol="PARN", ratio_to="2",
                ratio_from="1", cost_carry=None)
    assert _row(golden_db, _DAY) is None, "a SPLIT kept the seed of the SPINOFF it replaced"
    assert out["child_price_move"]["moved"] is False
    assert "已移除" in out["child_price_move"]["message"]


def test_a_legacy_row_with_an_intact_seed_is_moved_too(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    action = _save(api_client)
    golden_db.execute("UPDATE corporate_actions SET child_seed_json=NULL WHERE id=?",
                      (action,))
    golden_db.commit()
    _edit(api_client, action, date=_NEW_DAY.isoformat())
    assert _row(golden_db, _DAY) is None
    assert _is_seed_on(_row(golden_db, _NEW_DAY), _NEW_DAY)
    assert api_client.delete(f"{_BASE}/{action}").json()["child_price_removed"] is True
    assert _row(golden_db, _NEW_DAY) is None


def test_an_edit_that_leaves_the_slot_alone_leaves_the_seed_alone(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    action = _save(api_client)
    before = _row(golden_db, _DAY)
    out = _edit(api_client, action, note="只改備註")
    assert out["child_price_move"] is None
    assert _row(golden_db, _DAY) == before
