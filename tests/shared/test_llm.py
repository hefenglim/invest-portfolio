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
