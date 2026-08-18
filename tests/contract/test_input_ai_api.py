import base64
import sqlite3
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion import agents as agents_mod
from portfolio_dash.data_ingestion.agents import (
    AiDraftList,
    CashDraft,
    DivDraft,
    TxnDraft,
    UnparsedRow,
)
from portfolio_dash.shared.llm_config import (
    AINotActivated,
    LLMBudgetExceeded,
    LLMUnavailable,
    ModelConfig,
    upsert_model,
)
from portfolio_dash.shared.models.enums import Side

# A minimal valid PNG payload (8-byte magic + a little body) — the server sniffs magic bytes.
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_PNG_B64 = base64.b64encode(_PNG).decode("ascii")
_PNG_DATA_URI = "data:image/png;base64," + _PNG_B64


def _fake_ok(*_a: object, **_k: object) -> AiDraftList:
    return AiDraftList(rows=[TxnDraft(account_id="tw_broker", symbol="2330", side=Side.BUY,
                                      date=date(2026, 6, 2), shares=Decimal("10"),
                                      price=Decimal("600"))])


def _seed_model(conn: sqlite3.Connection, alias: str, *, vision: bool,
                enabled: bool = True) -> None:
    upsert_model(conn, ModelConfig(
        id=alias, model_alias=alias, provider="openai", model_name=alias,
        vision=vision, enabled=enabled,
    ))


def test_ai_preview_ok(api_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents_mod, "complete_structured", _fake_ok)
    r = api_client.post("/api/input/ai/preview", json={"text": "在元大買 10 股 2330 @ 600"})
    assert r.status_code == 200
    b = r.json()
    # W4 (AI-D18): one preview + one commit CSV PER KIND, keyed by the existing import kind.
    assert b["previews"]["transactions"]["summary"]["total"] == 1
    assert b["previews"]["transactions"]["rows"][0]["data"]["symbol"] == "2330"
    assert b["meta"]["via"] == "litellm"
    assert "2330" in b["csv_texts"]["transactions"]
    assert b["unparsed"] == []


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


