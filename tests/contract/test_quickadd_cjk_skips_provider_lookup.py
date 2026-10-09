"""A name typed into the code box never waits on a provider quote lookup (owner 2026-09-30).

Observed by the verifier (R8 F-01): on 觀察清單 › 加入, typing 台積電 first ran the provider
quote lookup — the TWSE/TPEx board probe, a live quote fetch and a name lookup — which took
25–30 s to answer 「查無報價」, and only then went on to AI 辨識. A string containing CJK
characters can never be an exchange code (``shared/symbol_format.py`` holds the owner-signed
code shapes: TW ``2330`` / ``00878B``, US ``AAPL`` / ``BRK.B``, MY ``5225``), so every backend
door that looks up user-typed text answers such input from the registry alone:

* ``GET /api/instruments/lookup`` (``lookup_instrument``) — the quick-add dialog's lookup,
  also the provider verification inside ``POST /api/instruments/ai-resolve``;
* ``quick_register`` with ``force=False`` — ``POST /api/instruments/quick`` and the
  manual-trade auto-register door — refuses at once with the same 查無報價 it gave after
  the wait.

The outcome is byte-identical to the slow path (a CJK string never had a quote); only the
wait is gone. A pure code keeps today's behaviour exactly: it still reaches the provider
(F-02 — and a pure code must never auto-trigger AI, which the frontend half pins).
The browser half is ``tests/e2e/test_quickadd_cjk_name_flow.py``.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.api import instrument_service
from portfolio_dash.pricing.results import RefreshSummary
from portfolio_dash.shared import symbol_format
from portfolio_dash.shared.enums import Market
from tests.conftest import GOLDEN_NOW

_WEB = Path(__file__).resolve().parents[2] / "web"


@pytest.fixture
def provider_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every provider-bound call the lookup / register service can make."""
    calls: list[str] = []

    def _probe(symbol: str, **_: object) -> None:
        calls.append(f"probe:{symbol}")

    def _quotes(conn: Any, registry: Any, instruments: list[Any], fx_pairs: Any, *,
                now: datetime, **_: object) -> RefreshSummary:
        calls.append("quotes:" + ",".join(r.symbol for r in instruments))
        return RefreshSummary(ok={}, failed=[r.symbol for r in instruments], fetched_at=now)

    def _name(symbol: str, market: Any, **_: object) -> None:
        calls.append(f"name:{symbol}")

    def _bursa(symbol: str) -> None:
        calls.append(f"bursa:{symbol}")

    monkeypatch.setattr(instrument_service, "probe_tw_board", _probe)
    monkeypatch.setattr(instrument_service, "refresh_quotes", _quotes)
    monkeypatch.setattr(instrument_service, "lookup_name", _name)
    monkeypatch.setattr(instrument_service, "bursa_name", _bursa)
    return calls


# --- the predicate ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["台積電", "2330 台積電", "鴻海", "トヨタ", "삼성전자", "玉山金。"])
def test_cjk_text_is_never_a_code(text: str) -> None:
    assert symbol_format.contains_cjk(text)
    assert not symbol_format.looks_like_market_code(text)


@pytest.mark.parametrize("text", ["2330", "00878B", "AAPL", "BRK.B", "5225", "ZZZZ9",
                                  "Apple Inc", ""])
def test_latin_and_digit_text_is_not_cjk(text: str) -> None:
    assert not symbol_format.contains_cjk(text)


def test_the_quick_add_dialog_mirrors_the_same_character_ranges() -> None:
    """The browser skips its lookup call on the same predicate; a range added on one side
    only would send a name to the slow path (or a code to AI) on the other."""
    src = (_WEB / "inst-quickadd.js").read_text(encoding="utf-8")
    m = re.search(r"const CJK_RE = /\[(.+?)\]/;", src)
    assert m, "inst-quickadd.js has no CJK_RE"
    assert m.group(1) == symbol_format.CJK_CLASS


# --- the lookup door ----------------------------------------------------------------------


@pytest.mark.parametrize("market", ["TW", "US", "MY"])
def test_a_name_lookup_answers_from_the_registry_without_a_provider_call(
    api_client: TestClient, provider_calls: list[str], market: str
) -> None:
    r = api_client.get("/api/instruments/lookup", params={"symbol": "台積電", "market": market})
    assert r.status_code == 200, r.text
    assert r.json()["found"] is False and r.json()["registered"] is False
    assert provider_calls == [], provider_calls


def test_a_pure_code_still_reaches_the_provider(
    api_client: TestClient, provider_calls: list[str]
) -> None:
    """F-02's input: a pure code keeps today's path exactly — the provider is asked."""
    r = api_client.get("/api/instruments/lookup", params={"symbol": "ZZZZ9", "market": "TW"})
    assert r.status_code == 200 and r.json()["found"] is False
    assert "probe:ZZZZ9" in provider_calls and "quotes:ZZZZ9" in provider_calls


def test_a_registered_code_still_answers_from_the_registry(
    api_client: TestClient, provider_calls: list[str]
) -> None:
    r = api_client.get("/api/instruments/lookup", params={"symbol": "2330", "market": "TW"})
    assert r.json()["found"] is True and r.json()["registered"] is True
    assert provider_calls == []


# --- the register doors -------------------------------------------------------------------


def test_the_quick_register_door_refuses_a_name_at_once(
    api_client: TestClient, provider_calls: list[str]
) -> None:
    r = api_client.post("/api/instruments/quick", json={"symbol": "台積電", "market": "TW"})
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "quote_not_found"
    assert "查無 台積電 的報價" in err["message"]
    assert provider_calls == [], provider_calls


def test_the_service_refuses_a_name_without_a_provider_call(
    golden_db: sqlite3.Connection, provider_calls: list[str]
) -> None:
    """``quick_register(force=False)`` is also the manual-trade auto-register door."""
    with pytest.raises(instrument_service.QuickRegisterError) as exc:
        instrument_service.quick_register(
            golden_db, symbol="聯電", market=Market.TW, now=GOLDEN_NOW, force=False)
    assert exc.value.code == "quote_not_found" and exc.value.status == 422
    assert provider_calls == [], provider_calls
    assert golden_db.execute(
        "SELECT COUNT(*) FROM instruments WHERE symbol = '聯電'").fetchone()[0] == 0
