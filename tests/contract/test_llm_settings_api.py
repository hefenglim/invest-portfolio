"""Contract tests for the LLM settings API (spec 16): config / models / roles / quota.

Self-contained: an in-memory DB bootstrapped to the AI-off state, a local FastAPI app
mounting ONLY ``llm_settings.router`` (so the suite does not depend on app.py wiring
this router yet), ``get_conn`` overridden, sockets re-enabled for the in-process
TestClient transport, and the model-test LLM call monkeypatched so NO real network.
"""

import sqlite3
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_socket import disable_socket, enable_socket

from portfolio_dash.api.deps import get_conn
from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.api.routers import llm_settings
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.shared import llm
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    set_role,
    upsert_model,
)


def _seed_model(conn: sqlite3.Connection, alias: str = "claude-sonnet", **kw: object) -> None:
    base: dict[str, object] = dict(
        id=alias,
        model_alias=alias,
        provider="anthropic",
        model_name="claude-sonnet-4-5",
        api_key="sk-abcdef1234567890a2f",
        vision=True,
        input_price_per_mtok=Decimal("3.00"),
        output_price_per_mtok=Decimal("15.00"),
        context_window=200000,
        max_output_tokens=8192,
        timeout_seconds=60,
        max_retries=2,
        enabled=True,
    )
    base.update(kw)
    upsert_model(conn, ModelConfig(**base))  # type: ignore[arg-type]


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    bootstrap_db(c)  # creates ledger + LLM tables, AI-off
    _seed_model(c)
    add_topup(c, Decimal("10.00"), note="seed top-up")
    set_role(c, LLMRole.DEFAULT, "claude-sonnet")
    # one usage row so by_model / by_agent / daily / health have content
    llm.log_usage(
        c,
        model="claude-sonnet-4-5",
        agent="ai_agents_input",
        input_tokens=1000,
        output_tokens=200,
        cost=Decimal("1.92"),
    )
    yield c
    c.close()


@pytest.fixture
def client(conn: sqlite3.Connection) -> Iterator[TestClient]:
    enable_socket()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(llm_settings.router, prefix="/api")
    app.dependency_overrides[get_conn] = lambda: conn
    c = TestClient(app)
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        disable_socket(allow_unix_socket=True)


# --- 16.1 GET /api/llm/config -------------------------------------------------


def test_get_config_masks_key_and_shows_roles_quota_usage(client: TestClient) -> None:
    r = client.get("/api/llm/config")
    assert r.status_code == 200
    b = r.json()

    # models: key masked, never raw
    model = next(m for m in b["models"] if m["alias"] == "claude-sonnet")
    assert model["api_key_masked"] == "sk-•••a2f"
    assert "api_key" not in model
    assert model["provider"] == "anthropic"
    assert model["price_in"] == "3.00" and model["price_out"] == "15.00"
    assert model["vision"] is True
    assert model["health"] == "ok"  # has a logged call
    assert model["last_called"] is not None

    # roles include master / master_fallback (spec 04 §4.3 overlay)
    roles = b["roles"]
    assert roles["default_model"] == "claude-sonnet"
    assert "master_model" in roles and "master_fallback" in roles
    assert roles["master_model"] is None

    # quota: remaining = topups - usage, threshold present, topups listed
    quota = b["quota"]
    assert quota["remaining_usd"] == "8.08"  # 10.00 - 1.92
    assert "alert_threshold_usd" in quota
    assert len(quota["topups"]) == 1 and quota["topups"][0]["amount_usd"] == "10.00"
    # 3B: ai_active for the off-dashboard quota chip; the fixture binds an enabled model
    # to the DEFAULT role -> AI is active.
    assert quota["ai_active"] is True

    # usage: by_model / by_agent / daily series shape
    usage = b["usage"]
    bm = next(x for x in usage["by_model"] if x["alias"] == "claude-sonnet")
    assert bm["calls"] == 1 and bm["tokens_in"] == 1000 and bm["cost_usd"] == "1.92"
    ba = next(x for x in usage["by_agent"] if x["agent"] == "ai_agents_input")
    assert ba["cost_usd"] == "1.92"
    assert "dates" in usage["daily"] and "series" in usage["daily"]
    series0 = usage["daily"]["series"][0]
    assert "alias" in series0 and "costs" in series0
    assert len(series0["costs"]) == len(usage["daily"]["dates"])


# --- 16.2 model CRUD ----------------------------------------------------------


