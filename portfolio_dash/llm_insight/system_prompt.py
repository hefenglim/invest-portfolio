"""The single global system prompt (config_store single-row), prepended to every render.

Spec 06.2 returns ``system_prompt`` from preview; spec 07 has a ``system`` assembly
layer; spec 04 ``use_system_prompt`` toggles it. Neither spec explicitly owns this CRUD,
but it is foundational to rendering, so it lands here (spec 06a, reconciliation #6).

Stored as one editable global value via :mod:`config_store` (category ``system_prompt``);
the default is the ``web/settings-prompts.js`` ``PROMPTS_DATA.system_prompt`` text.
"""

import sqlite3
from datetime import datetime

from portfolio_dash.llm_insight import official_templates
from portfolio_dash.shared import config_store

# The shipped default IS the official library's system prompt (2026-07-05 program:
# first-touch experience = the official optimum; the old inline v1 text is superseded).
DEFAULT_SYSTEM_PROMPT = official_templates.SYSTEM_PROMPT_BODY

_CATEGORY = "system_prompt"

_DDL = (
    "CREATE TABLE IF NOT EXISTS system_prompt_config "
    "(id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT NOT NULL, updated_at TEXT NOT NULL)"
)


def _create(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _seed(conn: sqlite3.Connection) -> None:
    """Insert the single default row (id=1). Idempotent (config_store seeds once)."""
    conn.execute(
        "INSERT INTO system_prompt_config (id, body, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (DEFAULT_SYSTEM_PROMPT, datetime(2026, 5, 28).isoformat()),
    )


def ensure_system_prompt_seeded(conn: sqlite3.Connection) -> None:
    """Create the single-row table (always) and seed the default body (once)."""
    config_store.ensure_seeded(conn, _CATEGORY, create=_create, seed=_seed)


def get_system_prompt(conn: sqlite3.Connection) -> dict[str, str]:
    """Return ``{"body", "updated_at"}`` for the global system prompt.

    Falls back to the default when the row is somehow absent (defensive — seeding runs
    in the app lifespan and in golden_db).
    """
    ensure_system_prompt_seeded(conn)
    row = conn.execute(
        "SELECT body, updated_at FROM system_prompt_config WHERE id = 1"
    ).fetchone()
    if row is None:
        return {"body": DEFAULT_SYSTEM_PROMPT, "updated_at": datetime(2026, 5, 28).isoformat()}
    return {"body": row["body"], "updated_at": row["updated_at"]}


def set_system_prompt(conn: sqlite3.Connection, body: str, *, now: datetime) -> dict[str, str]:
    """Overwrite the global system prompt body; stamp ``updated_at`` with *now*."""
    ensure_system_prompt_seeded(conn)
    updated_at = now.isoformat()
    conn.execute(
        "INSERT INTO system_prompt_config (id, body, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at",
        (body, updated_at),
    )
    conn.commit()
    return {"body": body, "updated_at": updated_at}
