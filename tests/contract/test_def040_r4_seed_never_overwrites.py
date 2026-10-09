"""DEF-040 R4 (verifier's R3 bounce, spec 2026-09-24 §1 + appendix A): a SPINOFF's child seed
price is written ONLY into an empty ``(child, day)`` slot, and a delete takes back only what
that action wrote.

The R3 fix handled the happy path — the child had no price on the action day — and
destroyed data on the unhappy one: with a provider quote already there, ``write_seed_price``
replaced it through ``upsert_prices``' ``ON CONFLICT`` (no copy kept), the list promised
``restorable: true``, the delete answered ``restored: true``, and the quote was gone for good
(appendix A: ``BEFORE {48.70, yfinance}`` → ``AFTER_SAVE {50.00, manual}`` →
``AFTER_DELETE None``).

Every pre-existing state of the ``(child, day)`` price row is exercised against every delete
door (single delete, group delete, CSV batch undo — all three run ``_delete_actions``), and
each one must leave the row BYTE-IDENTICAL to what it was before the save (manual D-10:
「子公司起始價全部回到行動前的狀態，逐位元比對」):

* a provider quote (the verifier's probe);
* an ORPHAN seed — a seed-signature row no live action owns (left by a pre-R3 delete, or by
  DEF-060's pre-R4 date edit on the demo). Signature alone cannot tell it from this action's
  own seed, which is why the save now RECORDS what it wrote (``child_seed_json``);
* another live SPINOFF's seed on the same child and day;
* a provider fetch that later replaces the seed (kept by R3's test module).

The promise never claims a removal the delete cannot deliver: a save that wrote nothing
promises nothing (``child_price_restore: null``) and its delete reports nothing.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import insert_transaction, upsert_instrument
from portfolio_dash.pricing.providers.base import ProviderBase
from portfolio_dash.pricing.results import PriceRow
from portfolio_dash.pricing.seed import SEED_SOURCE, write_seed_price
from portfolio_dash.pricing.store import upsert_prices
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from tests.conftest import GOLDEN_NOW
from tests.contract.test_def040_spinoff_delete_takes_its_seed_price import (
    _BASE,
    _DAY,
    _listed,
    _seed_parent,
)


def _row(conn: sqlite3.Connection, day: date = _DAY) -> dict[str, Any] | None:
    """EVERY column of the child's price row — the byte-compare D-10 asks for."""
    row = conn.execute(
        "SELECT * FROM prices WHERE instrument='CHLD' AND as_of_date=?",
        (day.isoformat(),)).fetchone()
    return None if row is None else dict(row)


def _quote(conn: sqlite3.Connection, close: str = "48.70", day: date = _DAY) -> None:
    upsert_prices(conn, [PriceRow(instrument="CHLD", market=Market.US, as_of=day,
                                  close=Decimal(close), source="yfinance")],
                  fetched_at=GOLDEN_NOW)
    conn.commit()


def _register_child(conn: sqlite3.Connection) -> None:
    upsert_instrument(conn, Instrument(symbol="CHLD", market=Market.US,
                                       quote_ccy=Currency.USD, sector="Tech", name="Child"))
    conn.commit()


def _save(client: TestClient, price: str | None = "50.00") -> dict[str, Any]:
    body: dict[str, Any] = {
        "account_id": "schwab", "date": _DAY.isoformat(), "kind": "SPINOFF",
        "from_symbol": "PARN", "to_symbol": "CHLD", "ratio_to": "1", "ratio_from": "2",
        "cost_carry": "0.2", "ack_warnings": True}
    if price is not None:
        body["to_symbol_price"] = price
    r = client.post(_BASE, json=body)
    assert r.status_code == 201, r.text
    return dict(r.json())


# ------------------------------------------------------------------ a provider quote


