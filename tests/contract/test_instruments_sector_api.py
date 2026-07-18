"""Contract: GET /api/instruments/sectors + POST /api/instruments/ai-sector (FU-D31).

The AI reply is ALWAYS re-mapped through canonical_sector server-side, so a non-canonical
value can never escape the endpoint (mapped=False leaves the frontend selection unchanged).
LLM degradation surfaces as the standard 402 / 409 / 503 envelope via the global handlers.
"""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api.routers import instruments as inst_mod
from portfolio_dash.api.routers.instruments import AiSectorReply
from portfolio_dash.shared.llm_config import (
    AINotActivated,
    LLMBudgetExceeded,
    LLMUnavailable,
)
from portfolio_dash.shared.sectors import CANONICAL_SECTORS


def _fake_reply(sector: str) -> Callable[..., AiSectorReply]:
    def _f(*_a: object, **_k: object) -> AiSectorReply:
        return AiSectorReply(sector=sector)
    return _f


def test_sectors_endpoint_returns_canonical_vocabulary(api_client: TestClient) -> None:
    r = api_client.get("/api/instruments/sectors")
    assert r.status_code == 200
    sectors = r.json()["sectors"]
    assert len(sectors) == len(CANONICAL_SECTORS)
    keys = [s["key"] for s in sectors]
    assert keys == [s["key"] for s in CANONICAL_SECTORS]  # order preserved
    assert "Technology" in keys and "Semiconductors" in keys
    assert keys[-1] == "Unclassified"
    for s in sectors:
        assert s["key"] and s["zh"]  # dual-text label material


def test_ai_sector_happy_canonical(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inst_mod, "complete_structured", _fake_reply("Technology"))
    r = api_client.post("/api/instruments/ai-sector",
                        json={"symbol": "AAPL", "name": "Apple", "market": "US"})
    assert r.status_code == 200
    assert r.json() == {"sector": "Technology", "mapped": True}


def test_ai_sector_synonym_is_mapped_server_side(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model reply that is a SYNONYM (or wrong case) is normalized to the canonical key."""
    monkeypatch.setattr(inst_mod, "complete_structured", _fake_reply("金融"))
    r = api_client.post("/api/instruments/ai-sector", json={"symbol": "1888"})
    assert r.status_code == 200
    assert r.json() == {"sector": "Financials", "mapped": True}


def test_ai_sector_lowercase_synonym_mapped(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inst_mod, "complete_structured", _fake_reply("  tech "))
    r = api_client.post("/api/instruments/ai-sector", json={"symbol": "MSFT"})
    assert r.json() == {"sector": "Technology", "mapped": True}


def test_ai_sector_unmappable_reply_is_not_applied(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An off-vocabulary reply passes through unchanged AND is flagged mapped=False, so the
    frontend keeps the user's selection (never writes a non-canonical value)."""
    monkeypatch.setattr(inst_mod, "complete_structured", _fake_reply("Nonsense Sector"))
    r = api_client.post("/api/instruments/ai-sector", json={"symbol": "ZZZZ"})
    assert r.status_code == 200
    assert r.json() == {"sector": "Nonsense Sector", "mapped": False}


def test_ai_sector_blank_symbol_400(api_client: TestClient) -> None:
    r = api_client.post("/api/instruments/ai-sector", json={"symbol": "   "})
    assert r.status_code == 400


def test_ai_sector_passes_symbol_and_vocabulary_into_prompt(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def spy(prompt: str, schema: type, *, agent: str, conn: object = None) -> AiSectorReply:
        captured["prompt"] = prompt
        captured["agent"] = agent
        return AiSectorReply(sector="Technology")

    monkeypatch.setattr(inst_mod, "complete_structured", spy)
    api_client.post("/api/instruments/ai-sector",
                    json={"symbol": "aapl", "name": "Apple", "market": "US"})
    prompt = str(captured["prompt"])
    assert "AAPL" in prompt and "Apple" in prompt and "US" in prompt
    assert "Technology" in prompt and "Semiconductors" in prompt  # canonical vocabulary
    assert captured["agent"] == "ai_sector"


@pytest.mark.parametrize(("exc", "status", "code"), [
    (LLMBudgetExceeded("AI 額度用盡"), 402, "budget_exceeded"),
    (AINotActivated("AI 未啟用"), 409, "ai_not_activated"),
    (LLMUnavailable("provider down"), 503, "llm_unavailable"),
])
def test_ai_sector_degradation_maps_to_standard_envelope(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
    exc: Exception, status: int, code: str,
) -> None:
    def _boom(*_a: object, **_k: object) -> AiSectorReply:
        raise exc
    monkeypatch.setattr(inst_mod, "complete_structured", _boom)
    r = api_client.post("/api/instruments/ai-sector", json={"symbol": "AAPL"})
    assert r.status_code == status and r.json()["error"]["code"] == code