def test_post_model_created_and_key_masked(client: TestClient) -> None:
    r = client.post(
        "/api/llm/models",
        json={
            "alias": "qwen-vl",
            "provider": "openrouter",
            "model_name": "qwen/qwen2.5-vl-72b",
            "api_base": "https://openrouter.ai/api/v1",
            "api_key": "sk-secretkey99999tail",
            "vision": True,
            "price_in": "0.40",
            "price_out": "0.40",
            "context_window": 32000,
            "max_output_tokens": 2048,
            "timeout_seconds": 90,
            "max_retries": 1,
            "enabled": False,
            "notes": "testing",
        },
    )
    assert r.status_code == 201
    b = r.json()
    assert b["alias"] == "qwen-vl"
    assert b["api_key_masked"] == "sk-•••ail"
    assert "api_key" not in b
    assert b["enabled"] is False


def test_post_duplicate_alias_409(client: TestClient) -> None:
    r = client.post(
        "/api/llm/models",
        json={
            "alias": "claude-sonnet",
            "provider": "anthropic",
            "model_name": "dup",
            "price_in": "1.00",
            "price_out": "1.00",
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "duplicate_alias"


def test_put_unknown_model_404(client: TestClient) -> None:
    r = client.put("/api/llm/models/nope", json={"notes": "x"})
    assert r.status_code == 404


def test_put_updates_subset(client: TestClient) -> None:
    r = client.put("/api/llm/models/claude-sonnet", json={"enabled": False, "notes": "paused"})
    assert r.status_code == 200
    b = r.json()
    assert b["enabled"] is False and b["notes"] == "paused"
    assert b["api_key_masked"] == "sk-•••a2f"  # key preserved, still masked


def test_delete_in_use_model_422(client: TestClient) -> None:
    # claude-sonnet is bound to the DEFAULT role in the fixture
    r = client.delete("/api/llm/models/claude-sonnet")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "model_in_use"


def test_delete_unused_model_ok(client: TestClient) -> None:
    client.post(
        "/api/llm/models",
        json={
            "alias": "spare",
            "provider": "openai",
            "model_name": "gpt-4o-mini",
            "price_in": "0.15",
            "price_out": "0.60",
        },
    )
    r = client.delete("/api/llm/models/spare")
    assert r.status_code == 200


# --- 16.3 PUT /api/llm/roles --------------------------------------------------


def test_put_roles_sets_master(client: TestClient) -> None:
    _seed_model(_conn_of(client), alias="master-m", enabled=True)
    r = client.put(
        "/api/llm/roles",
        json={
            "default_model": "claude-sonnet",
            "default_fallback": None,
            "vision_model": "claude-sonnet",
            "vision_fallback": None,
            "master_model": "master-m",
            "master_fallback": None,
        },
    )
    assert r.status_code == 200
    assert r.json()["master_model"] == "master-m"


def test_put_roles_unknown_alias_400(client: TestClient) -> None:
    r = client.put(
        "/api/llm/roles",
        json={
            "default_model": "ghost",
            "default_fallback": None,
            "vision_model": None,
            "vision_fallback": None,
            "master_model": None,
            "master_fallback": None,
        },
    )
    assert r.status_code == 400


def test_put_roles_fallback_equals_main_400(client: TestClient) -> None:
    r = client.put(
        "/api/llm/roles",
        json={
            "default_model": "claude-sonnet",
            "default_fallback": "claude-sonnet",
            "vision_model": None,
            "vision_fallback": None,
            "master_model": None,
            "master_fallback": None,
        },
    )
    assert r.status_code == 400


# --- 16.4 test / topup / quota ------------------------------------------------


def test_model_test_hermetic_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Msg:
        content = "pong"

    class _Choice:
        message = _Msg()

    class _Usage:
        prompt_tokens = 3
        completion_tokens = 1

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    def _fake_completion(**_kw: object) -> _Resp:
        return _Resp()

    monkeypatch.setattr(llm.litellm, "completion", _fake_completion)
    r = client.post("/api/llm/models/claude-sonnet/test")
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] is True
    assert "reply_snippet" in b


def test_model_test_hermetic_failure_still_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(**_kw: object) -> object:
        raise RuntimeError("401 invalid api key")

    monkeypatch.setattr(llm.litellm, "completion", _boom)
    r = client.post("/api/llm/models/claude-sonnet/test")
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] is False and "error_detail" in b


def test_topup_changes_remaining(client: TestClient) -> None:
    r = client.post("/api/llm/quota/topup", json={"amount_usd": "5.00", "note": "extra"})
    assert r.status_code == 200
    # spec 16.4: remaining = Σ top-ups - Σ usage = (10.00 + 5.00) - 1.92 = 13.08
    assert r.json()["remaining_usd"] == "13.08"


def test_topup_non_positive_400(client: TestClient) -> None:
    r = client.post("/api/llm/quota/topup", json={"amount_usd": "0", "note": "x"})
    assert r.status_code == 400


