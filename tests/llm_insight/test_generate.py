"""Unit tests for generation orchestration (spec 04.0/4.9) — run_insight_type.

run_insight_type is the PURE controller: it assembles layers → complete_structured →
stores cards. Conn-bearing inputs (dashboard data, prices, snapshots, fx) are FED IN as
per-symbol VarContexts + a RunInputs gate bundle — this module imports neither pricing nor
data_ingestion (architecture.md). The LLM seam is monkeypatched (no network).
"""

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from portfolio_dash.llm_insight import composer_store as cs
from portfolio_dash.llm_insight import generate
from portfolio_dash.llm_insight import insights_store as istore
from portfolio_dash.llm_insight import variables as V
from portfolio_dash.llm_insight.generate import RunInputs
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import (
    LLMBudgetExceeded,
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
    '"symbol":null,"confidence":70,"prediction":{"metric":"price_change",'
    '"direction":"up","target_pct":"0.03","horizon_days":5}}'
)


class _Usage:
    def __init__(self) -> None:
        self.prompt_tokens = 100
        self.completion_tokens = 20


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [type("M", (), {"message": type("X", (), {"content": content})()})()]
        self.usage = _Usage()


@pytest.fixture
def conn(golden_db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    cs.ensure_seeded(golden_db)
    istore.ensure_tables(golden_db)
    ensure_llm_seeded(golden_db)
    upsert_model(golden_db, ModelConfig(
        id="m", model_alias="m", provider="openai", model_name="m",
        input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2"),
    ))
    set_role(golden_db, LLMRole.DEFAULT, "m")
    add_topup(golden_db, Decimal("100"))
    yield golden_db


def _ctx(conn: sqlite3.Connection, symbol: str | None = None) -> V.VarContext:
    data = build_dashboard(conn, now=NOW, reporting=Currency.TWD)
    return V.VarContext(data=data, now=NOW, symbol=symbol)


def _portfolio_combo(conn: sqlite3.Connection) -> int:
    sp = cs.create_strategy(conn, name="S", body="觀察 {{kpis_json}}", now=NOW)
    it = cs.create_insight_type(conn, name="Daily", scope="portfolio", now=NOW)
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    return it.id


def _patch_llm(monkeypatch: pytest.MonkeyPatch, content: str = _CARD_JSON) -> None:
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp(content))


# --- happy path: portfolio combo, one card ------------------------------------


