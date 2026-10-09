"""I-2 / I-3: the corporate-action CSV door and the import-batch undo treat an EXCHANGE's
per-symbol settings exactly like the manual form and the ledger tab's 刪除 do.

I-2 (F-3's CSV half). The manual door moved the target weight INSIDE the action's
transaction and recorded it (``weight_move_json``); the CSV door still moved it in the router
AFTER ``commit_preview`` had committed, and recorded nothing — so a failure there left the
EXCHANGE standing with the weight on the dead ticker, and deleting the imported row could never
move the weight back (``_delete_actions`` restores only what a row recorded). The band, by
contrast, already moved inside the row writer and was recorded: the two settings were
asymmetric at this door.

I-3 (F-4). ``provenance.delete_batch`` removed a batch's corporate actions with a bare
``DELETE … WHERE import_batch_id=?``: the band and the weight stayed on the new ticker, a
linked fee would have outlived its action, no audit row was written, and — found while fixing
it — a SPLIT undone by batch left its symbol's stored closes in post-split terms, because the
price reconcile the ledger delete runs was never run. The undo now goes through the ledger
tab's own ``_delete_actions`` (one delete, not two) and re-expresses the prices.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import (
    MovedBand,
    MovedWeight,
    get_instrument,
    list_corporate_actions,
    upsert_instrument,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.strategy.target_weights import load_target_weights, save_target_weights
from tests.conftest import GOLDEN_NOW

D = Decimal
DAY = date(2026, 6, 10)
LATER = date(2026, 6, 20)
_HEADER = "account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from\n"


def _register(conn: sqlite3.Connection, *symbols: str) -> None:
    for sym in symbols:
        upsert_instrument(conn, Instrument(symbol=sym, market=Market.TW,
                                           quote_ccy=Currency.TWD, sector="Semis",
                                           name=sym, board="TWSE"))
    conn.commit()


def _band(conn: sqlite3.Connection, symbol: str) -> tuple[Decimal | None, Decimal | None]:
    inst = get_instrument(conn, symbol)
    assert inst is not None
    return inst.target_low, inst.target_high


def _set_band(conn: sqlite3.Connection, symbol: str) -> None:
    conn.execute("UPDATE instruments SET target_low='500', target_high='700', "
                 "target_set_at='2026-01-05' WHERE symbol=?", (symbol,))
    conn.commit()


def _import(client: TestClient, *lines: str) -> dict[str, Any]:
    r = client.post("/api/import/commit", json={
        "kind": "corporate_actions", "csv_text": _HEADER + "".join(lines),
        "ack_warnings": True})
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def _closes(conn: sqlite3.Connection, symbol: str) -> list[tuple[str, str]]:
    return [(r["close"], r["split_basis"]) for r in conn.execute(
        "SELECT close, split_basis FROM prices WHERE instrument=? ORDER BY as_of_date",
        (symbol,)).fetchall()]


# ------------------------------------------------------------------ I-2: the CSV door


def test_the_csv_exchange_records_the_weight_and_the_band_it_moved(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """★ Both settings recorded on the imported row — the manual door's F-3 / DEF-021 shape."""
    _register(golden_db, "NEWCO")
    _set_band(golden_db, "2330")
    save_target_weights(golden_db, {"2330": D("0.25")}, now=GOLDEN_NOW)
    body = _import(api_client, f"tw_broker,{DAY.isoformat()},EXCHANGE,2330,NEWCO,1,1\n")
    assert body["written"] == 1
    assert body["weights_moved"] == [
        {"from_symbol": "2330", "to_symbol": "NEWCO", "weight": "0.25"}]
    (row,) = list_corporate_actions(golden_db)
    assert row.weight_move == MovedWeight(from_symbol="2330", to_symbol="NEWCO",
                                          weight=D("0.25"))
    assert isinstance(row.band_move, MovedBand) and row.band_move.to_symbol == "NEWCO"
    assert load_target_weights(golden_db) == {"NEWCO": D("0.25")}


