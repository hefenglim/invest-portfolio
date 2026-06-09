# LLM Config Management + Token Budget Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single-endpoint LLM client with a DB-backed model registry, four nullable role-defaults, a USD token-budget ledger with a hard gate, and image (vision) input — on a reusable settings-seeding framework.

**Architecture:** All in `shared/` (lowest layer, imported everywhere, imports nothing internal). `config_store.py` gives a generic "create-always / seed-once" framework + a `settings_meta` marker. `llm_config.py` owns the four LLM tables, the registry/role/budget data access, the three degradation exceptions, and model selection with runtime fallback. `llm.py` expands `complete_structured` to gate on budget, select by role, fail over to the fallback model, support vision content, and log cost from the selected model's pricing. The settings-page UI is `web_ui/` and out of scope.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, Pydantic v2, LiteLLM, Decimal-as-TEXT, pytest, mypy strict, ruff.

---

## File Structure

- Create `portfolio_dash/shared/config_store.py` — generic settings framework: `settings_meta` table, `ensure_seeded(conn, category, *, create, seed)` (create idempotently always, seed once), `restore_defaults(conn, category, *, seed)`.
- Create `portfolio_dash/shared/llm_config.py` — LLM tables DDL (`create_llm_tables`), `ensure_llm_seeded` / `seed_llm_defaults` / `restore_llm_defaults` (AI-off state), `ModelConfig` + registry CRUD, `LLMRole` + role-default access, `select_models` (with fallback), budget ledger (`budget_remaining` / `reset_budget` / `check_budget`), `litellm_model_string`, and the exceptions `LLMError` / `LLMUnavailable` / `AINotActivated` / `LLMBudgetExceeded`.
- Modify `portfolio_dash/shared/llm.py` — re-export the exceptions; keep `ModelPricing` / `cost_of` / `log_usage`; rewrite `complete_structured` (conn required, `images=`, budget gate, role selection + fallback, vision messages, pricing from registry).
- Modify `portfolio_dash/data_ingestion/schema.py` — remove `llm_usage` (now owned by `shared/llm_config.py`).
- Modify `portfolio_dash/data_ingestion/agents.py` — drop `pricing`; catch `LLMError` and map `exc.kind` → issue.
- Modify `portfolio_dash/data_ingestion/config_seed.py` — remove the superseded `ModelPricing` + `DEFAULT_LLM_MODELS`.
- Modify `portfolio_dash/shared/config.py` — remove the unused `llm_endpoint` / `llm_api_key` / `llm_active_model` fields.
- Create `portfolio_dash/bootstrap.py` — `bootstrap_db(conn)`: ledger tables + `ensure_llm_seeded`. A package-root composition root that sits *above* the layers (so `shared/` stays pure and imports nothing internal).
- Tests: `tests/shared/test_config_store.py`, `tests/shared/test_llm_config.py`, rewrite `tests/shared/test_llm.py`; update `tests/data_ingestion/conftest.py` + `tests/data_ingestion/test_agents.py`.

---

### Task 1: Generic settings framework (`config_store.py`)

**Files:**
- Create: `portfolio_dash/shared/config_store.py`
- Test: `tests/shared/test_config_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/shared/test_config_store.py
"""Tests for the generic create-always / seed-once settings framework."""

import sqlite3
from collections.abc import Iterator

import pytest

from portfolio_dash.shared.config_store import ensure_seeded, restore_defaults


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _create(c: sqlite3.Connection) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS demo (k TEXT PRIMARY KEY, v TEXT)")


def test_seed_runs_once_create_runs_always(conn: sqlite3.Connection) -> None:
    seeds = {"n": 0}

    def seed(c: sqlite3.Connection) -> None:
        seeds["n"] += 1
        c.execute("INSERT INTO demo (k, v) VALUES ('a', 'default')")

    ensure_seeded(conn, "demo", create=_create, seed=seed)
    ensure_seeded(conn, "demo", create=_create, seed=seed)  # second call: create yes, seed no

    assert seeds["n"] == 1
    rows = list(conn.execute("SELECT k, v FROM demo"))
    assert len(rows) == 1 and rows[0]["v"] == "default"


def test_restore_defaults_reapplies_seed(conn: sqlite3.Connection) -> None:
    def seed(c: sqlite3.Connection) -> None:
        c.execute("INSERT INTO demo (k, v) VALUES ('a', 'default') "
                  "ON CONFLICT(k) DO UPDATE SET v='default'")

    ensure_seeded(conn, "demo", create=_create, seed=seed)
    conn.execute("UPDATE demo SET v='changed' WHERE k='a'")
    restore_defaults(conn, "demo", seed=seed)
    assert conn.execute("SELECT v FROM demo WHERE k='a'").fetchone()["v"] == "default"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/shared/test_config_store.py -v`
Expected: FAIL — `ModuleNotFoundError: portfolio_dash.shared.config_store`.

