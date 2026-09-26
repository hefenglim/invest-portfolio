"""DEF-066 / DEF-073: an insight run's ``job_runs.detail`` is a Chinese sentence, every time.

3be67db wrote ``detail = f"{stop_reason}: {str(exc)[:300]}"`` for a run that stopped — the
litellm English (``llm_unavailable_mid_run: provider error (haiku-4.5): tenacity import
failed …``) reached the 排程中心 verbatim — and, when a run had no detail of its own, fell
back to the machine enum: ``ok`` for every clean run, ``R3_no_live_templates; R2_universe_
empty`` for a skip, ``budget_exhausted_mid_run`` for the R6 stop. ``web/pipeline.js`` prints
``r.detail || ppSkipReason(r.reason)``, so a non-empty enum detail BYPASSED the zh label map.

``job_runs.reason`` keeps the enum (spec 07 §7.4 — the machine field); only ``detail``, the
human field, changes.
"""

import re
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from litellm import exceptions as litellm_errors

from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import generate
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.llm_insight.generate import RunInputs
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)

NOW = datetime(2026, 6, 11, 14, 30, tzinfo=ZoneInfo("Asia/Taipei"))
_CARD_JSON = (
    '{"title":"洞察","summary":"量縮","body_md":"**2330** 量縮整理。","tags":["TW"],'
    '"symbol":null,"confidence":70,"prediction":null}'
)
#: An English word of 2+ letters. Model aliases are the only identifiers a detail may name.
_WORD = re.compile(r"[A-Za-z]{2,}")


class _Usage:
    prompt_tokens = 100
    completion_tokens = 20


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [type("M", (), {"message": type("X", (), {"content": content})()})()]
        self.usage = _Usage()


def _model(alias: str) -> ModelConfig:
    return ModelConfig(
        id=alias, model_alias=alias, provider="openai", model_name=alias,
        input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2"), max_retries=2,
    )


@pytest.fixture
def conn(golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
         ) -> Iterator[sqlite3.Connection]:
    cs.ensure_seeded(golden_db)
    istore.ensure_tables(golden_db)
    ensure_llm_seeded(golden_db)
    upsert_model(golden_db, _model("flash-lite"))
    upsert_model(golden_db, _model("haiku"))
    set_role(golden_db, LLMRole.DEFAULT, "flash-lite")
    set_role(golden_db, LLMRole.DEFAULT_FALLBACK, "haiku")
    add_topup(golden_db, Decimal("100"))
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)
    monkeypatch.setattr(llm_mod, "_sleep", lambda _s: None, raising=False)
    yield golden_db


def _ctx(conn: sqlite3.Connection, symbol: str | None = None) -> V.VarContext:
    return V.VarContext(data=build_dashboard(conn, now=NOW, reporting=Currency.TWD),
                        now=NOW, symbol=symbol)


def _portfolio_task(conn: sqlite3.Connection) -> int:
    sp = cs.create_strategy(conn, name="S", body="觀察 {{kpis_json}}", now=NOW)
    it = cs.create_insight_type(conn, name="Daily", scope="portfolio", now=NOW)
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    return it.id


def _run(conn: sqlite3.Connection, it_id: int, **inputs: Any) -> generate.RunResult:
    return generate.run_insight_type(
        conn, it_id, var_contexts={None: _ctx(conn)},
        inputs=RunInputs(budget_remaining=Decimal("100"), **inputs), now=NOW,
    )


def _row(conn: sqlite3.Connection, it_id: int) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT status, reason, detail FROM job_runs WHERE job_id = ? ORDER BY id DESC",
        (f"insight:{it_id}",),
    ).fetchone()
    assert row is not None
    return row


def _english_words(detail: str, *allowed: str) -> list[str]:
    return [w for w in _WORD.findall(detail) if w not in {"AI", "JSON", "HTTP", *allowed}]


def test_both_models_fail_the_detail_names_each_in_chinese(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DEF-066 evidence: truncated JSON twice from the primary, then the backup errors."""

    def completion(**kw: Any) -> _Resp:
        if kw["model"] == "openai/flash-lite":
            return _Resp('{"title": "洞')
        raise litellm_errors.InternalServerError(
            message="overloaded", llm_provider="anthropic", model="haiku")

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    it_id = _portfolio_task(conn)
    result = _run(conn, it_id)
    row = _row(conn, it_id)
    assert (result.status, row["status"], row["reason"]) == (
        "partial", "partial", "llm_unavailable_mid_run")
    assert row["detail"] == (
        "AI 呼叫失敗，本次停止（尚未產出卡片）：主模型 flash-lite：回應不是完整 JSON（2 次）；"
        "備援 haiku：供應商服務異常（HTTP 500，已重試 2 次）"
    )
    assert "tenacity" not in row["detail"]
    assert not _english_words(row["detail"], "flash", "lite", "haiku"), row["detail"]


def test_a_clean_run_says_what_it_produced(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp(_CARD_JSON))
    it_id = _portfolio_task(conn)
    _run(conn, it_id)
    first = _row(conn, it_id)
    assert (first["status"], first["detail"]) == ("ok", "產生 1 張卡")
    _run(conn, it_id)  # same day, same inputs → cache hit, no LLM call
    second = _row(conn, it_id)
    assert (second["status"], second["detail"]) == ("ok", "產生 0 張卡（1 張沿用當日快取）")


def test_a_skipped_run_writes_the_gate_messages_not_the_enums(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp(_CARD_JSON))
    it = cs.create_insight_type(
        conn, name="Watch", scope="per_symbol", universe={"mode": "custom", "symbols": []},
        now=NOW,
    )
    generate.run_insight_type(
        conn, it.id, var_contexts={},
        inputs=RunInputs(budget_remaining=Decimal("100"), universe_symbols=[]), now=NOW,
    )
    row = _row(conn, it.id)
    assert row["reason"] == "R3_no_live_templates"  # the machine field keeps its enum
    assert row["detail"] == "未執行：組合的策略段全空（全部停用/封存）；標的宇宙為空（清單已出清）"


def test_an_unknown_task_and_a_budget_stop_are_sentences(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp(_CARD_JSON))
    generate.run_insight_type(conn, 9999, var_contexts={},
                              inputs=RunInputs(budget_remaining=Decimal("1")), now=NOW)
    assert _row(conn, 9999)["detail"] == "找不到洞察任務 #9999，未執行"

    sp = cs.create_strategy(conn, name="S2", body="{{symbol_detail_json}}", now=NOW)
    it = cs.create_insight_type(
        conn, name="W", scope="per_symbol",
        universe={"mode": "custom", "symbols": ["2330", "AAPL"]}, now=NOW,
    )
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    generate.run_insight_type(
        conn, it.id, var_contexts={"2330": _ctx(conn, "2330"), "AAPL": _ctx(conn, "AAPL")},
        inputs=RunInputs(budget_remaining=Decimal("0.0001"),
                         universe_symbols=["2330", "AAPL"]),
        now=NOW,
    )
    row = _row(conn, it.id)
    assert (row["status"], row["reason"]) == ("partial", "budget_exhausted_mid_run")
    assert row["detail"] == "AI 額度用盡，本次停止（已產出 1 張卡保留）"
