"""DB-backed LLM configuration: model registry, role-defaults, budget ledger.

Owns the four LLM tables and the three degradation exceptions. Depends only on
``shared/config_store`` (and stdlib); imports nothing from upper layers. Later
tasks append the model registry, role selection, and budget ledger here.
"""

import sqlite3
from enum import StrEnum

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