def test_a_failing_weight_move_takes_the_imported_rows_with_it(
    api_client: TestClient, golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ DEF-020's shape at the bulk door: the move and the rows share ONE transaction, so a
    failure leaves neither the EXCHANGE, nor its band move, nor a half-moved weight."""
    _register(golden_db, "NEWCO")
    _set_band(golden_db, "2330")
    save_target_weights(golden_db, {"2330": D("0.25")}, now=GOLDEN_NOW)

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("weight store unavailable")

    monkeypatch.setattr("portfolio_dash.api.routers.input_center.move_target_weight", _boom)
    with pytest.raises(RuntimeError, match="weight store unavailable"):
        _import(api_client, f"tw_broker,{DAY.isoformat()},EXCHANGE,2330,NEWCO,1,1\n")
    assert list_corporate_actions(golden_db) == []
    assert load_target_weights(golden_db) == {"2330": D("0.25")}
    assert _band(golden_db, "2330") == (D("500"), D("700"))
    assert golden_db.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0


def test_deleting_the_imported_exchange_on_its_tab_moves_the_weight_back(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _register(golden_db, "NEWCO")
    save_target_weights(golden_db, {"2330": D("0.25")}, now=GOLDEN_NOW)
    _import(api_client, f"tw_broker,{DAY.isoformat()},EXCHANGE,2330,NEWCO,1,1\n")
    (row,) = list_corporate_actions(golden_db)
    d = api_client.delete(f"/api/ledgers/corporate-actions/{row.id}")
    assert d.status_code == 200, d.text
    assert d.json()["weight_restored"] is True
    assert load_target_weights(golden_db) == {"2330": D("0.25")}


# ------------------------------------------------------------ I-3: the batch undo


def test_undoing_the_batch_gives_back_the_band_and_the_weight(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """★ The F-4 finding: 復原 on the import history used to leave both settings on NEWCO."""
    _register(golden_db, "NEWCO")
    _set_band(golden_db, "2330")
    save_target_weights(golden_db, {"2330": D("0.25")}, now=GOLDEN_NOW)
    batch = _import(api_client, f"tw_broker,{DAY.isoformat()},EXCHANGE,2330,NEWCO,1,1\n")
    assert _band(golden_db, "NEWCO") == (D("500"), D("700"))

    d = api_client.delete(f"/api/import/batches/{batch['import_batch_id']}")
    assert d.status_code == 200, d.text
    body = d.json()
    assert body["deleted"] == 1
    assert list_corporate_actions(golden_db) == []
    assert _band(golden_db, "2330") == (D("500"), D("700"))
    assert _band(golden_db, "NEWCO") == (None, None)
    assert load_target_weights(golden_db) == {"2330": D("0.25")}
    assert [b["restored"] for b in body["band_restore"]] == [True]
    assert [w["restored"] for w in body["weight_restore"]] == [True]
    # …and the delete left the same audit trail the ledger tab's 刪除 leaves.
    assert golden_db.execute(
        "SELECT COUNT(*) FROM ledger_audit WHERE table_name='corporate_actions' "
        "AND action='delete'").fetchone()[0] == 1


def test_a_touched_band_is_left_alone_with_the_reason_on_the_undo(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _register(golden_db, "NEWCO")
    _set_band(golden_db, "2330")
    batch = _import(api_client, f"tw_broker,{DAY.isoformat()},EXCHANGE,2330,NEWCO,1,1\n")
    golden_db.execute("UPDATE instruments SET target_low='520' WHERE symbol='NEWCO'")
    golden_db.commit()
    body = api_client.delete(f"/api/import/batches/{batch['import_batch_id']}").json()
    (verdict,) = body["band_restore"]
    assert verdict["restored"] is False and "NEWCO" in verdict["reason"]
    assert _band(golden_db, "NEWCO") == (D("520"), D("700"))
    assert body["weight_restore"] == []


def test_a_chain_in_one_batch_is_undone_newest_first(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """2330 → NEWCO → NEWCO2 in one file carried the band two hops; only undoing the SECOND
    hop first finds each destination still holding exactly the band it recorded."""
    _register(golden_db, "NEWCO", "NEWCO2")
    _set_band(golden_db, "2330")
    save_target_weights(golden_db, {"2330": D("0.25")}, now=GOLDEN_NOW)
    batch = _import(api_client,
                    f"tw_broker,{DAY.isoformat()},EXCHANGE,2330,NEWCO,1,1\n",
                    f"tw_broker,{LATER.isoformat()},EXCHANGE,NEWCO,NEWCO2,1,1\n")
    assert batch["written"] == 2
    assert _band(golden_db, "NEWCO2") == (D("500"), D("700"))
    assert load_target_weights(golden_db) == {"NEWCO2": D("0.25")}
    body = api_client.delete(f"/api/import/batches/{batch['import_batch_id']}").json()
    assert body["deleted"] == 2
    assert _band(golden_db, "2330") == (D("500"), D("700"))
    assert _band(golden_db, "NEWCO") == (None, None)
    assert _band(golden_db, "NEWCO2") == (None, None)
    assert load_target_weights(golden_db) == {"2330": D("0.25")}


def test_undoing_a_split_batch_restores_the_stored_closes(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    """Found while fixing I-3: the bare batch DELETE never ran the price reconcile, so the
    SPLIT's re-expressed closes outlived the SPLIT itself."""
    assert _closes(golden_db, "2330") == [("600", "1")]
    batch = _import(api_client, f"tw_broker,{DAY.isoformat()},SPLIT,2330,2330,10,1\n")
    assert _closes(golden_db, "2330") == [("6000", "10")]
    body = api_client.delete(f"/api/import/batches/{batch['import_batch_id']}").json()
    assert body["prices_restated"] == 1
    assert _closes(golden_db, "2330") == [("600", "1")]


def test_a_batch_without_corporate_actions_keeps_its_payload(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    r = api_client.post("/api/import/commit", json={
        "kind": "cash", "csv_text": "account,date,kind,ccy,amount\n"
                                    "tw_broker,2026-06-01,DEPOSIT,TWD,1000\n"})
    assert r.status_code == 200, r.text
    body = api_client.delete(f"/api/import/batches/{r.json()['import_batch_id']}").json()
    assert set(body) == {"deleted", "import_batch_id"}