- [ ] **Step 3: Write minimal implementation**

```python
# portfolio_dash/shared/config_store.py
"""Generic DB-backed settings framework: create-always, seed-once, restore-to-default.

Reusable across config categories (``llm`` first; fees / accounts / prompts /
data_sources migrate onto the same primitive later). ``create`` must use
``CREATE TABLE IF NOT EXISTS`` so it is safe to run on every startup; ``seed`` runs
exactly once per category (tracked in ``settings_meta``).
"""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

CreateFn = Callable[[sqlite3.Connection], None]
SeedFn = Callable[[sqlite3.Connection], None]

_META_DDL = (
    "CREATE TABLE IF NOT EXISTS settings_meta "
    "(category TEXT PRIMARY KEY, seeded_at TEXT NOT NULL)"
)


def ensure_seeded(
    conn: sqlite3.Connection, category: str, *, create: CreateFn, seed: SeedFn
) -> None:
    """Ensure *category*'s tables exist (always) and are seeded (once)."""
    conn.execute(_META_DDL)
    create(conn)
    seeded = conn.execute(
        "SELECT 1 FROM settings_meta WHERE category = ?", (category,)
    ).fetchone()
    if seeded is None:
        seed(conn)
        conn.execute(
            "INSERT INTO settings_meta (category, seeded_at) VALUES (?, ?)",
            (category, datetime.now(UTC).isoformat()),
        )
        conn.commit()


def restore_defaults(conn: sqlite3.Connection, category: str, *, seed: SeedFn) -> None:
    """Re-apply *category*'s default state by re-running its idempotent *seed*."""
    seed(conn)
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/shared/test_config_store.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/shared/config_store.py tests/shared/test_config_store.py
git commit -m "feat(shared): generic create-always/seed-once settings framework"
```

---

### Task 2: LLM tables, exceptions, and AI-off seed (`llm_config.py` part 1)

**Files:**
- Create: `portfolio_dash/shared/llm_config.py`
- Test: `tests/shared/test_llm_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/shared/test_llm_config.py
"""Tests for the LLM config store: tables, seed, registry, roles, budget."""

import sqlite3
from collections.abc import Iterator
from decimal import Decimal

import pytest

from portfolio_dash.shared.llm_config import (
    LLMRole,
    create_llm_tables,
    ensure_llm_seeded,
    restore_llm_defaults,
)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _tables(c: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_create_tables_makes_all_four(conn: sqlite3.Connection) -> None:
    create_llm_tables(conn)
    assert {"llm_models", "llm_defaults", "llm_budget_events", "llm_usage"} <= _tables(conn)


def test_seed_is_ai_off_four_null_roles(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    rows = {r["role"]: r["model_id"] for r in conn.execute("SELECT role, model_id FROM llm_defaults")}
    assert set(rows) == {r.value for r in LLMRole}
    assert all(v is None for v in rows.values())  # AI cleanly off
    assert conn.execute("SELECT COUNT(*) c FROM llm_models").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM llm_budget_events").fetchone()["c"] == 0


def test_restore_defaults_clears_roles(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    conn.execute("UPDATE llm_defaults SET model_id = 'x' WHERE role = 'default'")
    restore_llm_defaults(conn)
    assert conn.execute("SELECT model_id FROM llm_defaults WHERE role='default'").fetchone()["model_id"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/shared/test_llm_config.py -v`
Expected: FAIL — `ModuleNotFoundError: portfolio_dash.shared.llm_config`.

- [ ] **Step 3: Write minimal implementation**

```python
# portfolio_dash/shared/llm_config.py
"""DB-backed LLM configuration: model registry, role-defaults, budget ledger.

Owns the four LLM tables and the three degradation exceptions. Depends only on
``shared/config_store`` (and stdlib); imports nothing from upper layers.
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/shared/test_llm_config.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/shared/llm_config.py tests/shared/test_llm_config.py
git commit -m "feat(shared): LLM tables + exceptions + AI-off seed"
```

---

### Task 3: Model registry CRUD (`ModelConfig` + persistence)

**Files:**
- Modify: `portfolio_dash/shared/llm_config.py`
- Test: `tests/shared/test_llm_config.py`

- [ ] **Step 1: Write the failing test (append to the file)**

