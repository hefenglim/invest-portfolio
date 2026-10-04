"""CR-12 / CR-13 (pre-merge code review, 2026-10-04): the cash guards read the ledgers ONCE.

CR-12. ``fx_edit_negative_guard`` asked ``_pool_dip`` about up to four pools, and every call
re-read all five ledgers a pool timeline is built from (cash movements, FX conversions,
transactions, dividends, instruments) — after ``fx_change_guard`` had already read them through
``cash_pool_fn``, whose own docstring says "Bound ONCE per request/import, never per row"
(trap #21). Measured before the fix on the golden ledger: a same-account FX edit issued 17
full-table SELECTs over those five tables (fx_conversions 5, each other table 3), a
cross-account edit 27; ``edit_movement`` 13 and ``fx_delete_guard`` 7 the same way. The
doors now load one snapshot (``api/routers/cash.py::load_cash_ledgers``) and every guard in
the request reads it.

What is counted: full-table ``SELECT … FROM <ledger>`` statements (no ``WHERE``) the request
issues, read off SQLite's own trace callback — not a mock of the store, so a second read by
any route can be seen. The ceiling per table is one snapshot read, plus one for the door's
own by-id lookup where it has one (``get_fx_conversion`` / ``get_cash_movement`` list the
table to find the row — a store-level shape this review did not touch).

CR-13. ``replay_guard._pool_refusal`` re-implemented ``cash._pool_dip`` (pool_lines →
running_eod → caused_dip, before vs after) because ``_pool_dip`` took no would-be trades or
dividends. There is now one copy, and the structural test below holds it there.

Behaviour is pinned elsewhere, unchanged: DEF-090/091/092/093 and DEF-049's contract tests
assert every refusal's code, status and sentence.
"""

import ast
import re
import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.store import (
    get_fx_conversion,
    insert_cash_movement,
)
from portfolio_dash.shared.enums import Currency

_LEDGERS = ("cash_movements", "fx_conversions", "transactions", "dividends", "instruments")
_FROM = re.compile(r"\bFROM\s+(\w+)", re.IGNORECASE)
_PKG = Path(__file__).resolve().parents[2] / "portfolio_dash"


def _full_reads(conn: sqlite3.Connection, call: Callable[[], Any]) -> tuple[Any, Counter[str]]:
    """Run *call* and count the full-table SELECTs it issues per ledger table."""
    seen: Counter[str] = Counter()

    def trace(sql: str) -> None:
        stmt = " ".join(sql.split())
        if not stmt.upper().startswith("SELECT") or " WHERE " in stmt.upper():
            return
        m = _FROM.search(stmt)
        if m is not None and m.group(1) in _LEDGERS:
            seen[m.group(1)] += 1

    conn.set_trace_callback(trace)
    try:
        out = call()
    finally:
        conn.set_trace_callback(None)
    return out, seen


def _at_most_once(seen: Counter[str], *, lookup: str | None = None) -> None:
    """Each ledger read once by the guard snapshot (+1 for the door's own by-id lookup)."""
    ceiling = {t: 1 + (1 if t == lookup else 0) for t in _LEDGERS}
    over = {t: n for t, n in seen.items() if n > ceiling[t]}
    assert not over, f"ledgers re-read in one request: {dict(seen)} (ceiling {ceiling})"


def _golden_fx(client: TestClient) -> dict[str, Any]:
    rows: list[dict[str, Any]] = client.get("/api/ledgers/fx", params={"limit": 500}).json()["rows"]
    (fx,) = [r for r in rows if r["account_id"] == "schwab" and r["date"] == "2026-01-08"]
    return fx


def _fund(conn: sqlite3.Connection, account: str, ccy: Currency, amount: str) -> int:
    move_id = insert_cash_movement(conn, account_id=account, move_date=date(2026, 1, 2),
                                   kind="DEPOSIT", ccy=ccy, amount=Decimal(amount))
    conn.commit()
    return move_id


