"""SQL-statement and replay ceilings of the medium routes (PERF plan §7.2, spec §16).

The medium routes are the pages' own reads that are neither the dashboard nor history:
信號, 輸入中心, 標的清單, 帳本, 現金, 待確認匯入. Measured first, on the demo corpus with two years
of synthetic quarterly dividend events (10 instruments, 3 accounts), with SQLite's own trace
callback — every statement the request issues — and the number of ledger replays
(``build_book`` / ``iter_books`` calls), then fixed only where an N+1 or a redundant read
showed:

======================================  =========  ========  =====================================
route                                   before     after     what changed
======================================  =========  ========  =====================================
GET /api/input/context                  137        55        one ActionIndex for the request (each
                                                             ``current_shares`` re-read the whole
                                                             corporate-action ledger: 30 reads);
                                                             only (account, symbol) pairs with a
                                                             share event are asked at all
GET /api/instruments                    133        124       ``board_status`` read once, not per
                                                             instrument (10 reads)
GET /api/dividend-inbox (and /count)    459        366       the detection's one ActionIndex is
                                                             handed to every entitlement read (58
                                                             full reads of ``corporate_actions``);
                                                             the dividend model read once per
                                                             (account, market), not per event
======================================  =========  ========  =====================================

Every response is byte-identical before and after (measured: the bodies of all four routes
compared byte for byte). The other medium routes showed no N+1 and no redundant replay — each
replays the ledger at most once — and their measured counts are pinned below as ceilings, so
a new per-row read or a second replay fails here.

Not fixed here, because the reads belong to modules outside the routers (recorded in the
package report): ``data_ingestion/holdings.py::holds_position`` reads ``accounts`` once per
symbol (10 of ``/api/signals``' 99 and ``/api/instruments``' statements), and
``validate_dividend`` reads the account's binding per inbox item.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.app import create_app
from portfolio_dash.api.deps import get_conn, get_now
from portfolio_dash.api.dividend_inbox import ensure_tables as ensure_inbox_tables
from portfolio_dash.portfolio import cost_basis
from tests.conftest import init_golden_base
from tests.reference import corpora

_FROM = re.compile(r"\bFROM\s+(\w+)", re.IGNORECASE)


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[sqlite3.Connection,
                                                                        datetime]]:
    """The demo corpus plus quarterly synthetic dividend events for every stock over the two
    years before its clock — the inbox's detection loop is symbol × event × account."""
    base = tmp_path_factory.getbasetemp() / corpora.TEMPLATE_DIR_NAME
    path = corpora.copy_db(corpora.ensure_template("demo", base),
                           tmp_path_factory.mktemp("medium_routes") / "demo.db")
    conn = corpora.open_db(path)
    init_golden_base(conn)  # idempotent: every table a medium route reads
    ensure_inbox_tables(conn)
    now = corpora.CORPORA["demo"].now
    for s in conn.execute("SELECT symbol, market, quote_ccy FROM instruments "
                          "WHERE asset_type = 'stock'").fetchall():
        day = now.date() - timedelta(days=730)
        while day < now.date():
            conn.execute(
                "INSERT OR IGNORE INTO dividend_events (instrument, market, ex_date, pay_date, "
                "cash_amount, stock_amount, currency, source, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (s["symbol"], s["market"], day.isoformat(),
                 (day + timedelta(days=20)).isoformat(), "1.25",
                 "0.5" if s["market"] == "TW" else None, s["quote_ccy"], "synthetic",
                 now.isoformat()))
            day += timedelta(days=91)
    conn.commit()
    yield conn, now
    conn.close()


@pytest.fixture
def client(demo_db: tuple[sqlite3.Connection, datetime]) -> Iterator[TestClient]:
    conn, now = demo_db
    enable_socket()
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_now] = lambda: now
    try:
        yield TestClient(app)
    finally:
        disable_socket(allow_unix_socket=True)


@contextmanager
def _replays_counted(monkeypatch: pytest.MonkeyPatch) -> Iterator[Counter[str]]:
    """Count every ledger replay, however the caller imported the function: each module
    attribute that IS ``build_book`` / ``iter_books`` is swapped for a counting wrapper."""
    calls: Counter[str] = Counter()
    for name in ("build_book", "iter_books"):
        original = getattr(cost_basis, name)

        def counting(*a: Any, _name: str = name, _fn: Callable[..., Any] = original,
                     **kw: Any) -> Any:
            calls[_name] += 1
            return _fn(*a, **kw)

        for module in list(sys.modules.values()):
            if getattr(module, name, None) is original:
                monkeypatch.setattr(module, name, counting)
    yield calls


def _measure(client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
             path: str, params: dict[str, Any] | None = None,
             ) -> tuple[int, list[str], Counter[str]]:
    client.get(path, params=params)  # warm: lazy imports and first-use caches
    stmts: list[str] = []
    conn.set_trace_callback(lambda sql: stmts.append(" ".join(sql.split())))
    try:
        with monkeypatch.context() as mp, _replays_counted(mp) as replays:
            r = client.get(path, params=params)
    finally:
        conn.set_trace_callback(None)
    assert r.status_code == 200, (path, r.text[:300])
    return r.status_code, stmts, replays


def _full_reads(stmts: list[str], table: str) -> int:
    return sum(1 for s in stmts if s.upper().startswith("SELECT") and " WHERE " not in s.upper()
               and (m := _FROM.search(s)) is not None and m.group(1) == table)


