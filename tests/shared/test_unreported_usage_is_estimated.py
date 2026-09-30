"""A reply the provider did not meter is booked as an ESTIMATE, never as a free call.

Owner ruling 2026-09-30 on the verifier's R10 observation ④ (「gemini 截斷 JSON 的重試在
llm-usage 記 0 tokens／$0（供應商未回報用量）」). Measured on demo before the fix: 20 of 1,113
``llm_usage`` rows read 0 tokens / $0, every one ``google/gemini-2.5-flash-lite`` via
OpenRouter; 18 were a reply cut off mid-string (as short as 12 characters — not a
``max_tokens`` stop) that failed to parse, 2 a successful ``ai_instrument_resolve``. The
provider sent no usage block and LiteLLM filled in zeros, which every seam read as measured.

Root cause (91ec1b0): three seams read ``usage.prompt_tokens`` / ``usage.completion_tokens``
verbatim — ``shared/llm.py:516`` (structured), ``shared/llm.py:730-731`` (free text) and
``api/routers/llm_settings.py:467-468`` (the connection test, which also skipped the row
entirely when ``usage`` was None). A call always has prompt tokens, so both counts at zero
means "not reported"; the three seams now go through ``llm.metered_usage``, which counts the
prompt and the reply locally and marks the row ``usage_estimated``.

Why no test caught it: every fake provider in the suite answers with a usage block. The
quota, the per-agent totals and (since the same round) the run costs are sums of these rows,
so a zero was not visible anywhere — it was simply absent from every total.
"""

import ast
import csv
import io
import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from pytest_socket import disable_socket, enable_socket

import portfolio_dash
from portfolio_dash.api.deps import get_conn
from portfolio_dash.api.errors import register_error_handlers
from portfolio_dash.api.routers import llm_settings
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.export.usage import build_llm_usage_csv
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared import llm_fail_log as fail_log
from portfolio_dash.shared.llm_config import (
    LLMRole,
    LLMUnavailable,
    ModelConfig,
    add_topup,
    quota_remaining,
    set_role,
    upsert_model,
)

_PKG = Path(portfolio_dash.__file__).resolve().parent

#: #519's reply on demo, verbatim: 29 characters, cut inside the title string.
_CUT = '{\n  "title": "玉山金(2884) 股價回升，'


class _Card(BaseModel):
    title: str
    summary: str


class _NoUsage:
    """A LiteLLM response with no usage block at all."""

    def __init__(self, content: str) -> None:
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]
        self.usage = None


class _ZeroUsage(_NoUsage):
    """What LiteLLM hands back when the provider omitted usage: a Usage of zeros."""

    def __init__(self, content: str) -> None:
        super().__init__(content)
        self.usage = type("U", (), {"prompt_tokens": 0, "completion_tokens": 0})()


class _Reported(_NoUsage):
    def __init__(self, content: str) -> None:
        super().__init__(content)
        self.usage = type("U", (), {"prompt_tokens": 1200, "completion_tokens": 80})()


def _model() -> ModelConfig:
    return ModelConfig(
        id="gemini-m", model_alias="gemini-m", provider="openrouter",
        model_name="google/gemini-m", api_key="test-key-not-a-credential", max_retries=0,
        input_price_per_mtok=Decimal("0.10"), output_price_per_mtok=Decimal("0.40"),
    )


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    fail_log.ensure_table(c)
    upsert_model(c, _model())
    set_role(c, LLMRole.DEFAULT, "gemini-m")
    add_topup(c, Decimal("10"))
    yield c
    c.close()


def _script(monkeypatch: pytest.MonkeyPatch, *replies: object) -> None:
    queue = list(replies)
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: True)
    monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: queue.pop(0))


def _usage_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, input_tokens, output_tokens, cost, usage_estimated FROM llm_usage "
        "ORDER BY id"
    ).fetchall()


@pytest.mark.parametrize("reply", [_NoUsage, _ZeroUsage], ids=["no-usage", "zero-usage"])
def test_a_cut_off_reply_is_billed_as_an_estimate_not_free(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, reply: type[_NoUsage]
) -> None:
    """#519 / #528 replayed: two cut-off replies, no usage. Both rows are estimates > 0."""
    _script(monkeypatch, reply(_CUT), reply(_CUT))
    before = quota_remaining(conn)
    with pytest.raises(LLMUnavailable):
        llm_mod.complete_structured_meta("寫一張卡", _Card, agent="insight_generate", conn=conn)
    rows = _usage_rows(conn)
    assert len(rows) == 2
    for row in rows:
        assert row["usage_estimated"] == 1
        assert row["input_tokens"] > 0 and row["output_tokens"] > 0
        assert Decimal(row["cost"]) > 0
    # The quota moved by exactly what the ledger booked — the calls are no longer free.
    assert before - quota_remaining(conn) == sum(Decimal(r["cost"]) for r in rows)
    # Each captured failure still points at the row it was billed on.
    assert {r["usage_id"] for r in fail_log.list_rows(conn)} == {r["id"] for r in rows}


