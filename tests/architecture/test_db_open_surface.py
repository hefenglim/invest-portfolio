"""Guardrail: pin the database-open surface (multi-user prep — Phase 0, FU-D39).

Why this test exists
--------------------
The multi-user roadmap (docs/reports/2026-07-17-r3-research-pack.md §R-1, and the
short note docs/reports/2026-07-18-multiuser-phase0.md) splits today's single SQLite
file into a per-user ``ledger.db`` + a shared ``market.db`` + a central ``control.db``,
alongside the ``news.db`` that already lives in its own file. That split only stays
cheap while the set of places that *open a database connection* is tiny and known.

Phase 0 does NOT perform the split. It installs this guardrail so the open surface
cannot silently sprawl before the split happens: every personal DB access must route
through the shared choke-point (``portfolio_dash.shared.db`` — ``get_connection`` /
``session`` — or ``api.deps.get_conn``), and news through ``portfolio_dash.news.store``
(``news_session``). A NEW raw ``sqlite3.connect(...)`` anywhere else fails this test.

How the scan works (robustness / no false positives)
-----------------------------------------------------
The scan **tokenizes** each module and counts only the real code 4-gram
``sqlite3 . connect (`` (and the equivalent constructor ``sqlite3 . Connection (``).
Because it walks ``tokenize`` NAME/OP tokens, COMMENT and STRING tokens are ignored
**by construction** — the literal text ``sqlite3.connect(`` appearing in a comment or a
docstring/string can never inflate a count, and a type annotation ``sqlite3.Connection``
(no call parens) never matches. ``test_scanner_ignores_comments_and_strings`` proves this.

MAINTENANCE NOTE — opener forms in scope
----------------------------------------
Only the qualified ``sqlite3.connect(`` / ``sqlite3.Connection(`` forms exist in the tree
today, so those are what we pin. If a future change introduces another opener form — a
bare ``from sqlite3 import connect`` alias, or a different DBAPI library (``aiosqlite``,
``apsw``, ...) — extend ``_OPENER_ATTRS`` / this scan to cover it AND record why in the
Phase 0 doc. Adding an unrelated opener without updating the guardrail is exactly the
drift this test is meant to catch elsewhere; keep the scanner honest about its own scope.
"""

import io
import token
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "portfolio_dash"

# Qualified sqlite3 attributes that OPEN a new database handle. Both forms are equivalent
# opens: ``sqlite3.connect(...)`` (the standard opener) and ``sqlite3.Connection(...)``
# (the underlying constructor). A type hint ``sqlite3.Connection`` (no call parens) is not
# an open and is not matched — the scan requires a following ``(``.
_OPENER_ATTRS: frozenset[str] = frozenset({"connect", "Connection"})

# The PINNED allow-list: {repo-relative posix path -> number of direct opens}. Every entry
# carries a one-line justification. Confirmed by actually running the scan (Phase 0, FU-D39).
ALLOWLIST: dict[str, int] = {
    # THE canonical choke-point: get_connection()/session() open the per-request personal
    # ledger connection. All personal DB access must route through here (Phase 0 invariant).
    "portfolio_dash/shared/db.py": 1,
    # The separate-DB precedent: news.db beside the ledger, opened lazily on demand. This is
    # the template the deferred market.db split (Phase 1) will mirror — its own opener by design.
    "portfolio_dash/news/store.py": 1,
    # Low-level backup / integrity utility (imports only stdlib + shared). It uses the sqlite3
    # online-backup API (Connection.backup, needs a raw src + dst handle) and PRAGMA
    # integrity_check over an arbitrary db_path, so it legitimately opens raw handles and
    # CANNOT route through session(). 2 opens for backup (src+dst) + 1 for integrity_check.
    # TODO(market-db-split): a later phase adds market.db/control.db as extra backup targets
    # (additive fan-out) — revisit this count then; it is a permitted ops exception, not drift.
    "portfolio_dash/ops/backup.py": 3,
}


