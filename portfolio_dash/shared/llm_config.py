"""DB-backed LLM configuration: model registry, role-defaults, budget ledger.

Owns the four LLM tables and the three degradation exceptions. Depends only on
``shared/config_store`` (and stdlib); imports nothing from upper layers. Later
tasks append the model registry, role selection, and budget ledger here.
"""

import sqlite3
from datetime import UTC, datetime
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
    MASTER = "master"  # spec 04 §4.3: the orchestrating "master" agent
    MASTER_FALLBACK = "master_fallback"


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
    """Create all four LLM tables idempotently (+ additive column migrations)."""
    conn.executescript(_DDL)
    # Request-detail ledger (2026-07-07): provider-reported cached prompt tokens per call.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_usage)")}
    if "cache_tokens" not in cols:
        conn.execute("ALTER TABLE llm_usage ADD COLUMN cache_tokens INTEGER NOT NULL DEFAULT 0")
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


def set_role(conn: sqlite3.Connection, role: LLMRole, model_id: str | None) -> None:
    """Bind *role* to *model_id* (or None to disable that role)."""
    conn.execute(
        "INSERT INTO llm_defaults (role, model_id) VALUES (?, ?) "
        "ON CONFLICT(role) DO UPDATE SET model_id = excluded.model_id",
        (role.value, model_id),
    )
    conn.commit()


def get_role_model_id(conn: sqlite3.Connection, role: LLMRole) -> str | None:
    row = conn.execute(
        "SELECT model_id FROM llm_defaults WHERE role = ?", (role.value,)
    ).fetchone()
    return row["model_id"] if row is not None else None


def select_role_models(
    conn: sqlite3.Connection, primary: LLMRole, fallback: LLMRole
) -> list[ModelConfig]:
    """Return the ordered [primary, fallback] enabled models for a role pair.

    The generic core behind :func:`select_models`; used directly for the spec-04.3 master
    role (``MASTER`` / ``MASTER_FALLBACK``). Skips an unbound or disabled model in either
    slot. Raises :exc:`AINotActivated` when neither role resolves to an enabled model
    (e.g. master unset → the self-correct pipeline pauses). The order drives runtime
    failover in ``shared/llm.py``.
    """
    chain: list[ModelConfig] = []
    for role in (primary, fallback):
        model_id = get_role_model_id(conn, role)
        if model_id is None:
            continue
        model = get_model(conn, model_id)
        if model is not None and model.enabled:
            chain.append(model)
    if not chain:
        raise AINotActivated(f"no enabled model configured for the {primary.value} role")
    return chain


def select_models(conn: sqlite3.Connection, *, vision: bool) -> list[ModelConfig]:
    """Return the ordered [primary, fallback] enabled models for the task kind.

    Thin wrapper over :func:`select_role_models` selecting the vision or default role pair.
    Raises :exc:`AINotActivated` when neither role resolves to an enabled model.
    """
    primary, fallback = (
        (LLMRole.VISION, LLMRole.VISION_FALLBACK)
        if vision
        else (LLMRole.DEFAULT, LLMRole.DEFAULT_FALLBACK)
    )
    return select_role_models(conn, primary, fallback)


def budget_remaining(conn: sqlite3.Connection) -> Decimal:
    """Remaining USD = Σ(all top-up amounts) − Σ(all usage cost). The single source
    of truth for the gate, the settings page, and the dashboard chip.

    Cumulative and never ``None``: a fresh DB with no top-ups has remaining ``0``.
    There is no "reset" concept — every budget event is a positive top-up that adds.
    """
    topups = conn.execute(
        "SELECT amount_usd FROM llm_budget_events"
    ).fetchall()
    spent = conn.execute("SELECT cost FROM llm_usage").fetchall()
    total_topups = sum((Decimal(r["amount_usd"]) for r in topups), Decimal("0"))
    total_spent = sum((Decimal(r["cost"]) for r in spent), Decimal("0"))
    return total_topups - total_spent


def check_budget(conn: sqlite3.Connection) -> None:
    """Gate: raise :exc:`LLMBudgetExceeded` when cumulative remaining is ``<= 0``.

    A fresh DB ($0 topped up) and a fully-consumed budget both block here — you must
    top up before AI calls run, even when models/roles are configured.
    """
    remaining = budget_remaining(conn)
    if remaining <= 0:
        raise LLMBudgetExceeded(f"token budget exhausted (remaining ${remaining})")


