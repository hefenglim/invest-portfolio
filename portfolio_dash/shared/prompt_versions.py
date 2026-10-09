"""Append-only version history of the GLOBAL prompt bodies (DEF-057, owner ruling 2026-09-24).

The owner ruled that the system prompt and the news-organizer prompt keep versions exactly as
the strategy prompts do since DEF-033: every save that changes the body keeps a version, the
history can be read, diffed and restored (a restore is itself a NEW version — history is never
rewritten), 還原官方 is a version too, and the bodies that existed before versioning began are
back-filled as v1. The task self-evaluation prompt is deliberately NOT versioned (ruling ④).

**Why one ``prompt_versions`` table keyed by ``kind``, and why it lives in ``shared/``.**
The two bodies are single-row configs owned by two different modules —
``llm_insight/system_prompt.py`` and ``news/organizer_prompt.py`` — and ``news/`` may import
nothing from ``llm_insight`` except ``official_templates`` (``architecture.md``). A history both
stores append to from their own write function (so no door can bypass it) therefore has
exactly one legal home: here, beside ``config_store`` and ``ui_prefs``. One table keyed by
``kind`` instead of two copies: the next global prompt is a row in :data:`KIND_LABELS`, not a
third table.

**Why DEF-033's ``strategy_prompt_versions`` is NOT folded in.** Its rows carry a
``strategy_id`` and the strategy's ``name`` at save time, cards reference them by
``(strategy_id, version)``, and the demo already holds real history there. Moving those rows
would renumber the ids its routes address for no behavioural gain, so the table stays untouched
and what is shared is the machinery around it: the line diff (``llm_insight/prompt_diff.py``),
the source labels below, the wire shape of one version, and the page's history modal.

Pure persistence: stdlib + pydantic only. No LLM, no money, no float. Every function takes the
caller's connection and none commits except :func:`backfill` (which commits only when it
actually inserted), so a store can put its body write and its version row in ONE transaction.
"""

import sqlite3
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Kind = Literal["system", "news"]

#: zh name of each versioned global prompt (the history modal's title; the 404 message).
KIND_LABELS: dict[str, str] = {
    "system": "系統提示詞",
    "news": "新聞整理提示詞",
}

#: Why a version row exists. ``user_save`` / ``reset_official`` / ``restore`` are written by
#: the stores' write doors; ``migration`` / ``backfill`` only by :func:`backfill`.
Source = Literal["user_save", "reset_official", "restore", "migration", "backfill"]

#: zh labels for the history list — the SAME words DEF-033 shows for the same events
#: (``composer_store.VERSION_SOURCE_LABELS`` builds on this dict), so one event never reads
#: two ways on one page.
SOURCE_LABELS: dict[str, str] = {
    "user_save": "儲存",
    "reset_official": "還原官方",
    "restore": "回復",
    "migration": "啟用版本記錄時的內容",
    "backfill": "補記（內容在版本記錄外被改動）",
}

_DDL = """
CREATE TABLE IF NOT EXISTS prompt_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    source TEXT NOT NULL,
    restored_from INTEGER,
    saved_at TEXT NOT NULL,
    UNIQUE (kind, version)
)
"""


class PromptVersion(BaseModel):
    """One ``prompt_versions`` row — a body a global prompt HAD, never rewritten."""

    id: int
    kind: str
    version: int
    body: str
    source: str  # a Source value (str so a future source never breaks a read)
    restored_from: int | None  # the version number a ``restore`` copied, else None
    saved_at: str


def ensure_table(conn: sqlite3.Connection) -> None:
    """Create the history table (idempotent; additive — nothing existing is touched)."""
    conn.execute(_DDL)


def _from_row(row: sqlite3.Row) -> PromptVersion:
    return PromptVersion(
        id=row["id"], kind=row["kind"], version=row["version"], body=row["body"],
        source=row["source"], restored_from=row["restored_from"], saved_at=row["saved_at"],
    )


