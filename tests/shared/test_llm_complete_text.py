"""Tests for shared.llm.complete_text — free-text completion (no JSON schema).

Mirrors complete_structured's gate/select/log path but returns a raw reply + usage.
Records an llm_usage row tagged with the caller's agent (here prompt_test); honours the
budget gate (LLMBudgetExceeded), role activation (AINotActivated), and provider failure
(LLMUnavailable). The litellm seam is monkeypatched so NO real network is hit.
"""

import sqlite3
from collections.abc import Iterator
from decimal import Decimal

import pytest

from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.llm import complete_text, cost_of
from portfolio_dash.shared.llm_config import (
    AINotActivated,
    LLMBudgetExceeded,
    LLMRole,
    LLMUnavailable,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
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
    def __init__(self, content: str, pt: int = 12, ct: int = 4) -> None:
        self.choices = [_Msg(content)]
        self.usage = _Usage(pt, ct)


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
    add_topup(c, Decimal("100"))
    yield c
    c.close()


def test_returns_reply_and_logs_usage(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp("free text reply"))
    out = complete_text("hi", agent="prompt_test", conn=conn)
    assert out.reply == "free text reply"
    assert out.model == "a"
    assert out.tokens_in == 12 and out.tokens_out == 4
    assert out.cost == cost_of(
        llm_mod.ModelPricing(
            model="a", input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2")
        ),
        12,
        4,
    )
    row = conn.execute("SELECT agent, cost FROM llm_usage").fetchone()
    assert row["agent"] == "prompt_test"


def test_passes_system_message(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    seen: dict[str, object] = {}

    def completion(**kw: object) -> _Resp:
        seen.update(kw)
        return _Resp("ok")

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    complete_text("user body", agent="prompt_test", conn=conn, system="SYS")
    messages = seen["messages"]
    assert isinstance(messages, list)
    assert messages[0] == {"role": "system", "content": "SYS"}
    assert messages[-1] == {"role": "user", "content": "user body"}


def test_no_system_message_when_none(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    seen: dict[str, object] = {}

    def completion(**kw: object) -> _Resp:
        seen.update(kw)
        return _Resp("ok")

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    complete_text("just user", agent="prompt_test", conn=conn)
    messages = seen["messages"]
    assert isinstance(messages, list)
    assert messages == [{"role": "user", "content": "just user"}]


def test_budget_gate_blocks(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    conn.execute(
        "INSERT INTO llm_usage (ts, model, agent, input_tokens, output_tokens, cost) "
        "VALUES ('2999-01-01T00:00:00+00:00', 'm', 'a', 1, 1, '100')"
    )
    conn.commit()
    monkeypatch.setattr(
        llm_mod.litellm, "completion", lambda **kw: pytest.fail("called past budget")
    )
    with pytest.raises(LLMBudgetExceeded):
        complete_text("hi", agent="prompt_test", conn=conn)


def test_not_activated_when_no_role(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    set_role(conn, LLMRole.DEFAULT, None)
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp("x"))
    with pytest.raises(AINotActivated):
        complete_text("hi", agent="prompt_test", conn=conn)


def test_provider_error_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    def boom(**kw: object) -> _Resp:
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm_mod.litellm, "completion", boom)
    with pytest.raises(LLMUnavailable):
        complete_text("hi", agent="prompt_test", conn=conn)