def test_a_reported_usage_is_booked_exactly_as_reported(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(monkeypatch, _Reported('{"title": "a", "summary": "b"}'))
    done = llm_mod.complete_structured_meta("寫", _Card, agent="insight_generate", conn=conn)
    [row] = _usage_rows(conn)
    assert (row["input_tokens"], row["output_tokens"], row["usage_estimated"]) == (1200, 80, 0)
    assert Decimal(row["cost"]) == Decimal("0.000152")  # 1200 × 0.10 + 80 × 0.40, per M
    assert done.usage_estimated is False and done.cost == Decimal("0.000152")


def test_a_successful_reply_without_usage_carries_the_estimate_and_says_so(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#221 / #295 on demo: an ``ai_instrument_resolve`` that parsed, with no usage."""
    _script(monkeypatch, _NoUsage('{"title": "a", "summary": "b"}'))
    done = llm_mod.complete_structured_meta("辨識", _Card, agent="ai_instrument_resolve",
                                            conn=conn)
    [row] = _usage_rows(conn)
    assert done.usage_estimated is True and row["usage_estimated"] == 1
    assert (done.tokens_in, done.tokens_out) == (row["input_tokens"], row["output_tokens"])
    assert done.cost == Decimal(row["cost"]) > 0


def test_the_free_text_path_estimates_too(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(monkeypatch, _ZeroUsage("今日組合小幅上漲。"))
    done = llm_mod.complete_text("寫一句摘要", agent="digest_note", conn=conn)
    [row] = _usage_rows(conn)
    assert done.usage_estimated is True and row["usage_estimated"] == 1
    assert row["input_tokens"] > 0 and row["output_tokens"] > 0 and Decimal(row["cost"]) > 0


@pytest.fixture
def client(conn: sqlite3.Connection) -> Iterator[TestClient]:
    enable_socket()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(llm_settings.router, prefix="/api")
    app.dependency_overrides[get_conn] = lambda: conn
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        disable_socket(allow_unix_socket=True)


def test_the_connection_test_books_its_call_even_without_usage(
    conn: sqlite3.Connection, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ping used to write NO row when ``usage`` was None — a paid call left off entirely."""
    _script(monkeypatch, _NoUsage("pong"))
    assert client.post("/api/llm/models/gemini-m/test").json()["ok"] is True
    [row] = _usage_rows(conn)
    assert row["usage_estimated"] == 1 and Decimal(row["cost"]) > 0


def test_the_request_ledger_marks_the_estimate(
    conn: sqlite3.Connection, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(monkeypatch, _NoUsage(_CUT), _Reported(_CUT))
    with pytest.raises(LLMUnavailable):
        llm_mod.complete_structured_meta("寫", _Card, agent="insight_generate", conn=conn)
    rows = client.get("/api/llm/requests").json()["rows"]
    assert [r["estimated"] for r in rows] == [False, True]  # newest first
    assert rows[1]["tokens_in"] > 0 and Decimal(rows[1]["cost_usd"]) > 0


def test_the_usage_export_marks_the_estimate(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The llm-usage CSV (I-01, H-02's reconciliation file) says which rows are estimates:
    a trailing ``usage_estimated`` column, so every earlier column stays where it was."""
    _script(monkeypatch, _Reported(_CUT), _NoUsage(_CUT))
    with pytest.raises(LLMUnavailable):
        llm_mod.complete_structured_meta("寫", _Card, agent="insight_generate", conn=conn)
    lines = build_llm_usage_csv(conn, frm=None, to=None).content.decode("utf-8-sig")
    table = list(csv.reader(io.StringIO(lines)))
    assert table[0][-1] == "usage_estimated"
    assert [row[-1] for row in table[1:]] == ["0", "1"]


def test_the_estimate_is_local_and_never_breaks_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LiteLLM's bundled tokenizer (pytest-socket bans the network here); a tokenizer that
    raises degrades to one token per character instead of failing the call."""
    assert 0 < llm_mod.estimate_tokens(_CUT) <= len(_CUT)
    assert llm_mod.estimate_tokens("") == 0

    def _boom(**_kw: Any) -> int:
        raise RuntimeError("tokenizer unavailable")

    monkeypatch.setattr(llm_mod.litellm, "token_counter", _boom)
    assert llm_mod.estimate_tokens(_CUT) == len(_CUT)


# --- the class: every seam that books a call goes through metered_usage ------------------

def _py_files() -> list[Path]:
    return sorted(p for p in _PKG.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_seam_reads_the_provider_counts_directly() -> None:
    """``prompt_tokens`` / ``completion_tokens`` read as attributes anywhere in the package is
    the old shape: a count taken on trust, zero when the provider sent none. The only
    reader is ``metered_usage`` (by name, through ``getattr``)."""
    hits = []
    for path in _py_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in {
                "prompt_tokens", "completion_tokens"
            }:
                hits.append(f"{path.relative_to(_PKG).as_posix()}:{node.lineno} .{node.attr}")
    assert hits == []


def test_every_usage_row_states_whether_it_was_reported() -> None:
    """Every ``log_usage`` call passes ``estimated=`` — a new seam cannot book a call
    without deciding whether the provider metered it. Found by scan, not listed."""
    calls: list[str] = []
    missing: list[str] = []
    for path in _py_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(
                node.func, "id", None)
            if name != "log_usage":
                continue
            where = f"{path.relative_to(_PKG).as_posix()}:{node.lineno}"
            calls.append(where)
            if not any(k.arg == "estimated" for k in node.keywords):
                missing.append(where)
    assert len(calls) == 3, calls  # structured, free text, connection test
    assert missing == []
