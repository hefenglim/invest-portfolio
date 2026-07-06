"""Contract tests for the prompt-foundation API (spec 06a).

Covers GET /api/prompt-vars, GET/PUT /api/system-prompt, POST /api/prompts/preview
(always 200, diagnostic, REAL computed values, never calls the LLM), and
POST /api/prompts/test (real LiteLLM seam monkeypatched; budget gate -> 402; 422 on
unknown/scope-violating tokens). Uses the shared golden_db + api_client fixtures
(guest auth mode), plus the llm-config seeding helpers for the /prompts/test cases.
"""

import sqlite3
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api.deps import get_conn
from portfolio_dash.shared import llm
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    set_role,
    upsert_model,
)


def _conn_of(client: TestClient) -> sqlite3.Connection:
    return client.app.dependency_overrides[get_conn]()  # type: ignore[attr-defined,no-any-return]


def _seed_llm(conn: sqlite3.Connection, *, topup: str = "10.00") -> None:
    """Seed one enabled default model + a budget top-up so /prompts/test is allowed."""
    upsert_model(
        conn,
        ModelConfig(
            id="claude-sonnet",
            model_alias="claude-sonnet",
            provider="anthropic",
            model_name="claude-sonnet-4-5",
            api_key="sk-abcdef1234567890a2f",
            input_price_per_mtok=Decimal("3.00"),
            output_price_per_mtok=Decimal("15.00"),
            max_output_tokens=8192,
            enabled=True,
        ),
    )
    set_role(conn, LLMRole.DEFAULT, "claude-sonnet")
    add_topup(conn, Decimal(topup), note="seed top-up")


# --- 6.1 GET /api/prompt-vars -------------------------------------------------


def test_prompt_vars_shape(api_client: TestClient) -> None:
    rows = api_client.get("/api/prompt-vars").json()
    # + 1 batch-④ news var (symbol_news_json) = 32.
    assert len(rows) == 32
    date_tokens = {r["token"] for r in rows} & {"now", "card_created_at", "eval_date"}
    assert date_tokens == {"now", "card_created_at", "eval_date"}
    # the batch-③ signal vars surface in the variable area for custom prompts.
    tokens = {r["token"] for r in rows}
    assert {"technical_signals_json", "fear_greed_json", "symbol_news_json"} <= tokens
    h = next(r for r in rows if r["token"] == "holdings_json")
    assert h["scope"] == "portfolio" and h["available"] is True
    assert set(h) == {"token", "name", "category", "scope", "desc", "available", "sample",
                      "required_tier", "tier_ok", "tier_label"}  # tier fields (spec 20.15.3)
    # chips went live (spec 20.2); the spec-04 'ai' vars stay unavailable.
    inst = next(r for r in rows if r["token"] == "institutional_json")
    assert inst["available"] is True
    ai = next(r for r in rows if r["token"] == "backtest_json")
    assert ai["available"] is False
    # 9 categories present (+ the batch-④ 'news' category)
    assert len({r["category"] for r in rows}) == 9


# --- 6.2 GET/PUT /api/system-prompt -------------------------------------------


def test_system_prompt_get_put(api_client: TestClient) -> None:
    got = api_client.get("/api/system-prompt").json()
    assert got["body"]  # default seeded
    assert "updated_at" in got
    r = api_client.put("/api/system-prompt", json={"body": "新守則"})
    assert r.status_code == 200 and r.json()["body"] == "新守則"
    assert api_client.get("/api/system-prompt").json()["body"] == "新守則"


# --- 6.2 POST /api/prompts/preview (always 200, real values, no LLM) ----------


def test_preview_always_200_with_diagnostics(api_client: TestClient) -> None:
    r = api_client.post(
        "/api/prompts/preview",
        json={
            "body": "{{holdings_json}} {{bogus_json}} {{symbol_detail_json}}",
            "scope": "portfolio",
            "symbol": None,
        },
    )
    assert r.status_code == 200
    b = r.json()
    assert "bogus_json" in b["unknown_tokens"]
    assert "symbol_detail_json" in b["scope_violations"]
    assert "holdings_json" in b["tokens_used"]
    assert b["system_prompt"] and b["est_tokens"] > 0
    assert "2330" in b["rendered"]  # REAL computed values, not mock samples