def _account(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT account_id FROM transactions ORDER BY id LIMIT 1")
               .fetchone()[0])


#: route -> (query, statement ceiling, replay ceiling), measured on this corpus after the
#: fixes above (the three fixed routes) or as found (every other route). A ceiling is the
#: measured count: a new per-row read or a second replay must be a reviewed change here.
_CEILINGS: dict[str, tuple[Callable[[sqlite3.Connection], dict[str, Any]], int, int]] = {
    "/api/signals": (lambda c: {}, 99, 0),
    "/api/input/context": (lambda c: {}, 55, 0),
    "/api/input/holdings": (lambda c: {"account": _account(c)}, 20, 1),
    "/api/import/batches": (lambda c: {"limit": 20}, 2, 0),
    "/api/instruments": (lambda c: {}, 124, 0),
    "/api/instruments/sectors": (lambda c: {}, 1, 0),
    "/api/ledgers/transactions": (lambda c: {"limit": 50}, 6, 0),
    "/api/ledgers/dividends": (lambda c: {"limit": 50}, 9, 1),
    "/api/ledgers/fx": (lambda c: {"limit": 50}, 5, 0),
    "/api/ledgers/cash": (lambda c: {"limit": 50}, 5, 0),
    "/api/ledgers/openings": (lambda c: {}, 5, 0),
    "/api/ledgers/corporate-actions": (lambda c: {}, 12, 1),
    "/api/cash": (lambda c: {"limit": 50}, 13, 1),
    "/api/cash/statement": (lambda c: {"account": _account(c)}, 10, 1),
    "/api/dividend-inbox": (lambda c: {}, 366, 0),
    "/api/dividend-inbox/count": (lambda c: {}, 366, 0),
    "/api/dividend-inbox/skipped": (lambda c: {}, 3, 0),
}


@pytest.mark.parametrize("path", sorted(_CEILINGS))
def test_every_medium_route_stays_under_its_ceilings(
    client: TestClient, demo_db: tuple[sqlite3.Connection, datetime],
    monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    conn, _ = demo_db
    params, max_sql, max_replays = _CEILINGS[path]
    _, stmts, replays = _measure(client, conn, monkeypatch, path, params(conn))
    assert len(stmts) <= max_sql, (path, len(stmts), Counter(stmts).most_common(5))
    assert sum(replays.values()) <= max_replays, (path, dict(replays))


def test_the_replay_counter_sees_a_replay(
    client: TestClient, demo_db: tuple[sqlite3.Connection, datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: the cash page replays the ledger exactly once (its one snapshot's
    dividend refusals), so a counter that saw nothing could not pass the ceilings above."""
    conn, _ = demo_db
    _, _, replays = _measure(client, conn, monkeypatch, "/api/cash", {"limit": 50})
    assert dict(replays) == {"build_book": 1}


def test_the_input_context_reads_the_action_ledger_once_and_asks_no_empty_position(
    client: TestClient, demo_db: tuple[sqlite3.Connection, datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before: one full read of ``corporate_actions`` per account × instrument (30), and four
    share statements for every pair, held or not. A watchlist-only instrument (no ledger row
    in any account) must now cost nothing at all."""
    conn, _ = demo_db
    _, before, _ = _measure(client, conn, monkeypatch, "/api/input/context")
    assert _full_reads(before, "corporate_actions") == 1
    for i in range(5):
        conn.execute("INSERT INTO instruments (symbol, market, quote_ccy, sector, name) "
                     "VALUES (?, 'US', 'USD', 'Technology', ?)", (f"ZZW{i}", f"觀察 {i}"))
    conn.commit()
    try:
        _, after, _ = _measure(client, conn, monkeypatch, "/api/input/context")
    finally:
        conn.execute("DELETE FROM instruments WHERE symbol LIKE 'ZZW%'")
        conn.commit()
    assert len(after) == len(before)


def test_the_instrument_list_reads_board_status_once(
    client: TestClient, demo_db: tuple[sqlite3.Connection, datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, _ = demo_db
    _, stmts, _ = _measure(client, conn, monkeypatch, "/api/instruments")
    board = [s for s in stmts if "board_status" in s]
    assert board == ["SELECT symbol, board_status FROM instruments"]


@pytest.mark.parametrize("path", ["/api/dividend-inbox", "/api/dividend-inbox/count"])
def test_the_inbox_detection_reads_the_action_ledger_once(
    client: TestClient, demo_db: tuple[sqlite3.Connection, datetime],
    monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    """Before: 58 full reads of ``corporate_actions`` — one per entitlement read
    (``shares_on`` without the detection's index) and per validated item."""
    conn, _ = demo_db
    _, stmts, _ = _measure(client, conn, monkeypatch, path)
    assert _full_reads(stmts, "corporate_actions") == 1
    # The detection found items to validate: the ceiling above is not met by an empty loop.
    body = client.get("/api/dividend-inbox").json()
    assert len(body["rows"]) >= 10


def test_the_count_and_the_list_still_agree(client: TestClient) -> None:
    rows = client.get("/api/dividend-inbox").json()
    count = client.get("/api/dividend-inbox/count").json()
    assert rows["total_count"] == len(rows["rows"]) == count["count"]
