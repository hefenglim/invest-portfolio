"""Durable capture of what an LLM call actually sent and actually got back, on failure.

Why this exists
---------------
Nothing else in this codebase retains a prompt or a raw completion. ``shared.llm`` builds
``messages`` as a local, reads ``content`` as a local, parses it, and drops both -- on
success AND on failure (see ``_complete_with_meta``). ``llm_usage`` records tokens and
cost, never text. So when an extraction came back wrong there was no way, after the fact,
to ask *what did the model actually say* -- only which parsed field mismatched.

That blind spot has already cost a wrong conclusion: the W4 baseline recorded a stock
dividend failure as "contained to the dividend ledger" when the containment could not be
checked, because the raw reply was gone (AI-D64, 2026-08-28).

What it stores
--------------
One row per ATTEMPT, not per call. ``_complete_with_meta`` bills before it parses and
salvages once with a second full provider call, so one logical failure can be two billed
attempts with two different raw outputs; a per-call row would hide half of what was paid
for. ``usage_id`` links each row to the ``llm_usage`` row of the same attempt.

``agent`` IS the task category. Every call site in the app already passes one
(``ai_agents_input``, insight generation, news organize, master scoring, symbol resolve,
digest notes), and it already threads into ``llm_usage``. A parallel taxonomy would be a
second name for the same thing -- and in production the finer categories are not knowable
anyway: when extraction fails you do not yet know it was a dividend.

Bounds
------
``_KEEP`` newest rows, pruned on insert (the ``api/action_log.py`` precedent), and each
text column capped at ``_MAX_TEXT`` with a ``truncated`` flag. Worst case is therefore
about 300 x 3 x 16 KiB ~= 14 MiB, which is a hard ceiling rather than a hope. Rows roll
off silently, so the panel shows the oldest timestamp: that is the user's cue to download
before the corpus loses its tail.

Base64 image payloads are NEVER stored. A vision call carries up to four images of up to
5 MB each inside ``messages``; only the text part is kept, plus ``image_count``.

Known gap (stated, not papered over)
------------------------------------
``api/routers/llm_settings.py``'s model-ping reaches the provider through the LiteLLM
entry point directly, bypassing ``shared.llm`` entirely. Its failures do not reach this
log and this module does not pretend otherwise. (Spelled out rather than quoted verbatim:
``tests/llm_insight/test_prompt_registry.py`` scans the package for that call token to
find unregistered prompt call sites, and a mention in prose would read as one.)

Layer note: this lives in ``shared/`` because ``shared/llm.py`` imports it, and ``shared/``
may not import anything above itself. It depends on stdlib plus ``shared.clock`` only.
"""

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from portfolio_dash.shared.clock import app_now

#: Newest rows kept; older ones are pruned as part of each insert.
_KEEP = 300
#: Per-text-column cap. Beyond this the value is cut and ``truncated`` is set.
_MAX_TEXT = 16384

#: Every outcome this log can carry. ``ok`` appears ONLY under capture-all mode (below).
OUTCOMES = (
    "provider_error",     # the provider call itself raised
    "invalid_json",       # the reply was not JSON at all
    "schema_mismatch",    # the reply was valid JSON but did not match the schema
    "budget_exceeded",    # the budget gate refused before any call
    "ai_not_activated",   # no enabled model for the role
    "unparsed_rows",      # a SUCCESSFUL call whose model confessed it could not classify rows
    "ok",                 # eval/capture-all only; never written in production
)

# --- capture mode -----------------------------------------------------------------
# Production records failures only. The corpus evaluator turns this on so that EVERY
# attempt is kept, which is the only way to see what a model returned for a case that
# "failed" by field comparison rather than by raising. Explicit module state rather than
# an env var: greppable, testable, and it cannot be switched on by accident from a shell.
_capture_all = False


def set_capture_mode(all_attempts: bool) -> None:
    """Turn capture-all on/off. Default OFF; only the eval harness turns it on."""
    global _capture_all
    _capture_all = all_attempts


def capture_all() -> bool:
    """True when successful attempts are being captured too (eval harness only)."""
    return _capture_all


@dataclass(frozen=True)
class FailRecord:
    """One captured attempt. All text already capped; ``truncated`` says whether it was."""

    created_at: str
    agent: str
    outcome: str
    model: str
    attempt: int
    prompt: str
    raw_output: str
    error_reason: str
    source_text: str | None
    image_count: int
    usage_id: int | None
    truncated: int


# --- conn-bearing store (bounded ring; never a source of truth) ---------------------

_DDL = """
CREATE TABLE IF NOT EXISTS llm_fail_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL,
    agent        TEXT    NOT NULL,
    outcome      TEXT    NOT NULL,
    model        TEXT    NOT NULL DEFAULT '',
    attempt      INTEGER NOT NULL DEFAULT 1,
    prompt       TEXT    NOT NULL DEFAULT '',
    raw_output   TEXT    NOT NULL DEFAULT '',
    error_reason TEXT    NOT NULL DEFAULT '',
    source_text  TEXT,
    image_count  INTEGER NOT NULL DEFAULT 0,
    usage_id     INTEGER,
    truncated    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_llm_fail_log_agent ON llm_fail_log(agent);
"""

