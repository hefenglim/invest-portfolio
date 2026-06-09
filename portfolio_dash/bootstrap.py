"""Package-root DB composition root: ledger tables + LLM config tables (seeded AI-off).

This module sits *above* the layered modules; it is the only place allowed to import
both ``data_ingestion`` and ``shared``. Keeping it out of ``shared/`` preserves the
one-way rule: ``shared/`` (incl. ``llm_config``) imports nothing internal.
"""

import sqlite3

from portfolio_dash.data_ingestion.schema import create_tables
from portfolio_dash.shared.llm_config import ensure_llm_seeded


def bootstrap_db(conn: sqlite3.Connection) -> None:
    """Create all ledger tables and the LLM config store (seeded to the AI-off state)."""
    create_tables(conn)
    ensure_llm_seeded(conn)
