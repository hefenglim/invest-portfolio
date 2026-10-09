"""Options W6 — the 待確認到期 inbox (spec §7.2, OPT-D13): detector, skip, confirm, confirm-all.

Synthetic ledger, valuation day 2026-06-10 (every contract below but ``LIVE`` expired
2026-05-15). Each contract is one case of the detector's rule — "an option position whose
expiry is before the server's today while the BOOK's position is non-zero, per account ×
contract, not skipped":

* ``SHORT``    sold 2 @ 2.00 (fee 1.30)          → item, 賣方 2 口, premium 398.70 realized
* ``LONG``     bought 1 @ 2.50 (fee 0.65)        → item, 買方 1 口, premium 250.65 lost
* ``LIVE``     sold 1, expires 2026-12-18        → no item (not expired)
* ``CLOSED``   sold 1, bought back before expiry → no item (book flat)
* ``PARTIAL``  sold 3, 1 assigned on the expiry  → item, 賣方 2 口 (the book's remainder)
* ``SKIPPED``  sold 1, skipped                   → no item; listed by /skipped
* ``REFUSED``  sold 1, an ``expired`` BUY of 2   → item, 待釐清: the replay refused the close
* ``STRANDED`` sold 1, expired 1, then a back-dated second sale → item, 待釐清: the stored
  ``expired`` row no longer equals |open|

The router is mounted on the test's OWN app (``api/app.py`` wires it in W7b), with the repo's
dependency overrides.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api import option_expiry as inbox
from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.routers import option_expiry as router_mod
from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import (
    insert_transaction,
    list_transactions,
    load_ledger_bundle,
)
from portfolio_dash.data_ingestion.validate import EXPIRED_ROW_STRANDED_MESSAGE
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.options import REFUSED_CLOSE_REMEDY, CloseReason
from tests.conftest import _seed_golden, init_golden_base

D = Decimal
NOW = datetime(2026, 6, 10, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
EXPIRY = date(2026, 5, 15)
SHORT = "XYZ260515P00040000"
LONG = "XYZ260515C00060000"
LIVE = "XYZ261218P00050000"
CLOSED = "ABC260417P00030000"
PARTIAL = "DEF260515P00020000"
SKIPPED = "GHI260515P00010000"
REFUSED = "JKL260515P00010000"
STRANDED = "MNO260515P00010000"
_ACCT = "schwab"


def _trade(conn: sqlite3.Connection, symbol: str, side: Side, qty: str, price: str,
           on: date, *, fee: str = "0", short: bool = False,
           close_reason: CloseReason | None = None) -> int:
    """A raw ledger row — no door, so a 待釐清 shape can be planted on purpose."""
    return insert_transaction(conn, account_id=_ACCT, symbol=symbol, side=side,
                              quantity=D(qty), price=D(price), fees=D(fee), tax=D("0"),
                              trade_date=on, short_sale=short, close_reason=close_reason)


def seed_expiry_ledger(conn: sqlite3.Connection) -> None:
    """The golden stock ledger plus the eight contracts of the module docstring."""
    _seed_golden(conn)
    for sym in (SHORT, LONG, LIVE, CLOSED, PARTIAL, SKIPPED, REFUSED, STRANDED):
        register_option_contract(conn, sym)
    _trade(conn, SHORT, Side.SELL, "200", "2.00", date(2026, 3, 2), fee="1.30", short=True)
    _trade(conn, LONG, Side.BUY, "100", "2.50", date(2026, 3, 3), fee="0.65")
    _trade(conn, LIVE, Side.SELL, "100", "1.00", date(2026, 3, 4), fee="0.65", short=True)
    _trade(conn, CLOSED, Side.SELL, "100", "1.00", date(2026, 3, 5), fee="0.65", short=True)
    _trade(conn, CLOSED, Side.BUY, "100", "0.20", date(2026, 4, 1), fee="0.65")
    _trade(conn, PARTIAL, Side.SELL, "300", "1.50", date(2026, 3, 2), fee="1.95", short=True)
    _trade(conn, PARTIAL, Side.BUY, "100", "0", EXPIRY, close_reason="assigned")
    _trade(conn, SKIPPED, Side.SELL, "100", "0.50", date(2026, 3, 2), fee="0.65", short=True)
    _trade(conn, REFUSED, Side.SELL, "100", "0.50", date(2026, 3, 2), fee="0.65", short=True)
    _trade(conn, REFUSED, Side.BUY, "200", "0", EXPIRY, close_reason="expired")
    _trade(conn, STRANDED, Side.SELL, "100", "0.50", date(2026, 3, 2), fee="0.65", short=True)
    _trade(conn, STRANDED, Side.BUY, "100", "0", EXPIRY, close_reason="expired")
    _trade(conn, STRANDED, Side.SELL, "100", "0.50", date(2026, 4, 1), fee="0.65", short=True)
    conn.commit()


def _memory_db(*, seed: bool = True, boot: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_golden_base(conn)
    if seed:
        seed_expiry_ledger(conn)
    else:
        _seed_golden(conn)
    if boot:
        inbox.ensure_tables(conn)   # what api/app.py's lifespan does (W7b)
        if seed:
            inbox.mark_skipped(conn, _ACCT, SKIPPED, now=NOW)
    return conn


def _app(conn: sqlite3.Connection) -> FastAPI:
    app = FastAPI()
    app.include_router(router_mod.router, prefix="/api")
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_now] = lambda: NOW
    return app


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = _memory_db()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def client(db: sqlite3.Connection) -> Iterator[TestClient]:
    enable_socket()
    try:
        yield TestClient(_app(db))
    finally:
        disable_socket(allow_unix_socket=True)


def _items(client: TestClient) -> dict[str, dict[str, Any]]:
    r = client.get("/api/option-expiry")
    assert r.status_code == 200
    return {i["symbol"]: i for i in r.json()["items"]}


# --- detector -------------------------------------------------------------------------------


def test_the_detector_lists_exactly_the_held_expired_positions(client: TestClient) -> None:
    items = _items(client)
    assert set(items) == {SHORT, LONG, PARTIAL, REFUSED, STRANDED}
    assert client.get("/api/option-expiry/count").json() == {"count": 5}


def test_an_item_carries_the_spec_keys_and_wording(client: TestClient) -> None:
    items = _items(client)
    short = items[SHORT]
    assert set(short) == {"account_id", "account_name", "symbol", "contract", "direction",
                          "contracts", "expiry", "days_past", "premium_text", "text",
                          "confirmable", "remedy"}
    assert short == {
        "account_id": "schwab", "account_name": "{account:schwab}", "symbol": SHORT,
        "contract": "XYZ 2026-05-15 40 Put", "direction": "short", "contracts": "2",
        "expiry": "2026-05-15", "days_past": 26, "premium_text": "398.70 USD",
        "text": ("{account:schwab} XYZ 2026-05-15 40 Put・賣方 2 口・到期日已過 26 天 → "
                 "到期作廢，權利金 398.70 USD 全數實現"),
        "confirmable": True, "remedy": None,
    }
    long = items[LONG]
    assert (long["direction"], long["contracts"], long["premium_text"]) == (
        "long", "1", "250.65 USD")
    assert long["text"] == ("{account:schwab} XYZ 2026-05-15 60 Call・買方 1 口・到期日已過 "
                            "26 天 → 到期作廢，權利金 250.65 USD 全數損失")


def test_a_partly_assigned_position_lists_the_books_remainder(client: TestClient) -> None:
    item = _items(client)[PARTIAL]
    assert (item["direction"], item["contracts"], item["confirmable"]) == ("short", "2", True)


def test_a_contract_with_a_refused_close_is_not_confirmable(client: TestClient) -> None:
    item = _items(client)[REFUSED]
    assert (item["confirmable"], item["remedy"]) == (False, REFUSED_CLOSE_REMEDY)


def test_a_stored_expiry_the_position_survived_is_not_confirmable(client: TestClient) -> None:
    item = _items(client)[STRANDED]
    assert (item["confirmable"], item["remedy"]) == (False, EXPIRED_ROW_STRANDED_MESSAGE)


def test_the_count_is_zero_on_a_stock_ledger() -> None:
    conn = _memory_db(seed=False)
    try:
        assert inbox.pending_count(conn, now=NOW) == 0
        assert inbox.detect(conn, now=NOW) == []
    finally:
        conn.close()


def test_the_detector_reads_the_servers_today() -> None:
    """The same ledger a day before the expiry lists nothing."""
    conn = _memory_db()
    try:
        before = datetime(2026, 5, 15, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        assert inbox.detect(conn, now=before) == []
        after = datetime(2026, 5, 16, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        assert {i.symbol for i in inbox.detect(conn, now=after)} >= {SHORT, LONG}
    finally:
        conn.close()


# --- confirm --------------------------------------------------------------------------------


def _rows(conn: sqlite3.Connection, symbol: str) -> list[Any]:
    return [t for t in list_transactions(conn) if t.symbol == symbol]


def test_confirm_writes_exactly_the_price_zero_expiry_row(
    client: TestClient, db: sqlite3.Connection,
) -> None:
    r = client.post("/api/option-expiry/confirm", json={"account_id": _ACCT, "symbol": SHORT})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and isinstance(body["txn_id"], int)
    row = next(t for t in _rows(db, SHORT) if t.id == body["txn_id"])
    assert (row.account_id, row.side, row.quantity, row.price, row.fees, row.tax,
            row.close_reason, row.trade_date, row.short_sale) == (
        _ACCT, Side.BUY, D("200"), D("0"), D("0"), D("0"), "expired", EXPIRY, False)
    # The replay books the whole remaining premium as the short cover (spec §4.1).
    book = build_book(load_ledger_bundle(db), allow_oversell=True)
    [cover] = [x for x in book.realized.rows if x.symbol == SHORT]
    assert (cover.kind, cover.realized) == ("short_cover", D("398.70"))
    assert SHORT not in _items(client)


def test_confirm_on_a_long_writes_a_sell(client: TestClient, db: sqlite3.Connection) -> None:
    r = client.post("/api/option-expiry/confirm", json={"account_id": _ACCT, "symbol": LONG})
    assert r.status_code == 200, r.text
    row = next(t for t in _rows(db, LONG) if t.id == r.json()["txn_id"])
    assert (row.side, row.quantity, row.price, row.close_reason, row.trade_date) == (
        Side.SELL, D("100"), D("0"), "expired", EXPIRY)


def test_confirm_on_the_partial_remainder_closes_it(
    client: TestClient, db: sqlite3.Connection,
) -> None:
    r = client.post("/api/option-expiry/confirm",
                    json={"account_id": _ACCT, "symbol": PARTIAL})
    assert r.status_code == 200, r.text
    row = next(t for t in _rows(db, PARTIAL) if t.id == r.json()["txn_id"])
    assert (row.side, row.quantity, row.close_reason) == (Side.BUY, D("200"), "expired")
    assert PARTIAL not in _items(client)


def test_an_unconfirmable_item_is_refused_and_nothing_is_written(
    client: TestClient, db: sqlite3.Connection,
) -> None:
    before = len(list_transactions(db))
    r = client.post("/api/option-expiry/confirm",
                    json={"account_id": _ACCT, "symbol": REFUSED})
    assert r.status_code == 422
    assert r.json()["error"] == {"code": "option_expiry_unconfirmable",
                                 "message": REFUSED_CLOSE_REMEDY}
    assert len(list_transactions(db)) == before


def test_confirm_refuses_a_contract_the_inbox_does_not_list(
    client: TestClient, db: sqlite3.Connection,
) -> None:
    before = len(list_transactions(db))
    for sym in (LIVE, CLOSED, SKIPPED, "NOPE"):
        r = client.post("/api/option-expiry/confirm", json={"account_id": _ACCT, "symbol": sym})
        assert r.status_code == 422, sym
        assert r.json()["error"]["code"] == "option_expiry_not_pending"
        assert r.json()["error"]["message"] == inbox.NOT_PENDING_MESSAGE
    assert len(list_transactions(db)) == before


def test_confirm_all_reports_each_item_on_its_own(
    client: TestClient, db: sqlite3.Connection,
) -> None:
    r = client.post("/api/option-expiry/confirm-all", json={})
    assert r.status_code == 200, r.text
    results = {x["symbol"]: x for x in r.json()["results"]}
    assert set(results) == {SHORT, LONG, PARTIAL, REFUSED, STRANDED}
    for sym in (SHORT, LONG, PARTIAL):
        assert results[sym]["ok"] is True and isinstance(results[sym]["txn_id"], int)
        assert "error" not in results[sym]
    assert results[REFUSED] == {"account_id": _ACCT, "symbol": REFUSED, "ok": False,
                                "error": REFUSED_CLOSE_REMEDY}
    assert results[STRANDED] == {"account_id": _ACCT, "symbol": STRANDED, "ok": False,
                                 "error": EXPIRED_ROW_STRANDED_MESSAGE}
    assert set(_items(client)) == {REFUSED, STRANDED}
    expired = [t for t in list_transactions(db) if t.close_reason == "expired"]
    assert {t.symbol for t in expired} == {SHORT, LONG, PARTIAL, REFUSED, STRANDED}


# --- skip -----------------------------------------------------------------------------------


def test_skip_hides_an_item_and_unskip_restores_it(client: TestClient) -> None:
    ok = client.post("/api/option-expiry/skip", json={"account_id": _ACCT, "symbol": SHORT})
    assert ok.json() == {"ok": True}
    assert SHORT not in _items(client)
    assert client.get("/api/option-expiry/count").json() == {"count": 4}
    skipped = client.get("/api/option-expiry/skipped").json()["items"]
    assert {s["symbol"] for s in skipped} == {SKIPPED, SHORT}
    [entry] = [s for s in skipped if s["symbol"] == SHORT]
    assert {k: entry[k] for k in ("account_id", "account_name", "symbol", "contract",
                                  "expiry")} == {
        "account_id": _ACCT, "account_name": "{account:schwab}", "symbol": SHORT,
        "contract": "XYZ 2026-05-15 40 Put", "expiry": "2026-05-15"}
    assert entry["skipped_at"] == NOW.isoformat()
    again = client.post("/api/option-expiry/unskip",
                        json={"account_id": _ACCT, "symbol": SHORT})
    assert again.json() == {"ok": True}
    assert SHORT in _items(client)
    assert [s["symbol"] for s in client.get("/api/option-expiry/skipped").json()["items"]] == [
        SKIPPED]


def test_skip_refuses_a_symbol_that_is_not_a_contract(client: TestClient) -> None:
    r = client.post("/api/option-expiry/skip", json={"account_id": _ACCT, "symbol": "AAPL"})
    assert r.status_code == 422
    assert "不是選擇權合約代號" in r.json()["error"]["message"]


# --- a GET never writes (DEF-065) -----------------------------------------------------------

_WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|BEGIN)\b", re.I)
_GETS = ("/api/option-expiry", "/api/option-expiry/count", "/api/option-expiry/skipped")


@pytest.mark.parametrize("booted", [True, False], ids=["booted", "no-skip-table"])
def test_the_gets_open_no_write_transaction(tmp_path: Path, booted: bool) -> None:
    """Every GET answers on a file database while ANOTHER connection holds the write lock,
    issues no write statement, and leaves no transaction open — with the skip table created
    at boot, and without it (a GET must read "no skips", never create the table)."""
    path = tmp_path / "inbox.db"
    setup = sqlite3.connect(path)
    setup.row_factory = sqlite3.Row
    init_golden_base(setup)
    seed_expiry_ledger(setup)
    if booted:
        inbox.ensure_tables(setup)
        inbox.mark_skipped(setup, _ACCT, SKIPPED, now=NOW)
    setup.close()

    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")         # the write lock, held for the whole test
    conn = sqlite3.connect(path, timeout=0.2, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    enable_socket()
    try:
        client = TestClient(_app(conn))
        for path_ in _GETS:
            r = client.get(path_)
            assert r.status_code == 200, (path_, r.text)
            assert not conn.in_transaction, path_
        # Booted: SKIPPED is skipped. Without the table nothing can be, so it is listed.
        assert client.get("/api/option-expiry/count").json() == {"count": 5 if booted else 6}
    finally:
        disable_socket(allow_unix_socket=True)
        holder.execute("ROLLBACK")
        holder.close()
        conn.close()
    writes = [s for s in statements if _WRITE.match(s)]
    assert not writes, writes
    exists = sqlite3.connect(path).execute(
        "SELECT 1 FROM sqlite_master WHERE name='option_expiry_skips'").fetchone()
    assert (exists is not None) is booted
