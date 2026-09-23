"""DEF-022 (functional test manual D-11, 2026-09-23): a business DATE comes from the app
clock, never from ``date.today()`` / ``datetime.now(UTC).date()`` / ``utcnow()``.

``shared/clock.py`` (decision Q6, 2026-07-07) makes Asia/Taipei the ONLY day anchor for
business logic and calls the scheduler's old ``datetime.now(UTC)`` a bug: between 00:00
and 07:59 Taipei the two clocks disagree on "today". ``store.upsert_instrument`` stamped
``target_set_at`` — the date ``validate.py`` compares against a split's trade date to
decide whether D44 fires — with ``datetime.now(UTC).date()``, so a band set on the morning
of a split was dated the day before it. Seven quote providers dated a latest quote's
``as_of`` with ``date.today()`` (the HOST's zone, which on the VM is UTC — neither clock),
and the LLM usage chart walked its 30-day window from a UTC day.

This guard scans ``portfolio_dash/**/*.py`` by AST for the four call shapes that yield a
DATE from a non-app clock. An INSTANT (``datetime.now(UTC).isoformat()`` for an audit
``at``, a ``fetched_at``, a ``created_at``) is a different thing — a timestamp with its
zone attached — and is deliberately NOT in scope: the rule is about the day anchor.
``_ALLOWED`` lists a file that may keep one, with the reason; it is empty today.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2] / "portfolio_dash"
_CLOCK = _ROOT / "shared" / "clock.py"

#: "path relative to portfolio_dash" -> reason. Empty: every hit found on 2026-09-23 was a
#: business date and was moved onto ``shared.clock.app_now().date()``.
_ALLOWED: dict[str, str] = {}


def _is_now_call(node: ast.expr) -> bool:
    """``<x>.now(...)`` or ``<x>.utcnow(...)`` — the clock reads."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"now", "utcnow"})


def offending_calls(tree: ast.AST) -> list[tuple[int, str]]:
    """Every call that yields a date from a non-app clock, with its line."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        # date.today()
        if func.attr == "today" and isinstance(func.value, ast.Name) and func.value.id == "date":
            hits.append((node.lineno, "date.today()"))
        # datetime.utcnow()
        elif func.attr == "utcnow":
            hits.append((node.lineno, "utcnow()"))
        # datetime.now(...).date() / datetime.now().date()
        elif func.attr == "date" and _is_now_call(func.value):
            hits.append((node.lineno, "now(...).date()"))
    return hits


def _files() -> list[Path]:
    return sorted(p for p in _ROOT.rglob("*.py") if p != _CLOCK)


def _rel(path: Path) -> str:
    return path.relative_to(_ROOT).as_posix()


def test_the_detector_can_see_every_shape() -> None:
    src = (
        "from datetime import UTC, date, datetime\n"
        "a = date.today()\n"
        "b = datetime.now(UTC).date()\n"
        "c = datetime.now().date()\n"
        "d = datetime.utcnow()\n"
        "e = datetime.now(UTC).isoformat()   # an instant: allowed\n"
        "f = app_now().date()                # the app clock: allowed\n"
    )
    assert [h[1] for h in offending_calls(ast.parse(src))] == [
        "date.today()", "now(...).date()", "now(...).date()", "utcnow()"]


@pytest.mark.parametrize("path", _files(), ids=_rel)
def test_business_dates_come_from_the_app_clock(path: Path) -> None:
    hits = offending_calls(ast.parse(path.read_text(encoding="utf-8")))
    if _rel(path) in _ALLOWED:
        assert hits, f"{_rel(path)} is allowed but has no hit — delete the entry"
        return
    assert hits == [], (
        f"{_rel(path)} derives a business date from a non-app clock: {hits}. Use "
        "`portfolio_dash.shared.clock.app_now().date()` (Q6: Asia/Taipei is the only day "
        "anchor), or add the file to _ALLOWED with the reason it is an instant, not a day.")


def test_the_allowlist_is_not_stale() -> None:
    for rel in _ALLOWED:
        assert (_ROOT / rel).exists(), f"{rel} is allowed but gone — delete the entry"
