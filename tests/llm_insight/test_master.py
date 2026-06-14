"""Master-role narrative scoring + calibration generation + validator (spec 04.3/4.5/4.8).

The master LLM seam is monkeypatched (no network). The master role just selects a
different model row; the seam is the same ``llm.litellm.completion``.
"""

import sqlite3
from collections.abc import Iterator
from decimal import Decimal

import pytest

from portfolio_dash.llm_insight import master
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.llm_config import (
    AINotActivated,
    LLMRole,
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
    def __init__(self, content: str, pt: int = 10, ct: int = 5) -> None:
        self.choices = [_Msg(content)]
        self.usage = _Usage(pt, ct)


def _model(model_id: str) -> ModelConfig:
    return ModelConfig(
        id=model_id, model_alias=model_id, provider="openai", model_name=model_id,
        input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2"),
    )


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_llm_seeded(c)
    upsert_model(c, _model("master"))
    set_role(c, LLMRole.MASTER, "master")
    add_topup(c, Decimal("100"))
    yield c
    c.close()


@pytest.fixture
def conn_no_master() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_llm_seeded(c)
    add_topup(c, Decimal("100"))
    yield c
    c.close()


# --- score_narrative ----------------------------------------------------------


def test_score_narrative_uses_master_role(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    seen: list[str] = []

    def completion(**kw: object) -> _Resp:
        seen.append(str(kw["model"]))
        return _Resp('{"narrative_score": 75, "miss": false, "note": "方向正確但幅度高估"}')

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    out = master.score_narrative(
        card_text="AAPL 看多，預期上漲", snapshot_then="snap-create",
        actual_now="實際 +1.5%", eval_prompt=None, conn=conn,
    )
    assert out["narrative_score"] == 75
    assert out["miss"] is False
    assert "幅度" in out["note"]
    assert seen == ["openai/master"]  # master role chain
    # usage logged with the master agent label.
    row = conn.execute("SELECT agent FROM llm_usage").fetchone()
    assert row["agent"] == "master_score"


def test_score_narrative_custom_eval_prompt(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    prompts: list[str] = []

    def completion(**kw: object) -> _Resp:
        msgs = kw["messages"]
        assert isinstance(msgs, list)
        prompts.append("".join(str(m.get("content")) for m in msgs))
        return _Resp('{"narrative_score": 50, "miss": true, "note": "n"}')

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    master.score_narrative(
        card_text="card", snapshot_then="s", actual_now="a",
        eval_prompt="自訂檢驗：請特別關注產業輪動", conn=conn,
    )
    assert any("產業輪動" in p for p in prompts)  # custom eval prompt is injected


def test_score_narrative_master_unset_raises(
    monkeypatch: pytest.MonkeyPatch, conn_no_master: sqlite3.Connection
) -> None:
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp('{"x":1}'))
    with pytest.raises(AINotActivated):
        master.score_narrative(
            card_text="c", snapshot_then="s", actual_now="a", eval_prompt=None,
            conn=conn_no_master,
        )


# --- generate_calibration -----------------------------------------------------


def test_generate_calibration_returns_body_and_cause(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    calls: list[str] = []

    def completion(**kw: object) -> _Resp:
        msgs = kw["messages"]
        assert isinstance(msgs, list)
        calls.append("".join(str(m.get("content")) for m in msgs))
        # generate then the validator review call: both return JSON. We branch on content.
        joined = calls[-1]
        if "審查" in joined or "validate" in joined.lower():
            return _Resp('{"ok": true, "reasons": []}')
        return _Resp('{"body": "新版校正：1) 保留有效條款 2) 修訂幅度高估", "cause": "連續高估"}')

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    out = master.generate_calibration(
        active_body="舊版校正規則", miss_samples=[{"insight_id": 1, "notes": "高估"}],
        bins=[{"bucket": "80-100", "calibration_error_pp": "30"}], conn=conn,
    )
    assert "校正" in out["body"]
    assert out["cause"] == "連續高估"


def test_generate_calibration_system_prompt_carries_safety_lock(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    sent: list[str] = []

    def completion(**kw: object) -> _Resp:
        msgs = kw["messages"]
        assert isinstance(msgs, list)
        sent.append("".join(str(m.get("content")) for m in msgs))
        return _Resp('{"body": "b", "cause": "c"}')

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    master.generate_calibration(active_body="x", miss_samples=[], bins=[], conn=conn)
    # The §4.8 safety lock must appear in the master prompt (folded into the call).
    joined = " ".join(sent)
    assert "重構" in joined or "精簡" in joined  # reconstruct + trim old logic
    assert "字數" in joined or "上限" in joined  # word cap
    assert "廢話" in joined or "無預測" in joined  # no vague/predictionless filler
    assert "附加" in joined or "新增" in joined  # append-only spirit


def test_generate_calibration_master_unset_raises(
    monkeypatch: pytest.MonkeyPatch, conn_no_master: sqlite3.Connection
) -> None:
    monkeypatch.setattr(
        llm_mod.litellm, "completion", lambda **kw: _Resp('{"body":"b","cause":"c"}')
    )
    with pytest.raises(AINotActivated):
        master.generate_calibration(
            active_body="x", miss_samples=[], bins=[], conn=conn_no_master
        )


# --- validate_calibration -----------------------------------------------------


def test_validate_calibration_keyword_denylist_rejects(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    # A denylisted phrase (越權建議 / 幣別混算) is rejected WITHOUT an LLM call.
    called = {"n": 0}

    def completion(**kw: object) -> _Resp:
        called["n"] += 1
        return _Resp('{"ok": true, "reasons": []}')

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    ok, reasons = master.validate_calibration("規則：可直接幣別混算後比較", conn=conn)
    assert ok is False
    assert any("幣別混算" in r for r in reasons)
    assert called["n"] == 0  # short-circuited before the LLM review


def test_validate_calibration_llm_review_pass(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    monkeypatch.setattr(
        llm_mod.litellm, "completion",
        lambda **kw: _Resp('{"ok": true, "reasons": []}'),
    )
    ok, reasons = master.validate_calibration("健全的校正規則，聚焦敘事準確度", conn=conn)
    assert ok is True
    assert reasons == []


def test_validate_calibration_llm_review_reject(
    monkeypatch: pytest.MonkeyPatch, conn: sqlite3.Connection
) -> None:
    monkeypatch.setattr(
        llm_mod.litellm, "completion",
        lambda **kw: _Resp('{"ok": false, "reasons": ["越權：要求調整持倉部位"]}'),
    )
    ok, reasons = master.validate_calibration("一條看似中性的規則", conn=conn)
    assert ok is False
    assert any("越權" in r for r in reasons)


def test_validate_calibration_master_unset_raises(
    monkeypatch: pytest.MonkeyPatch, conn_no_master: sqlite3.Connection
) -> None:
    monkeypatch.setattr(
        llm_mod.litellm, "completion", lambda **kw: _Resp('{"ok":true,"reasons":[]}')
    )
    with pytest.raises(AINotActivated):
        master.validate_calibration("健全規則", conn=conn_no_master)
