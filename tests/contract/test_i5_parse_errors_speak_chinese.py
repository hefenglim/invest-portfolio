"""I-5 (F-5): an import row's 原因 column never shows Python's own English.

``data_ingestion/opening_import.py`` answered every parse failure with
``Issue(kind="parse_error", message=str(exc))`` — so a missing ``account`` column read
「'account'」, a bad date 「Invalid isoformat string: '2026/01/02'」 and a non-numeric share count
「[<class 'decimal.ConversionSyntax'>]」. The class scan (every ``message=str(exc)`` that reaches a
user-visible issue) found two more doors with the same leak: the corporate-action importer
(date cells parsed by ``date.fromisoformat`` directly, and a ``NaN`` ratio reaching pydantic's
English report through the ``ValueError`` arm), and the AI door, whose LLM refusals
(``LLMUnavailable("provider error (…): …")``) were forwarded verbatim into the error envelope.

Why no guard caught it: ``tests/architecture/test_user_messages_are_zh_tw.py`` scans
``Issue(message=<literal>)`` — ``str(exc)`` is a call, not a literal, so it is invisible to a
static scan. This file is the behavioural guard: a matrix of malformed cells through EVERY
import door's real preview route, asserting on what the owner would read.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from portfolio_dash.data_ingestion.agents import AiInputResult
from portfolio_dash.data_ingestion.validate import Issue

_CJK = re.compile(r"[一-鿿]")
#: Fragments only CPython / pydantic write. Any of them in a 原因 cell is the leak.
_ENGLISH = re.compile(
    r"Invalid isoformat|ConversionSyntax|<class|validation error|Input should|"
    r"provider error|malformed response|no enabled model|^'[^']*'$")

#: kind -> header + malformed rows. Each row breaks ONE thing the parser owns.
_MATRIX: dict[str, tuple[str, list[str]]] = {
    "openings": ("account,symbol,shares,original_cost_total,build_date", [
        "tw_broker,2330,1000,500000,2026/13/45",      # impossible date
        "tw_broker,2330,1000,500000,",                # blank date
        "tw_broker,2330,abc,500000,2026-01-02",       # non-numeric shares
        "tw_broker,2330,,500000,2026-01-02",          # blank shares
        "tw_broker,2330,NaN,500000,2026-01-02",       # NaN shares
        "tw_broker,2330,1000,Infinity,2026-01-02",    # non-finite total
        "tw_broker,2330,1000,xx,2026-01-02",          # non-numeric total
    ]),
    "corporate_actions": ("account,date,kind,from_symbol,to_symbol,ratio_to,ratio_from", [
        "tw_broker,2026/13/01,SPLIT,2330,2330,2,1",
        "tw_broker,,SPLIT,2330,2330,2,1",
        "tw_broker,2026-06-10,SPLIT,2330,2330,abc,1",
        "tw_broker,2026-06-10,SPLIT,2330,2330,NaN,1",
        "tw_broker,2026-06-10,SPLIT,2330,2330,2,Infinity",
    ]),
    "cash": ("account,date,kind,ccy,amount", [
        "tw_broker,2026/13/01,DEPOSIT,TWD,100",
        "tw_broker,2026-06-10,DEPOSIT,TWD,abc",
        "tw_broker,2026-06-10,DEPOSIT,TWD,NaN",
    ]),
    "fx": ("account,date,from_ccy,from_amount,to_ccy,to_amount", [
        "schwab,2026/13/01,TWD,32000,USD,1000",
        "schwab,2026-06-10,TWD,abc,USD,1000",
    ]),
    "dividends": ("account,symbol,date,type,gross", [
        "tw_broker,2330,2026/13/01,CASH,100",
        "tw_broker,2330,2026-06-10,CASH,abc",
    ]),
    "transactions": ("account,symbol,side,date,shares,price", [
        "tw_broker,2330,BUY,2026/13/01,1000,600",
        "tw_broker,2330,BUY,2026-06-10,abc,600",
    ]),
}

#: A file missing a REQUIRED column, per kind — the 「'account'」 shape.
_MISSING: dict[str, str] = {
    "openings": "symbol,shares,original_cost_total,build_date\n2330,1000,500000,2026-01-02\n",
    "corporate_actions": "account,kind,from_symbol,to_symbol,ratio_to,ratio_from\n"
                         "tw_broker,SPLIT,2330,2330,2,1\n",
    "dividends": "account,date,type,gross\ntw_broker,2026-06-10,CASH,100\n",
}


def _messages(client: TestClient, kind: str, text: str) -> list[str]:
    r = client.post("/api/import/preview", json={"kind": kind, "csv_text": text})
    assert r.status_code == 200, r.text
    return [row["reason"] for row in r.json()["rows"]
            if row["status"] != "ok" and row["reason"]]


@pytest.mark.parametrize("kind", sorted(_MATRIX))
def test_no_parse_error_reaches_the_owner_in_english(
    api_client: TestClient, golden_db: sqlite3.Connection, kind: str
) -> None:
    header, rows = _MATRIX[kind]
    for line in rows:
        msgs = _messages(api_client, kind, f"{header}\n{line}\n")
        assert msgs, (kind, line)                      # the row IS refused …
        for m in msgs:                                 # … in the owner's language
            assert _CJK.search(m) and not _ENGLISH.search(m), (kind, line, m)


@pytest.mark.parametrize("kind", sorted(_MISSING))
def test_a_missing_column_is_named_in_a_sentence(
    api_client: TestClient, golden_db: sqlite3.Connection, kind: str
) -> None:
    msgs = _messages(api_client, kind, _MISSING[kind])
    assert msgs and all("缺少必填欄位" in m for m in msgs[:1]), msgs
    assert not any(_ENGLISH.search(m) for m in msgs), msgs


def test_the_opening_row_names_the_column_it_could_not_read(
    api_client: TestClient, golden_db: sqlite3.Connection
) -> None:
    (msg,) = _messages(api_client, "openings",
                       "account,symbol,shares,original_cost_total,build_date\n"
                       "tw_broker,2330,abc,500000,2026-01-02\n")
    assert msg == "股數（shares）不是數字：abc"


@pytest.mark.parametrize("raised", [
    Issue(kind="llm_unavailable", message="provider error (openrouter/x): 502 Bad Gateway"),
    Issue(kind="ai_not_activated", message="no enabled model configured for the input role"),
])
def test_the_ai_door_never_forwards_an_english_llm_refusal(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch, raised: Issue
) -> None:
    def _refuse(*_a: Any, **_k: Any) -> AiInputResult:
        return AiInputResult(error=raised)

    monkeypatch.setattr("portfolio_dash.api.routers.input_center.ai_agents_input", _refuse)
    r = api_client.post("/api/input/ai/preview", json={"text": "買 2330 一張"})
    assert r.status_code in (409, 503), r.text
    body = r.json()["error"]
    assert body["code"] == raised.kind
    assert _CJK.search(body["message"]) and not _ENGLISH.search(body["message"]), body


def test_a_chinese_llm_refusal_keeps_its_own_words(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_prefer_zh``'s rule: a sentence written for the owner is kept, not replaced."""
    own = Issue(kind="budget_exceeded", message="AI 額度用盡（本月上限 US$5）")

    def _refuse(*_a: Any, **_k: Any) -> AiInputResult:
        return AiInputResult(error=own)

    monkeypatch.setattr("portfolio_dash.api.routers.input_center.ai_agents_input", _refuse)
    r = api_client.post("/api/input/ai/preview", json={"text": "買 2330 一張"})
    assert r.status_code == 402 and r.json()["error"]["message"] == own.message
