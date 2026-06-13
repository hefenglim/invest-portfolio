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
    assert len(rows) == 26
    h = next(r for r in rows if r["token"] == "holdings_json")
    assert h["scope"] == "portfolio" and h["available"] is True
    assert set(h) == {"token", "name", "category", "scope", "desc", "available", "sample"}
    inst = next(r for r in rows if r["token"] == "institutional_json")
    assert inst["available"] is False
    # 8 categories present
    assert len({r["category"] for r in rows}) == 8


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
