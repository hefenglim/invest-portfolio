"""The single global system prompt (config_store single-row), prepended to every render.

Spec 06.2 returns ``system_prompt`` from preview; spec 07 has a ``system`` assembly
layer; spec 04 ``use_system_prompt`` toggles it. Neither spec explicitly owns this CRUD,
but it is foundational to rendering, so it lands here (spec 06a, reconciliation #6).

Stored as one editable global value via :mod:`config_store` (category ``system_prompt``);
the default is the ``web/settings-prompts.js`` ``PROMPTS_DATA.system_prompt`` text.

DEF-057 (owner ruling 2026-09-24): the body keeps a version history in
``shared/prompt_versions.py`` (kind ``system``). Every write door of this module appends the
new body there in the SAME transaction as the body write — :func:`set_system_prompt`
(儲存, 還原官方) and :func:`restore_system_prompt_version` (回復) — and
:func:`ensure_system_prompt_seeded` back-fills the current body as v1 on the first boot after
the table appeared (or as a ``backfill`` version when something wrote the row around this
module). A card records the version it was assembled with (:class:`SystemPromptRef`).
"""

import sqlite3
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from portfolio_dash.llm_insight import official_templates
from portfolio_dash.shared import config_store
from portfolio_dash.shared import prompt_versions as pv

# The shipped default IS the official library's system prompt (2026-07-05 program:
# first-touch experience = the official optimum; the old inline v1 text is superseded).
DEFAULT_SYSTEM_PROMPT = official_templates.SYSTEM_PROMPT_BODY

_CATEGORY = "system_prompt"
_KIND: pv.Kind = "system"
_SEED_AT = datetime(2026, 5, 28)

_DDL = (
    "CREATE TABLE IF NOT EXISTS system_prompt_config "
    "(id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT NOT NULL, updated_at TEXT NOT NULL)"
)


class SystemPromptRef(BaseModel):
    """What an insight card records about its system-prompt layer (DEF-057).

    ``used`` is False when no system layer was assembled (the task's ``use_system_prompt`` is
    off, or the zero-LLM anomaly card had no prompt at all). ``version`` is the version of the
    body that WAS assembled, looked up by body; None when it matches no stored version —
    reported as 「版本不明」, never guessed. A card that stores no ref at all predates the
    record (「生成時版本未記錄」).
    """

    used: bool
    version: int | None = None


def _create(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _seed(conn: sqlite3.Connection) -> None:
    """Insert the single default row (id=1). Idempotent (config_store seeds once)."""
    conn.execute(
        "INSERT INTO system_prompt_config (id, body, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (DEFAULT_SYSTEM_PROMPT, _SEED_AT.isoformat()),
    )


def ensure_system_prompt_seeded(conn: sqlite3.Connection) -> None:
    """Create the single-row table (always), seed the default body (once), and make the
    stored body the newest version of the history (DEF-057 back-fill; idempotent)."""
    config_store.ensure_seeded(conn, _CATEGORY, create=_create, seed=_seed)
    row = conn.execute(
        "SELECT body, updated_at FROM system_prompt_config WHERE id = 1"
    ).fetchone()
    if row is not None:
        pv.backfill(conn, _KIND, str(row["body"]), str(row["updated_at"]))


def get_system_prompt(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return ``{"body", "updated_at", "current_version"}`` for the global system prompt.

    Falls back to the default when the row is somehow absent (defensive — seeding runs
    in the app lifespan and in golden_db). ``current_version`` (DEF-057, additive) is the
    number of the newest history row — the version a card assembled NOW records.
    """
    ensure_system_prompt_seeded(conn)
    row = conn.execute(
        "SELECT body, updated_at FROM system_prompt_config WHERE id = 1"
    ).fetchone()
    if row is None:
        return {"body": DEFAULT_SYSTEM_PROMPT, "updated_at": _SEED_AT.isoformat(),
                "current_version": pv.version_of_body(conn, _KIND, DEFAULT_SYSTEM_PROMPT)}
    return {"body": row["body"], "updated_at": row["updated_at"],
            "current_version": pv.current_version_no(conn, _KIND)}


def _write(
    conn: sqlite3.Connection,
    body: str,
    *,
    now: datetime,
    source: pv.Source,
    restored_from: int | None = None,
) -> dict[str, Any]:
    """The ONE body write: overwrite the row and append the version, then commit together."""
    ensure_system_prompt_seeded(conn)
    updated_at = now.isoformat()
    conn.execute(
        "INSERT INTO system_prompt_config (id, body, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at",
        (body, updated_at),
    )
    pv.record_write(conn, _KIND, body, source=source, now=now, restored_from=restored_from)
    conn.commit()
    return {"body": body, "updated_at": updated_at,
            "current_version": pv.current_version_no(conn, _KIND)}


def set_system_prompt(
    conn: sqlite3.Connection,
    body: str,
    *,
    now: datetime,
    source: pv.Source = "user_save",
) -> dict[str, Any]:
    """Overwrite the global system prompt body; stamp ``updated_at`` with *now*.

    DEF-057: a body that differs from the newest version is appended as the next version,
    stamped *source* (``user_save`` for 儲存, ``reset_official`` for 還原官方). An unchanged
    body adds no version.
    """
    return _write(conn, body, now=now, source=source)


def reset_system_prompt(conn: sqlite3.Connection, *, now: datetime) -> dict[str, Any]:
    """Restore the body to the official library version — itself a version (DEF-057)."""
    return set_system_prompt(
        conn, official_templates.SYSTEM_PROMPT_BODY, now=now, source="reset_official"
    )


def restore_system_prompt_version(
    conn: sqlite3.Connection, version_id: int, *, now: datetime
) -> tuple[dict[str, Any], bool, int] | None:
    """Make an old version's body current again → ``(wire, changed, restored_from)``.

    None when *version_id* is unknown or belongs to another prompt. History is never
    rewritten: the restore is a NEW version (source ``restore``, ``restored_from`` = the chosen
    number). Restoring the body that is already current writes nothing (``changed`` False).
    """
    ensure_system_prompt_seeded(conn)
    chosen = pv.get_version(conn, version_id)
    if chosen is None or chosen.kind != _KIND:
        return None
    current = get_system_prompt(conn)
    if current["body"] == chosen.body:
        return current, False, chosen.version
    wire = _write(conn, chosen.body, now=now, source="restore", restored_from=chosen.version)
    return wire, True, chosen.version
