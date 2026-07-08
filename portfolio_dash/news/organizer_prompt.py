"""The editable news-organizer system prompt (single-row config in the MAIN DB).

Mirrors ``llm_insight/system_prompt.py``: one user-editable global value via
``config_store``, defaulting to ``official_templates.NEWS_ORGANIZER_PROMPT`` with a
reset-to-official path. Small config (not article text) so it lives in the ledger DB
next to the other prompts, not the separate news DB.
"""

import sqlite3
from datetime import datetime

from portfolio_dash.llm_insight import official_templates
from portfolio_dash.shared import config_store

_CATEGORY = "news_prompt"
_SEED_AT = datetime(2026, 7, 6)

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
    """Create the single-row table (always) and seed the official default (once)."""
    config_store.ensure_seeded(conn, _CATEGORY, create=_create, seed=_seed)


def get_news_prompt(conn: sqlite3.Connection) -> dict[str, str]:
    """Return ``{"body", "updated_at"}`` for the news-organizer prompt (default-safe)."""
    ensure_news_prompt_seeded(conn)
    row = conn.execute(
        "SELECT body, updated_at FROM news_prompt_config WHERE id = 1"
    ).fetchone()
    if row is None:
        return {"body": official_templates.NEWS_ORGANIZER_PROMPT,
                "updated_at": _SEED_AT.isoformat()}
    return {"body": row["body"], "updated_at": row["updated_at"]}


def set_news_prompt(conn: sqlite3.Connection, body: str, *, now: datetime) -> dict[str, str]:
    """Overwrite the news-organizer prompt; stamp ``updated_at`` with *now*."""
    ensure_news_prompt_seeded(conn)
    updated_at = now.isoformat()
    conn.execute(
        "INSERT INTO news_prompt_config (id, body, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at",
        (body, updated_at),
    )
    conn.commit()
    return {"body": body, "updated_at": updated_at}


def reset_news_prompt(conn: sqlite3.Connection, *, now: datetime) -> dict[str, str]:
    """Restore the news-organizer prompt to the official library version."""
    return set_news_prompt(conn, official_templates.NEWS_ORGANIZER_PROMPT, now=now)