def test_preview_per_symbol(api_client: TestClient) -> None:
    r = api_client.post(
        "/api/prompts/preview",
        json={"body": "{{symbol_detail_json}}", "scope": "per_symbol", "symbol": "2330"},
    )
    assert r.status_code == 200
    b = r.json()
    assert b["scope_violations"] == []
    assert "2330" in b["rendered"]


def test_fx_rates_json_carries_a_rate(api_client: TestClient) -> None:
    # Senior-review I-1/C-2: fx_rates_json must emit the actual spot rate, not just as_of/stale.
    r = api_client.post(
        "/api/prompts/preview",
        json={"body": "{{fx_rates_json}}", "scope": "portfolio", "symbol": None},
    )
    assert r.status_code == 200
    rendered = r.json()["rendered"]
    assert "USD_TWD" in rendered and '"rate"' in rendered  # golden DB holds an AAPL (USD) position


def test_dividends_json_is_per_event_list(api_client: TestClient) -> None:
    # Senior-review I-2: dividends_json is the per-event ledger (symbol/type/ccy), not a summary.
    r = api_client.post(
        "/api/prompts/preview",
        json={"body": "{{dividends_json}}", "scope": "portfolio", "symbol": None},
    )
    rendered = r.json()["rendered"]
    assert '"symbol"' in rendered and '"ccy"' in rendered and "2330" in rendered


def test_all_available_tokens_render_valid_json(api_client: TestClient) -> None:
    # Senior-review C-1: every available token renders to valid JSON (per its scope).
    import json as _json

    rows = api_client.get("/api/prompt-vars").json()
    for v in rows:
        if not v["available"]:
            continue
        scope = v["scope"]
        symbol = "2330" if scope == "per_symbol" else None
        r = api_client.post(
            "/api/prompts/preview",
            json={"body": "{{" + v["token"] + "}}", "scope": scope, "symbol": symbol},
        )
        assert r.status_code == 200, v["token"]
        rendered = r.json()["rendered"]
        assert "⚠unknown" not in rendered, v["token"]
        _json.loads(rendered)  # the single token rendered to a valid JSON document


def test_preview_prepends_system_prompt(api_client: TestClient) -> None:
    api_client.put("/api/system-prompt", json={"body": "SYS-MARKER"})
    r = api_client.post(
        "/api/prompts/preview",
        json={"body": "{{as_of}}", "scope": "portfolio", "symbol": None},
    )
    assert r.json()["system_prompt"] == "SYS-MARKER"


# --- 6.2 POST /api/prompts/test (real LLM seam, budget, 422) ------------------


