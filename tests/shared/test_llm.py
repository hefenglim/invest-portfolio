"""Tests for portfolio_dash.shared.llm — LLM client + structured output + usage log."""

import sqlite3
from collections.abc import Iterator
from decimal import Decimal

import pytest
from pydantic import BaseModel

from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.llm import (
    LLMUnavailable,
    ModelPricing,
    complete_structured,
    cost_of,
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
    model="m",
    input_price_per_mtok=Decimal("1"),
    output_price_per_mtok=Decimal("2"),
)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE llm_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, "
        "model TEXT, agent TEXT, input_tokens INTEGER, output_tokens INTEGER, cost TEXT)"
    )
    yield c
    c.close()


def test_cost_of() -> None:
    assert cost_of(_PRICING, 1_000_000, 1_000_000) == Decimal("3")  # 1*1 + 1*2


def test_parses_and_logs_usage(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp('{"x": 7}'))
    out = complete_structured("hi", Out, agent="test", conn=conn, pricing=_PRICING)
    assert out.x == 7
    rows = list(
        conn.execute(
            "SELECT model, agent, input_tokens, output_tokens, cost FROM llm_usage"
        )
    )
    assert len(rows) == 1 and rows[0]["agent"] == "test"


def test_retry_once_then_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def bad(**kw: object) -> _Resp:
        calls["n"] += 1
        return _Resp("not json")

    monkeypatch.setattr(llm_mod.litellm, "completion", bad)
    with pytest.raises(LLMUnavailable):
        complete_structured("hi", Out, agent="test")
    assert calls["n"] == 2  # retried once


def test_provider_error_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kw: object) -> _Resp:
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm_mod.litellm, "completion", boom)
    with pytest.raises(LLMUnavailable):
        complete_structured("hi", Out, agent="test")
