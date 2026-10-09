"""The AI-extraction corpus scorer compares ``stated_amount`` too (owner 2026-09-30, item 14b).

DEF-036 gave a transaction draft a TRANSCRIBED field, ``stated_amount`` — the statement's own
成交金額, which the model must COPY and never compute — and gave the corpus's txn rows a
``stated_amount`` + ``amount_check`` (mismatch / consistent / absent) beside ``fields``.
``tests/data_ingestion/test_ai_extraction_corpus.py`` replays those EXPECTED drafts through the
door, which proves the door's arithmetic; the half it leaves to the live runner is whether
the MODEL copies the amount. ``scripts/ai_extraction_eval.py`` compared only CSV columns, and
``stated_amount`` is deliberately not one — so a model that computed 100 × 46 = 4,600 instead
of copying 50,000 (the exact failure DEF-036 exists to catch) scored a clean PASS.

These tests drive the scorer with the REAL door and a stub model (no LLM call; pytest-socket
bans the network): a copied amount scores clean, a computed / dropped / invented one is a
miss on ``stated_amount`` AND on the door's verdict, and a case that asserts no amount is
scored exactly as before.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.agents import (
    AiDraftList,
    AiInputResult,
    TxnDraft,
    ai_agents_input,
)
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.data_ingestion.validate import CashPool
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from scripts import ai_extraction_eval as script
from tests.ai_completion import completing

CORPUS = Path(__file__).resolve().parents[1] / "golden" / "ai_extraction" / "cases.json"


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    seed_accounts(c)
    yield c
    c.close()


def _case(case_id: str) -> dict[str, Any]:
    cases: list[dict[str, Any]] = json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]
    return next(c for c in cases if c["id"] == case_id)


def _pool(account_id: str, ccy: Currency, **kw: object) -> CashPool:
    return CashPool(balance=Decimal("999999999"), low=Decimal("999999999"))


def _run(conn: sqlite3.Connection, case: dict[str, Any], stated: str | None) -> AiInputResult:
    """The case's own expected fields, with the model's ``stated_amount`` set to *stated*."""
    fields = case["expect"]["rows"][0]["fields"]
    upsert_instrument(conn, Instrument(symbol=fields["symbol"], market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Financials",
                                       name=fields["symbol"]))
    draft = TxnDraft(
        account_id=fields["account"], symbol=fields["symbol"], side=Side(fields["side"]),
        date=date.fromisoformat(fields["date"]), shares=Decimal(fields["shares"]),
        price=Decimal(fields["price"]),
        stated_amount=None if stated is None else Decimal(stated))

    def _model(prompt: str, schema: type, *, agent: str, conn: object = None,
               images: list[bytes] | None = None,
               model_override: str | None = None) -> AiDraftList:
        return AiDraftList(rows=[draft])

    return ai_agents_input(conn, case["input"], pool=_pool, completer=completing(_model),
                           today=date.fromisoformat(case.get("today", "2026-08-18")))


def test_a_copied_amount_scores_clean(conn: sqlite3.Connection) -> None:
    case = _case("amount-contradiction-measured")
    tally = script.Tally()
    misses = script.score_case(case, _run(conn, case, "50000"), tally)
    assert misses == []
    assert tally.amount == {"stated_amount": {"hit": 1, "miss": 0},
                            "amount_check": {"hit": 1, "miss": 0}}
    # the CSV fields are scored exactly as before, and the amount is NOT one of them
    assert tally.stats["hit"] == len(case["expect"]["rows"][0]["fields"])
    assert tally.stats["miss"] == 0


def test_a_computed_amount_is_a_miss_on_the_value_and_on_the_verdict(
    conn: sqlite3.Connection,
) -> None:
    """DEF-036's failure: 100 × 46 = 4,600 computed instead of the stated 50,000 copied —
    the row then LOOKS consistent, which is exactly what hides the contradiction."""
    case = _case("amount-contradiction-measured")
    tally = script.Tally()
    misses = script.score_case(case, _run(conn, case, "4600"), tally)
    assert "txn[0].stated_amount: want '50000' got '4600'" in misses
    assert "txn[0].amount_check: want 'mismatch' got 'consistent'" in misses
    assert tally.amount["stated_amount"]["miss"] == 1
    assert tally.amount["amount_check"]["miss"] == 1
    assert tally.stats["miss"] == 0  # every CSV column was right — only the copy was wrong


def test_a_dropped_amount_is_a_miss(conn: sqlite3.Connection) -> None:
    case = _case("amount-consistent-gross")
    misses = script.score_case(case, _run(conn, case, None), script.Tally())
    assert "txn[0].stated_amount: want '19000' got None" in misses
    assert "txn[0].amount_check: want 'consistent' got 'absent'" in misses


def test_an_invented_amount_is_a_miss(conn: sqlite3.Connection) -> None:
    """No total in the text: the model must leave it absent, never compute 30 × 960."""
    case = _case("amount-absent")
    misses = script.score_case(case, _run(conn, case, "28800"), script.Tally())
    assert "txn[0].stated_amount: want None got '28800'" in misses
    assert "txn[0].amount_check: want 'absent' got 'consistent'" in misses


def test_the_value_is_compared_as_a_number(conn: sqlite3.Connection) -> None:
    """``50000.00`` is the stated 50,000 — the same Decimal rule as the numeric CSV fields."""
    case = _case("amount-contradiction-measured")
    assert script.score_case(case, _run(conn, case, "50000.00"), script.Tally()) == []


def test_a_case_that_asserts_no_amount_is_scored_as_before(conn: sqlite3.Connection) -> None:
    """Older cases carry no ``amount_check``: nothing about amounts is asserted or counted,
    whatever the model transcribed."""
    case = _case("amount-contradiction-measured")
    row = dict(case["expect"]["rows"][0])
    row.pop("stated_amount")
    row.pop("amount_check")
    legacy = {**case, "expect": {**case["expect"], "rows": [row]}}
    tally = script.Tally()
    assert script.score_case(legacy, _run(conn, case, "4600"), tally) == []
    assert tally.amount == {"stated_amount": {"hit": 0, "miss": 0},
                            "amount_check": {"hit": 0, "miss": 0}}
