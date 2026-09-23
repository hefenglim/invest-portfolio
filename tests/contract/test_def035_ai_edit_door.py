"""DEF-035 / DEF-036 at the HTTP seam: the AI door returns editable drafts and takes them back.

* A first parse answers ``drafts`` (per kind, row-aligned with ``previews``) and the cash-kind
  vocabulary the draft table's 類型 select is built from — server-owned, so the browser keeps
  no fourth copy of the zh labels (AI-D21).
* The SAME door, given ``drafts`` instead of text, re-validates them WITHOUT calling the
  model — the LLM seam is booby-trapped below to prove it.
* ``stated_amount`` (DEF-036) rides the draft, and its contradiction flag rides the row data.
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion import agents as agents_mod
from portfolio_dash.data_ingestion.agents import AiDraftList, CashDraft, TxnDraft
from portfolio_dash.shared.models.enums import Side


def _parse(*_a: object, **_k: object) -> AiDraftList:
    return AiDraftList(rows=[
        TxnDraft(account_id="tw_broker", symbol="2330", side=Side.BUY, date=date(2026, 6, 2),
                 shares=Decimal("100"), price=Decimal("600"), stated_amount=Decimal("6000")),
        CashDraft(account_id="tw_broker", date=date(2026, 6, 1), cash_kind="入金",
                  ccy="TWD", amount=Decimal("50000")),
    ])


def _no_llm(*_a: object, **_k: object) -> AiDraftList:
    raise AssertionError("an edit re-validation must never call the model")


def test_a_first_parse_returns_row_aligned_drafts_and_the_cash_vocabulary(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agents_mod, "complete_structured", _parse)
    r = api_client.post("/api/input/ai/preview", json={"text": "買 2330 100 股 @600；入金"})
    assert r.status_code == 200, r.text
    b = r.json()
    txn = b["drafts"]["transactions"]
    assert len(txn) == len(b["previews"]["transactions"]["rows"]) == 1
    assert txn[0] == {
        "kind": "txn", "account_id": "tw_broker", "symbol": "2330", "side": "BUY",
        "date": "2026-06-02", "shares": "100", "price": "600", "daytrade": False,
        "short_sale": False, "is_etf": False, "note": None, "market": "",
        "stated_amount": "6000",
    }
    # 100 × 600 = 60,000 against a stated 6,000 → the contradiction reaches the row data
    data = b["previews"]["transactions"]["rows"][0]["data"]
    assert data["amount_mismatch"] == "1" and data["stated_amount"] == "6000"
    assert b["previews"]["transactions"]["rows"][0]["status"] == "warn"
    vocab = {v["kind"]: v for v in b["cash_kinds"]}
    assert vocab["DEPOSIT"] == {"kind": "DEPOSIT", "label": "入金", "sign": "1"}
    assert vocab["BROKER_FEE"]["sign"] == "-1"


def test_edited_drafts_are_revalidated_without_the_model(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agents_mod, "complete_structured", _no_llm)
    edited = {"rows": [{
        "kind": "txn", "account_id": "tw_broker", "symbol": "2330", "side": "BUY",
        "date": "2026-06-02", "shares": "10", "price": "600", "stated_amount": "6000",
    }]}
    r = api_client.post("/api/input/ai/preview", json={"drafts": edited})
    assert r.status_code == 200, r.text
    b = r.json()
    row = b["previews"]["transactions"]["rows"][0]
    assert row["data"]["quantity"] == "10"
    assert "amount_mismatch" not in row["data"]           # 10 × 600 = 6,000 — consistent now
    assert "tw_broker,2330,BUY,2026-06-02,10,600" in b["csv_texts"]["transactions"]
    assert b["drafts"]["transactions"][0]["shares"] == "10"


def test_a_malformed_edit_is_a_400_in_chinese_never_a_500(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agents_mod, "complete_structured", _no_llm)
    bad = {"rows": [{"kind": "txn", "account_id": "tw_broker", "symbol": "2330",
                     "side": "BUY", "date": "2026-06-02", "shares": "1,200",
                     "price": "600"}]}
    r = api_client.post("/api/input/ai/preview", json={"drafts": bad})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"
