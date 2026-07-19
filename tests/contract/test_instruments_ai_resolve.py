"""Contract: POST /api/instruments/ai-resolve — the UNIFIED AI instrument-resolve (R6-B).

ONE endpoint + ONE prompt behind every registration entry point: raw input + target market →
local exchange code + name + GICS sector (+ optional industry) in a single structured reply.
The reply is ADVISORY — the endpoint canonicalizes the sector and re-verifies the returned
symbol against the REAL provider quote/name lookup (the sole registration authority) before any
auto-fill. Behaviours pinned here (item 7): resolved-high-verified auto-fill; sector-out-of-vocab
downgrade → candidates; honest not_found (never fabricates); completer degradation → the standard
402/409/503 envelope; verification-fail downgrade; registered short-circuit (no LLM); the
sector_only re-detect that SKIPS the short-circuit; and that temperature=0 rides into the call.
"""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api.instrument_service import InstrumentLookup
from portfolio_dash.api.routers import instruments as inst_mod
from portfolio_dash.api.routers.instruments import (
    AiInstrumentResolveReply,
    AiResolveCandidate,
)
from portfolio_dash.shared.llm_config import (
    AINotActivated,
    LLMBudgetExceeded,
    LLMUnavailable,
)


def _completer(reply: AiInstrumentResolveReply) -> Callable[..., AiInstrumentResolveReply]:
    def _f(*_a: object, **_k: object) -> AiInstrumentResolveReply:
        return reply
    return _f


def _lookup_found(name: str = "台積電") -> Callable[..., InstrumentLookup]:
    def _f(*_a: object, **_k: object) -> InstrumentLookup:
        return InstrumentLookup(found=True, registered=False, name=name,
                               sector="", board="TWSE")
    return _f


def _lookup_miss() -> Callable[..., InstrumentLookup]:
    def _f(*_a: object, **_k: object) -> InstrumentLookup:
        return InstrumentLookup(found=False)
    return _f


# --- resolved (verified + high confidence) ---------------------------------------------


