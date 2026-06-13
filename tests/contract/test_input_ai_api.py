from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion import agents as agents_mod
from portfolio_dash.data_ingestion.agents import AiDraft, AiDraftList
from portfolio_dash.shared.llm_config import AINotActivated, LLMBudgetExceeded
from portfolio_dash.shared.models.enums import Side


def _fake_ok(*_a: object, **_k: object) -> AiDraftList:
    return AiDraftList(drafts=[AiDraft(account_id="tw_broker", symbol="2330", side=Side.BUY,
                                       date=date(2026, 6, 2), shares=Decimal("10"),
                                       price=Decimal("600"))])


def test_ai_preview_ok(api_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents_mod, "complete_structured", _fake_ok)
    r = api_client.post("/api/input/ai/preview", json={"text": "在元大買 10 股 2330 @ 600"})
    assert r.status_code == 200
    b = r.json()
    assert b["summary"]["total"] == 1
    assert b["rows"][0]["data"]["symbol"] == "2330"
    assert b["meta"]["via"] == "litellm"
    assert "csv_text" in b and "2330" in b["csv_text"]


def test_ai_preview_budget_402(api_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> AiDraftList:
        raise LLMBudgetExceeded("AI 額度用盡")
    monkeypatch.setattr(agents_mod, "complete_structured", _boom)
    r = api_client.post("/api/input/ai/preview", json={"text": "x"})
    assert r.status_code == 402 and r.json()["error"]["code"] == "budget_exceeded"


def test_ai_preview_not_activated_409(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: object, **_k: object) -> AiDraftList:
        raise AINotActivated("AI 未啟用")
    monkeypatch.setattr(agents_mod, "complete_structured", _boom)
    r = api_client.post("/api/input/ai/preview", json={"text": "x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "ai_not_activated"
