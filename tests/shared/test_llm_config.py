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
    budget_remaining,
    check_budget,
    create_llm_tables,
    delete_model,
    ensure_llm_seeded,
    get_model,
    get_role_model_id,
    list_models,
    reset_budget,
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
    assert budget_remaining(conn) is not None and budget_remaining(conn) < 0  # type: ignore[operator]
    reset_budget(conn, Decimal("100"))               # new start line, future usage only
    rem = budget_remaining(conn)
    assert rem is not None and rem == Decimal("100")


def test_check_budget_blocks_when_negative(conn: sqlite3.Connection) -> None:
    ensure_llm_seeded(conn)
    reset_budget(conn, Decimal("1"))
    _spend(conn, "2999-01-01T00:00:00+00:00", "2")
    with pytest.raises(LLMBudgetExceeded):
        check_budget(conn)
