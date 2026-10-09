"""DEF-021 (functional test manual D-10, 2026-09-23): deleting an EXCHANGE moves the band
back — when, and only when, nothing has touched it since.

``store.move_target_band``'s docstring said 「⚠ Not reversible … recorded as a limitation
the entry surface states」 — and the entry surface never stated it: the preview said 「會一併
移到 2882」 full stop, the delete confirm said nothing, and after the delete the band stayed
on the new ticker (2882: 40／55, 2884: empty), where ``target_cross`` kept firing for a
position the ledger no longer held.

Ruling (this wave): CONDITIONALLY reversible. The save records what moved on the row
(``corporate_actions.band_move_json``); the delete moves it back only while the destination
still carries EXACTLY the recorded band (both levels + ``target_set_at``) and the source has
none, reports ``band_restored: true`` — otherwise ``false`` with a reason, and touches
neither symbol. The list row carries the same verdict (``band_restore``) so the delete
confirm can quote it beforehand, through the same predicate.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.store import (
    MovedBand,
    band_move_from_json,
    band_move_to_json,
    get_instrument,
    insert_corporate_action,
    list_corporate_actions,
    move_target_band,
    pending_band_restore,
    restore_target_band,
    upsert_instrument,
)
from portfolio_dash.shared.corporate_actions import CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument

D = Decimal
_BASE = "/api/ledgers/corporate-actions"
SPLIT_DAY = date(2026, 6, 10)
SET_ON = date(2026, 3, 1)


def _band(conn: sqlite3.Connection, symbol: str, *, low: str | None = None,
          high: str | None = None, on: date = SET_ON) -> None:
    inst = get_instrument(conn, symbol)
    assert inst is not None
    upsert_instrument(conn, inst.model_copy(update={
        "target_low": D(low) if low else None, "target_high": D(high) if high else None}),
        today=on)


def _levels(conn: sqlite3.Connection, symbol: str) -> tuple[D | None, D | None, date | None]:
    inst = get_instrument(conn, symbol)
    assert inst is not None
    return inst.target_low, inst.target_high, inst.target_set_at


# ------------------------------------------------------------------- the store's own rules


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    for sym in ("OLD", "NEW"):
        upsert_instrument(c, Instrument(symbol=sym, market=Market.US,
                                        quote_ccy=Currency.USD, sector="Tech", name=sym))
    c.commit()
    return c


def test_the_record_round_trips_byte_for_byte() -> None:
    moved = MovedBand(from_symbol="OLD", to_symbol="NEW", target_low=D("40"),
                      target_high=D("55.5"), set_at=SET_ON)
    assert band_move_from_json(band_move_to_json(moved)) == moved
    assert band_move_from_json(None) is None
    assert band_move_from_json("") is None
    assert band_move_from_json("not json") is None      # a hand-edited column degrades
    assert band_move_from_json('{"from_symbol": 1}') is None


def test_the_row_stores_what_it_moved_and_the_delete_moves_it_back() -> None:
    c = _conn()
    _band(c, "OLD", low="40", high="55")
    moved = move_target_band(c, from_symbol="OLD", to_symbol="NEW")
    assert moved is not None
    row_id = insert_corporate_action(
        c, account_id="schwab", action_date=SPLIT_DAY, kind=CorporateActionKind.EXCHANGE,
        from_symbol="OLD", to_symbol="NEW", ratio_to=D("1"), ratio_from=D("1"),
        band_move=moved)
    (stored,) = [a for a in list_corporate_actions(c) if a.id == row_id]
    assert stored.band_move == moved

    verdict = restore_target_band(c, stored.band_move)
    assert verdict is not None and verdict.restored and verdict.restorable
    assert _levels(c, "OLD") == (D("40"), D("55"), SET_ON)   # the date rides back too
    assert _levels(c, "NEW") == (None, None, None)


def test_the_predicate_and_the_write_are_one_rule() -> None:
    """The confirm quotes `pending_band_restore`; the delete runs `restore_target_band`.
    They must agree, and the read must not write."""
    c = _conn()
    _band(c, "OLD", low="40")
    moved = move_target_band(c, from_symbol="OLD", to_symbol="NEW")
    promised = pending_band_restore(c, moved)
    assert promised is not None and promised.restorable and not promised.restored
    assert _levels(c, "NEW")[0] == D("40")                    # reading changed nothing
    performed = restore_target_band(c, moved)
    assert performed is not None and performed.restored
    # …and afterwards the same predicate says there is nothing to restore any more.
    again = pending_band_restore(c, moved)
    assert again is not None and not again.restorable


def test_a_changed_destination_band_is_left_alone_with_a_reason() -> None:
    c = _conn()
    _band(c, "OLD", low="40", high="55")
    moved = move_target_band(c, from_symbol="OLD", to_symbol="NEW")
    _band(c, "NEW", low="45", high="55", on=date(2026, 9, 1))   # the owner moved a level
    verdict = restore_target_band(c, moved)
    assert verdict is not None and not verdict.restorable and not verdict.restored
    assert verdict.reason is not None and "NEW" in verdict.reason and "已改動" in verdict.reason
    assert "下限 40" in verdict.reason and "下限 45" in verdict.reason
    assert _levels(c, "NEW") == (D("45"), D("55"), date(2026, 9, 1))
    assert _levels(c, "OLD") == (None, None, None)


def test_a_source_that_got_its_own_band_is_left_alone_with_a_reason() -> None:
    c = _conn()
    _band(c, "OLD", low="40")
    moved = move_target_band(c, from_symbol="OLD", to_symbol="NEW")
    _band(c, "OLD", high="99", on=date(2026, 9, 1))
    verdict = restore_target_band(c, moved)
    assert verdict is not None and not verdict.restored
    assert verdict.reason is not None and "另有目標價" in verdict.reason
    assert _levels(c, "OLD") == (None, D("99"), date(2026, 9, 1))
    assert _levels(c, "NEW") == (D("40"), None, SET_ON)


def test_a_cleared_destination_band_counts_as_changed() -> None:
    c = _conn()
    _band(c, "OLD", low="40")
    moved = move_target_band(c, from_symbol="OLD", to_symbol="NEW")
    _band(c, "NEW")                                            # cleared
    verdict = restore_target_band(c, moved)
    assert verdict is not None and not verdict.restored
    assert verdict.reason is not None and "目前 無" in verdict.reason


def test_nothing_recorded_is_a_quiet_none() -> None:
    c = _conn()
    assert pending_band_restore(c, None) is None
    assert restore_target_band(c, None) is None


def test_the_restore_can_defer_its_commit_to_the_delete_transaction() -> None:
    c = _conn()
    _band(c, "OLD", low="40")
    moved = move_target_band(c, from_symbol="OLD", to_symbol="NEW")
    assert restore_target_band(c, moved, commit=False) is not None
    c.rollback()
    assert _levels(c, "NEW")[0] == D("40") and _levels(c, "OLD")[0] is None


# --------------------------------------------------------------------- through the routes


def _newco(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="NEWCO", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Semis",
                                       name="NewCo", board="TWSE"))


def _exchange(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "account_id": "tw_broker", "date": SPLIT_DAY.isoformat(), "kind": "EXCHANGE",
        "from_symbol": "2330", "to_symbol": "NEWCO", "ratio_to": "1", "ratio_from": "1",
        "ack_warnings": True,
    }
    base.update(over)
    return base


def test_the_saved_row_records_the_move_and_the_list_promises_the_reversal(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _newco(golden_db)
    _band(golden_db, "2330", low="40", high="55")
    r = api_client.post(_BASE, json=_exchange())
    assert r.status_code == 201, r.text
    (row,) = api_client.get(_BASE).json()["rows"]
    assert row["band_move"] == {"from_symbol": "2330", "to_symbol": "NEWCO",
                                "target_low": "40", "target_high": "55",
                                "set_at": SET_ON.isoformat()}
    assert row["band_restore"]["restorable"] is True
    assert row["band_restore"]["restored"] is False
    assert row["band_restore"]["reason"] is None


def test_delete_moves_the_band_back_when_it_was_not_touched(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """★ The reproduction: 2884 40／55 → EXCHANGE → delete → 2882 still 40／55, 2884 empty."""
    _newco(golden_db)
    _band(golden_db, "2330", low="40", high="55")
    r = api_client.post(_BASE, json=_exchange())
    assert r.status_code == 201, r.text
    assert _levels(golden_db, "NEWCO") == (D("40"), D("55"), SET_ON)
    (action_id,) = r.json()["ids"]

    d = api_client.delete(f"{_BASE}/{action_id}")
    assert d.status_code == 200, d.text
    assert d.json()["band_restored"] is True
    assert d.json()["band_restore"]["band"]["to_symbol"] == "NEWCO"
    assert _levels(golden_db, "2330") == (D("40"), D("55"), SET_ON)
    assert _levels(golden_db, "NEWCO") == (None, None, None)


def test_delete_leaves_a_band_the_owner_changed_and_says_why(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _newco(golden_db)
    _band(golden_db, "2330", low="40", high="55")
    r = api_client.post(_BASE, json=_exchange())
    (action_id,) = r.json()["ids"]
    # The owner re-tunes the floor on the new ticker through the ordinary door.
    u = api_client.put("/api/instruments/NEWCO", json={"target_low": "45"})
    assert u.status_code == 200, u.text
    (row,) = api_client.get(_BASE).json()["rows"]
    assert row["band_restore"]["restorable"] is False
    assert "已改動" in row["band_restore"]["reason"]

    d = api_client.delete(f"{_BASE}/{action_id}")
    assert d.status_code == 200, d.text
    assert d.json()["band_restored"] is False
    assert "NEWCO" in d.json()["band_restore"]["reason"]
    assert _levels(golden_db, "NEWCO")[:2] == (D("45"), D("55"))
    assert _levels(golden_db, "2330") == (None, None, None)


def test_a_row_without_a_record_reports_nothing_about_the_band(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Every row written before DEF-021, and every SPLIT: no record, no claim, no change."""
    _newco(golden_db)
    r = api_client.post(_BASE, json={
        "account_id": "tw_broker", "date": SPLIT_DAY.isoformat(), "kind": "SPLIT",
        "from_symbol": "2330", "to_symbol": "2330", "ratio_to": "10", "ratio_from": "1"})
    assert r.status_code == 201, r.text
    (row,) = api_client.get(_BASE).json()["rows"]
    assert row["band_move"] is None and row["band_restore"] is None
    (action_id,) = r.json()["ids"]
    d = api_client.delete(f"{_BASE}/{action_id}")
    assert d.status_code == 200 and d.json()["band_restored"] is None