def _count_opens(source: str) -> int:
    """Count qualified ``sqlite3.connect(`` / ``sqlite3.Connection(`` opens in ``source``.

    Tokenizes the source and matches only real NAME/OP code tokens, so opener text inside
    comments or string/doc literals is ignored, and bare type annotations do not match.
    """
    code: list[tuple[int, str]] = [
        (tok.type, tok.string)
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type in (token.NAME, token.OP)
    ]
    count = 0
    for i in range(len(code) - 3):
        t0, t1, t2, t3 = code[i], code[i + 1], code[i + 2], code[i + 3]
        if (
            t0 == (token.NAME, "sqlite3")
            and t1 == (token.OP, ".")
            and t2[0] == token.NAME
            and t2[1] in _OPENER_ATTRS
            and t3 == (token.OP, "(")
        ):
            count += 1
    return count


def _scan_package() -> dict[str, int]:
    """Scan ``portfolio_dash/**/*.py`` -> {repo-relative posix path: open count} (>0 only)."""
    assert PACKAGE_ROOT.is_dir(), f"package root not found: {PACKAGE_ROOT}"
    files = sorted(PACKAGE_ROOT.rglob("*.py"))
    # Sanity guard: if path resolution were wrong the scan would find ~nothing and the
    # guardrail would silently pass. The package is well over 100 modules.
    assert len(files) > 50, (
        f"scan visited only {len(files)} .py files under {PACKAGE_ROOT} — path resolution "
        "looks wrong; the guardrail would be meaningless."
    )
    found: dict[str, int] = {}
    for path in files:
        opens = _count_opens(path.read_text(encoding="utf-8"))
        if opens:
            found[path.relative_to(REPO_ROOT).as_posix()] = opens
    return found


def test_db_open_surface_matches_allowlist() -> None:
    """The set of (file, direct-open count) must match the pinned allow-list exactly."""
    found = _scan_package()
    unexpected = {f: c for f, c in found.items() if f not in ALLOWLIST}
    drifted = {
        f: (ALLOWLIST[f], found[f]) for f in found if f in ALLOWLIST and found[f] != ALLOWLIST[f]
    }
    gone = {f: c for f, c in ALLOWLIST.items() if f not in found}

    problems: list[str] = []
    if unexpected:
        problems.append(
            "NEW direct sqlite3 open(s) outside the allow-list: "
            + ", ".join(f"{f} (x{c})" for f, c in sorted(unexpected.items()))
        )
    if drifted:
        problems.append(
            "open count changed for: "
            + ", ".join(
                f"{f} (pinned {old} -> found {new})" for f, (old, new) in sorted(drifted.items())
            )
        )
    if gone:
        problems.append(
            "allow-listed file(s) no longer open a DB (prune the allow-list): "
            + ", ".join(f"{f} (was x{c})" for f, c in sorted(gone.items()))
        )

    assert not problems, (
        "DB-open surface drift (multi-user Phase 0 guardrail — see "
        "docs/reports/2026-07-18-multiuser-phase0.md):\n  "
        + "\n  ".join(problems)
        + "\n\nEvery PERSONAL database access must go through portfolio_dash.shared.db "
        "(get_connection / session) or api.deps.get_conn; news uses "
        "portfolio_dash.news.store (news_session). Do NOT add a raw sqlite3.connect(...) "
        "elsewhere. If a new opener is genuinely unavoidable, add it to ALLOWLIST in this "
        "file WITH a one-line justification and record it in the Phase 0 architecture doc."
    )


def test_scanner_ignores_comments_and_strings() -> None:
    """The tokenizing scanner counts real calls only — never comments, strings, or hints."""
    sample = (
        "import sqlite3\n"
        "def f(conn: sqlite3.Connection) -> None:\n"  # type hint, no call -> not counted
        "    # sqlite3.connect( in a comment must not count\n"
        '    note = "sqlite3.connect( in a string must not count"\n'
        "    real = sqlite3.connect(':memory:')\n"  # the only real open -> counts once
        "    print(conn, note, real)\n"
    )
    assert _count_opens(sample) == 1
    # A pure type-annotation / no-open module counts zero.
    assert _count_opens("import sqlite3\nx: sqlite3.Connection\n") == 0