def test_prompts_test_success_records_usage_and_quota(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _conn_of(api_client)
    _seed_llm(conn)

    class _Msg:
        content = "這是一段測試洞察。"

    class _Choice:
        message = _Msg()

    class _Usage:
        prompt_tokens = 1842
        completion_tokens = 96

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    monkeypatch.setattr(llm.litellm, "completion", lambda **_kw: _Resp())

    r = api_client.post(
        "/api/prompts/test",
        json={"body": "{{holdings_json}}", "scope": "portfolio", "symbol": None},
    )
    assert r.status_code == 200
    b = r.json()
    assert b["reply"] == "這是一段測試洞察。"
    assert b["via"] == "litellm"
    assert b["tokens_in"] == 1842 and b["tokens_out"] == 96
    assert isinstance(b["cost_usd"], str) and isinstance(b["quota_remaining"], str)
    # a llm_usage row was recorded with agent=prompt_test
    row = conn.execute(
        "SELECT agent FROM llm_usage WHERE agent = 'prompt_test'"
    ).fetchone()
    assert row is not None


def test_prompts_test_unknown_token_422(api_client: TestClient) -> None:
    conn = _conn_of(api_client)
    _seed_llm(conn)
    r = api_client.post(
        "/api/prompts/test",
        json={"body": "{{bogus_json}}", "scope": "portfolio", "symbol": None},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_prompts_test_scope_violation_422(api_client: TestClient) -> None:
    conn = _conn_of(api_client)
    _seed_llm(conn)
    r = api_client.post(
        "/api/prompts/test",
        json={"body": "{{symbol_detail_json}}", "scope": "portfolio", "symbol": None},
    )
    assert r.status_code == 422


def test_prompts_test_budget_exhausted_402(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _conn_of(api_client)
    # model + role but NO top-up -> remaining 0 -> budget gate 402
    _seed_llm(conn, topup="0")
    # completion must never be reached, but guard it anyway
    monkeypatch.setattr(
        llm.litellm, "completion", lambda **_kw: pytest.fail("LLM called despite no budget")
    )
    r = api_client.post(
        "/api/prompts/test",
        json={"body": "{{holdings_json}}", "scope": "portfolio", "symbol": None},
    )
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "budget_exceeded"


# --- official template library (2026-07-05 program) ----------------------------


def test_prompt_templates_library_shape(api_client: TestClient) -> None:
    b = api_client.get("/api/prompt-templates").json()
    assert b["library_version"].startswith("official-")
    assert "時效第一" in b["system_prompt"]["body"]
    assert b["system_prompt"]["version"]
    names = [t["name"] for t in b["strategies"]]
    assert "持倉週報策略" in names and "個股健檢策略" in names
    assert all(t["version"] and t["body"] and t["scope"] for t in b["strategies"])


def test_fresh_default_system_prompt_is_official(api_client: TestClient) -> None:
    # First-touch experience = the official optimum: a fresh DB seeds the library
    # version, not the retired v1 text.
    body = api_client.get("/api/system-prompt").json()["body"]
    assert "時效第一" in body
    assert "不提供買賣建議，只描述風險與現象" not in body  # the v1 rule is superseded


def test_system_prompt_reset_restores_official(api_client: TestClient) -> None:
    api_client.put("/api/system-prompt", json={"body": "使用者自訂版"})
    assert api_client.get("/api/system-prompt").json()["body"] == "使用者自訂版"
    b = api_client.post("/api/system-prompt/reset").json()
    assert "時效第一" in b["body"]
    assert api_client.get("/api/system-prompt").json()["body"] == b["body"]


def test_preview_per_market_slices_values(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    # SR gap-fill: PromptBody gained scope=per_market + market; the preview must
    # render the market slice (TW body contains no US holding) with clean tokens.
    r = api_client.post(
        "/api/prompts/preview",
        json={"body": "{{holdings_json}}", "scope": "per_market", "market": "TW"},
    )
    assert r.status_code == 200
    b = r.json()
    assert b["unknown_tokens"] == [] and b["scope_violations"] == []
    assert "2330" in b["rendered"] and "AAPL" not in b["rendered"]


# --- L3 fix (2026-07-07): preview and run share ONE close-window ---------------


def test_preview_context_uses_technical_window_for_closes(
    golden_db: sqlite3.Connection,
) -> None:
    # ctx.closes spans the 400d technical window (honest 52w/MA120 signals in preview,
    # same as the run path); price_points stays bounded to the recent 180d.
    from datetime import timedelta

    from portfolio_dash.api.routers.prompts import (
        _HISTORY_DAYS,
        _TECHNICAL_HISTORY_DAYS,
        PromptBody,
        _build_context,
    )
    from portfolio_dash.pricing.results import PriceRow
    from portfolio_dash.pricing.store import upsert_prices
    from portfolio_dash.shared.enums import Currency as C
    from portfolio_dash.shared.enums import Market as M
    from tests.conftest import GOLDEN_NOW

    assert _TECHNICAL_HISTORY_DAYS == 400 and _HISTORY_DAYS == 180
    as_of = GOLDEN_NOW.date()
    old = as_of - timedelta(days=300)   # inside 400d, outside 180d
    recent = as_of - timedelta(days=10)
    upsert_prices(golden_db, [
        PriceRow(instrument="2330", market=M.TW, as_of=old,
                 close=Decimal("450"), source="test"),
        PriceRow(instrument="2330", market=M.TW, as_of=recent,
                 close=Decimal("610"), source="test"),
    ], fetched_at=GOLDEN_NOW)

    ctx = _build_context(
        golden_db, PromptBody(body="x", scope="per_symbol", symbol="2330"),
        GOLDEN_NOW, C.TWD,
    )
    assert Decimal("450") in ctx.closes            # long window feeds the signals
    dates = {p["date"] for p in ctx.price_points}
    assert old.isoformat() not in dates            # …but not the rendered history
    assert recent.isoformat() in dates
