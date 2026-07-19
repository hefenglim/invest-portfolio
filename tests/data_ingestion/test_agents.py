import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.data_ingestion.agents import (
    AiDraft,
    AiDraftList,
    Completer,
    ai_agents_input,
)
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


# --- FU-D41: soft symbol-format check per account market (post-parse) -------------------


def _one_draft_completer(account_id: str, symbol: str) -> Completer:
    """A completer emitting one BUY draft for *account_id*/*symbol* (format-check probes)."""

    def _f(
        prompt: str, schema: type, *, agent: str, conn: object = None,
        images: list[bytes] | None = None, model_override: str | None = None,
    ) -> AiDraftList:
        return AiDraftList(drafts=[AiDraft(
            account_id=account_id, symbol=symbol, side=Side.BUY,
            date=date(2026, 6, 1), shares=Decimal("10"), price=Decimal("76"),
        )])

    return _f


def test_ai_input_flags_us_ticker_on_tw_account_as_format_warning(
    conn: sqlite3.Connection,
) -> None:
    # The owner's bug: 「前天聯電買入1張，76元」 parsed to the US ADR ticker "UMC" on a
    # tw_broker row. The soft check appends a WARNING (needs_confirm — never blocks) and
    # never rewrites the symbol; the row also keeps its unregistered-symbol hard issue.
    _setup(conn)
    result = ai_agents_input(conn, "前天聯電買入1張，76元",
                             completer=_one_draft_completer("tw_broker", "UMC"))
    row = result.preview.rows[0]
    warn = [i for i in row.issues if i.kind == "symbol_format_mismatch"]
    assert len(warn) == 1
    assert warn[0].needs_confirm is True  # warning severity — surfaces, never blocks
    assert warn[0].message == "代號格式與帳戶市場不符，請確認"
    # the symbol is NOT rewritten by the check (the real lookup stays the authority).
    assert row.payload.get("symbol") == "UMC"


def test_ai_input_numeric_tw_symbol_is_clean_of_format_warning(
    conn: sqlite3.Connection,
) -> None:
    # 2303 (unregistered but correctly-shaped TWSE code) must NOT trigger the format
    # warning — only the ordinary unregistered-symbol issue applies.
    _setup(conn)
    result = ai_agents_input(conn, "前天聯電買入1張，76元",
                             completer=_one_draft_completer("tw_broker", "2303"))
    row = result.preview.rows[0]
    assert not any(i.kind == "symbol_format_mismatch" for i in row.issues)


def test_ai_input_flags_cjk_symbol_on_us_account(conn: sqlite3.Connection) -> None:
    # The US-account mirror: a CJK (or numeric) symbol on a USD account is format-flagged.
    _setup(conn)
    result = ai_agents_input(conn, "嘉信買台積電 5 股 @210",
                             completer=_one_draft_completer("schwab", "台積電"))
    row = result.preview.rows[0]
    assert any(i.kind == "symbol_format_mismatch" for i in row.issues)


def test_ai_input_registered_clean_row_has_no_format_warning(
    conn: sqlite3.Connection,
) -> None:
    # The happy path (registered 2330 on tw_broker) stays byte-identical: zero issues.
    _setup(conn)
    result = ai_agents_input(conn, "buy 1000 2330 @600", completer=_good_completer)
    assert result.preview.rows[0].issues == []
