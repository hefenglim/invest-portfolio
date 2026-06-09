"""DB-backed LLM configuration: model registry, role-defaults, budget ledger.

Owns the four LLM tables and the three degradation exceptions. Depends only on
``shared/config_store`` (and stdlib); imports nothing from upper layers. Later
tasks append the model registry, role selection, and budget ledger here.
"""

import sqlite3
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel

from portfolio_dash.shared import config_store


class LLMError(Exception):
    """Base for all LLM-layer refusals. Callers catch this and map ``kind``."""

    kind = "llm_error"


class LLMUnavailable(LLMError):
    """Provider errored or returned unusable output."""

    kind = "llm_unavailable"


class AINotActivated(LLMError):
    """The required role has no enabled model configured (AI is off)."""

    kind = "ai_not_activated"


class LLMBudgetExceeded(LLMError):
    """The USD budget for the current period is exhausted."""

    kind = "budget_exceeded"


class LLMRole(StrEnum):
    DEFAULT = "default"
    DEFAULT_FALLBACK = "default_fallback"
    VISION = "vision"
    VISION_FALLBACK = "vision_fallback"


_DDL = """
CREATE TABLE IF NOT EXISTS llm_models (
    id TEXT PRIMARY KEY,
    model_alias TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    api_base TEXT,
    api_key TEXT,
    vision INTEGER NOT NULL DEFAULT 0,
    input_price_per_mtok TEXT NOT NULL,
    output_price_per_mtok TEXT NOT NULL,
    context_window INTEGER,
    max_output_tokens INTEGER,
    timeout_seconds INTEGER,
    max_retries INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS llm_defaults (
    role TEXT PRIMARY KEY,
    model_id TEXT REFERENCES llm_models(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS llm_budget_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, amount_usd TEXT NOT NULL, note TEXT
);
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, model TEXT NOT NULL, agent TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cost TEXT NOT NULL
);
"""


def create_llm_tables(conn: sqlite3.Connection) -> None:
    """Create all four LLM tables idempotently."""
    conn.executescript(_DDL)
    conn.commit()


def seed_llm_defaults(conn: sqlite3.Connection) -> None:
    """Seed/restore the four role rows to NULL (the AI-off state). Idempotent."""
    for role in LLMRole:
        conn.execute(
            "INSERT INTO llm_defaults (role, model_id) VALUES (?, NULL) "
            "ON CONFLICT(role) DO UPDATE SET model_id = NULL",
            (role.value,),
        )
    conn.commit()


def ensure_llm_seeded(conn: sqlite3.Connection) -> None:
    """Create LLM tables (always) and seed the AI-off default state (once)."""
    config_store.ensure_seeded(conn, "llm", create=create_llm_tables, seed=seed_llm_defaults)


def restore_llm_defaults(conn: sqlite3.Connection) -> None:
    """Reset the four role-defaults to NULL (turn the AI layer off)."""
    config_store.restore_defaults(conn, "llm", seed=seed_llm_defaults)


class ModelConfig(BaseModel):
    """A single registered LLM model (one ``llm_models`` row)."""

    model_config = {"protected_namespaces": ()}  # allow fields named model_*

    id: str
    model_alias: str
    provider: str  # openai | openrouter | anthropic | openai-compatible
    model_name: str
    api_base: str | None = None
    api_key: str | None = None
    vision: bool = False
    input_price_per_mtok: Decimal = Decimal("0")
    output_price_per_mtok: Decimal = Decimal("0")
    context_window: int | None = None
    max_output_tokens: int | None = None
    timeout_seconds: int | None = None
    max_retries: int | None = None
    enabled: bool = True
    notes: str | None = None


_COLS = (
    "id", "model_alias", "provider", "model_name", "api_base", "api_key", "vision",
    "input_price_per_mtok", "output_price_per_mtok", "context_window",
    "max_output_tokens", "timeout_seconds", "max_retries", "enabled", "notes",
)


def _to_row(m: ModelConfig) -> tuple[object, ...]:
    return (
        m.id, m.model_alias, m.provider, m.model_name, m.api_base, m.api_key,
        1 if m.vision else 0, str(m.input_price_per_mtok), str(m.output_price_per_mtok),
        m.context_window, m.max_output_tokens, m.timeout_seconds, m.max_retries,
        1 if m.enabled else 0, m.notes,
    )


def _from_row(r: sqlite3.Row) -> ModelConfig:
    return ModelConfig(
        id=r["id"], model_alias=r["model_alias"], provider=r["provider"],
        model_name=r["model_name"], api_base=r["api_base"], api_key=r["api_key"],
        vision=bool(r["vision"]),
        input_price_per_mtok=Decimal(r["input_price_per_mtok"]),
        output_price_per_mtok=Decimal(r["output_price_per_mtok"]),
        context_window=r["context_window"], max_output_tokens=r["max_output_tokens"],
        timeout_seconds=r["timeout_seconds"], max_retries=r["max_retries"],
        enabled=bool(r["enabled"]), notes=r["notes"],
    )


def upsert_model(conn: sqlite3.Connection, model: ModelConfig) -> None:
    """Insert or update a model by ``id``."""
    placeholders = ", ".join("?" for _ in _COLS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in _COLS if c != "id")
    conn.execute(
        f"INSERT INTO llm_models ({', '.join(_COLS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        _to_row(model),
    )
    conn.commit()


def get_model(conn: sqlite3.Connection, model_id: str) -> ModelConfig | None:
    row = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM llm_models WHERE id = ?", (model_id,)
    ).fetchone()
    return _from_row(row) if row is not None else None


def list_models(conn: sqlite3.Connection) -> list[ModelConfig]:
    return [
        _from_row(r)
        for r in conn.execute(f"SELECT {', '.join(_COLS)} FROM llm_models ORDER BY id")
    ]


def delete_model(conn: sqlite3.Connection, model_id: str) -> None:
    """Delete a model and null any role binding that referenced it."""
    conn.execute("UPDATE llm_defaults SET model_id = NULL WHERE model_id = ?", (model_id,))
    conn.execute("DELETE FROM llm_models WHERE id = ?", (model_id,))
    conn.commit()
