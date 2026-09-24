"""Every row that leaves a LEDGER table leaves through the store's own delete (DEF-049).

``provenance.delete_batch`` used to run ``DELETE FROM {table} WHERE import_batch_id=?`` over
four ledgers. That one statement skipped both things every single-row delete does: the replay
guard the ledger tab runs first, and the ``ledger_audit`` before-image the store writes. On
demo, undoing a buy that a later hand-entered sell depended on turned the sell into a 賣超,
discarded the cost basis and left no trace of the rows — while 刪除 on the same buy asked first.

The BEHAVIOUR is pinned where it matters, through the HTTP door
(``tests/contract/test_def049_batch_undo_replay_guard.py``: refusal, ack, audit, action log).
This file is the CLASS guard: the next bulk door written as a bare ``DELETE FROM transactions``
fails here, naming the file and the function, before it ships. A bare statement is allowed
only in the store's per-row delete functions (which audit first) and in the few places listed
in :data:`_ALLOWED` with the reason each is safe.

How it scans: every string literal in ``portfolio_dash/`` and ``scripts/`` (plain, implicitly
concatenated, or f-string — an f-string's ``{…}`` survives as a placeholder), except
docstrings. A ``DELETE FROM`` whose table is a ledger table — or a ``{placeholder}``, which
could be one — must sit in an allowlisted function. ``executemany`` and statements assembled
in a variable are caught the same way, because the literal is scanned wherever it appears.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCANNED = ("portfolio_dash", "scripts")

#: The ledgers — the tables every report is rebuilt from (domain-ledger.md, 重算).
_LEDGERS = frozenset({
    "transactions", "dividends", "fx_conversions", "cash_movements",
    "opening_inventory", "corporate_actions",
})

_DELETE = re.compile(r"\bDELETE\s+FROM\s+[\"'`\[]?(\{[^}]*\}|\w+)", re.IGNORECASE)

#: (path, function) -> why a bare DELETE there is not a bypass.
_ALLOWED: dict[tuple[str, str], str] = {
    ("portfolio_dash/data_ingestion/store.py", "delete_transaction"):
        "the store's per-row delete: audits the before-image first",
    ("portfolio_dash/data_ingestion/store.py", "delete_dividend"):
        "the store's per-row delete: audits the before-image first",
    ("portfolio_dash/data_ingestion/store.py", "delete_fx_conversion"):
        "the store's per-row delete: audits the before-image first",
    ("portfolio_dash/data_ingestion/store.py", "delete_cash_movement"):
        "the store's per-row delete: audits the before-image first",
    ("portfolio_dash/data_ingestion/store.py", "delete_opening"):
        "the store's per-row delete: audits the before-image first",
    ("portfolio_dash/data_ingestion/store.py", "delete_corporate_action"):
        "the store's per-row delete: audits the action AND each linked fee movement first",
    ("portfolio_dash/data_ingestion/store.py", "delete_instrument"):
        "dynamic table from a fixed tuple of market-data / derived tables (prices, "
        "dividend_events, signal_states, signal_history, alert_events) — never a ledger; "
        "the purge route refuses a symbol with ledger history before calling it",
    ("scripts/delete_insight_cards.py", "main"):
        "dynamic table from the script's fixed insight-card tables (dry-run default, "
        "--apply, audited in action_log) — never a ledger",
}


def _docstring_ids(tree: ast.Module) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def _text(node: ast.AST) -> str | None:
    """A string literal's text; an f-string's interpolations become ``{…}``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str)
                       else "{…}" for v in node.values)
    return None


def ledger_deletes(source: str, rel: str) -> list[tuple[str, str, str, int]]:
    """Every ``DELETE FROM <ledger or {placeholder}>`` literal in *source*:
    ``(path, enclosing function, table, line)``."""
    tree = ast.parse(source)
    docs = _docstring_ids(tree)
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    found: list[tuple[str, str, str, int]] = []
    for node in ast.walk(tree):
        if id(node) in docs or isinstance(parents.get(id(node)), ast.JoinedStr):
            continue
        text = _text(node)
        if not text:
            continue
        for m in _DELETE.finditer(text):
            table = m.group(1)
            if not (table.startswith("{") or table.lower() in _LEDGERS):
                continue
            fn, cur = "<module>", parents.get(id(node))
            while cur is not None:
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn = cur.name
                    break
                cur = parents.get(id(cur))
            found.append((rel, fn, table, getattr(node, "lineno", 0)))
    return found