def test_ai_preview_unavailable_503(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: object, **_k: object) -> AiDraftList:
        raise LLMUnavailable("provider down")
    monkeypatch.setattr(agents_mod, "complete_structured", _boom)
    r = api_client.post("/api/input/ai/preview", json={"text": "x"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "llm_unavailable"


# --- W4 (AI-D17/D18/D21): the discriminated union over the wire ---------------------------


def _fake_mixed(*_a: object, **_k: object) -> AiDraftList:
    """One draft of each kind plus a confessed unparsed row — the mixed-statement shape."""
    return AiDraftList(
        rows=[
            TxnDraft(account_id="tw_broker", symbol="2330", side=Side.BUY,
                     date=date(2026, 6, 2), shares=Decimal("10"), price=Decimal("600")),
            DivDraft(account_id="tw_broker", symbol="2330", date=date(2026, 6, 3),
                     type="CASH", gross=Decimal("1000")),
            CashDraft(account_id="tw_broker", date=date(2026, 6, 1),
                      cash_kind="入金", ccy="TWD", amount=Decimal("50000")),
        ],
        unparsed=[UnparsedRow(text="TWD 轉 USD 31500", reason="換匯請改用換匯登錄")],
    )


def test_ai_preview_mixed_union_three_buckets(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One prompt, three kinds: each lands in its own preview + CSV, keyed by import kind."""
    monkeypatch.setattr(agents_mod, "complete_structured", _fake_mixed)
    r = api_client.post("/api/input/ai/preview", json={"text": "mixed"})
    assert r.status_code == 200
    b = r.json()
    assert set(b["previews"]) == {"transactions", "dividends", "cash"}
    assert set(b["csv_texts"]) == {"transactions", "dividends", "cash"}
    assert b["previews"]["dividends"]["rows"][0]["data"]["gross"] == "1000"
    # AI-D21: the cash row carries the server-owned zh label + explicit sign.
    cash_row = b["previews"]["cash"]["rows"][0]
    assert cash_row["data"]["kind"] == "DEPOSIT"      # the zh alias canonicalised at the door
    assert cash_row["data"]["kind_label"] == "入金"
    assert cash_row["data"]["sign"] == "1"
    # AI-D17: the confessed unparsed row rides the wire verbatim.
    assert b["unparsed"] == [{"text": "TWD 轉 USD 31500", "reason": "換匯請改用換匯登錄"}]


def test_ai_preview_cash_withdraw_guard_is_wired(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AI-D18's load-bearing assertion: the AI door binds the REAL pool probe.

    A withdrawal from an empty pool must surface ``withdraw_insufficient_balance`` as a
    hard issue — if the router ever dropped the injection, this row would preview clean
    and the AI door would ship a weaker guard than the manual form next to it.
    """

    def _withdraw(*_a: object, **_k: object) -> AiDraftList:
        return AiDraftList(rows=[CashDraft(
            account_id="tw_broker", date=date(2026, 6, 5), cash_kind="WITHDRAW",
            ccy="TWD", amount=Decimal("999999"),
        )])

    monkeypatch.setattr(agents_mod, "complete_structured", _withdraw)
    r = api_client.post("/api/input/ai/preview", json={"text": "提領 999999"})
    assert r.status_code == 200
    row = r.json()["previews"]["cash"]["rows"][0]
    assert row["status"] == "error"
    assert "不可透支" in (row["reason"] or "")


# --- FU-D20: screenshot intake + per-run model picker ----------------------------------


def test_ai_preview_accepts_base64_and_data_uri_images(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A screenshot (raw base64 AND data-URI form) is decoded to bytes and passed through;
    the model only EXTRACTS — the numbers still flow through the preview pipeline."""
    captured: dict[str, object] = {}

    def spy(prompt: str, schema: type, *, agent: str, conn: object = None,
            images: list[bytes] | None = None, model_override: str | None = None,
            ) -> AiDraftList:
        captured["images"] = images
        captured["model_override"] = model_override
        return _fake_ok()

    monkeypatch.setattr(agents_mod, "complete_structured", spy)
    # raw base64 (no prefix) AND a full data-URI in the same request.
    r = api_client.post("/api/input/ai/preview",
                        json={"text": "", "images": [_PNG_B64, _PNG_DATA_URI]})
    assert r.status_code == 200
    imgs = captured["images"]
    assert isinstance(imgs, list) and len(imgs) == 2
    assert all(isinstance(b, bytes) and b.startswith(b"\x89PNG") for b in imgs)
    assert captured["model_override"] is None  # no alias picked -> auto (vision role chain)


def test_ai_preview_oversize_image_400(api_client: TestClient) -> None:
    big = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024)).decode("ascii")
    r = api_client.post("/api/input/ai/preview", json={"text": "x", "images": [big]})
    assert r.status_code == 400 and r.json()["error"]["field"] == "images"


def test_ai_preview_wrong_magic_400(api_client: TestClient) -> None:
    not_image = base64.b64encode(b"this is definitely not an image").decode("ascii")
    r = api_client.post("/api/input/ai/preview", json={"text": "x", "images": [not_image]})
    assert r.status_code == 400 and r.json()["error"]["field"] == "images"


def test_ai_preview_invalid_base64_400(api_client: TestClient) -> None:
    r = api_client.post("/api/input/ai/preview",
                        json={"text": "x", "images": ["!!! not base64 !!!"]})
    assert r.status_code == 400 and r.json()["error"]["field"] == "images"


def test_ai_preview_too_many_images_400(api_client: TestClient) -> None:
    r = api_client.post("/api/input/ai/preview", json={"text": "x", "images": [_PNG_B64] * 5})
    assert r.status_code == 400 and r.json()["error"]["field"] == "images"


def test_ai_preview_empty_text_no_image_400(api_client: TestClient) -> None:
    r = api_client.post("/api/input/ai/preview", json={"text": "   "})
    assert r.status_code == 400 and r.json()["error"]["field"] == "text"


def test_ai_preview_unknown_alias_400(api_client: TestClient) -> None:
    r = api_client.post("/api/input/ai/preview",
                        json={"text": "buy", "model_alias": "does-not-exist"})
    assert r.status_code == 400 and r.json()["error"]["field"] == "model_alias"


def test_ai_preview_disabled_alias_400(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_model(golden_db, "disabled-m", vision=True, enabled=False)
    r = api_client.post("/api/input/ai/preview",
                        json={"text": "buy", "model_alias": "disabled-m"})
    assert r.status_code == 400 and r.json()["error"]["field"] == "model_alias"


def test_ai_preview_non_vision_alias_with_images_400(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    _seed_model(golden_db, "text-only", vision=False)
    r = api_client.post("/api/input/ai/preview",
                        json={"text": "buy", "images": [_PNG_B64], "model_alias": "text-only"})
    assert r.status_code == 400 and r.json()["error"]["field"] == "model_alias"


def test_ai_preview_model_alias_reaches_completer(
    api_client: TestClient, golden_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_model(golden_db, "picked-m", vision=True)
    captured: dict[str, object] = {}

    def spy(prompt: str, schema: type, *, agent: str, conn: object = None,
            images: list[bytes] | None = None, model_override: str | None = None,
            ) -> AiDraftList:
        captured["model_override"] = model_override
        return _fake_ok()

    monkeypatch.setattr(agents_mod, "complete_structured", spy)
    r = api_client.post("/api/input/ai/preview",
                        json={"text": "buy 2330", "model_alias": "picked-m"})
    assert r.status_code == 200
    assert captured["model_override"] == "picked-m"


# --- FU-D33: preview-row machine code for an unregistered symbol ------------------------


def _fake_unregistered(*_a: object, **_k: object) -> AiDraftList:
    """A draft whose symbol (ZZZZ9) is NOT in the golden registry — the AI-input path emits the
    unregistered-symbol block for it (resolution is exact-only, so no coercion to 2330/AAPL)."""
    return AiDraftList(rows=[TxnDraft(account_id="schwab", symbol="ZZZZ9", side=Side.BUY,
                                      date=date(2026, 6, 2), shares=Decimal("10"),
                                      price=Decimal("100"))])


def test_ai_preview_unregistered_symbol_carries_code(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FU-D33: an unregistered-symbol row carries the STABLE ``code`` + its symbol so the AI pane
    can render an inline 立即註冊 action; the human ``reason`` text is unchanged (additive)."""
    monkeypatch.setattr(agents_mod, "complete_structured", _fake_unregistered)
    r = api_client.post("/api/input/ai/preview", json={"text": "buy ZZZZ9"})
    assert r.status_code == 200
    row = r.json()["previews"]["transactions"]["rows"][0]
    assert row["code"] == "unregistered_symbol"
    assert row["data"]["symbol"] == "ZZZZ9"
    assert row["status"] == "error"  # unregistered = hard block until registered
    assert "ZZZZ9" in (row["reason"] or "")  # reason still names the symbol (not replaced)


def test_ai_preview_registered_symbol_code_is_null(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered-symbol row carries ``code: null`` — the field is purely additive."""
    monkeypatch.setattr(agents_mod, "complete_structured", _fake_ok)
    r = api_client.post("/api/input/ai/preview", json={"text": "buy 2330"})
    assert r.status_code == 200
    assert r.json()["previews"]["transactions"]["rows"][0]["code"] is None
