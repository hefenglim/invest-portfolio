"""I-1's class guard: a function that takes ``commit`` forwards it to EVERY commit-capable
callee — so ``commit=False`` really means "the caller owns the transaction".

The defect this exists for: ``data_ingestion/corporate_action_import.py::
write_corporate_action_row`` took ``commit`` and forwarded it to the band move and the SPINOFF
child registration, but called ``insert_corporate_action(...)`` without it. The insert's own
default is ``commit=True``, so every row committed itself the moment it was written and
``commit_preview``'s all-or-nothing rollback (``preview.py``) had nothing left to undo — a
three-row corporate-action CSV whose third row failed kept the first two. Nothing caught it:
the round-trip and batch tests only ever exercised batches that succeeded, and a call that
omits a keyword with a default is invisible to mypy.

Rule, checked over ``portfolio_dash/``: inside any function with a ``commit`` parameter, a
call to any function (by name) that ALSO has a ``commit`` parameter must pass ``commit=``
explicitly — forwarding it, or pinning ``False`` on purpose. Measured on 2026-09-23: 26
commit-capable functions, 12 such nested calls, 1 missing (the insert above), 0 allowlisted.
A second rule catches the other shape of the same leak: inside a ``commit``-taking function,
no call to a function that commits UNCONDITIONALLY (a bare ``conn.commit()`` not under an
``if … commit …``).
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2] / "portfolio_dash"

#: ``file:caller->callee`` -> reason. Empty on purpose: every nested call forwards today.
_ALLOWED: dict[str, str] = {}


def _callee(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _commits_unconditionally(fn: ast.FunctionDef) -> bool:
    """A bare ``<x>.commit()`` in *fn* that no ``if`` mentioning ``commit`` guards."""
    found = False

    def visit(node: ast.AST, guarded: bool) -> None:
        nonlocal found
        if isinstance(node, ast.If):
            inner = guarded or "commit" in ast.unparse(node.test)
            for s in node.body:
                visit(s, inner)
            for s in node.orelse:
                visit(s, guarded)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "commit" and not node.args and not guarded):
            found = True
        for child in ast.iter_child_nodes(node):
            visit(child, guarded)

    for stmt in fn.body:
        visit(stmt, False)
    return found


def _scan(sources: dict[str, str]) -> tuple[list[str], int, int]:
    """(violations, commit-capable functions, nested commit-capable calls)."""
    trees = {name: ast.parse(src) for name, src in sources.items()}
    takes_commit: set[str] = set()
    unconditional: set[str] = set()
    owners: list[tuple[str, ast.FunctionDef]] = []
    for name, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            params = {a.arg for a in [*node.args.args, *node.args.kwonlyargs]}
            if "commit" in params:
                takes_commit.add(node.name)
                owners.append((name, node))
            if _commits_unconditionally(node):
                unconditional.add(node.name)
    bad: list[str] = []
    nested = 0
    for name, fn in owners:
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            callee = _callee(node)
            if callee is None or callee == "commit":
                continue
            key = f"{name}:{fn.name}->{callee}"
            if callee in takes_commit:
                nested += 1
                if not any(k.arg == "commit" for k in node.keywords) and key not in _ALLOWED:
                    bad.append(f"{key} (line {node.lineno}) does not pass commit=")
            elif callee in unconditional and key not in _ALLOWED:
                bad.append(f"{key} (line {node.lineno}) calls a function that always commits")
    return bad, len(owners), nested


def _package_sources() -> dict[str, str]:
    return {p.relative_to(_ROOT).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(_ROOT.rglob("*.py"))}


def test_every_commit_taking_function_forwards_commit() -> None:
    bad, owners, nested = _scan(_package_sources())
    assert owners >= 20 and nested >= 10, (owners, nested)   # the scan still sees the code
    assert not bad, (
        "a function that takes `commit` calls a commit-capable function without forwarding "
        "it — `commit=False` would then commit mid-transaction (I-1):\n" + "\n".join(bad))


def test_the_allowlist_is_not_stale() -> None:
    sources = _package_sources()
    present = {
        f"{name}:{fn.name}->{_callee(c)}"
        for name, src in sources.items()
        for fn in ast.walk(ast.parse(src)) if isinstance(fn, ast.FunctionDef)
        for c in ast.walk(fn) if isinstance(c, ast.Call)
    }
    assert not (set(_ALLOWED) - present), set(_ALLOWED) - present


def test_the_guard_can_actually_fail() -> None:
    """The I-1 shape and the always-commits shape, each caught; the forwarding shape passes."""
    src = (
        "def insert(conn, *, commit=True):\n"
        "    conn.execute('x')\n"
        "    if commit:\n"
        "        conn.commit()\n"
        "def always(conn):\n"
        "    conn.commit()\n"
        "def writer(conn, *, commit=True):\n"
        "    return insert(conn)\n"
        "def writer2(conn, *, commit=True):\n"
        "    always(conn)\n"
        "def good(conn, *, commit=True):\n"
        "    return insert(conn, commit=commit)\n"
    )
    bad, _owners, _nested = _scan({"m.py": src})
    assert [b.split(" ")[0] for b in bad] == ["m.py:writer->insert", "m.py:writer2->always"]
