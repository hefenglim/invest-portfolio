import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.agents import AiDraft, AiDraftList, ai_agents_input
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.csv_import import write_transaction_row
from portfolio_dash.data_ingestion.preview import commit_preview
from portfolio_dash.data_ingestion.store import list_transactions, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.llm import AINotActivated, LLMBudgetExceeded, LLMUnavailable
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side


def _setup(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(
        conn,
        Instrument(
            symbol="2330",
            market=Market.TW,
            quote_ccy=Currency.TWD,
            sector="Tech",
            name="台積電",
        ),
    )


def _good_completer(
    prompt: str,
    schema: type,
    *,
    agent: str,
    conn: object = None,
    images: list[bytes] | None = None,
    model_override: str | None = None,
) -> AiDraftList:
    return AiDraftList(
        drafts=[
            AiDraft(
                account_id="tw_broker",
                symbol="2330",
                side=Side.BUY,
                date=date(2026, 6, 1),
                shares=Decimal("1000"),
                price=Decimal("600"),
            )
        ]
    )


def test_ai_input_builds_preview_with_fee_no_write(conn: sqlite3.Connection) -> None:
    _setup(conn)
    result = ai_agents_input(conn, "buy 1000 2330 @600", completer=_good_completer)
    p = result.preview
    assert len(p.rows) == 1 and p.rows[0].fee == Decimal("855")
    assert list_transactions(conn, account_id="tw_broker") == []  # not written


def test_ai_input_commit_writes(conn: sqlite3.Connection) -> None:
    _setup(conn)
    result = ai_agents_input(conn, "buy 1000 2330 @600", completer=_good_completer)
    commit_preview(conn, result.preview, accept={0}, writer=write_transaction_row)
    assert len(list_transactions(conn, account_id="tw_broker")) == 1


@pytest.mark.parametrize(
    "exc, kind",
    [
        (LLMUnavailable("down"), "llm_unavailable"),
        (AINotActivated("off"), "ai_not_activated"),
        (LLMBudgetExceeded("broke"), "budget_exceeded"),
    ],
)
def test_ai_input_degrades_with_kind(
    conn: sqlite3.Connection, exc: Exception, kind: str
) -> None:
    _setup(conn)

    def boom(
        prompt: str, schema: type, *, agent: str, conn: object = None,
        images: list[bytes] | None = None, model_override: str | None = None,
    ) -> AiDraftList:
        raise exc

    result = ai_agents_input(conn, "buy ...", completer=boom)
    assert result.preview.rows[0].issues[0].kind == kind


def test_ai_input_prompt_carries_live_account_catalog(conn: sqlite3.Connection) -> None:
    # The model can only use VALID account ids if the prompt lists them — without
    # the catalog it guessed ("嘉信" → a made-up charles_schwab) and every
    # non-example account failed validation with "unknown account" (2026-07-05,
    # found live on the test instance).
    _setup(conn)
    seen: dict[str, str] = {}

    def spy_completer(
        prompt: str, schema: type, *, agent: str, conn: object = None,
        images: list[bytes] | None = None, model_override: str | None = None,
    ) -> AiDraftList:
        seen["prompt"] = prompt
        return AiDraftList(drafts=[])

    ai_agents_input(conn, "在嘉信買 10 股 AAPL @211.40", completer=spy_completer)
    prompt = seen["prompt"]
    assert "<accounts>" in prompt
    for account_id in ("tw_broker", "schwab", "moomoo_my_us", "moomoo_my_my"):
        assert account_id in prompt


def test_ai_input_prompt_carries_today_anchor(conn: sqlite3.Connection) -> None:
    # Yearless dates ("7/3") must resolve against a known today — the prompt now
    # carries <today> fed by the router's clock (audit §2.7).
    _setup(conn)
    seen: dict[str, str] = {}

    def spy_completer(
        prompt: str, schema: type, *, agent: str, conn: object = None,
        images: list[bytes] | None = None, model_override: str | None = None,
    ) -> AiDraftList:
        seen["prompt"] = prompt
        return AiDraftList(drafts=[])

    ai_agents_input(conn, "7/3 買 2330", completer=spy_completer, today=date(2026, 7, 5))
    assert "<today>2026-07-05</today>" in seen["prompt"]
    assert "recent PAST occurrence" in seen["prompt"]  # rule text wraps across a newline


def test_prompt_is_centralized_in_official_templates(conn: sqlite3.Connection) -> None:
    # FU-D20 drift guard: agents.py no longer owns the prompt body — it imports the
    # code-owned constant from llm_insight.official_templates.
    from portfolio_dash.data_ingestion import agents as agents_mod
    from portfolio_dash.llm_insight.official_templates import AI_INPUT_PROMPT_BODY

    assert agents_mod._PROMPT is AI_INPUT_PROMPT_BODY


def test_ai_input_forwards_images_and_model_alias_to_completer(
    conn: sqlite3.Connection,
) -> None:
    # The router-supplied screenshot bytes + per-run model alias must reach the completion
    # callable (which then routes to the vision role / puts the alias at the chain head).
    _setup(conn)
    seen: dict[str, object] = {}

    def spy(
        prompt: str, schema: type, *, agent: str, conn: object = None,
        images: list[bytes] | None = None, model_override: str | None = None,
    ) -> AiDraftList:
        seen["images"] = images
        seen["model_override"] = model_override
        return AiDraftList(drafts=[])

    ai_agents_input(
        conn, "", completer=spy, images=[b"\x89PNG\r\n\x1a\nDATA"], model_alias="my-vision",
    )
    assert seen["images"] == [b"\x89PNG\r\n\x1a\nDATA"]
    assert seen["model_override"] == "my-vision"