_PROVIDER_PREFIX = {
    "openai": "openai",
    "openrouter": "openrouter",
    "anthropic": "anthropic",
    "openai-compatible": "openai",  # uses the OpenAI adapter + an explicit api_base
}


def litellm_model_string(model: ModelConfig) -> str:
    """Compose the LiteLLM ``provider/model`` string from a registry row."""
    prefix = _PROVIDER_PREFIX.get(model.provider, "openai")
    return f"{prefix}/{model.model_name}"


# --- spec 16: settings reads/writes (CRUD support, quota view, threshold) -----


def roles_using_model(conn: sqlite3.Connection, model_id: str) -> list[str]:
    """Return the role names (``LLMRole`` values) currently bound to *model_id*.

    Used by the API to block (HTTP 422) deletion of a model still assigned to a role.
    """
    return [
        row["role"]
        for row in conn.execute(
            "SELECT role FROM llm_defaults WHERE model_id = ? ORDER BY role", (model_id,)
        )
    ]


def all_role_bindings(conn: sqlite3.Connection) -> dict[str, str | None]:
    """Return ``{role_value: model_id_or_None}`` for every defined :class:`LLMRole`."""
    bound = {
        row["role"]: row["model_id"]
        for row in conn.execute("SELECT role, model_id FROM llm_defaults")
    }
    return {role.value: bound.get(role.value) for role in LLMRole}


def add_topup(
    conn: sqlite3.Connection, amount_usd: Decimal, note: str | None = None
) -> None:
    """Append a positive top-up to the budget ledger (append-only). The canonical
    budget writer: ``remaining`` (:func:`budget_remaining`) subtracts cumulative usage
    from cumulative top-ups, so each top-up adds — there is no reset semantics.
    """
    conn.execute(
        "INSERT INTO llm_budget_events (ts, amount_usd, note) VALUES (?, ?, ?)",
        (datetime.now(UTC).isoformat(), str(amount_usd), note),
    )
    conn.commit()


def list_topups(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """All budget events oldest-first: ``[{"at", "amount_usd", "note"}]``."""
    return [
        {
            "at": row["ts"],
            "amount_usd": row["amount_usd"],
            "note": row["note"],
        }
        for row in conn.execute(
            "SELECT ts, amount_usd, note FROM llm_budget_events ORDER BY ts ASC, id ASC"
        )
    ]


def quota_remaining(conn: sqlite3.Connection) -> Decimal:
    """Spec-16 remaining = Σ all top-ups − Σ all usage cost. Delegates to
    :func:`budget_remaining` so the gate, settings page, and dashboard chip share one
    source of truth (they return identical values).
    """
    return budget_remaining(conn)


_THRESHOLD_DDL = (
    "CREATE TABLE IF NOT EXISTS llm_quota_config "
    "(id INTEGER PRIMARY KEY CHECK (id = 1), alert_threshold_usd TEXT)"
)
# Default per the spec-03 §3.1 SR clarification ("預設值 1.00"): quota_low fires when
# remaining < 1.00 until the user sets their own threshold (spec 16 single source of truth).
_DEFAULT_THRESHOLD = Decimal("1.00")


def _ensure_threshold_table(conn: sqlite3.Connection) -> None:
    conn.execute(_THRESHOLD_DDL)


def get_alert_threshold(conn: sqlite3.Connection) -> Decimal:
    """Read the quota-low alert threshold (USD); defaults to 1.00 when never set
    (spec 03 §3.1 ``quota_low`` SR — single source of truth in spec 16's quota config)."""
    _ensure_threshold_table(conn)
    row = conn.execute(
        "SELECT alert_threshold_usd FROM llm_quota_config WHERE id = 1"
    ).fetchone()
    if row is None or row["alert_threshold_usd"] is None:
        return _DEFAULT_THRESHOLD
    return Decimal(row["alert_threshold_usd"])


def set_alert_threshold(conn: sqlite3.Connection, amount_usd: Decimal) -> None:
    """Set the quota-low alert threshold (USD). Single-row upsert."""
    _ensure_threshold_table(conn)
    conn.execute(
        "INSERT INTO llm_quota_config (id, alert_threshold_usd) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET alert_threshold_usd = excluded.alert_threshold_usd",
        (str(amount_usd),),
    )
    conn.commit()
