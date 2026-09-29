"""DEF-084: a confession row (``unparsed_rows``) names the model and the usage row it came from.

Root cause (3a35454): ``data_ingestion/agents.py:558-570`` recorded the AI door's
``unparsed_rows`` capture with ``fail_log.record(conn, agent=…, outcome="unparsed_rows",
source_text=…, prompt=…, raw_output=…, error_reason=…)`` — no ``model``, no ``usage_id``.
The call had succeeded, so a model and a billed ``llm_usage`` row existed; the door simply
had no hold of either, because its completion seam returned the parsed value alone.
Demo: 6 rows (#499–#518) with model "" and usage_id null.

Why no test caught it: ``test_ai_union.py::test_confessed_unparsed_rows_are_recorded_once``
asserted the columns it was written for — outcome, agent, source text, raw output, the
prompt (added after the 2026-08-28 "zero-char prompt" finding) — and never model or usage.
Its fake completer returned a bare ``AiDraftList``, so there was no model in the test to
compare against: the seam's shape made the omission unobservable. A pin that names fields
certifies those fields only (the same lesson as the preview audit of 2026-08-27).

The guards below compare the confession row with what the SEAM itself writes about a reply,
and scan for every capture made outside the seam.
"""

import ast
import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import portfolio_dash
from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.agents import ai_agents_input
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.validate import CashPool
from portfolio_dash.shared import llm as llm_mod
from portfolio_dash.shared import llm_fail_log as fail_log
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.llm_config import (
    LLMRole,
    ModelConfig,
    add_topup,
    ensure_llm_seeded,
    set_role,
    upsert_model,
)

_PKG = Path(portfolio_dash.__file__).resolve().parent


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]
        self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5})()


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    ensure_llm_seeded(c)
    fail_log.ensure_table(c)
    upsert_model(c, ModelConfig(
        id="flash", model_alias="flash", provider="openrouter",
        model_name="google/gemini-2.5-flash-lite", api_key="test-key-not-a-credential",
        max_retries=0, input_price_per_mtok=Decimal("1"), output_price_per_mtok=Decimal("2"),
    ))
    set_role(c, LLMRole.DEFAULT, "flash")
    add_topup(c, Decimal("10"))
    seed_accounts(c)
    yield c
    c.close()


def _pool(account_id: str, ccy: Currency, **kw: object) -> CashPool:
    return CashPool(balance=Decimal("999999999"), low=Decimal("999999999"))


def _replies(monkeypatch: pytest.MonkeyPatch, *contents: str) -> None:
    queue = list(contents)
    monkeypatch.setattr(llm_mod.litellm, "supports_response_schema", lambda **kw: False)

    def completion(**kw: Any) -> _Resp:
        return _Resp(queue.pop(0))

    monkeypatch.setattr(llm_mod.litellm, "completion", completion)


_CONFESSION = json.dumps({"rows": [], "unparsed": [
    {"text": "8/2 TSLA call 權利金 300", "reason": "invalid symbol"},
    {"text": "8/3 ???", "reason": "invalid date"},
]})


def test_a_confession_names_its_model_and_usage_row(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-05 replayed through the REAL seam (only the provider is scripted)."""
    _replies(monkeypatch, _CONFESSION)
    ai_agents_input(conn, "8/2 TSLA call 權利金 300\n8/3 ???", pool=_pool,
                    today=date(2026, 9, 29))
    rows = fail_log.list_rows(conn)
    assert [r["outcome"] for r in rows] == ["unparsed_rows"]
    usage = conn.execute(
        "SELECT id, model FROM llm_usage WHERE agent='ai_agents_input'").fetchall()
    assert len(usage) == 1
    assert rows[0]["model"] == "google/gemini-2.5-flash-lite" == usage[0]["model"]
    assert rows[0]["usage_id"] == usage[0]["id"]


def test_a_confession_row_is_as_complete_as_the_seams_own_rows(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The class guard: every column the seam fills about a reply, the door fills too.

    The seam writes ``schema_mismatch`` for a reply with the wrong fields (then the retry
    answers with a confession). Both rows describe a billed reply, so both must be filled
    on the same columns — a future column the seam starts filling fails here on its own.
    """
    _replies(monkeypatch, json.dumps({"wrong": True}), _CONFESSION)
    ai_agents_input(conn, "8/2 TSLA", pool=_pool, today=date(2026, 9, 29))
    by = {r["outcome"]: r for r in fail_log.list_rows(conn)}
    assert set(by) == {"schema_mismatch", "unparsed_rows"}

    def filled(row: dict[str, Any]) -> set[str]:
        return {k for k, v in row.items() if v not in (None, "", 0)
                and k not in ("id", "created_at", "attempt", "truncated", "image_count",
                              "source_text", "outcome")}

    assert filled(by["schema_mismatch"]) <= filled(by["unparsed_rows"]), (
        filled(by["schema_mismatch"]) - filled(by["unparsed_rows"]))


def test_every_capture_outside_the_seam_names_model_and_usage() -> None:
    """``shared/llm.py`` also records refusals that happen BEFORE any model is chosen
    (budget / not activated), which legitimately carry neither. A capture anywhere else is
    by construction about a reply it received, so it must pass both keywords."""
    offenders, seen = [], 0
    for path in sorted(_PKG.rglob("*.py")):
        rel = path.relative_to(_PKG).as_posix()
        if rel in ("shared/llm.py", "shared/llm_fail_log.py"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "record"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in ("fail_log", "llm_fail_log")):
                seen += 1
                given = {kw.arg for kw in node.keywords}
                if not {"model", "usage_id"} <= given:
                    offenders.append(f"{rel}:{node.lineno}")
    assert seen >= 1, "the scan found no capture at all — it is not looking where it should"
    assert offenders == []
