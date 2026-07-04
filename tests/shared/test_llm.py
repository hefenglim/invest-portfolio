"""Tests for shared.llm.complete_structured (budget gate, role selection, fallback)."""

import sqlite3
from collections.abc import Iterator
from decimal import Decimal

import pytest
from pydantic import BaseModel

from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.llm import (
    ModelPricing,
    complete_structured,
    complete_text,
    cost_of,
)
from portfolio_dash.shared.llm_config import (
    AINotActivated,
    LLMBudgetExceeded,
    LLMRole,
    LLMUnavailable,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    select_role_models,
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
    # Unified budget model: no top-up = remaining 0 = blocked. The happy-path tests
    # exercise the LLM flow, not the budget gate, so fund the budget generously here.
    add_topup(c, Decimal("100"))
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
    # Fixture topped up $100; log usage past it so cumulative remaining <= 0 -> blocked.
    conn.execute(
        "INSERT INTO llm_usage (ts, model, agent, input_tokens, output_tokens, cost) "
        "VALUES ('2999-01-01T00:00:00+00:00', 'm', 'a', 1, 1, '100')"
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


def test_response_format_passed_when_supported(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    # complete_structured derives a json_schema response_format from the Pydantic schema
    # and passes it to litellm.completion when the provider supports it (spec 04.10).
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: True)
    seen: dict[str, object] = {}

    def completion(**kw: object) -> _Resp:
        seen.update(kw)
        return _Resp('{"x": 5}')

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    out = complete_structured("hi", Out, agent="t", conn=conn)
    assert out.x == 5
    rf = seen.get("response_format")
    assert isinstance(rf, dict)
    assert rf["type"] == "json_schema"
    # the schema name + properties come from the Pydantic model.
    schema = rf["json_schema"]
    assert isinstance(schema, dict)
    assert "x" in schema["schema"]["properties"]


def test_response_format_omitted_when_unsupported(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    # A provider that does not support a response schema → never receives response_format
    # (graceful fallback to plain prompt+parse), and the call still parses + logs usage.
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)
    seen: dict[str, object] = {}

    def completion(**kw: object) -> _Resp:
        seen.update(kw)
        return _Resp('{"x": 8}')

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    out = complete_structured("hi", Out, agent="rf_off", conn=conn)
    assert out.x == 8
    assert "response_format" not in seen
    row = conn.execute("SELECT agent FROM llm_usage WHERE agent = 'rf_off'").fetchone()
    assert row is not None  # usage logged on the no-rf path


def test_response_format_probe_failure_degrades_to_no_rf(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    # If the capability probe itself raises (litellm can't classify the model), we treat
    # it as unsupported and never send response_format — graceful, never crash.
    def boom_probe(**kw: object) -> bool:
        raise RuntimeError("unknown model for probe")

    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", boom_probe)
    seen: dict[str, object] = {}

    def completion(**kw: object) -> _Resp:
        seen.update(kw)
        return _Resp('{"x": 4}')

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    out = complete_structured("hi", Out, agent="t", conn=conn)
    assert out.x == 4
    assert "response_format" not in seen


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


# --- master role selection (spec 04.3) ----------------------------------------


def test_select_role_models_orders_primary_then_fallback(conn: sqlite3.Connection) -> None:
    upsert_model(conn, _model("master_p"))
    upsert_model(conn, _model("master_f"))
    set_role(conn, LLMRole.MASTER, "master_p")
    set_role(conn, LLMRole.MASTER_FALLBACK, "master_f")
    chain = select_role_models(conn, LLMRole.MASTER, LLMRole.MASTER_FALLBACK)
    assert [m.id for m in chain] == ["master_p", "master_f"]


def test_select_role_models_skips_disabled_and_unbound(conn: sqlite3.Connection) -> None:
    upsert_model(conn, _model("master_p", enabled=False))  # disabled → skipped
    upsert_model(conn, _model("master_f"))
    set_role(conn, LLMRole.MASTER, "master_p")
    set_role(conn, LLMRole.MASTER_FALLBACK, "master_f")
    chain = select_role_models(conn, LLMRole.MASTER, LLMRole.MASTER_FALLBACK)
    assert [m.id for m in chain] == ["master_f"]  # only the enabled fallback


def test_select_role_models_unset_raises_not_activated(conn: sqlite3.Connection) -> None:
    # No master roles bound → AINotActivated (the master pipeline pauses).
    with pytest.raises(AINotActivated):
        select_role_models(conn, LLMRole.MASTER, LLMRole.MASTER_FALLBACK)


def test_complete_structured_master_role_selects_master_chain(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    upsert_model(conn, _model("m_master"))
    set_role(conn, LLMRole.MASTER, "m_master")
    seen: list[str] = []

    def completion(**kw: object) -> _Resp:
        seen.append(str(kw["model"]))
        return _Resp('{"x": 11}')

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    out = complete_structured("score this", Out, agent="master_score", conn=conn,
                              role=LLMRole.MASTER)
    assert out.x == 11
    assert seen == ["openai/m_master"]  # master role chain, not the default 'a'
    row = conn.execute(
        "SELECT agent FROM llm_usage WHERE agent = 'master_score'"
    ).fetchone()
    assert row is not None  # usage logged with the master agent label


def test_complete_structured_master_unset_raises_not_activated(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp('{"x": 1}'))
    with pytest.raises(AINotActivated):
        complete_structured("x", Out, agent="m", conn=conn, role=LLMRole.MASTER)


def test_complete_text_master_role_selects_master_chain(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    upsert_model(conn, _model("m_master2"))
    set_role(conn, LLMRole.MASTER, "m_master2")
    seen: list[str] = []

    def completion(**kw: object) -> _Resp:
        seen.append(str(kw["model"]))
        return _Resp("a free-text review")

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    out = complete_text("review", agent="master_review", conn=conn, role=LLMRole.MASTER)
    assert out.reply == "a free-text review"
    assert seen == ["openai/m_master2"]


# --- structured-output contract for live providers (2026-07-05 fix) ------------
# LiteLLM's capability map returns False for e.g. every ``openrouter/*`` id, so
# response_format never reaches those providers — the prompt itself must always
# carry the JSON-only contract, and parsing must tolerate fenced/prose-wrapped
# replies (both observed live on the test instance).


def test_structured_prompt_always_carries_json_contract(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)
    seen: dict[str, object] = {}

    def completion(**kw: object) -> _Resp:
        seen.update(kw)
        return _Resp('{"x": 1}')

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    complete_structured("hi", Out, agent="t", conn=conn)
    messages = seen["messages"]
    assert isinstance(messages, list)
    content = str(messages[0]["content"])
    assert content.startswith("hi")
    assert "<output_format>" in content
    assert '"properties"' in content and '"x"' in content  # the Out JSON schema inlined


def test_parses_fenced_json_reply(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    monkeypatch.setattr(
        llm_mod.litellm, "completion", lambda **kw: _Resp('```json\n{"x": 6}\n```')
    )
    out = complete_structured("hi", Out, agent="t", conn=conn)
    assert out.x == 6


def test_parses_prose_wrapped_json_reply(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    monkeypatch.setattr(
        llm_mod.litellm, "completion",
        lambda **kw: _Resp('好的，以下是結果：\n{"x": 12}\n以上。'),
    )
    out = complete_structured("hi", Out, agent="t", conn=conn)
    assert out.x == 12


def test_extract_json_passthrough_and_slicing() -> None:
    from portfolio_dash.shared.llm import _extract_json

    assert _extract_json('{"x": 1}') == '{"x": 1}'
    assert _extract_json("no json here") == "no json here"  # unchanged, caller re-fails
    assert _extract_json('```json\n{"x": 2}\n```') == '{"x": 2}'
    assert _extract_json('前言 {"x": 3} 後記') == '{"x": 3}'


def test_complete_structured_default_role_unchanged(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    # Back-compat: omitting role uses the DEFAULT chain (the fixture's model 'a').
    seen: list[str] = []

    def completion(**kw: object) -> _Resp:
        seen.append(str(kw["model"]))
        return _Resp('{"x": 2}')

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    out = complete_structured("hi", Out, agent="t", conn=conn)
    assert out.x == 2
    assert seen == ["openai/a"]