def test_the_csv_door_records_the_move_too(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Both doors, one rule: an imported EXCHANGE is as reversible as a typed one."""
    _newco(golden_db)
    _band(golden_db, "2330", low="40")
    csv_text = ("account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from\n"
                f"tw_broker,{SPLIT_DAY.isoformat()},EXCHANGE,2330,NEWCO,1,1\n")
    r = api_client.post("/api/import/commit", json={
        "kind": "corporate_actions", "csv_text": csv_text, "ack_warnings": True})
    assert r.status_code == 200, r.text
    (a,) = list_corporate_actions(golden_db)
    assert a.band_move is not None and a.band_move.target_low == D("40")
    d = api_client.delete(f"{_BASE}/{a.id}")
    assert d.status_code == 200 and d.json()["band_restored"] is True
    assert _levels(golden_db, "2330")[0] == D("40")


def test_the_form_states_the_undo_rule_and_the_confirm_quotes_the_verdict() -> None:
    """The half the old docstring claimed existed: the entry surface SAYS what happens."""
    from pathlib import Path

    web = Path(__file__).resolve().parents[2] / "web"
    form = (web / "corp-action-form.js").read_text(encoding="utf-8")
    assert "的目標價未再改動會自動移回" in form
    ledger = (web / "ledger.js").read_text(encoding="utf-8")
    assert "band_restore" in ledger and "會自動移回" in ledger and "目標價不會移回" in ledger
    assert "reorg_fee" in ledger and "會一併刪除這筆行動的重組費用出金" in ledger