```python
# tests/shared/test_llm_config.py  (append)
from portfolio_dash.shared.llm_config import (  # noqa: E402
    ModelConfig,
    delete_model,
    get_model,
    list_models,
    upsert_model,
)


def _model(**kw: object) -> ModelConfig:
    base: dict[str, object] = dict(
        id="opus", model_alias="Opus 4.8", provider="anthropic",
        model_name="claude-opus-4-8", vision=True,
        input_price_per_mtok=Decimal("1.50"), output_price_per_mtok=Decimal("15.00"),
    )
    base.update(kw)
    return ModelConfig(**base)  # type: ignore[arg-type]


def test_upsert_get_roundtrip_preserves_decimal(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    upsert_model(conn, _model())
    got = get_model(conn, "opus")
    assert got is not None
    assert got.model_alias == "Opus 4.8" and got.vision is True
    assert got.input_price_per_mtok == Decimal("1.50")
    assert got.output_price_per_mtok == Decimal("15.00")


def test_upsert_updates_existing(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    upsert_model(conn, _model())
    upsert_model(conn, _model(model_alias="Renamed", enabled=False))
    got = get_model(conn, "opus")
    assert got is not None and got.model_alias == "Renamed" and got.enabled is False
    assert len(list_models(conn)) == 1


def test_delete_model_nulls_role_binding(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    upsert_model(conn, _model())
    conn.execute("UPDATE llm_defaults SET model_id='opus' WHERE role='default'")
    delete_model(conn, "opus")
    assert get_model(conn, "opus") is None
    assert conn.execute("SELECT model_id FROM llm_defaults WHERE role='default'").fetchone()["model_id"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/shared/test_llm_config.py -k roundtrip -v`
Expected: FAIL — `ImportError: cannot import name 'ModelConfig'`.

- [ ] **Step 3: Write minimal implementation (append to `llm_config.py`)**

```python
# portfolio_dash/shared/llm_config.py  (append)


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/shared/test_llm_config.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/shared/llm_config.py tests/shared/test_llm_config.py
git commit -m "feat(shared): LLM model registry CRUD (Decimal-as-TEXT)"
```

---

### Task 4: Role-defaults + model selection with fallback

**Files:**
- Modify: `portfolio_dash/shared/llm_config.py`
- Test: `tests/shared/test_llm_config.py`

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/shared/test_llm_config.py  (append)
from portfolio_dash.shared.llm_config import (  # noqa: E402
    AINotActivated,
    get_role_model_id,
    select_models,
    set_role,
)


