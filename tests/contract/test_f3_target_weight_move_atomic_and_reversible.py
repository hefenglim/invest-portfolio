"""F-3 (DEF-020 / DEF-021 class, dispatched 2026-09-23): the target WEIGHT an EXCHANGE carries
across moves inside the action's own transaction, and a delete moves it back — when, and only
when, nothing has touched it since.

DEF-021 fixed exactly this for the price-alert BAND and the sweep that followed found the
owner's other per-symbol setting in the same shape: ``strategy/target_weights.py::
move_target_weight`` ran AFTER ``add_corporate_action`` had already committed (so a failure
there left an action whose weight never followed it — DEF-020's half-landed state), and
deleting the EXCHANGE left the weight filed under the new ticker (DEF-021's one-way move).

Ruling, mirroring the band: the move joins the action's transaction (``commit=False``); the
EXCHANGE row records what moved (``corporate_actions.weight_move_json`` — its own additive
column, because the band record means "a band moved" and a weight-only move would have to
forge an empty band into it); the delete moves the weight back only while the destination
still carries EXACTLY the recorded weight and the source carries none, and reports
``weight_restored`` with the reason when it does not.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.store import (
    MovedWeight,
    insert_corporate_action,
    list_corporate_actions,
    upsert_instrument,
    weight_move_from_json,
    weight_move_to_json,
)
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.strategy.target_weights import (
    load_target_weights,
    move_target_weight,
    pending_weight_move,
    pending_weight_restore,
    restore_target_weight,
    save_target_weights,
)

D = Decimal
NOW = datetime(2026, 9, 23, tzinfo=UTC)
_BASE = "/api/ledgers/corporate-actions"
ACTION_DAY = date(2026, 6, 10)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    for sym in ("OLD", "NEW"):
        upsert_instrument(c, Instrument(symbol=sym, market=Market.US,
                                        quote_ccy=Currency.USD, sector="Tech", name=sym))
    c.commit()
    return c


def _set(conn: sqlite3.Connection, **weights: str) -> None:
    save_target_weights(conn, {s: D(w) for s, w in weights.items()}, now=NOW)


# ------------------------------------------------------------ the store / strategy rules


def test_the_record_round_trips_and_degrades_quietly() -> None:
    moved = MovedWeight(from_symbol="OLD", to_symbol="NEW", weight=D("0.2500"))
    assert weight_move_from_json(weight_move_to_json(moved)) == moved
    # The recorded digits are the config's own digits, so "0.2500" stays "0.2500".
    assert '"0.2500"' in weight_move_to_json(moved)
    assert weight_move_from_json(None) is None
    assert weight_move_from_json("") is None
    assert weight_move_from_json("not json") is None
    assert weight_move_from_json('{"from_symbol": "OLD"}') is None


def test_the_promise_and_the_move_are_one_predicate() -> None:
    c = _conn()
    _set(c, OLD="0.25")
    promised = pending_weight_move(c, from_symbol="OLD", to_symbol="NEW")
    assert promised == MovedWeight(from_symbol="OLD", to_symbol="NEW", weight=D("0.25"))
    assert load_target_weights(c) == {"OLD": D("0.25")}          # reading moved nothing
    assert move_target_weight(c, from_symbol="OLD", to_symbol="NEW", now=NOW) == D("0.25")
    assert pending_weight_move(c, from_symbol="OLD", to_symbol="NEW") is None


def test_the_move_can_defer_its_commit_to_the_action_transaction() -> None:
    """★ The DEF-020 half: one rollback must take the weight back with the action rows."""
    c = _conn()
    _set(c, OLD="0.25")
    assert move_target_weight(c, from_symbol="OLD", to_symbol="NEW", now=NOW,
                              commit=False) == D("0.25")
    c.rollback()
    assert load_target_weights(c) == {"OLD": D("0.25")}


def test_an_untouched_weight_moves_back() -> None:
    c = _conn()
    _set(c, OLD="0.25", KEEP="0.10")
    moved = pending_weight_move(c, from_symbol="OLD", to_symbol="NEW")
    move_target_weight(c, from_symbol="OLD", to_symbol="NEW", now=NOW)
    verdict = restore_target_weight(c, moved, now=NOW)
    assert verdict is not None and verdict.restorable and verdict.restored
    assert load_target_weights(c) == {"OLD": D("0.25"), "KEEP": D("0.10")}
    again = pending_weight_restore(c, moved)
    assert again is not None and not again.restorable          # nothing left to restore


def test_a_changed_destination_weight_is_left_alone_with_a_reason() -> None:
    c = _conn()
    _set(c, OLD="0.25")
    moved = pending_weight_move(c, from_symbol="OLD", to_symbol="NEW")
    move_target_weight(c, from_symbol="OLD", to_symbol="NEW", now=NOW)
    _set(c, NEW="0.30")                                          # the owner re-tuned it
    verdict = restore_target_weight(c, moved, now=NOW)
    assert verdict is not None and not verdict.restorable and not verdict.restored
    assert verdict.reason is not None and "已改動" in verdict.reason
    assert "25%" in verdict.reason and "30%" in verdict.reason
    assert load_target_weights(c) == {"NEW": D("0.30")}


def test_a_cleared_destination_weight_counts_as_changed() -> None:
    c = _conn()
    _set(c, OLD="0.25")
    moved = pending_weight_move(c, from_symbol="OLD", to_symbol="NEW")
    move_target_weight(c, from_symbol="OLD", to_symbol="NEW", now=NOW)
    _set(c)                                                      # all targets cleared
    verdict = restore_target_weight(c, moved, now=NOW)
    assert verdict is not None and not verdict.restored
    assert verdict.reason is not None and "已清除" in verdict.reason
    assert load_target_weights(c) == {}


def test_a_source_that_got_its_own_weight_is_left_alone_with_a_reason() -> None:
    c = _conn()
    _set(c, OLD="0.25")
    moved = pending_weight_move(c, from_symbol="OLD", to_symbol="NEW")
    move_target_weight(c, from_symbol="OLD", to_symbol="NEW", now=NOW)
    _set(c, NEW="0.25", OLD="0.05")
    verdict = restore_target_weight(c, moved, now=NOW)
    assert verdict is not None and not verdict.restored
    assert verdict.reason is not None and "另有目標權重" in verdict.reason
    assert load_target_weights(c) == {"NEW": D("0.25"), "OLD": D("0.05")}


def test_nothing_recorded_is_a_quiet_none() -> None:
    c = _conn()
    assert pending_weight_restore(c, None) is None
    assert restore_target_weight(c, None, now=NOW) is None


def test_the_restore_can_defer_its_commit_to_the_delete_transaction() -> None:
    c = _conn()
    _set(c, OLD="0.25")
    moved = pending_weight_move(c, from_symbol="OLD", to_symbol="NEW")
    move_target_weight(c, from_symbol="OLD", to_symbol="NEW", now=NOW)
    assert restore_target_weight(c, moved, now=NOW, commit=False) is not None
    c.rollback()
    assert load_target_weights(c) == {"NEW": D("0.25")}


def test_the_row_stores_what_it_moved() -> None:
    c = _conn()
    moved = MovedWeight(from_symbol="OLD", to_symbol="NEW", weight=D("0.25"))
    row_id = insert_corporate_action(
        c, account_id="schwab", action_date=ACTION_DAY, kind=CorporateActionKind.EXCHANGE,
        from_symbol="OLD", to_symbol="NEW", ratio_to=D("1"), ratio_from=D("1"),
        weight_move=moved)
    (stored,) = [a for a in list_corporate_actions(c) if a.id == row_id]
    assert stored.weight_move == moved and stored.band_move is None


# --------------------------------------------------------------------- through the routes


def _newco(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="NEWCO", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Semis",
                                       name="NewCo", board="TWSE"))
    conn.commit()


def _exchange(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "account_id": "tw_broker", "date": ACTION_DAY.isoformat(), "kind": "EXCHANGE",
        "from_symbol": "2330", "to_symbol": "NEWCO", "ratio_to": "1", "ratio_from": "1",
        "ack_warnings": True,
    }
    base.update(over)
    return base


def test_the_move_lands_with_the_action_or_not_at_all(
    api_client: TestClient, golden_db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ DEF-020's shape. A failure in the weight move used to arrive AFTER the action had
    committed: the EXCHANGE stood, the weight stayed on the dead ticker. Now the two share
    one transaction, so the failure takes the action rows with it."""
    _newco(golden_db)
    _set(golden_db, **{"2330": "0.25"})

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("weight store unavailable")

    monkeypatch.setattr("portfolio_dash.api.routers.ledgers.move_target_weight", _boom)
    with pytest.raises(RuntimeError):
        api_client.post(_BASE, json=_exchange())
    assert list_corporate_actions(golden_db) == []
    assert load_target_weights(golden_db) == {"2330": D("0.25")}


