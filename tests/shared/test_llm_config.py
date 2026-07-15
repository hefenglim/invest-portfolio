"""Tests for the LLM config store: tables, seed, registry, roles, budget."""

import sqlite3
from collections.abc import Iterator
from decimal import Decimal

import pytest

from portfolio_dash.shared.llm_config import (
    AINotActivated,
    LLMBudgetExceeded,
    LLMRole,
    ModelConfig,
    add_topup,
    ai_active,
    budget_remaining,
    check_budget,
    create_llm_tables,
    delete_model,
    ensure_llm_seeded,
    get_model,
    get_role_model_id,
    list_models,
    litellm_model_string,
    quota_remaining,
    restore_llm_defaults,
    select_models,
    set_role,
    upsert_model,
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
    rows = {
        r["role"]: r["model_id"]
        for r in conn.execute("SELECT role, model_id FROM llm_defaults")
    }
    assert set(rows) == {r.value for r in LLMRole}
    assert all(v is None for v in rows.values())  # AI cleanly off
    assert conn.execute("SELECT COUNT(*) c FROM llm_models").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM llm_budget_events").fetchone()["c"] == 0


def test_restore_defaults_clears_roles(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    conn.execute("UPDATE llm_defaults SET model_id = 'x' WHERE role = 'default'")
    restore_llm_defaults(conn)
    row = conn.execute("SELECT model_id FROM llm_defaults WHERE role='default'").fetchone()
    assert row["model_id"] is None


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
    row = conn.execute(
        "SELECT model_id FROM llm_defaults WHERE role='default'"
    ).fetchone()
    assert row["model_id"] is None


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


# --- ai_active predicate (P3 batch 3 · 3B) -----------------------------------


def test_ai_active_false_on_fresh_ai_off(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)  # four roles seeded to NULL, no models
    assert ai_active(conn) is False


def test_ai_active_true_when_role_bound_to_enabled_model(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    upsert_model(conn, _model())  # enabled by default
    set_role(conn, LLMRole.DEFAULT, "opus")
    assert ai_active(conn) is True


def test_ai_active_false_when_only_bound_model_is_disabled(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    upsert_model(conn, _model(enabled=False))
    set_role(conn, LLMRole.DEFAULT, "opus")
    assert ai_active(conn) is False


def test_ai_active_true_for_any_role_binding(conn: sqlite3.Connection) -> None:
    # A non-default role (vision) alone still means AI is usable.
    ensure_llm_seeded(conn)
    upsert_model(conn, _model(id="v"))
    set_role(conn, LLMRole.VISION, "v")
    assert ai_active(conn) is True


def test_ai_active_false_when_tables_missing() -> None:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    try:
        assert ai_active(c) is False  # no llm_defaults table -> defensive False (AI off)
    finally:
        c.close()


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


def _spend(conn: sqlite3.Connection, ts: str, cost: str) -> None:
    conn.execute(
        "INSERT INTO llm_usage (ts, model, agent, input_tokens, output_tokens, cost) "
        "VALUES (?, 'm', 'a', 1, 1, ?)",
        (ts, cost),
    )
    conn.commit()


def test_fresh_db_zero_remaining_blocks(conn: sqlite3.Connection) -> None:
    # Unified model: no top-up -> Σtopups − Σusage = 0; 0 <= 0 -> blocked.
    ensure_llm_seeded(conn)
    assert budget_remaining(conn) == Decimal("0")
    with pytest.raises(LLMBudgetExceeded):
        check_budget(conn)  # $0 topped up must block, even with models configured


def test_topup_is_cumulative_and_subtracts_usage(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    add_topup(conn, Decimal("10"))
    assert budget_remaining(conn) == Decimal("10")
    _spend(conn, "2026-06-13T00:00:00+00:00", "4")
    assert budget_remaining(conn) == Decimal("6")
    # A second top-up ADDS (proves cumulative, not a reset): 10 + 5 - 4 = 11.
    add_topup(conn, Decimal("5"))
    assert budget_remaining(conn) == Decimal("11")
    # Drive total usage to $15 (4 + 11) against $15 topped up -> remaining 0 -> blocked.
    _spend(conn, "2026-06-13T01:00:00+00:00", "11")
    assert budget_remaining(conn) == Decimal("0")
    with pytest.raises(LLMBudgetExceeded):
        check_budget(conn)


def test_check_budget_allows_when_positive(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    add_topup(conn, Decimal("10"))
    _spend(conn, "2026-06-13T00:00:00+00:00", "3")
    check_budget(conn)  # remaining 7 > 0 -> no block


def test_check_budget_blocks_when_exhausted(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    add_topup(conn, Decimal("1"))
    _spend(conn, "2026-06-13T00:00:00+00:00", "2")  # remaining -1 <= 0
    with pytest.raises(LLMBudgetExceeded):
        check_budget(conn)


def test_quota_remaining_equals_budget_remaining(conn: sqlite3.Connection) -> None:
    # Single source of truth: the two functions agree at every step.
    ensure_llm_seeded(conn)
    assert quota_remaining(conn) == budget_remaining(conn)  # both 0 on fresh DB
    add_topup(conn, Decimal("10"))
    assert quota_remaining(conn) == budget_remaining(conn) == Decimal("10")
    _spend(conn, "2026-06-13T00:00:00+00:00", "4")
    assert quota_remaining(conn) == budget_remaining(conn) == Decimal("6")
    add_topup(conn, Decimal("5"))
    assert quota_remaining(conn) == budget_remaining(conn) == Decimal("11")


def test_litellm_model_string_by_provider() -> None:
    def s(provider: str, model_name: str) -> str:
        return litellm_model_string(_model(provider=provider, model_name=model_name))

    assert s("anthropic", "claude-opus-4-8") == "anthropic/claude-opus-4-8"
    assert s("openrouter", "x/y") == "openrouter/x/y"
    assert s("openai", "gpt-4o") == "openai/gpt-4o"
    # openai-compatible servers route through the openai adapter + api_base
    assert s("openai-compatible", "gemma-4") == "openai/gemma-4"