def test_put_quota_threshold(client: TestClient) -> None:
    r = client.put("/api/llm/quota", json={"alert_threshold_usd": "2.50"})
    assert r.status_code == 200
    assert r.json()["alert_threshold_usd"] == "2.50"
    # reflected on the config read
    cfg = client.get("/api/llm/config").json()
    assert cfg["quota"]["alert_threshold_usd"] == "2.50"


def _conn_of(client: TestClient) -> sqlite3.Connection:
    """Recover the overridden connection so a test can seed extra rows."""
    return client.app.dependency_overrides[get_conn]()  # type: ignore[attr-defined,no-any-return]


# --- GET /api/llm/requests (2026-07-07 request ledger) ---------------------------


def test_llm_requests_ledger_lists_and_filters(api_client: TestClient) -> None:
    from decimal import Decimal as D

    from portfolio_dash.api.deps import get_conn as _dep
    from portfolio_dash.shared.llm import log_usage

    conn = api_client.app.dependency_overrides[_dep]()  # type: ignore[attr-defined]
    log_usage(conn, model="gemini-2.5-flash-lite", agent="insight_generate",
              input_tokens=6257, output_tokens=1357, cost=D("0.0011685"),
              cache_tokens=1024)
    log_usage(conn, model="haiku-4.5", agent="news_organize",
              input_tokens=500, output_tokens=60, cost=D("0.0006"))
    r = api_client.get("/api/llm/requests").json()
    assert r["totals"]["count"] >= 2
    newest = r["rows"][0]
    assert set(newest) == {"ts", "model", "agent", "tokens_in", "tokens_out",
                           "cache_tokens", "cost_usd"}
    assert newest["model"] == "haiku-4.5" and newest["cache_tokens"] == 0
    second = r["rows"][1]
    assert second["cache_tokens"] == 1024 and second["cost_usd"] == "0.0011685"
    # ts is Taipei-normalized "YYYY-MM-DD HH:MM:SS"
    assert len(newest["ts"]) == 19 and newest["ts"][10] == " "
    # agent filter narrows + totals follow the filter
    news = api_client.get("/api/llm/requests?agent=news_organize").json()
    assert news["totals"]["count"] == 1 and news["rows"][0]["agent"] == "news_organize"
    assert "insight_generate" in news["agents"]  # option list stays global


def test_llm_requests_since_until_mixed_ts_formats(api_client: TestClient) -> None:
    """WPB: the ts range filter compares in UTC across BOTH stored ts generations.

    One legacy naive-UTC row (02:00 UTC == 10:00 Taipei) and one current +08:00 row
    (12:00 Taipei). A Taipei-expressed window 09:00–11:00 must catch ONLY the legacy
    row; a wide window catches both; totals follow the filtered set.
    """
    from decimal import Decimal as D

    from portfolio_dash.api.deps import get_conn as _dep

    conn = api_client.app.dependency_overrides[_dep]()  # type: ignore[attr-defined]
    conn.execute(
        "INSERT INTO llm_usage (ts, model, agent, input_tokens, output_tokens, cost) "
        "VALUES (?,?,?,?,?,?)",
        ("2026-07-06T02:00:00", "legacy-model", "win_test", 10, 5, str(D("0.001"))),
    )
    conn.execute(
        "INSERT INTO llm_usage (ts, model, agent, input_tokens, output_tokens, cost) "
        "VALUES (?,?,?,?,?,?)",
        ("2026-07-06T12:00:00+08:00", "new-model", "win_test", 20, 8, str(D("0.002"))),
    )
    conn.commit()

    narrow = api_client.get("/api/llm/requests", params={
        "agent": "win_test",
        "since": "2026-07-06T09:00:00+08:00",
        "until": "2026-07-06T11:00:00+08:00",
    }).json()
    assert narrow["totals"]["count"] == 1
    assert narrow["rows"][0]["model"] == "legacy-model"
    assert narrow["totals"]["total_cost_usd"] == "0.001"
    # display ts is Taipei-normalized for the aware row; legacy naive stays as stored
    wide = api_client.get("/api/llm/requests", params={
        "agent": "win_test",
        "since": "2026-07-06T00:00:00+08:00",
        "until": "2026-07-06T23:59:59+08:00",
    }).json()
    assert wide["totals"]["count"] == 2
    assert {r["model"] for r in wide["rows"]} == {"legacy-model", "new-model"}
    # an open-ended window (since only) works too
    tail = api_client.get("/api/llm/requests", params={
        "agent": "win_test", "since": "2026-07-06T11:00:00+08:00",
    }).json()
    assert tail["totals"]["count"] == 1 and tail["rows"][0]["model"] == "new-model"


def test_llm_requests_invalid_since_400(api_client: TestClient) -> None:
    r = api_client.get("/api/llm/requests", params={"since": "not-a-time"})
    assert r.status_code == 400
    assert r.json()["error"]["field"] == "since"