def current_version_no(conn: sqlite3.Connection, kind: Kind) -> int | None:
    """The newest version number of *kind*, or None (no row / table not created yet)."""
    try:
        row = conn.execute(
            "SELECT MAX(version) AS m FROM prompt_versions WHERE kind = ?", (kind,)
        ).fetchone()
    except sqlite3.OperationalError:  # a connection that never ran ensure_table
        return None
    return int(row["m"]) if row is not None and row["m"] is not None else None


def get_by_number(conn: sqlite3.Connection, kind: Kind, version: int) -> PromptVersion | None:
    """One version of *kind* by its number, or None."""
    row = conn.execute(
        "SELECT * FROM prompt_versions WHERE kind = ? AND version = ?", (kind, version)
    ).fetchone()
    return _from_row(row) if row is not None else None


def latest(conn: sqlite3.Connection, kind: Kind) -> PromptVersion | None:
    """The newest version of *kind* (its current body), or None."""
    no = current_version_no(conn, kind)
    return get_by_number(conn, kind, no) if no is not None else None


def list_versions(conn: sqlite3.Connection, kind: Kind) -> list[PromptVersion]:
    """The whole history of *kind*, newest first."""
    rows = conn.execute(
        "SELECT * FROM prompt_versions WHERE kind = ? ORDER BY version DESC", (kind,)
    ).fetchall()
    return [_from_row(r) for r in rows]


def get_version(conn: sqlite3.Connection, version_id: int) -> PromptVersion | None:
    """One version row by its id (any kind), or None."""
    row = conn.execute("SELECT * FROM prompt_versions WHERE id = ?", (version_id,)).fetchone()
    return _from_row(row) if row is not None else None


def version_of_body(conn: sqlite3.Connection, kind: Kind, body: str) -> int | None:
    """The version a result built from *body* records, or None when it matches none.

    The newest version whose body is exactly *body* — normally the current one, since every
    write door appends one. Looked up BY BODY, so what a card / news row records is the text
    it was actually built from, never a number read beside it. Read-only.
    """
    try:
        row = conn.execute(
            "SELECT MAX(version) AS m FROM prompt_versions WHERE kind = ? AND body = ?",
            (kind, body),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row["m"]) if row is not None and row["m"] is not None else None


def record_write(
    conn: sqlite3.Connection,
    kind: Kind,
    body: str,
    *,
    source: Source,
    now: datetime,
    restored_from: int | None = None,
) -> int | None:
    """Append *body* as the next version of *kind* unless it already IS the newest one.

    Returns the new version number, or None when nothing was appended (the body is unchanged —
    a duplicate row would only pad the list). No commit: the caller writes the body and this
    row in one transaction.
    """
    ensure_table(conn)
    newest = latest(conn, kind)
    if newest is not None and newest.body == body:
        return None
    version = (newest.version if newest is not None else 0) + 1
    conn.execute(
        "INSERT INTO prompt_versions (kind, version, body, source, restored_from, saved_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (kind, version, body, source, restored_from, now.isoformat()),
    )
    return version


def backfill(conn: sqlite3.Connection, kind: Kind, body: str, saved_at: str) -> bool:
    """Make *body* (the prompt's CURRENT stored body) the newest version of *kind*. Idempotent.

    The migration AND the invariant's repair, exactly as DEF-033's ``_backfill_versions``:
    no history yet → *body* becomes v1 (``migration``, stamped with the row's own
    ``updated_at``); a newest version that differs from *body* (a write that went around the
    store — direct SQL, a restored backup) → *body* is appended as ``backfill`` so the history
    never has a hole and a result built now can still name its version. INSERTs only; commits
    only when it inserted. Returns whether it did.
    """
    ensure_table(conn)
    newest = latest(conn, kind)
    if newest is not None and newest.body == body:
        return False
    version = (newest.version if newest is not None else 0) + 1
    source: Source = "migration" if newest is None else "backfill"
    conn.execute(
        "INSERT INTO prompt_versions (kind, version, body, source, restored_from, saved_at) "
        "VALUES (?, ?, ?, ?, NULL, ?)",
        (kind, version, body, source, saved_at),
    )
    conn.commit()
    return True