def _fx_body(fx: dict[str, Any], **change: Any) -> dict[str, Any]:
    return {"account_id": fx["account_id"], "date": fx["date"], "from_ccy": fx["from_ccy"],
            "from_amt": fx["from_amt"], "to_ccy": fx["to_ccy"], "to_amt": fx["to_amt"],
            **change}


# ------------------------------------------------------------------ CR-12: the FX doors


def test_an_fx_edit_reads_each_ledger_once_on_both_answers(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The asking edit (422) and the clean one (200): fx_change_guard + fx_edit_negative_guard
    share ONE snapshot. Before CR-12: fx_conversions 5, every other ledger 3."""
    _fund(golden_db, "schwab", Currency.TWD, "100000")
    fx = _golden_fx(api_client)
    asked, seen = _full_reads(golden_db, lambda: api_client.put(
        f"/api/ledgers/fx/{fx['id']}", json=_fx_body(fx, to_amt="500")))
    assert asked.status_code == 422 and asked.json()["error"]["code"] == "negative_cash"
    _at_most_once(seen, lookup="fx_conversions")
    clean, seen = _full_reads(golden_db, lambda: api_client.put(
        f"/api/ledgers/fx/{fx['id']}", json=_fx_body(fx, to_amt="1200")))
    assert clean.status_code == 200, clean.json()
    _at_most_once(seen, lookup="fx_conversions")


def test_a_cross_account_fx_edit_asks_about_four_pools_from_one_read(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """Four pools (old and new account × both legs) — the worst case: 27 reads before CR-12."""
    _fund(golden_db, "moomoo_my", Currency.MYR, "10000")
    fx = _golden_fx(api_client)
    r, seen = _full_reads(golden_db, lambda: api_client.put(
        f"/api/ledgers/fx/{fx['id']}",
        json=_fx_body(fx, account_id="moomoo_my", from_ccy="MYR", from_amt="4400")))
    assert r.status_code == 422 and r.json()["error"]["code"] == "negative_cash"
    _at_most_once(seen, lookup="fx_conversions")


def test_an_fx_delete_reads_each_ledger_once(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    fx = _golden_fx(api_client)
    r, seen = _full_reads(golden_db, lambda: api_client.delete(f"/api/ledgers/fx/{fx['id']}"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "negative_cash"
    _at_most_once(seen, lookup="fx_conversions")


def test_an_fx_entry_still_reads_each_ledger_once(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """``POST /api/cash/fx`` was already one read; the snapshot must not add a second."""
    _fund(golden_db, "schwab", Currency.TWD, "100000")
    r, seen = _full_reads(golden_db, lambda: api_client.post("/api/cash/fx", json={
        "account_id": "schwab", "date": "2026-01-09", "from_ccy": "TWD", "from_amt": "3200",
        "to_ccy": "USD", "to_amt": "100"}))
    assert r.status_code == 201, r.json()
    _at_most_once(seen)


# ------------------------------------------------------------------ CR-12: the movement doors


def test_a_movement_edit_reads_each_ledger_once(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """The hard guard (movement_guard → the pool probe) and the ack-able one (_pool_dip over
    the touched pools) share ONE snapshot. Before CR-12: cash_movements 5, others 2."""
    _fund(golden_db, "schwab", Currency.TWD, "100000")
    move_id = _fund(golden_db, "schwab", Currency.USD, "10")
    body = {"account_id": "schwab", "date": "2026-01-02", "kind": "DEPOSIT", "ccy": "USD"}
    r, seen = _full_reads(golden_db, lambda: api_client.put(
        f"/api/cash/movements/{move_id}", json={**body, "amount": "12"}))
    assert r.status_code == 200, r.json()
    _at_most_once(seen, lookup="cash_movements")
    # A deposit turned withdrawal: the target pool reads the ``without`` ledger — same read.
    r, seen = _full_reads(golden_db, lambda: api_client.put(
        f"/api/cash/movements/{move_id}", json={**body, "kind": "WITHDRAW", "ccy": "TWD",
                                                "amount": "1"}))
    assert r.status_code == 200, r.json()
    _at_most_once(seen, lookup="cash_movements")


def test_a_movement_delete_and_entry_read_each_ledger_once(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    move_id = _fund(golden_db, "schwab", Currency.USD, "10")
    r, seen = _full_reads(golden_db, lambda: api_client.delete(f"/api/cash/movements/{move_id}"))
    assert r.status_code == 200, r.json()
    _at_most_once(seen, lookup="cash_movements")
    r, seen = _full_reads(golden_db, lambda: api_client.post("/api/cash/movements", json={
        "account_id": "schwab", "date": "2026-01-03", "kind": "DEPOSIT", "ccy": "USD",
        "amount": "5"}))
    assert r.status_code == 201, r.json()
    _at_most_once(seen)


# ------------------------------------------------------------------ CR-13: one copy of the rule


def test_the_batch_undo_cash_check_reads_movements_and_fx_once(
    api_client: TestClient, golden_db: sqlite3.Connection,
) -> None:
    """Only the two cash tables are pinned here: the batch undo's equity replay
    (``_replay_block``) reads the trade ledgers itself, and that is another item's code."""
    r = api_client.post("/api/import/commit", json={
        "kind": "cash", "csv_text": "account,date,kind,ccy,amount\n"
                                    "schwab,2026-02-01,DEPOSIT,USD,500\n",
        "ack_warnings": True, "source_name": "t.csv"})
    batch = r.json()["import_batch_id"]
    insert_cash_movement(golden_db, account_id="schwab", move_date=date(2026, 2, 15),
                         kind="WITHDRAW", ccy=Currency.USD, amount=Decimal("400"))
    golden_db.commit()
    r, seen = _full_reads(golden_db, lambda: api_client.delete(f"/api/import/batches/{batch}"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "negative_cash"
    assert seen["cash_movements"] <= 1 and seen["fx_conversions"] <= 1, dict(seen)


def test_the_guards_given_a_snapshot_issue_no_sql(golden_db: sqlite3.Connection) -> None:
    """The pure half: once a door holds the snapshot, the scoped dip rule reads nothing."""
    from portfolio_dash.api.routers.cash import (
        _pool_dip,
        fx_edit_negative_guard,
        load_cash_ledgers,
    )

    ledgers = load_cash_ledgers(golden_db)
    (existing,) = [f for f in ledgers.fx
                   if f.account_id == "schwab" and f.date == date(2026, 1, 8)]
    edited = existing.model_copy(update={"to_amount": Decimal("500")})
    statements: list[str] = []
    golden_db.set_trace_callback(statements.append)
    try:
        dip = _pool_dip(ledgers, "schwab", Currency.USD,
                        fx=[edited if f.id == existing.id else f for f in ledgers.fx])
        asked = fx_edit_negative_guard(golden_db, existing, edited, ack_negative=False,
                                       ledgers=ledgers)
    finally:
        golden_db.set_trace_callback(None)
    assert statements == []
    assert dip is not None and dip.low == Decimal("-500")
    assert asked is not None and asked.status_code == 422
    assert get_fx_conversion(golden_db, existing.id) == existing   # nothing written


def _callers_of(name: str) -> list[str]:
    """Every ``module::function`` in the package whose body calls *name* directly."""
    out: list[str] = []
    for path in sorted(_PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == name):
                    out.append(f"{path.relative_to(_PKG).as_posix()}::{fn.name}")
                    break
    return sorted(set(out))


def test_the_scoped_dip_rule_has_exactly_one_copy() -> None:
    """``caused_dip`` (the ack-able doors' rule) is applied to two pool timelines in ONE place;
    the batch undo's ``_pool_refusal`` calls it there instead of rebuilding it. Before CR-13
    both ``cash.py::_pool_dip`` and ``replay_guard.py::_pool_refusal`` called ``caused_dip``."""
    assert _callers_of("caused_dip") == ["api/routers/cash.py::_pool_dip"]
    assert "api/replay_guard.py::_pool_refusal" in _callers_of("_pool_dip")