def test_probe_existing_quote_survives_spinoff_save_and_delete(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The verifier's appendix-A probe, verbatim in intent (4655845 fails it): the save must
    not touch the quote, the promise must not claim a removal, the delete must not take it."""
    _seed_parent(golden_db)
    _quote(golden_db)
    before = _row(golden_db)
    saved = _save(api_client)
    assert _row(golden_db) == before, "the save overwrote the provider's quote"
    assert saved["child_priced"] is None
    skipped = saved["child_price_skipped"]
    assert skipped is not None and skipped["symbol"] == "CHLD"
    assert skipped["date"] == _DAY.isoformat()
    assert skipped["existing_close"] == "48.70" and skipped["source"] == "yfinance"
    assert skipped["reason"] == (
        "CHLD 在 2026-03-16 已有正式報價 48.70（來源 yfinance），起始價未寫入"), skipped

    action = int(saved["ids"][0])
    assert _listed(api_client, action)["child_price_restore"] is None, (
        "a save that wrote no seed promised something about one")
    deleted = api_client.delete(f"{_BASE}/{action}").json()
    assert deleted["child_price_removed"] is None
    assert deleted["child_price_restore"] is None
    assert _row(golden_db) == before, "the delete took a quote the action never wrote"


def test_existing_quote_survives_the_group_delete(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    _quote(golden_db)
    before = _row(golden_db)
    _save(api_client)
    r = api_client.delete(f"{_BASE}/set", params={
        "from_symbol": "PARN", "date": _DAY.isoformat(), "kind": "SPINOFF"})
    assert r.status_code == 200, r.text
    assert r.json()["child_price_removed"] is None
    assert _row(golden_db) == before


def test_existing_quote_survives_the_import_batch_undo(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    _quote(golden_db)
    before = _row(golden_db)
    r = api_client.post("/api/import/commit", json={
        "kind": "corporate_actions",
        "csv_text": "account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from,cost_carry\n"
                    f"schwab,{_DAY.isoformat()},SPINOFF,PARN,CHLD,1,2,0.2\n",
        "ack_warnings": True})
    assert r.status_code == 200, r.text
    body = api_client.delete(f"/api/import/batches/{r.json()['import_batch_id']}").json()
    assert body["child_price_restore"] == [], body
    assert _row(golden_db) == before


def test_no_price_typed_over_an_existing_quote_promises_nothing(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    _quote(golden_db)
    before = _row(golden_db)
    saved = _save(api_client, price=None)
    assert saved["child_priced"] is None and saved["child_price_skipped"] is None
    action = int(saved["ids"][0])
    assert _listed(api_client, action)["child_price_restore"] is None
    assert api_client.delete(f"{_BASE}/{action}").json()["child_price_removed"] is None
    assert _row(golden_db) == before


# ------------------------------------------------------------------ an orphan seed


def _orphan_seed(conn: sqlite3.Connection, close: str = "30") -> None:
    """A seed-signature row no live action owns: what a pre-R3 delete (OBS-1) and a pre-R4
    date edit (DEF-060) left behind — written by the one seed writer, so its signature is
    exactly a save's."""
    _register_child(conn)
    assert write_seed_price(conn, symbol="CHLD", market=Market.US, on=_DAY,
                            close=Decimal(close), tz=GOLDEN_NOW.tzinfo).written
    conn.commit()


def test_an_orphan_seed_is_neither_overwritten_nor_taken_by_a_later_spinoff(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Signature alone cannot tell an orphan from this action's own seed — under R3 the
    delete of ANY spinoff on that day would have taken it. The save now records what it
    wrote (nothing, here), and the delete takes exactly that."""
    _seed_parent(golden_db)
    _orphan_seed(golden_db)
    before = _row(golden_db)
    saved = _save(api_client, price="55")
    assert saved["child_priced"] is None
    assert saved["child_price_skipped"]["reason"] == (
        "CHLD 在 2026-03-16 已有一筆手動輸入的起始價 30，起始價未寫入（既有價格不覆蓋）")
    assert _row(golden_db) == before
    action = int(saved["ids"][0])
    assert _listed(api_client, action)["child_price_restore"] is None
    assert api_client.delete(f"{_BASE}/{action}").json()["child_price_removed"] is None
    assert _row(golden_db) == before, "the delete took a seed this action never wrote"


def test_an_orphan_seed_survives_the_group_delete_and_the_batch_undo(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    _orphan_seed(golden_db)
    before = _row(golden_db)
    _save(api_client, price="55")
    api_client.delete(f"{_BASE}/set", params={
        "from_symbol": "PARN", "date": _DAY.isoformat(), "kind": "SPINOFF"})
    assert _row(golden_db) == before
    r = api_client.post("/api/import/commit", json={
        "kind": "corporate_actions",
        "csv_text": "account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from,cost_carry\n"
                    f"schwab,{_DAY.isoformat()},SPINOFF,PARN,CHLD,1,2,0.2\n",
        "ack_warnings": True})
    assert r.status_code == 200, r.text
    body = api_client.delete(f"/api/import/batches/{r.json()['import_batch_id']}").json()
    assert body["child_price_restore"] == [], body
    assert _row(golden_db) == before


def test_a_legacy_spinoff_row_keeps_r3s_signature_rule(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """A row saved before the record existed (``child_seed_json`` NULL) has only the
    signature to go by — R3's rule, unchanged for it: an intact seed leaves with it."""
    _seed_parent(golden_db)
    saved = _save(api_client)
    action = int(saved["ids"][0])
    golden_db.execute("UPDATE corporate_actions SET child_seed_json=NULL WHERE id=?",
                      (action,))
    golden_db.commit()
    assert _listed(api_client, action)["child_price_restore"]["restorable"] is True
    assert api_client.delete(f"{_BASE}/{action}").json()["child_price_removed"] is True
    assert _row(golden_db) is None


# ------------------------------------------------------------------ another action's seed
#
# A SECOND action cannot seed the same (child, day): the ledger refuses two corporate actions
# touching one symbol on one day (``same_date_action_conflict``, asserted below). So "another
# action's seed" is either the SAME event's other rows (a multi-account set: one save, one
# seed, recorded on every row) or legacy data written before that rule / the record existed.


def _second_holder(conn: sqlite3.Connection) -> None:
    insert_transaction(conn, account_id="moomoo_my", symbol="PARN", side=Side.BUY,
                       quantity=Decimal("40"), price=Decimal("100"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 12))
    conn.commit()


def test_a_second_spinoff_onto_the_same_child_and_day_is_refused_by_the_ledger(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    upsert_instrument(golden_db, Instrument(symbol="PAR2", market=Market.US,
                                            quote_ccy=Currency.USD, sector="Tech",
                                            name="Parent2"))
    insert_transaction(golden_db, account_id="schwab", symbol="PAR2", side=Side.BUY,
                       quantity=Decimal("100"), price=Decimal("80"),
                       fees=Decimal("0"), tax=Decimal("0"), trade_date=date(2026, 1, 12))
    golden_db.commit()
    _save(api_client, price="50")
    seeded = _row(golden_db)
    r = api_client.post(_BASE, json={
        "account_id": "schwab", "date": _DAY.isoformat(), "kind": "SPINOFF",
        "from_symbol": "PAR2", "to_symbol": "CHLD", "ratio_to": "1", "ratio_from": "4",
        "cost_carry": "0.1", "to_symbol_price": "60", "ack_warnings": True})
    assert r.status_code == 400 and "same_date_action_conflict" in r.text, r.text
    assert _row(golden_db) == seeded


def test_a_multi_account_set_writes_one_seed_that_only_the_set_delete_takes(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_parent(golden_db)
    _second_holder(golden_db)
    saved = _save(api_client, price="50")
    assert saved["written"] == 2 and saved["child_priced"] == "CHLD", saved
    for action in saved["ids"]:
        promise = _listed(api_client, int(action))["child_price_restore"]
        assert promise is not None and promise["restorable"] is True, promise
    single = api_client.delete(f"{_BASE}/{saved['ids'][0]}")
    assert single.status_code == 422, single.text     # F-32: one row of a set is refused
    assert _row(golden_db) is not None
    r = api_client.delete(f"{_BASE}/set", params={
        "from_symbol": "PARN", "date": _DAY.isoformat(), "kind": "SPINOFF"})
    assert r.status_code == 200 and r.json()["child_price_removed"] is True, r.text
    assert _row(golden_db) is None


def test_a_legacy_row_still_owning_the_seed_keeps_it_when_another_spinoff_leaves(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """``still_used`` over the R4 record: a row with NO record (legacy) that creates the same
    child on the same day still owns the seed by R3's rule, so the recorded owner's delete
    leaves it and says why. Written straight through the store — the API refuses the shape."""
    from portfolio_dash.data_ingestion.store import insert_corporate_action
    from portfolio_dash.shared.corporate_actions import CorporateActionKind

    _seed_parent(golden_db)
    saved = _save(api_client, price="50")
    legacy = insert_corporate_action(
        golden_db, account_id="schwab", action_date=_DAY, kind=CorporateActionKind.SPINOFF,
        from_symbol="PAR2", to_symbol="CHLD", ratio_to=Decimal("1"),
        ratio_from=Decimal("4"), cost_carry=Decimal("0.1"))
    golden_db.execute("UPDATE corporate_actions SET child_seed_json=NULL WHERE id=?",
                      (legacy,))
    golden_db.commit()
    seeded = _row(golden_db)
    body = api_client.delete(f"{_BASE}/{saved['ids'][0]}").json()
    assert body["child_price_removed"] is False
    assert "仍有其他分拆紀錄" in body["child_price_restore"]["reason"]
    assert _row(golden_db) == seeded


# ------------------------------------------------------------------ the form's own notice


def test_the_preview_says_beforehand_that_the_price_will_not_be_written(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """The owner reads the refusal while the form is still open, not only in a toast."""
    _seed_parent(golden_db)
    _register_child(golden_db)
    _quote(golden_db)
    r = api_client.post(f"{_BASE}/preview", json={
        "account_id": "schwab", "date": _DAY.isoformat(), "kind": "SPINOFF",
        "from_symbol": "PARN", "to_symbol": "CHLD", "ratio_to": "1", "ratio_from": "2",
        "cost_carry": "0.2", "to_symbol_price": "50"})
    assert r.status_code == 200, r.text
    skip = r.json()["child_price_skip"]
    assert skip is not None and skip["existing_close"] == "48.70"
    assert "起始價未寫入" in skip["reason"]


def test_the_seed_source_is_reserved_no_provider_writes_it() -> None:
    """``SEED_SOURCE`` is what makes a row removable; a provider stamping the same tag would
    make its quotes deletable by a corporate-action delete."""
    def subclasses(cls: type[ProviderBase]) -> list[type[ProviderBase]]:
        out = []
        for sub in cls.__subclasses__():
            out.append(sub)
            out.extend(subclasses(sub))
        return out

    import portfolio_dash.pricing.defaults  # noqa: F401 — imports every provider module

    names = {sub.name for sub in subclasses(ProviderBase)}
    assert len(names) >= 10, names
    assert SEED_SOURCE not in names


def test_a_seed_holding_another_value_than_the_recorded_one_is_not_taken(
    golden_db: sqlite3.Connection,
) -> None:
    """``expected_close``: a seed-signature row whose value is not the one this action
    recorded writing is not this action's row (legacy data can hold such a pair — R3 wrote
    one save's seed over another's), so the removal leaves it and says why."""
    from portfolio_dash.pricing.seed import pending_seed_removal, remove_seed_price

    _orphan_seed(golden_db, close="30")
    verdict = pending_seed_removal(golden_db, symbol="CHLD", on=_DAY,
                                   expected_close=Decimal("50"))
    assert verdict is not None and verdict.removable is False
    assert "不是這筆分拆登錄時寫入的 50" in (verdict.reason or "")
    assert remove_seed_price(golden_db, symbol="CHLD", on=_DAY,
                             expected_close=Decimal("50")) is not None
    assert _row(golden_db) is not None
    matching = remove_seed_price(golden_db, symbol="CHLD", on=_DAY,
                                 expected_close=Decimal("30.00"))
    assert matching is not None and matching.removed is True
    assert _row(golden_db) is None