def test_set_and_get_role(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    upsert_model(conn, _model())
    set_role(conn, LLMRole.DEFAULT, "opus")
    assert get_role_model_id(conn, LLMRole.DEFAULT) == "opus"
    set_role(conn, LLMRole.DEFAULT, None)
    assert get_role_model_id(conn, LLMRole.DEFAULT) is None


def test_select_text_uses_default_then_fallback(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    upsert_model(conn, _model(id="a"))
    upsert_model(conn, _model(id="b"))
    set_role(conn, LLMRole.DEFAULT, "a")
    set_role(conn, LLMRole.DEFAULT_FALLBACK, "b")
    chain = select_models(conn, vision=False)
    assert [m.id for m in chain] == ["a", "b"]


def test_select_skips_disabled_and_missing(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    upsert_model(conn, _model(id="a", enabled=False))
    upsert_model(conn, _model(id="b"))
    set_role(conn, LLMRole.DEFAULT, "a")          # disabled -> skipped
    set_role(conn, LLMRole.DEFAULT_FALLBACK, "b")
    assert [m.id for m in select_models(conn, vision=False)] == ["b"]


def test_select_all_null_raises_not_activated(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    with pytest.raises(AINotActivated):
        select_models(conn, vision=False)


def test_select_vision_uses_vision_roles(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    upsert_model(conn, _model(id="vis"))
    set_role(conn, LLMRole.VISION, "vis")
    assert [m.id for m in select_models(conn, vision=True)] == ["vis"]
    with pytest.raises(AINotActivated):  # text roles still unset
        select_models(conn, vision=False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/shared/test_llm_config.py -k "role or select" -v`
Expected: FAIL — `ImportError: cannot import name 'select_models'`.

- [ ] **Step 3: Write minimal implementation (append to `llm_config.py`)**

```python
# portfolio_dash/shared/llm_config.py  (append)


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


def select_models(conn: sqlite3.Connection, *, vision: bool) -> list[ModelConfig]:
    """Return the ordered [primary, fallback] enabled models for the task kind.

    Raises :exc:`AINotActivated` when neither role resolves to an enabled model.
    The order drives runtime failover in ``shared/llm.py``.
    """
    roles = (
        (LLMRole.VISION, LLMRole.VISION_FALLBACK)
        if vision
        else (LLMRole.DEFAULT, LLMRole.DEFAULT_FALLBACK)
    )
    chain: list[ModelConfig] = []
    for role in roles:
        model_id = get_role_model_id(conn, role)
        if model_id is None:
            continue
        model = get_model(conn, model_id)
        if model is not None and model.enabled:
            chain.append(model)
    if not chain:
        kind = "vision" if vision else "text"
        raise AINotActivated(f"no enabled model configured for {kind} tasks")
    return chain
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/shared/test_llm_config.py -v`
Expected: PASS (11 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/shared/llm_config.py tests/shared/test_llm_config.py
git commit -m "feat(shared): role-defaults + model selection with fallback"
```

---

### Task 5: Budget ledger + gate

**Files:**
- Modify: `portfolio_dash/shared/llm_config.py`
- Test: `tests/shared/test_llm_config.py`

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/shared/test_llm_config.py  (append)
from portfolio_dash.shared.llm_config import (  # noqa: E402
    LLMBudgetExceeded,
    budget_remaining,
    check_budget,
    reset_budget,
)


def _spend(conn: sqlite3.Connection, ts: str, cost: str) -> None:
    conn.execute(
        "INSERT INTO llm_usage (ts, model, agent, input_tokens, output_tokens, cost) "
        "VALUES (?, 'm', 'a', 1, 1, ?)",
        (ts, cost),
    )
    conn.commit()


def test_no_events_means_no_cap(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    assert budget_remaining(conn) is None
    check_budget(conn)  # never blocks when unset


def test_remaining_is_amount_minus_spend_since_reset(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    reset_budget(conn, Decimal("50"), note="2026-06-09T00:00:00+00:00")
    # backdate a row before the reset (excluded) and after (included)
    _spend(conn, "2025-01-01T00:00:00+00:00", "5")   # before -> ignored
    _spend(conn, "2999-01-01T00:00:00+00:00", "10")  # after  -> counted
    rem = budget_remaining(conn)
    assert rem is not None and rem == Decimal("40")


def test_latest_reset_wins(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    reset_budget(conn, Decimal("50"))
    _spend(conn, "2999-01-01T00:00:00+00:00", "60")  # drives first period negative
    assert budget_remaining(conn) is not None and budget_remaining(conn) < 0
    reset_budget(conn, Decimal("100"))               # new start line, future-dated usage only
    rem = budget_remaining(conn)
    assert rem is not None and rem == Decimal("100")


def test_check_budget_blocks_when_negative(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    reset_budget(conn, Decimal("1"))
    _spend(conn, "2999-01-01T00:00:00+00:00", "2")
    with pytest.raises(LLMBudgetExceeded):
        check_budget(conn)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/shared/test_llm_config.py -k budget -v`
Expected: FAIL — `ImportError: cannot import name 'budget_remaining'`.

- [ ] **Step 3: Write minimal implementation (append to `llm_config.py`)**

```python
# portfolio_dash/shared/llm_config.py  (append)


def reset_budget(
    conn: sqlite3.Connection, amount_usd: Decimal, note: str | None = None
) -> None:
    """Append a budget reset/recharge event (a fresh start line). History untouched."""
    conn.execute(
        "INSERT INTO llm_budget_events (ts, amount_usd, note) VALUES (?, ?, ?)",
        (datetime.now(UTC).isoformat(), str(amount_usd), note),
    )
    conn.commit()


def budget_remaining(conn: sqlite3.Connection) -> Decimal | None:
    """Remaining USD = latest reset amount − Σ usage cost dated at/after that reset.

    Returns ``None`` when no reset has ever been set (no cap; calls allowed).
    """
    latest = conn.execute(
        "SELECT ts, amount_usd FROM llm_budget_events ORDER BY ts DESC, id DESC LIMIT 1"
    ).fetchone()
    if latest is None:
        return None
    spent = Decimal("0")
    for row in conn.execute(
        "SELECT cost FROM llm_usage WHERE ts >= ?", (latest["ts"],)
    ):
        spent += Decimal(row["cost"])
    return Decimal(latest["amount_usd"]) - spent


def check_budget(conn: sqlite3.Connection) -> None:
    """Gate: raise :exc:`LLMBudgetExceeded` only when a cap is set and remaining < 0."""
    remaining = budget_remaining(conn)
    if remaining is not None and remaining < 0:
        raise LLMBudgetExceeded(f"token budget exhausted (remaining ${remaining})")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/shared/test_llm_config.py -v`
Expected: PASS (15 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/shared/llm_config.py tests/shared/test_llm_config.py
git commit -m "feat(shared): USD budget ledger + remaining<0 gate"
```

---

### Task 6: `litellm_model_string` provider mapping

**Files:**
- Modify: `portfolio_dash/shared/llm_config.py`
- Test: `tests/shared/test_llm_config.py`

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/shared/test_llm_config.py  (append)
from portfolio_dash.shared.llm_config import litellm_model_string  # noqa: E402


def test_litellm_model_string_by_provider() -> None:
    assert litellm_model_string(_model(provider="anthropic", model_name="claude-opus-4-8")) == "anthropic/claude-opus-4-8"
    assert litellm_model_string(_model(provider="openrouter", model_name="x/y")) == "openrouter/x/y"
    assert litellm_model_string(_model(provider="openai", model_name="gpt-4o")) == "openai/gpt-4o"
    # openai-compatible servers route through the openai adapter + api_base
    assert litellm_model_string(_model(provider="openai-compatible", model_name="gemma-4")) == "openai/gemma-4"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/shared/test_llm_config.py -k litellm_model_string -v`
Expected: FAIL — `ImportError: cannot import name 'litellm_model_string'`.

- [ ] **Step 3: Write minimal implementation (append to `llm_config.py`)**

```python
# portfolio_dash/shared/llm_config.py  (append)

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/shared/test_llm_config.py -v`
Expected: PASS (16 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/shared/llm_config.py tests/shared/test_llm_config.py
git commit -m "feat(shared): litellm provider->model-string mapping"
```

---

### Task 7: Rewrite `complete_structured` — gate, role selection, fallback, registry pricing

**Files:**
- Modify: `portfolio_dash/shared/llm.py`
- Test: rewrite `tests/shared/test_llm.py`

- [ ] **Step 1: Rewrite the test file**

```python
# tests/shared/test_llm.py
"""Tests for shared.llm.complete_structured (budget gate, role selection, fallback)."""

import sqlite3
from collections.abc import Iterator
from decimal import Decimal

import pytest
from pydantic import BaseModel

from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.llm import ModelPricing, complete_structured, cost_of
from portfolio_dash.shared.llm_config import (
    AINotActivated,
    LLMBudgetExceeded,
    LLMRole,
    LLMUnavailable,
    ModelConfig,
    ensure_llm_seeded,
    reset_budget,
    set_role,
    upsert_model,
)


class _Msg:
    def __init__(self, content: str) -> None:
        self.message = type("M", (), {"content": content})()


class _Usage:
    def __init__(self, pt: int, ct: int) -> None:
        self.prompt_tokens = pt
        self.completion_tokens = ct


class _Resp:
    def __init__(self, content: str, pt: int = 10, ct: int = 5) -> None:
        self.choices = [_Msg(content)]
        self.usage = _Usage(pt, ct)


class Out(BaseModel):
    x: int


_PRICING = ModelPricing(
    model="m", input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2")
)


def _model(model_id: str = "a", **kw: object) -> ModelConfig:
    base: dict[str, object] = dict(
        id=model_id, model_alias=model_id, provider="openai", model_name=model_id,
        input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2"),
    )
    base.update(kw)
    return ModelConfig(**base)  # type: ignore[arg-type]


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_llm_seeded(c)
    upsert_model(c, _model("a"))
    set_role(c, LLMRole.DEFAULT, "a")
    yield c
    c.close()


def test_cost_of() -> None:
    assert cost_of(_PRICING, 1_000_000, 1_000_000) == Decimal("3")  # 1*1 + 1*2


def test_parses_and_logs_usage_with_registry_pricing(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp('{"x": 7}'))
    out = complete_structured("hi", Out, agent="test", conn=conn)
    assert out.x == 7
    row = conn.execute("SELECT agent, cost FROM llm_usage").fetchone()
    assert row["agent"] == "test"
    assert Decimal(row["cost"]) == cost_of(_PRICING, 10, 5)  # priced from the registry row


def test_not_activated_when_no_role(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    set_role(conn, LLMRole.DEFAULT, None)
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp('{"x": 1}'))
    with pytest.raises(AINotActivated):
        complete_structured("hi", Out, agent="test", conn=conn)


def test_budget_gate_blocks(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    reset_budget(conn, Decimal("0.000001"))
    conn.execute(
        "INSERT INTO llm_usage (ts, model, agent, input_tokens, output_tokens, cost) "
        "VALUES ('2999-01-01T00:00:00+00:00', 'm', 'a', 1, 1, '1')"
    )
    conn.commit()
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp('{"x": 1}'))
    with pytest.raises(LLMBudgetExceeded):
        complete_structured("hi", Out, agent="test", conn=conn)


def test_fails_over_to_fallback_model(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    upsert_model(conn, _model("b"))
    set_role(conn, LLMRole.DEFAULT_FALLBACK, "b")
    calls: list[str] = []

    def completion(**kw: object) -> _Resp:
        calls.append(str(kw["model"]))
        if kw["model"] == "openai/a":
            raise RuntimeError("primary down")
        return _Resp('{"x": 9}')

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    out = complete_structured("hi", Out, agent="test", conn=conn)
    assert out.x == 9
    assert calls == ["openai/a", "openai/b"]  # tried primary, then fellover


def test_retry_once_then_unavailable(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    calls = {"n": 0}

    def bad(**kw: object) -> _Resp:
        calls["n"] += 1
        return _Resp("not json")

    monkeypatch.setattr(llm_mod.litellm, "completion", bad)
    with pytest.raises(LLMUnavailable):
        complete_structured("hi", Out, agent="test", conn=conn)
    assert calls["n"] == 2  # retried once on the single configured model


def test_provider_error_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    def boom(**kw: object) -> _Resp:
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm_mod.litellm, "completion", boom)
    with pytest.raises(LLMUnavailable):
        complete_structured("hi", Out, agent="test", conn=conn)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/shared/test_llm.py -v`
Expected: FAIL — `complete_structured` still has the old signature / no role selection.

- [ ] **Step 3: Rewrite `llm.py`**

```python
# portfolio_dash/shared/llm.py
"""LiteLLM client: budget gate, role-based selection with fallback, vision, usage log."""

import base64
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import litellm as litellm  # re-exported so tests can monkeypatch llm_mod.litellm
from pydantic import BaseModel, ValidationError

from portfolio_dash.shared.llm_config import (
    AINotActivated,
    LLMBudgetExceeded,
    LLMError,
    LLMUnavailable,
    ModelConfig,
    check_budget,
    litellm_model_string,
    select_models,
)

__all__ = [
    "AINotActivated",
    "LLMBudgetExceeded",
    "LLMError",
    "LLMUnavailable",
    "ModelPricing",
    "complete_structured",
    "cost_of",
    "log_usage",
]


class ModelPricing(BaseModel):
    """Per-model token pricing (USD per million tokens)."""

    model_config = {"protected_namespaces": ()}

    model: str
    input_price_per_mtok: Decimal
    output_price_per_mtok: Decimal


def cost_of(pricing: ModelPricing, input_tokens: int, output_tokens: int) -> Decimal:
    """Return total USD cost for a single completion given token counts."""
    return (
        Decimal(input_tokens) * pricing.input_price_per_mtok
        + Decimal(output_tokens) * pricing.output_price_per_mtok
    ) / Decimal("1000000")


def log_usage(
    conn: sqlite3.Connection,
    *,
    model: str,
    agent: str,
    input_tokens: int,
    output_tokens: int,
    cost: Decimal,
) -> None:
    """Append one row to the ``llm_usage`` table and commit."""
    conn.execute(
        "INSERT INTO llm_usage (ts, model, agent, input_tokens, output_tokens, cost) "
        "VALUES (?,?,?,?,?,?)",
        (datetime.now(UTC).isoformat(), model, agent, input_tokens, output_tokens, str(cost)),
    )
    conn.commit()


def _build_messages(prompt: str, images: list[bytes] | None) -> list[dict[str, object]]:
    """Assemble the chat messages; multimodal content when images are present."""
    if not images:
        return [{"role": "user", "content": prompt}]
    content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
    for img in images:
        b64 = base64.b64encode(img).decode("ascii")
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
        )
    return [{"role": "user", "content": content}]


def _complete_with[T: BaseModel](
    model: ModelConfig,
    messages: list[dict[str, object]],
    schema: type[T],
    *,
    agent: str,
    conn: sqlite3.Connection,
) -> T:
    """Try one model: call, log usage, parse (retry once). Raise LLMUnavailable on failure."""
    for _attempt in range(2):
        try:
            resp = litellm.completion(
                model=litellm_model_string(model),
                api_base=model.api_base or None,
                api_key=model.api_key or None,
                messages=messages,
                timeout=model.timeout_seconds,
                num_retries=model.max_retries or 0,
                max_tokens=model.max_output_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMUnavailable(f"provider error ({model.id}): {exc}") from exc

        content = resp.choices[0].message.content or ""
        usage = resp.usage
        log_usage(
            conn,
            model=model.model_name,
            agent=agent,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            cost=cost_of(
                ModelPricing(
                    model=model.model_name,
                    input_price_per_mtok=model.input_price_per_mtok,
                    output_price_per_mtok=model.output_price_per_mtok,
                ),
                usage.prompt_tokens,
                usage.completion_tokens,
            ),
        )
        try:
            return schema.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError, ValueError):
            continue
    raise LLMUnavailable(f"invalid structured output from {model.id}")


def complete_structured[T: BaseModel](
    prompt: str,
    schema: type[T],
    *,
    agent: str,
    conn: sqlite3.Connection,
    images: list[bytes] | None = None,
) -> T:
    """Call the configured LLM and parse the response into *schema*.

    Order: budget gate → role selection (vision if *images*) → try each candidate
    model in order (failover on provider error) → parse (retry once) → log cost.

    Raises :exc:`AINotActivated` (no model for the role), :exc:`LLMBudgetExceeded`
    (cap hit), or :exc:`LLMUnavailable` (all candidates failed). All subclass
    :exc:`LLMError`, so callers may catch the base for graceful degradation.
    """
    check_budget(conn)
    candidates = select_models(conn, vision=bool(images))
    messages = _build_messages(prompt, images)
    last: LLMUnavailable | None = None
    for model in candidates:
        try:
            return _complete_with(model, messages, schema, agent=agent, conn=conn)
        except LLMUnavailable as exc:
            last = exc
    raise last or LLMUnavailable("no model produced valid output")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/shared/test_llm.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/shared/llm.py tests/shared/test_llm.py
git commit -m "feat(shared): complete_structured gate+role+fallback, registry pricing"
```

---

### Task 8: Vision input path

**Files:**
- Test: `tests/shared/test_llm.py` (append)

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/shared/test_llm.py  (append)
from portfolio_dash.shared.llm import _build_messages  # noqa: E402


def test_build_messages_text_only() -> None:
    msgs = _build_messages("hello", None)
    assert msgs == [{"role": "user", "content": "hello"}]


def test_build_messages_with_image_blocks() -> None:
    msgs = _build_messages("describe", [b"PNGDATA"])
    content = msgs[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "describe"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_vision_call_routes_to_vision_role(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    upsert_model(conn, _model("v"))
    set_role(conn, LLMRole.VISION, "v")
    seen: list[str] = []

    def completion(**kw: object) -> _Resp:
        seen.append(str(kw["model"]))
        return _Resp('{"x": 3}')

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    out = complete_structured("describe", Out, agent="vis", conn=conn, images=[b"img"])
    assert out.x == 3
    assert seen == ["openai/v"]  # used the vision role, not the text default 'a'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/shared/test_llm.py -k "build_messages or vision" -v`
Expected: PASS for `_build_messages` cases if Task 7 is in; FAIL only if the vision-routing assertion regresses. (This task adds coverage; no new production code is required beyond Task 7. If all three pass immediately, the task is satisfied — record that in the commit.)

- [ ] **Step 3: Implementation**

No new production code — the vision path was implemented in Task 7 (`_build_messages` + `select_models(vision=True)`). This task locks it behind explicit tests.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/shared/test_llm.py -v`
Expected: PASS (11 passed).

- [ ] **Step 5: Commit**

```bash
git add tests/shared/test_llm.py
git commit -m "test(shared): lock vision message assembly + vision-role routing"
```

---

### Task 9: Bootstrap + move `llm_usage` out of `data_ingestion/schema.py`

**Files:**
- Create: `portfolio_dash/bootstrap.py`
- Modify: `portfolio_dash/data_ingestion/schema.py`
- Modify: `tests/data_ingestion/conftest.py`
- Test: `tests/test_bootstrap.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bootstrap.py
import sqlite3
from collections.abc import Iterator

import pytest

from portfolio_dash.bootstrap import bootstrap_db


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_bootstrap_creates_ledgers_and_llm_and_seeds_ai_off(conn: sqlite3.Connection) -> None:
    bootstrap_db(conn)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"transactions", "accounts", "llm_models", "llm_defaults", "llm_usage"} <= names
    # AI off after bootstrap
    roles = list(conn.execute("SELECT model_id FROM llm_defaults"))
    assert roles and all(r["model_id"] is None for r in roles)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_bootstrap.py -v`
Expected: FAIL — `ModuleNotFoundError: portfolio_dash.bootstrap`.

- [ ] **Step 3: Implementation**

Create the bootstrap composition root at the package root (above the layers):

```python
# portfolio_dash/bootstrap.py
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
```

Remove `llm_usage` from the data_ingestion DDL (now owned by `shared/llm_config.py`):

```python
# portfolio_dash/data_ingestion/schema.py — delete this block from _DDL:
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, model TEXT NOT NULL, agent TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cost TEXT NOT NULL
);
```

Update the data_ingestion test conftest so AI Agents Input has the LLM tables:

```python
# tests/data_ingestion/conftest.py
import sqlite3
from collections.abc import Iterator

import pytest

from portfolio_dash.bootstrap import bootstrap_db


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    yield c
    c.close()
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_bootstrap.py tests/data_ingestion -v`
Expected: PASS (bootstrap test + all data_ingestion tests; the AI Agents Input usage log now finds `llm_usage`).

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/bootstrap.py portfolio_dash/data_ingestion/schema.py tests/test_bootstrap.py tests/data_ingestion/conftest.py
git commit -m "refactor: bootstrap composition root; move llm_usage to shared/llm_config"
```

---

### Task 10: Rewire callers + drop dead config

**Files:**
- Modify: `portfolio_dash/data_ingestion/agents.py`
- Modify: `portfolio_dash/data_ingestion/config_seed.py`
- Modify: `portfolio_dash/shared/config.py`
- Test: `tests/data_ingestion/test_agents.py`

- [ ] **Step 1: Write/extend the failing test**

```python
# tests/data_ingestion/test_agents.py — replace the degradation test(s) with these
import sqlite3

import pytest

from portfolio_dash.data_ingestion.agents import AiDraftList, ai_agents_input
from portfolio_dash.shared.llm import AINotActivated, LLMBudgetExceeded, LLMUnavailable


def _completer_ok(*a: object, **k: object) -> AiDraftList:
    return AiDraftList(drafts=[])


def test_ai_input_no_pricing_arg(conn: sqlite3.Connection) -> None:
    # signature no longer accepts `pricing`; a clean call returns an empty preview
    preview = ai_agents_input(conn, "nothing to parse", completer=_completer_ok)
    assert preview.rows == []


@pytest.mark.parametrize(
    "exc, kind",
    [
        (LLMUnavailable("down"), "llm_unavailable"),
        (AINotActivated("off"), "ai_not_activated"),
        (LLMBudgetExceeded("broke"), "budget_exceeded"),
    ],
)
def test_ai_input_degrades_with_kind(
    conn: sqlite3.Connection, exc: Exception, kind: str
) -> None:
    def boom(*a: object, **k: object) -> AiDraftList:
        raise exc

    preview = ai_agents_input(conn, "buy something", completer=boom)
    assert len(preview.rows) == 1
    assert preview.rows[0].issues[0].kind == kind
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/data_ingestion/test_agents.py -v`
Expected: FAIL — `ai_agents_input` still imports/forwards `pricing`; only `LLMUnavailable` is caught.

- [ ] **Step 3: Implementation**

Rewire `agents.py` — drop `pricing`/`ModelPricing`, catch `LLMError`, map `kind`:

```python
# portfolio_dash/data_ingestion/agents.py — change the imports + signature + try/except

# imports: replace the shared.llm import line with:
from portfolio_dash.shared.llm import LLMError, complete_structured

# Completer stays: Completer = Callable[..., AiDraftList]

def ai_agents_input(
    conn: sqlite3.Connection,
    text: str,
    *,
    completer: Completer = complete_structured,
) -> ImportPreview:
    """Extract transactions from natural-language *text* and return a preview.

    (Docstring unchanged except: pricing is gone — cost is logged from the
    selected model's registry pricing; on any LLMError the row degrades with the
    error's ``kind``.)
    """
    try:
        result = completer(
            _PROMPT.format(text=text),
            AiDraftList,
            agent="ai_agents_input",
            conn=conn,
        )
    except LLMError as exc:
        return ImportPreview(
            rows=[
                PreviewRow(
                    index=0,
                    raw={"text": text},
                    issues=[Issue(kind=exc.kind, message=str(exc))],
                )
            ]
        )

    rows: list[PreviewRow] = []
    for idx, d in enumerate(result.drafts):
        inp = TxnInput(
            account_id=d.account_id, symbol=d.symbol, side=d.side,
            quantity=d.shares, price=d.price, trade_date=d.date,
            daytrade=d.daytrade, is_etf=d.is_etf, note=d.note,
        )
        rows.append(txn_preview_row(conn, idx, {"text": text}, inp))
    return ImportPreview(rows=rows)
```

Remove the superseded LLM bits from `config_seed.py`:

```python
# portfolio_dash/data_ingestion/config_seed.py — delete:
#   - the `ModelPricing` class
#   - `DEFAULT_LLM_MODELS: list[ModelPricing] = []`
# (and the now-unused Decimal import only if nothing else uses it — keep otherwise)
```

Remove the unused single-endpoint settings from `config.py`:

```python
# portfolio_dash/shared/config.py — delete from Settings:
#     # LLM / LiteLLM settings — all optional; empty string = use litellm defaults.
#     llm_endpoint: str = ""
#     llm_api_key: str = ""
#     llm_active_model: str = ""
# and update the class docstring line to: "DB-backed LLM config lives in llm_config."
```

- [ ] **Step 4: Run the full suite + gates**

Run: `python -m pytest -q`
Expected: PASS (all green; no reference to removed `pricing`/`ModelPricing`/`llm_*` settings).

Run: `python -m mypy`
Expected: `Success: no issues found` (uses the strict config + file set from `pyproject.toml`).

Run: `python -m ruff check .`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add portfolio_dash/data_ingestion/agents.py portfolio_dash/data_ingestion/config_seed.py portfolio_dash/shared/config.py tests/data_ingestion/test_agents.py
git commit -m "refactor: rewire AI Agents Input to registry API; drop dead LLM config"
```

---

## Final review (after all tasks)

- [ ] Dispatch a final holistic code review over the whole branch diff: boundary adherence (`shared/` imports nothing internal; only the package-root `portfolio_dash/bootstrap.py` imports both layers), money discipline (Decimal-as-TEXT, no float), the three degradation signals all reachable and caught, no `llm_usage` double-definition, `grep -rn "pricing=" portfolio_dash/data_ingestion` returns nothing.
- [ ] Run `python -m pytest -q`, `python -m mypy`, `python -m ruff check .` once more — all green.
- [ ] `CHANGELOG.md` `[Unreleased]` entry added for this sub-project; move the relevant "AI cost-info page" / model-registry lines from Planned to Added where now true; `grep -c "^## \[v" CHANGELOG.md` still `1`.
- [ ] `LESSONS_LEARNED.md` updated if anything was learned the hard way (e.g. the `litellm` import-time `.env` load interaction from the prior sub-project).
- [ ] Then use **superpowers:finishing-a-development-branch**.