def test_resolve_high_verified_auto_fills(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """High confidence + canonical sector + provider verification → status:"resolved" with the
    full auto-fill shape; the provider name is preferred over the AI name."""
    monkeypatch.setattr(inst_mod, "complete_structured", _completer(
        AiInstrumentResolveReply(symbol="2303", name="AI 名稱",
                                 gics_sector="Information Technology",
                                 gics_industry="Semiconductors", confidence="high")))
    monkeypatch.setattr(inst_mod, "lookup_instrument", _lookup_found(name="聯華電子"))
    r = api_client.post("/api/instruments/ai-resolve",
                        json={"query": "UMC 聯電", "market": "TW"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "resolved"
    assert body["symbol"] == "2303"
    assert body["name"] == "聯華電子"  # provider name preferred over the AI name
    assert body["sector"] == "Information Technology"
    assert body["industry"] == "Semiconductors"
    assert body["verified"] is True
    assert body["confidence"] == "high"
    assert body["prompt_version"]


def test_resolve_prefers_ai_name_when_provider_name_blank(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inst_mod, "complete_structured", _completer(
        AiInstrumentResolveReply(symbol="AAPL", name="Apple Inc",
                                 gics_sector="Information Technology", confidence="high")))
    monkeypatch.setattr(inst_mod, "lookup_instrument", _lookup_found(name=""))
    r = api_client.post("/api/instruments/ai-resolve",
                        json={"query": "蘋果", "market": "US"})
    assert r.json()["name"] == "Apple Inc"  # AI name is the fallback


# --- downgrades → candidates ------------------------------------------------------------


def test_sector_out_of_vocab_downgrades_to_candidates(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A high-confidence reply whose sector is OFF-vocabulary downgrades to candidates (the
    unverified primary first, its off-vocab sector blanked)."""
    monkeypatch.setattr(inst_mod, "complete_structured", _completer(
        AiInstrumentResolveReply(symbol="2303", name="聯電",
                                 gics_sector="Nonsense Sector", confidence="high")))
    monkeypatch.setattr(inst_mod, "lookup_instrument", _lookup_found())
    r = api_client.post("/api/instruments/ai-resolve",
                        json={"query": "聯電", "market": "TW"})
    body = r.json()
    assert body["status"] == "candidates"
    assert body["candidates"][0] == {"symbol": "2303", "name": "聯電",
                                     "sector": "", "verified": False}


def test_verification_fail_downgrades_to_candidates(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a high-confidence + canonical-sector reply becomes candidates when the REAL lookup
    cannot verify the symbol — the LLM never overrides the provider authority."""
    monkeypatch.setattr(inst_mod, "complete_structured", _completer(
        AiInstrumentResolveReply(symbol="9999", name="不存在",
                                 gics_sector="Information Technology", confidence="high")))
    monkeypatch.setattr(inst_mod, "lookup_instrument", _lookup_miss())
    r = api_client.post("/api/instruments/ai-resolve",
                        json={"query": "不存在的公司", "market": "TW"})
    body = r.json()
    assert body["status"] == "candidates"
    assert body["candidates"][0]["symbol"] == "9999"
    assert body["candidates"][0]["verified"] is False


def test_medium_confidence_lists_primary_then_ai_candidates(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Medium confidence → candidates: the unverified primary FIRST, then the AI's alternates
    (deduped, canonicalized, capped at 5), all verified:false."""
    monkeypatch.setattr(inst_mod, "complete_structured", _completer(
        AiInstrumentResolveReply(
            symbol="2303", name="聯電", gics_sector="Information Technology",
            confidence="medium",
            candidates=[
                AiResolveCandidate(symbol="2303", name="dupe"),  # dropped (== primary)
                AiResolveCandidate(symbol="3034", name="聯詠", gics_sector="金融"),  # synonym
                AiResolveCandidate(symbol="", name="blank"),  # dropped (blank symbol)
            ])))
    monkeypatch.setattr(inst_mod, "lookup_instrument", _lookup_found())
    r = api_client.post("/api/instruments/ai-resolve",
                        json={"query": "聯", "market": "TW"})
    body = r.json()
    assert body["status"] == "candidates"
    syms = [c["symbol"] for c in body["candidates"]]
    assert syms == ["2303", "3034"]  # primary first; dupe + blank dropped
    assert body["candidates"][1]["sector"] == "Financials"  # synonym canonicalized


# --- honest not-found (never fabricate) -------------------------------------------------


def test_not_found_never_fabricates_a_code(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inst_mod, "complete_structured", _completer(
        AiInstrumentResolveReply(not_found=True)))
    r = api_client.post("/api/instruments/ai-resolve",
                        json={"query": "???", "market": "TW"})
    body = r.json()
    assert body["status"] == "not_found"
    assert "symbol" not in body  # no fabricated code
    assert body["message"]
    # stage boundary: the endpoint registered nothing.
    listed = [i["symbol"] for i in api_client.get("/api/instruments").json()["list"]]
    assert "???" not in listed


def test_blank_symbol_reply_is_treated_as_not_found(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty symbol (with not_found=false) is still honest not-found — never fabricated."""
    monkeypatch.setattr(inst_mod, "complete_structured", _completer(
        AiInstrumentResolveReply(symbol="   ", confidence="low")))
    r = api_client.post("/api/instruments/ai-resolve",
                        json={"query": "???", "market": "TW"})
    assert r.json()["status"] == "not_found"


def test_blank_query_400(api_client: TestClient) -> None:
    r = api_client.post("/api/instruments/ai-resolve", json={"query": "   ", "market": "TW"})
    assert r.status_code == 400
    r2 = api_client.post("/api/instruments/ai-resolve", json={"market": "TW"})
    assert r2.status_code == 400  # query defaults blank → same 400, not a 422 schema error


# --- registered short-circuit + sector_only re-detect -----------------------------------


def test_registered_symbol_short_circuits_without_llm(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw input that is ALREADY a registered active symbol answers from the registry with NO
    LLM call — the completer must not be invoked."""
    def _boom(*_a: object, **_k: object) -> AiInstrumentResolveReply:
        raise AssertionError("registered short-circuit must not call the LLM")
    monkeypatch.setattr(inst_mod, "complete_structured", _boom)
    r = api_client.post("/api/instruments/ai-resolve",
                        json={"query": "2330", "market": "TW"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "resolved" and body["symbol"] == "2330"
    assert body["verified"] is True


def test_sector_only_skips_short_circuit_and_calls_llm(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watchlist 「AI 偵測產業類別」 re-detect (sector_only=true) MUST re-classify a known
    symbol via the LLM — the registry short-circuit is skipped so a blank/stale sector can be
    filled."""
    called = {"n": 0}

    def _spy(*_a: object, **_k: object) -> AiInstrumentResolveReply:
        called["n"] += 1
        return AiInstrumentResolveReply(symbol="2330", name="台積電",
                                        gics_sector="Information Technology",
                                        gics_industry="Semiconductors", confidence="high")
    monkeypatch.setattr(inst_mod, "complete_structured", _spy)
    monkeypatch.setattr(inst_mod, "lookup_instrument", _lookup_found())
    r = api_client.post("/api/instruments/ai-resolve",
                        json={"query": "2330", "market": "TW", "sector_only": True})
    assert called["n"] == 1  # the LLM WAS called despite 2330 being registered
    body = r.json()
    assert body["status"] == "resolved"
    assert body["sector"] == "Information Technology"
    assert body["industry"] == "Semiconductors"


# --- graceful degradation (never a 500, never blocks the form) --------------------------


@pytest.mark.parametrize(("exc", "status", "code"), [
    (LLMBudgetExceeded("AI 額度用盡"), 402, "budget_exceeded"),
    (AINotActivated("AI 未啟用"), 409, "ai_not_activated"),
    (LLMUnavailable("provider down"), 503, "llm_unavailable"),
])
def test_completer_degradation_maps_to_standard_envelope(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
    exc: Exception, status: int, code: str,
) -> None:
    def _boom(*_a: object, **_k: object) -> AiInstrumentResolveReply:
        raise exc
    monkeypatch.setattr(inst_mod, "complete_structured", _boom)
    # a NON-registered query so the pipeline reaches the LLM call.
    r = api_client.post("/api/instruments/ai-resolve",
                        json={"query": "聯電", "market": "TW"})
    assert r.status_code == status and r.json()["error"]["code"] == code


# --- prompt / call wiring ---------------------------------------------------------------


def test_call_passes_query_market_agent_and_temperature_zero(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def spy(prompt: str, schema: type, *, agent: str, conn: object = None,
            temperature: object = None, **_k: object) -> AiInstrumentResolveReply:
        captured["prompt"] = prompt
        captured["agent"] = agent
        captured["temperature"] = temperature
        return AiInstrumentResolveReply(not_found=True)

    monkeypatch.setattr(inst_mod, "complete_structured", spy)
    api_client.post("/api/instruments/ai-resolve", json={"query": "聯電", "market": "TW"})
    prompt = str(captured["prompt"])
    assert "聯電" in prompt and "TW" in prompt
    assert "聯電⇒2303" in prompt              # the local-exchange-code rules ride in
    assert "Information Technology" in prompt  # the embedded GICS vocabulary
    assert captured["agent"] == "ai_instrument_resolve"
    assert captured["temperature"] == 0        # deterministic classify/resolve
