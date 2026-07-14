"""Daily / weekly digest storage + config (Blueprint P3 batch 3 · Wave 1).

A leaf ops module: it imports ONLY stdlib + pydantic + ``portfolio_dash.shared``
(``config_store`` for the single-row config, ``clock`` for the seed / self-heal stamp).
It NEVER imports ``portfolio`` / ``pricing`` / ``strategy`` / ``api`` / ``scheduler`` —
the ASSEMBLY of a digest payload (which reads pricing + the portfolio core) lives in
``api/digest_service.py`` (architecture.md: ops is a leaf above shared; higher layers
call in, never the reverse). This module owns two tables:

* ``digests`` — one stored digest per ``(kind, digest_date)``; an idempotent per-day
  upsert so a re-run overwrites, never duplicates (mirrors the pricing idempotent-upsert
  rule — regeneration is safe).
* ``digest_config`` — a single-row JSON blob carrying the optional LLM one-liner switch
  (``llm_summary_enabled``, default **OFF** — owner ruling B3-D3), following the
  ``ops/notify.py`` config pattern (``_create`` / ``_seed`` / ``ensure_seeded`` /
  ``load_config`` / ``save_config``).

Payloads are opaque JSON strings assembled upstream (already Decimal-as-string) — no
money math happens here, so the leaf stays pure. Counts / strings only.
"""

import json
import sqlite3
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from portfolio_dash.shared import config_store
from portfolio_dash.shared.clock import app_now

_CATEGORY = "digest"
_SEED_AT = datetime(2026, 7, 14)

# The two persisted digest editions. The api-layer assembler + the router validate kind
# against this tuple before touching the DB (the table CHECK is the last line of defence).
VALID_KINDS: tuple[str, ...] = ("daily", "weekly")

_DDL_DIGESTS = (
    "CREATE TABLE IF NOT EXISTS digests ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "kind TEXT NOT NULL CHECK(kind IN ('daily','weekly')), "
    "digest_date TEXT NOT NULL, "
    "payload TEXT NOT NULL, "
    "generated_at TEXT NOT NULL, "
    "UNIQUE(kind, digest_date))"
)
_DDL_CONFIG = (
    "CREATE TABLE IF NOT EXISTS digest_config "
    "(id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT NOT NULL, updated_at TEXT NOT NULL)"
)


class DigestConfig(BaseModel):
    """The single-row digest config: the optional AI one-liner switch (default OFF)."""

    llm_summary_enabled: bool = False


def _default_config() -> DigestConfig:
    """The out-of-the-box config: the LLM one-liner OFF (owner ruling B3-D3)."""
    return DigestConfig()


def _create(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL_DIGESTS)
    conn.execute(_DDL_CONFIG)


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO digest_config (id, body, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (_default_config().model_dump_json(), _SEED_AT.isoformat()),
    )


def ensure_seeded(conn: sqlite3.Connection) -> None:
    """Create both tables (always) and seed the default config row once."""
    config_store.ensure_seeded(conn, _CATEGORY, create=_create, seed=_seed)


def _persist(conn: sqlite3.Connection, cfg: DigestConfig, *, now: datetime) -> None:
    conn.execute(
        "INSERT INTO digest_config (id, body, updated_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at",
        (cfg.model_dump_json(), now.isoformat()),
    )
    conn.commit()


def load_config(conn: sqlite3.Connection) -> DigestConfig:
    """Return the persisted :class:`DigestConfig` (self-seeding a missing row)."""
    ensure_seeded(conn)
    row = conn.execute("SELECT body FROM digest_config WHERE id = 1").fetchone()
    if row is None:
        cfg = _default_config()
        _persist(conn, cfg, now=app_now())
        return cfg
    return DigestConfig.model_validate_json(row["body"])


def save_config(conn: sqlite3.Connection, cfg: DigestConfig, *, now: datetime) -> DigestConfig:
    """Persist *cfg* and return it (caller has already validated)."""
    ensure_seeded(conn)
    _persist(conn, cfg, now=now)
    return cfg


# --- digest storage (idempotent per-day upsert) -------------------------------


def upsert_digest(
    conn: sqlite3.Connection,
    *,
    kind: str,
    digest_date: str,
    payload: str,
    generated_at: str,
) -> None:
    """Store (or overwrite) the digest for ``(kind, digest_date)``.

    Idempotent on the natural key: re-running the same day overwrites the payload +
    stamp, never inserting a duplicate row (regeneration is safe).
    """
    ensure_seeded(conn)
    conn.execute(
        "INSERT INTO digests (kind, digest_date, payload, generated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(kind, digest_date) DO UPDATE SET "
        "payload = excluded.payload, generated_at = excluded.generated_at",
        (kind, digest_date, payload, generated_at),
    )
    conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """A stored digest row → wire dict (payload JSON parsed back to an object)."""
    return {
        "kind": row["kind"],
        "digest_date": row["digest_date"],
        "generated_at": row["generated_at"],
        "payload": json.loads(row["payload"]),
    }


def get_latest(conn: sqlite3.Connection, kind: str) -> dict[str, Any] | None:
    """The most recent stored digest of *kind*, or ``None`` when none exists."""
    ensure_seeded(conn)
    row = conn.execute(
        "SELECT kind, digest_date, generated_at, payload FROM digests "
        "WHERE kind = ? ORDER BY digest_date DESC, id DESC LIMIT 1",
        (kind,),
    ).fetchone()
    return _row_to_dict(row) if row is not None else None


def get_history(
    conn: sqlite3.Connection, kind: str, *, offset: int, limit: int
) -> tuple[int, list[dict[str, Any]]]:
    """Page stored digests of *kind*, newest first. Returns ``(total, rows)``.

    ``total`` is constant across pages (the whole set of that kind) so the client knows
    when to stop paging (mirrors the whats-new history browser).
    """
    ensure_seeded(conn)
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM digests WHERE kind = ?", (kind,)
    ).fetchone()["n"]
    rows = conn.execute(
        "SELECT kind, digest_date, generated_at, payload FROM digests "
        "WHERE kind = ? ORDER BY digest_date DESC, id DESC LIMIT ? OFFSET ?",
        (kind, limit, offset),
    ).fetchall()
    return int(total), [_row_to_dict(r) for r in rows]


__all__ = [
    "VALID_KINDS",
    "DigestConfig",
    "ensure_seeded",
    "get_history",
    "get_latest",
    "load_config",
    "save_config",
    "upsert_digest",
]
