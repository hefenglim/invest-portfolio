"""Contract: MY (Bursa) offline-registry fallback in lookup + ai-resolve (W1 batch-A).

Malaysia's only live verifier (yfinance ``.KL``) lacks many Bursa counters, so a correct AI
answer used to be demoted from ``resolved`` to a candidate. The baked ``bursa_registry`` lets a
valid 4-digit code verify OFFLINE. Pinned here: (a) the quick-add lookup finds a registry MY
code even when the quote provider fails; (b) a non-registry MY code still fails (typo guard
intact); (c) ai-resolve reaches ``status:"resolved"`` for a high-confidence registry code
despite the provider not pricing it (the demotion fix, end to end); (d) a non-registry code
stays advisory (candidates), never falsely resolved.
"""

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import instrument_service
from portfolio_dash.api.routers import instruments as inst_mod
from portfolio_dash.api.routers.instruments import AiInstrumentResolveReply
from portfolio_dash.pricing.results import RefreshSummary


@pytest.fixture(autouse=True)
def _clear_ai_resolve_cache() -> Iterator[None]:
    """Isolate the process-global ai-resolve dedup cache (F4c) between tests (the frozen test
    clock never expires an entry on its own)."""
    inst_mod._AI_RESOLVE_CACHE.clear()
    yield
    inst_mod._AI_RESOLVE_CACHE.clear()


def _quotes_fail(
    conn: Any, registry: Any, instruments: list[Any], fx_pairs: Any, *, now: datetime
) -> RefreshSummary:
    """A provider that finds NO quote — the MY reality for many Bursa counters."""
    return RefreshSummary(ok={}, failed=[r.symbol for r in instruments], fetched_at=now)


def _completer(reply: AiInstrumentResolveReply) -> Any:
    def _f(*_a: object, **_k: object) -> AiInstrumentResolveReply:
        return reply
    return _f


# --- (a) lookup finds a registry MY code when the quote provider fails -------------------


def test_lookup_my_registry_code_found_when_quote_fails(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instrument_service, "refresh_quotes", _quotes_fail)
    r = api_client.get("/api/instruments/lookup", params={"symbol": "1155", "market": "MY"})
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert body["registered"] is False
    assert body["name"] == "MAYBANK"       # from the baked registry, not the failed provider
    assert body["board"] == ".KL"          # the MY default board
    # a lookup never registers the symbol
    listed = [i["symbol"] for i in api_client.get("/api/instruments").json()["list"]]
    assert "1155" not in listed


def test_lookup_my_registry_preserves_leading_zero(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instrument_service, "refresh_quotes", _quotes_fail)
    r = api_client.get("/api/instruments/lookup", params={"symbol": "0166", "market": "MY"})
    body = r.json()
    assert body["found"] is True and body["name"] == "INARI"


# --- (b) non-registry MY code still fails (typo guard intact) ---------------------------


def test_lookup_non_registry_my_code_not_found(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instrument_service, "refresh_quotes", _quotes_fail)
    r = api_client.get("/api/instruments/lookup", params={"symbol": "9999", "market": "MY"})
    assert r.status_code == 200 and r.json()["found"] is False


# --- (c) ai-resolve reaches status:"resolved" for a registry MY code (the demotion fix) --


def test_ai_resolve_my_registry_code_resolves_despite_no_quote(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A high-confidence AI answer whose symbol is a valid Bursa code now VERIFIES offline via
    the registry (the quote provider cannot price it), so it reaches ``status:"resolved"``
    instead of being demoted to a candidate. ``lookup_instrument`` is NOT stubbed — the real
    path + registry does the verification (that is exactly what this fix restores)."""
    monkeypatch.setattr(inst_mod, "complete_structured", _completer(
        AiInstrumentResolveReply(symbol="1155", name="AI 名稱",
                                 gics_sector="Financials", confidence="high")))
    monkeypatch.setattr(instrument_service, "refresh_quotes", _quotes_fail)
    r = api_client.post("/api/instruments/ai-resolve",
                        json={"query": "Maybank 馬銀行", "market": "MY"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "resolved"
    assert body["symbol"] == "1155"
    assert body["verified"] is True
    assert body["name"] == "MAYBANK"       # provider(registry) name preferred over the AI name
    assert body["sector"] == "Financials"
    assert body["confidence"] == "high"


# --- (d) a non-registry high-confidence code cannot verify -> candidates -----------------


def test_ai_resolve_non_registry_my_code_downgrades_to_candidates(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inst_mod, "complete_structured", _completer(
        AiInstrumentResolveReply(symbol="9999", name="不存在",
                                 gics_sector="Financials", confidence="high")))
    monkeypatch.setattr(instrument_service, "refresh_quotes", _quotes_fail)
    r = api_client.post("/api/instruments/ai-resolve", json={"query": "???", "market": "MY"})
    body = r.json()
    assert body["status"] == "candidates"
    assert body["candidates"][0]["symbol"] == "9999"
    assert body["candidates"][0]["verified"] is False
