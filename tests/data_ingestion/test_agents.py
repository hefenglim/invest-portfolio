import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.agents import AiDraft, AiDraftList, ai_agents_input
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.csv_import import write_transaction_row
from portfolio_dash.data_ingestion.preview import commit_preview
from portfolio_dash.data_ingestion.store import list_transactions, upsert_instrument
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.llm import LLMUnavailable
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
    pricing: object = None,
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
    p = ai_agents_input(conn, "buy 1000 2330 @600", completer=_good_completer)
    assert len(p.rows) == 1 and p.rows[0].fee == Decimal("855")
    assert list_transactions(conn, account_id="tw_broker") == []  # not written


def test_ai_input_commit_writes(conn: sqlite3.Connection) -> None:
    _setup(conn)
    p = ai_agents_input(conn, "buy 1000 2330 @600", completer=_good_completer)
    commit_preview(conn, p, accept={0}, writer=write_transaction_row)
    assert len(list_transactions(conn, account_id="tw_broker")) == 1


def test_ai_input_llm_unavailable_no_crash(conn: sqlite3.Connection) -> None:
    _setup(conn)

    def boom(
        prompt: str,
        schema: type,
        *,
        agent: str,
        conn: object = None,
        pricing: object = None,
    ) -> AiDraftList:
        raise LLMUnavailable("down")

    p = ai_agents_input(conn, "buy ...", completer=boom)
    assert any(i.kind == "llm_unavailable" for i in p.rows[0].issues)