_COLUMNS = (
    "created_at", "agent", "outcome", "model", "attempt",
    "prompt", "raw_output", "error_reason", "source_text",
    "image_count", "usage_id", "truncated",
)

_INSERT_SQL = (
    "INSERT INTO llm_fail_log (" + ", ".join(_COLUMNS) + ") VALUES ("
    + ", ".join("?" for _ in _COLUMNS) + ")"
)


def ensure_table(conn: sqlite3.Connection) -> None:
    """Create the table and its index if absent. Idempotent."""
    conn.executescript(_DDL)
    conn.commit()


def _cap(text: str) -> tuple[str, bool]:
    """Cut *text* to the cap, reporting whether anything was removed."""
    if len(text) <= _MAX_TEXT:
        return text, False
    return text[:_MAX_TEXT], True


def prompt_text_of(messages: list[dict[str, Any]]) -> tuple[str, int]:
    """Flatten chat *messages* to text, dropping image payloads and counting them.

    A vision call embeds each image as a base64 data URI inside the content list. Those
    are megabytes of noise with no diagnostic value, so only the text parts survive; the
    count is kept because "this failure had 3 images attached" IS diagnostic.
    """
    parts: list[str] = []
    images = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif item.get("type") == "image_url":
                    images += 1
    return "\n".join(parts), images


def record(
    conn: sqlite3.Connection,
    *,
    agent: str,
    outcome: str,
    model: str = "",
    attempt: int = 1,
    prompt: str = "",
    raw_output: str = "",
    error_reason: str = "",
    source_text: str | None = None,
    image_count: int = 0,
    usage_id: int | None = None,
) -> None:
    """Append one attempt and prune to ``_KEEP``. Never raises into the caller.

    A logging failure must not turn a degraded LLM call into a 500 -- the point of this
    table is to explain failures, not to create them. A missing table (a ledger-only
    database, an older deployment) therefore reads as "no capture", exactly the
    cross-layer obligation the architecture rules state for borrowed tables.
    """
    p, t1 = _cap(prompt)
    r, t2 = _cap(raw_output)
    e, t3 = _cap(error_reason)
    s: str | None = None
    t4 = False
    if source_text is not None:
        s, t4 = _cap(source_text)
    try:
        conn.execute(_INSERT_SQL, (
            app_now().isoformat(), agent, outcome, model, attempt,
            p, r, e, s, image_count, usage_id,
            1 if (t1 or t2 or t3 or t4) else 0,
        ))
        conn.execute(
            "DELETE FROM llm_fail_log WHERE id <= "
            "(SELECT MAX(id) FROM llm_fail_log) - ?",
            (_KEEP,),
        )
        conn.commit()
    except sqlite3.Error:
        return


def counts_by_agent(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Per-agent totals plus the oldest timestamp still held. Empty when absent."""
    try:
        rows = conn.execute(
            "SELECT agent, COUNT(*) AS n, MIN(created_at) AS oldest "
            "FROM llm_fail_log GROUP BY agent ORDER BY n DESC, agent"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"agent": r[0], "n": int(r[1]), "oldest": r[2]} for r in rows]


def total_count(conn: sqlite3.Connection) -> int:
    """Row count, or 0 when the table is absent."""
    try:
        row = conn.execute("SELECT COUNT(*) FROM llm_fail_log").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def list_rows(
    conn: sqlite3.Connection, *, agent: str | None = None, limit: int | None = None
) -> list[dict[str, object]]:
    """Newest-first rows as plain dicts, ready for JSON lines. Empty when absent."""
    sql = "SELECT id, " + ", ".join(_COLUMNS) + " FROM llm_fail_log"
    params: list[object] = []
    if agent:
        sql += " WHERE agent = ?"
        params.append(agent)
    sql += " ORDER BY id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    names = ("id", *_COLUMNS)
    return [dict(zip(names, r, strict=True)) for r in rows]


def delete_all(conn: sqlite3.Connection, *, agent: str | None = None) -> int:
    """Delete every row (or one agent's). Returns the number removed.

    This touches ``llm_fail_log`` and NOTHING else. In particular it must never reach
    ``llm_usage``: remaining budget is ``sum(topups) - sum(llm_usage.cost)``, so deleting
    usage rows would silently hand the user free budget. A test pins that, because a
    comment is not a control.
    """
    sql = "DELETE FROM llm_fail_log"
    params: list[object] = []
    if agent:
        sql += " WHERE agent = ?"
        params.append(agent)
    try:
        cur = conn.execute(sql, params)
        conn.commit()
    except sqlite3.OperationalError:
        return 0
    return int(cur.rowcount or 0)


def to_jsonl(rows: list[dict[str, object]]) -> str:
    """Render rows as JSON Lines: one complete record per line, UTF-8, newline-terminated."""
    return "".join(
        json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows
    )


__all__ = [
    "OUTCOMES",
    "FailRecord",
    "capture_all",
    "counts_by_agent",
    "delete_all",
    "ensure_table",
    "list_rows",
    "prompt_text_of",
    "record",
    "set_capture_mode",
    "to_jsonl",
    "total_count",
]
