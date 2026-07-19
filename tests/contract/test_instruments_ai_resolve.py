"""Contract: POST /api/instruments/ai-resolve (FU-D42c 「AI 判讀代號」).

The endpoint returns the LLM's symbol/name SUGGESTION — UNVERIFIED (``verified: false``)
and registering nothing. Verification is a SEPARATE stage: the dialog re-runs
GET /api/instruments/lookup with the suggestion, and that quote-backed lookup remains the
sole registration authority (invariant: the LLM never supplies a number of record; the
identification itself is qualitative). Both stages are exercised here. LLM degradation
surfaces as the standard 402 / 409 / 503 envelope via the global handlers.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import instrument_service
from portfolio_dash.api.routers import instruments as inst_mod
from portfolio_dash.api.routers.instruments import AiResolveReply
from portfolio_dash.pricing.results import RefreshSummary
from portfolio_dash.shared.llm_config import (
    AINotActivated,
    LLMBudgetExceeded,
    LLMUnavailable,
)


def _fake_reply(symbol: str, name: str = "") -> Any:
    def _f(*_a: object, **_k: object) -> AiResolveReply:
        return AiResolveReply(symbol=symbol, name=name)
    return _f


def test_ai_resolve_happy_path_returns_unverified_suggestion(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's bug input: the model maps 「聯電/UMC」 to the local code 2303. The reply
    is a suggestion only — flagged unverified, normalized upper, and NOTHING registered."""
    monkeypatch.setattr(inst_mod, "complete_structured", _fake_reply("2303", "聯電"))
    r = api_client.post("/api/instruments/ai-resolve",
                        json={"query": "UMC 聯電", "market": "TW"})
    assert r.status_code == 200
    assert r.json() == {"symbol": "2303", "name": "聯電", "verified": False}
    # stage boundary: the endpoint registered nothing (the lookup+confirm flow does that).
    listed = [i["symbol"] for i in api_client.get("/api/instruments").json()["list"]]
    assert "2303" not in listed


def test_ai_resolve_normalizes_symbol_case(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inst_mod, "complete_structured", _fake_reply("  aapl ", " Apple "))
    r = api_client.post("/api/instruments/ai-resolve",
                        json={"query": "蘋果", "market": "US"})
    assert r.json() == {"symbol": "AAPL", "name": "Apple", "verified": False}


def test_ai_resolve_empty_model_reply_passes_through_blank(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An honest cannot-identify reply (blank symbol) flows through unchanged — the dialog
    shows its own notice; the endpoint never invents a code."""
    monkeypatch.setattr(inst_mod, "complete_structured", _fake_reply(""))
    r = api_client.post("/api/instruments/ai-resolve",
                        json={"query": "???", "market": "TW"})
    assert r.status_code == 200
    assert r.json()["symbol"] == ""


def test_ai_resolve_missing_or_blank_query_400(api_client: TestClient) -> None:
    r = api_client.post("/api/instruments/ai-resolve", json={"query": "   ", "market": "TW"})
    assert r.status_code == 400
    r2 = api_client.post("/api/instruments/ai-resolve", json={"market": "TW"})
    assert r2.status_code == 400  # query defaults blank → same 400, not a 422 schema error


def test_ai_resolve_passes_query_and_market_into_prompt(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def spy(prompt: str, schema: type, *, agent: str, conn: object = None) -> AiResolveReply:
        captured["prompt"] = prompt
        captured["agent"] = agent
        return AiResolveReply(symbol="2303", name="聯電")

    monkeypatch.setattr(inst_mod, "complete_structured", spy)
    api_client.post("/api/instruments/ai-resolve", json={"query": "聯電", "market": "TW"})
    prompt = str(captured["prompt"])
    assert "聯電" in prompt and "TW" in prompt
    assert "聯電⇒2303" in prompt  # the FU-D41 local-exchange-code rules ride in the prompt
    assert captured["agent"] == "ai_symbol_resolve"


@pytest.mark.parametrize(("exc", "status", "code"), [
    (LLMBudgetExceeded("AI 額度用盡"), 402, "budget_exceeded"),
    (AINotActivated("AI 未啟用"), 409, "ai_not_activated"),
    (LLMUnavailable("provider down"), 503, "llm_unavailable"),
])
def test_ai_resolve_degradation_maps_to_standard_envelope(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
    exc: Exception, status: int, code: str,
) -> None:
    def _boom(*_a: object, **_k: object) -> AiResolveReply:
        raise exc
    monkeypatch.setattr(inst_mod, "complete_structured", _boom)
    r = api_client.post("/api/instruments/ai-resolve", json={"query": "聯電"})
    assert r.status_code == status and r.json()["error"]["code"] == code


# --- stage 2: the REAL lookup verifies (or refuses) the suggestion ----------------------


def test_suggestion_of_registered_symbol_verifies_via_lookup_network_free(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 1 suggests golden-registered 2330 → stage 2 (the real lookup) verifies it from
    stored metadata with NO provider call. The suggestion itself proved nothing — found=True
    comes from the lookup, the same authority every registration path uses."""
    monkeypatch.setattr(inst_mod, "complete_structured", _fake_reply("2330", "台積電"))
    r = api_client.post("/api/instruments/ai-resolve",
                        json={"query": "台積電", "market": "TW"})
    assert r.json()["verified"] is False  # stage 1 never verifies

    def _no_provider(*_a: object, **_k: object) -> None:
        raise AssertionError("lookup of a registered symbol must not hit the provider")

    monkeypatch.setattr(instrument_service, "refresh_quotes", _no_provider)
    monkeypatch.setattr(instrument_service, "probe_tw_board", _no_provider)
    lr = api_client.get("/api/instruments/lookup",
                        params={"symbol": r.json()["symbol"], "market": "TW"})
    assert lr.status_code == 200
    assert lr.json()["found"] is True and lr.json()["registered"] is True


def test_unverifiable_suggestion_stays_unfound_at_lookup(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 1 suggests a symbol NO provider can quote → stage 2 returns found=False, so the
    dialog keeps its confirm blocked (「AI 判讀後仍查無報價」): an LLM suggestion can never
    register without passing the real quote check."""
    monkeypatch.setattr(inst_mod, "complete_structured", _fake_reply("9999", "不存在"))
    r = api_client.post("/api/instruments/ai-resolve",
                        json={"query": "不存在的公司", "market": "TW"})
    sym = r.json()["symbol"]

    def _no_quote(conn: Any, registry: Any, instruments: list[Any], fx_pairs: Any,
                  *, now: Any) -> RefreshSummary:
        return RefreshSummary(ok={}, failed=[i.symbol for i in instruments],
                              fetched_at=now)

    monkeypatch.setattr(instrument_service, "refresh_quotes", _no_quote)
    monkeypatch.setattr(instrument_service, "probe_tw_board", lambda s, **k: "TWSE")
    lr = api_client.get("/api/instruments/lookup", params={"symbol": sym, "market": "TW"})
    assert lr.status_code == 200
    assert lr.json()["found"] is False  # the authority refused — no registration possible