def test_the_saved_row_records_the_weight_it_moved(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _newco(golden_db)
    _set(golden_db, **{"2330": "0.25"})
    r = api_client.post(_BASE, json=_exchange())
    assert r.status_code == 201, r.text
    assert r.json()["weight_moved"] == "0.25"
    (a,) = list_corporate_actions(golden_db)
    assert a.weight_move == MovedWeight(from_symbol="2330", to_symbol="NEWCO",
                                        weight=D("0.25"))
    assert load_target_weights(golden_db) == {"NEWCO": D("0.25")}


def test_delete_moves_the_weight_back_when_it_was_not_touched(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """★ The DEF-021 half: 2330 0.25 → EXCHANGE → delete → 2330 0.25 again, NEWCO none."""
    _newco(golden_db)
    _set(golden_db, **{"2330": "0.25"})
    (action_id,) = api_client.post(_BASE, json=_exchange()).json()["ids"]
    d = api_client.delete(f"{_BASE}/{action_id}")
    assert d.status_code == 200, d.text
    body = d.json()
    assert body["weight_restored"] is True
    assert body["weight_restore"] == {
        "from_symbol": "2330", "to_symbol": "NEWCO", "weight": "0.25",
        "restorable": True, "restored": True, "reason": None}
    assert load_target_weights(golden_db) == {"2330": D("0.25")}


def test_delete_leaves_a_weight_the_owner_changed_and_says_why(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _newco(golden_db)
    _set(golden_db, **{"2330": "0.25"})
    (action_id,) = api_client.post(_BASE, json=_exchange()).json()["ids"]
    _set(golden_db, NEWCO="0.30")
    d = api_client.delete(f"{_BASE}/{action_id}")
    assert d.status_code == 200, d.text
    assert d.json()["weight_restored"] is False
    assert "NEWCO" in d.json()["weight_restore"]["reason"]
    assert load_target_weights(golden_db) == {"NEWCO": D("0.30")}


def test_the_set_delete_restores_the_weight_too(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _newco(golden_db)
    _set(golden_db, **{"2330": "0.25"})
    assert api_client.post(_BASE, json=_exchange()).status_code == 201
    d = api_client.delete(f"{_BASE}/set", params={
        "from_symbol": "2330", "date": ACTION_DAY.isoformat(), "kind": "EXCHANGE"})
    assert d.status_code == 200, d.text
    assert d.json()["weight_restored"] is True
    assert load_target_weights(golden_db) == {"2330": D("0.25")}


def test_a_row_without_a_record_reports_nothing_about_the_weight(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """A SPLIT (or any row written before F-3): no record, no claim, no change."""
    _set(golden_db, **{"2330": "0.25"})
    r = api_client.post(_BASE, json={
        "account_id": "tw_broker", "date": ACTION_DAY.isoformat(), "kind": "SPLIT",
        "from_symbol": "2330", "to_symbol": "2330", "ratio_to": "10", "ratio_from": "1"})
    assert r.status_code == 201, r.text
    (action_id,) = r.json()["ids"]
    d = api_client.delete(f"{_BASE}/{action_id}")
    assert d.status_code == 200
    assert d.json()["weight_restored"] is None and d.json()["weight_restore"] is None
    assert load_target_weights(golden_db) == {"2330": D("0.25")}
