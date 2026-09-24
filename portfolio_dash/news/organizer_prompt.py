"""The editable news-organizer system prompt (single-row config in the MAIN DB).

Mirrors ``llm_insight/system_prompt.py``: one user-editable global value via
``config_store``, defaulting to ``official_templates.NEWS_ORGANIZER_PROMPT`` with a
reset-to-official path. Small config (not article text) so it lives in the ledger DB
next to the other prompts, not the separate news DB.

DEF-057 (owner ruling 2026-09-24): the body keeps a version history in
``shared/prompt_versions.py`` (kind ``news``) — ``shared/`` because ``news/`` may import
nothing from ``llm_insight`` beyond ``official_templates`` (``architecture.md``). Every write
door here (儲存, 還原官方, 回復) appends the new body in the SAME transaction as the body write,
and :func:`ensure_news_prompt_seeded` back-fills the current body as v1. Each organized news
row records the version it was organized with (``organized_news.prompt_version``).
"""

import sqlite3
from datetime import datetime
from typing import TypedDict

from portfolio_dash.llm_insight import official_templates
from portfolio_dash.shared import config_store
from portfolio_dash.shared import prompt_versions as pv

_CATEGORY = "news_prompt"
_KIND: pv.Kind = "news"
_SEED_AT = datetime(2026, 7, 6)


class NewsPromptWire(TypedDict):
    """The GET / PUT / reset payload (spec 2026-09-10-news-prompt-settings, owner D1(a)).

    ``is_official`` is decided HERE, against the library constant, so the settings page's
    badge has one truth and the frontend never holds the official body. ``current_version``
    (DEF-057, additive) is the number of the newest history row.
    """

    body: str
    updated_at: str
    official_version: str
    is_official: bool
    current_version: int | None


def _wire(conn: sqlite3.Connection, body: str, updated_at: str) -> NewsPromptWire:
    return {
        "body": body,
        "updated_at": updated_at,
        "official_version": official_templates.NEWS_ORGANIZER_PROMPT_VERSION,
        "is_official": body == official_templates.NEWS_ORGANIZER_PROMPT,
        "current_version": pv.version_of_body(conn, _KIND, body),
    }


_DDL = (
    "CREATE TABLE IF NOT EXISTS news_prompt_config "
    "(id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT NOT NULL, updated_at TEXT NOT NULL)"
)


def _create(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO news_prompt_config (id, body, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (official_templates.NEWS_ORGANIZER_PROMPT, _SEED_AT.isoformat()),
    )


def ensure_news_prompt_seeded(conn: sqlite3.Connection) -> None:
    """Create the single-row table (always), seed the official default (once), and make the
    stored body the newest version of the history (DEF-057 back-fill; idempotent)."""
    config_store.ensure_seeded(conn, _CATEGORY, create=_create, seed=_seed)
    row = conn.execute(
        "SELECT body, updated_at FROM news_prompt_config WHERE id = 1"
    ).fetchone()
    if row is not None:
        pv.backfill(conn, _KIND, str(row["body"]), str(row["updated_at"]))


def get_news_prompt(conn: sqlite3.Connection) -> NewsPromptWire:
    """The news-organizer prompt wire (default-safe): body, stamp, official version,
    whether the stored body still IS the official one, and its version number."""
    ensure_news_prompt_seeded(conn)
    row = conn.execute(
        "SELECT body, updated_at FROM news_prompt_config WHERE id = 1"
    ).fetchone()
    if row is None:
        return _wire(conn, official_templates.NEWS_ORGANIZER_PROMPT, _SEED_AT.isoformat())
    return _wire(conn, str(row["body"]), str(row["updated_at"]))


def _write(
    conn: sqlite3.Connection,
    body: str,
    *,
    now: datetime,
    source: pv.Source,
    restored_from: int | None = None,
) -> NewsPromptWire:
    """The ONE body write: overwrite the row and append the version, then commit together."""
    ensure_news_prompt_seeded(conn)
    updated_at = now.isoformat()
    conn.execute(
        "INSERT INTO news_prompt_config (id, body, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at",
        (body, updated_at),
    )
    pv.record_write(conn, _KIND, body, source=source, now=now, restored_from=restored_from)
    conn.commit()
    return _wire(conn, body, updated_at)


def set_news_prompt(
    conn: sqlite3.Connection, body: str, *, now: datetime, source: pv.Source = "user_save"
) -> NewsPromptWire:
    """Overwrite the news-organizer prompt; stamp ``updated_at`` with *now*.

    DEF-057: a changed body is appended as the next version (stamped *source*); an unchanged
    one adds none.
    """
    return _write(conn, body, now=now, source=source)


def reset_news_prompt(conn: sqlite3.Connection, *, now: datetime) -> NewsPromptWire:
    """Restore the news-organizer prompt to the official library version (a version too)."""
    return set_news_prompt(
        conn, official_templates.NEWS_ORGANIZER_PROMPT, now=now, source="reset_official"
    )


def restore_news_prompt_version(
    conn: sqlite3.Connection, version_id: int, *, now: datetime
) -> tuple[NewsPromptWire, bool, int] | None:
    """Make an old version's body current again → ``(wire, changed, restored_from)``.

    None when *version_id* is unknown or belongs to another prompt. The restore is a NEW
    version (source ``restore``); restoring the current body writes nothing.
    """
    ensure_news_prompt_seeded(conn)
    chosen = pv.get_version(conn, version_id)
    if chosen is None or chosen.kind != _KIND:
        return None
    current = get_news_prompt(conn)
    if current["body"] == chosen.body:
        return current, False, chosen.version
    wire = _write(conn, chosen.body, now=now, source="restore", restored_from=chosen.version)
    return wire, True, chosen.version