def _repo_hits() -> list[tuple[str, str, str, int]]:
    hits: list[tuple[str, str, str, int]] = []
    for top in _SCANNED:
        for path in sorted((_REPO / top).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(_REPO).as_posix()
            hits.extend(ledger_deletes(path.read_text(encoding="utf-8"), rel))
    return hits


def test_no_ledger_row_leaves_except_through_the_store() -> None:
    bypasses = [h for h in _repo_hits() if (h[0], h[1]) not in _ALLOWED]
    assert not bypasses, (
        "A bare DELETE on a ledger table outside the store's per-row deletes — it skips the "
        "replay guard and the ledger_audit before-image (DEF-049). Route it through "
        "store.delete_* (commit=False for a bulk door) or allowlist it with a reason: "
        f"{bypasses}")


def test_every_allowlisted_site_still_exists() -> None:
    """D39: an exception nobody uses is an exception nobody notices."""
    used = {(h[0], h[1]) for h in _repo_hits()}
    stale = sorted(set(_ALLOWED) - used)
    assert not stale, f"allowlist entries with no DELETE left — remove them: {stale}"


_PLANTED = '''
def undo(conn, batch_id):
    """Docstrings may say DELETE FROM transactions freely."""
    for table in ("transactions", "dividends"):
        conn.execute(f"DELETE FROM {table} WHERE import_batch_id=?", (batch_id,))
    sql = "DELETE FROM cash_movements WHERE import_batch_id=?"
    conn.executemany("delete from  fx_conversions WHERE id=?", [(1,)])
    conn.execute("DELETE FROM import_batches WHERE id=?", (batch_id,))
    return sql
'''


def test_the_scanner_catches_a_planted_bypass() -> None:
    """Proof the class guard bites: the old batch undo's shape, plus the variants a rewrite
    would reach for (a statement in a variable, ``executemany``, lower case), are all found;
    the docstring mention and the non-ledger table are not."""
    hits = ledger_deletes(_PLANTED, "planted.py")
    assert sorted((h[1], h[2]) for h in hits) == sorted([
        ("undo", "{…}"), ("undo", "cash_movements"), ("undo", "fx_conversions")])


@pytest.mark.parametrize("key", sorted(_ALLOWED))
def test_each_allowlist_entry_carries_a_reason(key: tuple[str, str]) -> None:
    assert len(_ALLOWED[key]) > 20


# ------------------------------------------------------------------ the doors themselves
#
# The second half of the class (DEF-049, lead ruling 2026-09-25): every HTTP door that
# REMOVES or REWRITES ledger rows must run a guard before it writes. The batch undo had none,
# and neither did the three corporate-action doors (DELETE one / DELETE set / PUT) — all four
# stranded a later sell silently. Their BEHAVIOUR is pinned by the contract tests
# (test_def049_batch_undo_replay_guard.py, test_def049_corporate_action_doors_replay.py);
# this walk is the net for the NEXT door: a DELETE/PUT on a ledger path whose handler calls
# none of the guards fails here, by route.

#: The guard functions a ledger door may run: the replay guard (equity ledgers) or the cash
#: pool guards (cash / FX ledgers, which no equity replay reads).
_GUARDS = frozenset({
    "_replay_guard", "action_change_guard", "batch_undo_guard",
    "fx_delete_guard", "fx_change_guard", "movement_guard", "_pool_low",
})
_LEDGER_PATHS = ("/api/ledgers/", "/api/import/batches", "/api/cash/movements")


def _called_names(fn: object) -> set[str]:
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))  # type: ignore[arg-type]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            names.add(f.id if isinstance(f, ast.Name)
                      else f.attr if isinstance(f, ast.Attribute) else "")
    return names


def _unguarded_doors(routes: list[tuple[str, str, object]]) -> list[str]:
    return [f"{m} {p}" for m, p, fn in routes if not (_called_names(fn) & _GUARDS)]


def _ledger_doors() -> list[tuple[str, str, object]]:
    from fastapi.routing import APIRoute

    from portfolio_dash.api.app import create_app

    out: list[tuple[str, str, object]] = []
    for r in create_app().routes:
        if not isinstance(r, APIRoute) or not r.path.startswith(_LEDGER_PATHS):
            continue
        for m in sorted(r.methods & {"DELETE", "PUT"}):
            out.append((m, r.path, r.endpoint))
    return out


def test_every_ledger_delete_and_edit_door_runs_a_guard() -> None:
    doors = _ledger_doors()
    # 8 DELETE + 6 PUT doors today; a shrinking count means the walk stopped seeing routes.
    assert len(doors) >= 14, doors
    assert _unguarded_doors(doors) == [], (
        "a ledger DELETE/PUT door writes without running a guard (DEF-049) — replay the "
        "would-be ledger through api/replay_guard.py, or the cash doors' pool guard")


def test_the_door_walk_catches_an_unguarded_handler() -> None:
    """Detection power: a handler that deletes without a guard is named; one that calls the
    guard (even inside a nested closure, like the batch undo) is not."""
    def bare(conn: object, action_id: int) -> None:
        conn.execute("SELECT 1")  # type: ignore[attr-defined]

    def guarded(conn: object) -> None:
        def inner() -> None:
            batch_undo_guard(conn)  # type: ignore[name-defined]  # noqa: F821
        inner()

    assert _unguarded_doors([("DELETE", "/api/ledgers/x", bare),
                             ("DELETE", "/api/ledgers/y", guarded)]) == [
        "DELETE /api/ledgers/x"]