def test_portfolio_run_generates_one_card(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_llm(monkeypatch)
    it_id = _portfolio_combo(conn)
    result = generate.run_insight_type(
        conn, it_id, var_contexts={None: _ctx(conn)},
        inputs=RunInputs(budget_remaining=Decimal("100")), now=NOW,
    )
    assert result.status == "ok"
    cards = istore.list_cards(conn, insight_type_id=it_id)
    assert len(cards) == 1
    assert cards[0].card.title == "洞察"
    assert cards[0].is_shadow is False
    # job_runs row written for the insight run
    row = conn.execute(
        "SELECT status, cost_usd FROM job_runs WHERE job_id = ?", (f"insight:{it_id}",)
    ).fetchone()
    assert row["status"] == "ok"
    assert Decimal(row["cost_usd"]) > 0


def test_run_records_llm_usage(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_llm(monkeypatch)
    it_id = _portfolio_combo(conn)
    generate.run_insight_type(
        conn, it_id, var_contexts={None: _ctx(conn)},
        inputs=RunInputs(budget_remaining=Decimal("100")), now=NOW,
    )
    n = conn.execute("SELECT COUNT(*) AS n FROM llm_usage").fetchone()["n"]
    assert n == 1


# --- fingerprint cache --------------------------------------------------------


def test_second_identical_run_hits_cache_no_llm(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def completion(**kw: object) -> _Resp:
        calls["n"] += 1
        return _Resp(_CARD_JSON)

    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)
    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    it_id = _portfolio_combo(conn)
    args = dict(var_contexts={None: _ctx(conn)}, inputs=RunInputs(budget_remaining=Decimal("100")))
    generate.run_insight_type(conn, it_id, now=NOW, **args)  # type: ignore[arg-type]
    generate.run_insight_type(conn, it_id, now=NOW, **args)  # type: ignore[arg-type]
    assert calls["n"] == 1  # second run reused the cached card (same-day identical inputs)
    # only the first run inserted a card (cache hit does not duplicate)
    assert len(istore.list_cards(conn, insight_type_id=it_id)) == 1


# --- per_symbol scope: one card per symbol (R8) -------------------------------


def test_per_symbol_run_one_card_each(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_llm(monkeypatch)
    sp = cs.create_strategy(conn, name="S", body="{{symbol_detail_json}}", now=NOW)
    it = cs.create_insight_type(
        conn, name="Watch", scope="per_symbol",
        universe={"mode": "custom", "symbols": ["2330", "AAPL"]}, now=NOW,
    )
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    result = generate.run_insight_type(
        conn, it.id,
        var_contexts={"2330": _ctx(conn, "2330"), "AAPL": _ctx(conn, "AAPL")},
        inputs=RunInputs(budget_remaining=Decimal("100"), universe_symbols=["2330", "AAPL"]),
        now=NOW,
    )
    assert result.status == "ok"
    assert len(istore.list_cards(conn, insight_type_id=it.id)) == 2


# --- R4 missing price -> deterministic zero-LLM card --------------------------


def test_missing_price_emits_deterministic_card_no_llm(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def completion(**kw: object) -> _Resp:
        calls["n"] += 1
        return _Resp(_CARD_JSON)

    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)
    monkeypatch.setattr(llm_mod.litellm, "completion", completion)
    sp = cs.create_strategy(conn, name="S", body="{{symbol_detail_json}}", now=NOW)
    it = cs.create_insight_type(
        conn, name="Watch", scope="per_symbol",
        universe={"mode": "custom", "symbols": ["2330", "MISSING"]}, now=NOW,
    )
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    generate.run_insight_type(
        conn, it.id,
        var_contexts={"2330": _ctx(conn, "2330"), "MISSING": _ctx(conn, "MISSING")},
        inputs=RunInputs(
            budget_remaining=Decimal("100"), universe_symbols=["2330", "MISSING"],
            missing_price_symbols=["MISSING"],
        ),
        now=NOW,
    )
    assert calls["n"] == 1  # only the priced symbol called the LLM
    cards = {c.symbol: c for c in istore.list_cards(conn, insight_type_id=it.id)}
    assert "MISSING" in cards
    assert "資料異常" in cards["MISSING"].card.title  # deterministic anomaly card
    assert cards["MISSING"].cost_usd == "0"  # zero cost


# --- R3 / R6 hard blocks -> skipped, no card ----------------------------------


def test_no_live_strategies_skips(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_llm(monkeypatch)
    it = cs.create_insight_type(conn, name="Empty", scope="portfolio", now=NOW)
    result = generate.run_insight_type(
        conn, it.id, var_contexts={None: _ctx(conn)},
        inputs=RunInputs(budget_remaining=Decimal("100")), now=NOW,
    )
    assert result.status == "skipped"
    assert "R3_no_live_templates" in result.reason
    assert istore.list_cards(conn, insight_type_id=it.id) == []


def test_budget_exhausted_skips(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_llm(monkeypatch)
    it_id = _portfolio_combo(conn)
    result = generate.run_insight_type(
        conn, it_id, var_contexts={None: _ctx(conn)},
        inputs=RunInputs(budget_remaining=Decimal("0")), now=NOW,
    )
    assert result.status == "skipped"
    assert "R6_quota" in result.reason


# --- R6 mid-iteration exhaustion -> partial, produced cards kept --------------


def test_mid_iteration_budget_exhaustion_is_partial(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The budget covers one card; the second symbol's gate sees remaining <= 0 -> stop.
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: _Resp(_CARD_JSON))
    sp = cs.create_strategy(conn, name="S", body="{{symbol_detail_json}}", now=NOW)
    it = cs.create_insight_type(
        conn, name="Watch", scope="per_symbol",
        universe={"mode": "custom", "symbols": ["2330", "AAPL"]}, now=NOW,
    )
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    # Drain the budget to a tiny amount so the first card consumes it all.
    result = generate.run_insight_type(
        conn, it.id,
        var_contexts={"2330": _ctx(conn, "2330"), "AAPL": _ctx(conn, "AAPL")},
        inputs=RunInputs(
            budget_remaining=Decimal("0.0001"), universe_symbols=["2330", "AAPL"]
        ),
        now=NOW,
    )
    cards = istore.list_cards(conn, insight_type_id=it.id)
    # at least one produced; the run is partial (budget ran out mid-iteration)
    assert result.status == "partial"
    assert result.reason == "budget_exhausted_mid_run"  # the true budget path keeps its enum
    assert len(cards) == 1


def test_on_alert_card_forced_short_horizon(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An on_alert card's stored horizon is capped at 3 trading days (spec 4.10), even when
    # the task default is larger.
    _patch_llm(monkeypatch)
    sp = cs.create_strategy(conn, name="S", body="{{symbol_detail_json}}", now=NOW)
    it = cs.create_insight_type(
        conn, name="Alert", scope="on_alert", alert_rules=["fx_drift"], enabled=True,
        horizon_days=10, now=NOW,
    )
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    generate.run_insight_type(
        conn, it.id, var_contexts={"schwab": _ctx(conn, "schwab")},
        inputs=RunInputs(
            budget_remaining=Decimal("100"), fired_rule="fx_drift", fired_symbol="schwab",
        ),
        now=NOW,
    )
    cards = istore.list_cards(conn, insight_type_id=it.id)
    assert len(cards) == 1
    assert cards[0].horizon_days <= 3  # forced short horizon for alert cards


def test_blocked_run_writes_skipped_job_run(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_llm(monkeypatch)
    it = cs.create_insight_type(conn, name="Empty", scope="portfolio", now=NOW)
    generate.run_insight_type(
        conn, it.id, var_contexts={None: _ctx(conn)},
        inputs=RunInputs(budget_remaining=Decimal("100")), now=NOW,
    )
    row = conn.execute(
        "SELECT status, reason FROM job_runs WHERE job_id = ?", (f"insight:{it.id}",)
    ).fetchone()
    assert row["status"] == "skipped"
    assert row["reason"] is not None


# --- fix #1: insights.model stores the model alias, never the symbol ----------


def test_stored_model_is_alias_not_symbol(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Register a model whose alias is distinct from any ticker; a per_symbol card must
    # store that alias in insights.model, NOT the symbol (regression: was card.symbol).
    upsert_model(conn, ModelConfig(
        id="sonnet", model_alias="claude-sonnet", provider="anthropic",
        model_name="claude-sonnet-x", input_price_per_mtok=Decimal("1"),
        output_price_per_mtok=Decimal("2"),
    ))
    set_role(conn, LLMRole.DEFAULT, "sonnet")
    _patch_llm(monkeypatch)
    sp = cs.create_strategy(conn, name="S", body="{{symbol_detail_json}}", now=NOW)
    it = cs.create_insight_type(
        conn, name="Watch", scope="per_symbol",
        universe={"mode": "custom", "symbols": ["2330"]}, now=NOW,
    )
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    generate.run_insight_type(
        conn, it.id, var_contexts={"2330": _ctx(conn, "2330")},
        inputs=RunInputs(budget_remaining=Decimal("100"), universe_symbols=["2330"]),
        now=NOW,
    )
    cards = istore.list_cards(conn, insight_type_id=it.id)
    assert len(cards) == 1
    assert cards[0].symbol == "2330"
    assert cards[0].model == "claude-sonnet"  # the alias actually used
    assert cards[0].model != "2330"  # NEVER the ticker symbol


# --- fix #4: a multi-block skip carries a SINGLE reason enum + full text in detail ----


def test_multi_block_skip_reason_is_single_enum(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A per_symbol combo with NO live strategies AND an empty universe fires two hard
    # blocks (R3 then R2). The job_runs.reason must be ONE enum (the first/highest-severity
    # block, R3_no_live_templates), with the full joined human text in detail (spec 07 §7.4).
    _patch_llm(monkeypatch)
    it = cs.create_insight_type(
        conn, name="Watch", scope="per_symbol",
        universe={"mode": "custom", "symbols": []}, now=NOW,
    )
    result = generate.run_insight_type(
        conn, it.id, var_contexts={},
        inputs=RunInputs(budget_remaining=Decimal("100"), universe_symbols=[]), now=NOW,
    )
    assert result.status == "skipped"
    row = conn.execute(
        "SELECT reason, detail FROM job_runs WHERE job_id = ?", (f"insight:{it.id}",)
    ).fetchone()
    # reason is exactly ONE enum value (no "; " join), the first blocking gate.
    assert row["reason"] == "R3_no_live_templates"
    assert ";" not in row["reason"]
    # the human detail keeps the full multi-block text (both R3 and R2 messages).
    assert "R3_no_live_templates" in row["detail"]
    assert "R2_universe_empty" in row["detail"]
    # RunResult.reason mirrors the single enum.
    assert result.reason == "R3_no_live_templates"


def test_anomaly_card_model_stays_none(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The R4 zero-LLM anomaly card keeps model="(none)" (no model was used).
    _patch_llm(monkeypatch)
    sp = cs.create_strategy(conn, name="S", body="{{symbol_detail_json}}", now=NOW)
    it = cs.create_insight_type(
        conn, name="Watch", scope="per_symbol",
        universe={"mode": "custom", "symbols": ["MISSING"]}, now=NOW,
    )
    cs.set_strategies(conn, it.id, [(sp.id, 0)])
    generate.run_insight_type(
        conn, it.id, var_contexts={"MISSING": _ctx(conn, "MISSING")},
        inputs=RunInputs(
            budget_remaining=Decimal("100"), universe_symbols=["MISSING"],
            missing_price_symbols=["MISSING"],
        ),
        now=NOW,
    )
    cards = istore.list_cards(conn, insight_type_id=it.id)
    assert len(cards) == 1
    assert cards[0].model == "(none)"


# --- honest mid-run failure classification (2026-07-05 live-ignition fix) ------
# Before the fix, EVERY LLMError mid-run was reported as budget_exhausted_mid_run —
# a provider/parse failure sent the operator to the top-up page instead of the
# provider/prompt. The reason now carries the exception's kind.


def test_llm_unavailable_mid_run_reason_not_budget(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)

    def boom(**kw: object) -> object:
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm_mod.litellm, "completion", boom)
    it_id = _portfolio_combo(conn)
    result = generate.run_insight_type(
        conn, it_id, var_contexts={None: _ctx(conn)},
        inputs=RunInputs(budget_remaining=Decimal("100")), now=NOW,
    )
    assert result.status == "partial"
    assert result.reason == "llm_unavailable_mid_run"
    row = conn.execute(
        "SELECT reason, detail FROM job_runs WHERE job_id = ?", (f"insight:{it_id}",)
    ).fetchone()
    assert row["reason"] == "llm_unavailable_mid_run"
    assert "provider down" in row["detail"]  # diagnosable from the runs list


def test_budget_exception_mid_run_keeps_budget_reason(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_budget(*a: object, **kw: object) -> object:
        raise LLMBudgetExceeded("token budget exhausted")

    monkeypatch.setattr(generate.llm, "complete_structured_meta", raise_budget)
    it_id = _portfolio_combo(conn)
    result = generate.run_insight_type(
        conn, it_id, var_contexts={None: _ctx(conn)},
        inputs=RunInputs(budget_remaining=Decimal("100")), now=NOW,
    )
    assert result.status == "partial"
    assert result.reason == "budget_exhausted_mid_run"


def test_job_run_timestamps_share_timezone(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # started_at comes from get_now (+08:00); finished_at must share that offset —
    # a UTC finish next to a +08:00 start displayed as an 8-hour-negative run.
    _patch_llm(monkeypatch)
    it_id = _portfolio_combo(conn)
    generate.run_insight_type(
        conn, it_id, var_contexts={None: _ctx(conn)},
        inputs=RunInputs(budget_remaining=Decimal("100")), now=NOW,
    )
    row = conn.execute(
        "SELECT started_at, finished_at FROM job_runs WHERE job_id = ?",
        (f"insight:{it_id}",),
    ).fetchone()
    assert row["started_at"].endswith("+08:00")
    assert row["finished_at"].endswith("+08:00")
