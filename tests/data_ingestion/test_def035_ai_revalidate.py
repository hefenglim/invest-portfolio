"""DEF-035 (functional test manual F-04 ①, 2026-09-23): an AI draft is editable, per row.

The draft table used to be text only — a mis-read share count could be unticked and re-typed
in the manual form, never corrected in place. Editing needs a SERVER round trip that is not
a second (paid) parse: the edited drafts go back through the SAME post-parse pipeline the
model's output went through — per-row preview, FU-D41 format check, the DEF-036 amount
check, the AI-D21 cash labels — and come back with a regenerated commit CSV. What the
preview shows after an edit is therefore still what the commit re-derives (AI-D18), and the
browser never assembles a CSV line of its own.
"""

import sqlite3
from datetime import date
from decimal import Decimal

from portfolio_dash.data_ingestion.agents import (
    AiDraftList,
    CashDraft,
    TxnDraft,
    ai_agents_input,
    revalidate_ai_drafts,
)
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.store import upsert_instrument
from portfolio_dash.data_ingestion.validate import CashPool
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side


def _pool(account_id: str, ccy: Currency, **kw: object) -> CashPool:
    return CashPool(balance=Decimal("999999999"), low=Decimal("999999999"))


def _setup(conn: sqlite3.Connection) -> None:
    seed_accounts(conn)
    upsert_instrument(conn, Instrument(symbol="2884", market=Market.TW,
                                       quote_ccy=Currency.TWD, sector="Financials",
                                       name="玉山金"))


def _txn(shares: str, price: str, stated: str | None = None) -> TxnDraft:
    return TxnDraft(account_id="tw_broker", symbol="2884", side=Side.BUY,
                    date=date(2026, 9, 22), shares=Decimal(shares), price=Decimal(price),
                    stated_amount=None if stated is None else Decimal(stated))


def test_an_edited_share_count_reprices_and_regenerates_the_csv(
    conn: sqlite3.Connection,
) -> None:
    _setup(conn)
    res = revalidate_ai_drafts(conn, AiDraftList(rows=[_txn("1000", "46.45")]), pool=_pool)
    row = res.previews["transactions"].rows[0]
    assert row.payload["quantity"] == "1000"
    # 1000 × 46.45 = 46,450 × 0.1425% = 66.19 → floor 66 (the engine, not the browser)
    assert row.fee == Decimal("66")
    assert "tw_broker,2884,BUY,2026-09-22,1000,46.45" in res.csv_texts["transactions"]
    # the drafts come back, in row order, so the next edit starts from the server's copy
    back = res.drafts["transactions"]
    assert all(isinstance(d, TxnDraft) for d in back)
    assert [d.shares for d in back if isinstance(d, TxnDraft)] == [Decimal("1000")]


def test_the_amount_check_reruns_on_the_edited_values(conn: sqlite3.Connection) -> None:
    """Fixing the price the model mis-read clears DEF-036's flag — the check is not a
    one-shot property of the LLM's first answer."""
    _setup(conn)
    flagged = revalidate_ai_drafts(
        conn, AiDraftList(rows=[_txn("100", "46", "50000")]), pool=_pool)
    assert flagged.previews["transactions"].rows[0].payload.get("amount_mismatch") == "1"
    fixed = revalidate_ai_drafts(
        conn, AiDraftList(rows=[_txn("1000", "50", "50000")]), pool=_pool)
    row = fixed.previews["transactions"].rows[0]
    assert "amount_mismatch" not in row.payload
    assert not [i for i in row.issues if i.kind == "amount_mismatch"]


def test_cash_rows_keep_their_label_and_sign_after_an_edit(conn: sqlite3.Connection) -> None:
    """The AI-D21 guard (zh label + explicit sign) is part of the pipeline, so a
    revalidated cash row carries it exactly as a first parse does."""
    _setup(conn)
    edited = CashDraft(account_id="tw_broker", date=date(2026, 9, 22),
                       cash_kind="BROKER_FEE", ccy="TWD", amount=Decimal("25"))
    res = revalidate_ai_drafts(conn, AiDraftList(rows=[edited]), pool=_pool)
    payload = res.previews["cash"].rows[0].payload
    assert payload["kind_label"] == "券商費用" and payload["sign"] == "-1"


def test_revalidation_calls_no_model_and_matches_a_first_parse(
    conn: sqlite3.Connection,
) -> None:
    """Same drafts → same preview and CSV whether they came from the model or an edit:
    one pipeline, two entry points. (The revalidation takes no completer at all, so it
    cannot spend tokens.)"""
    _setup(conn)
    drafts = [_txn("100", "46", "50000")]

    def _c(prompt: str, schema: type, *, agent: str, conn: object = None,
           images: list[bytes] | None = None,
           model_override: str | None = None) -> AiDraftList:
        return AiDraftList(rows=list(drafts))

    first = ai_agents_input(conn, "x", pool=_pool, completer=_c, today=date(2026, 9, 23))
    again = revalidate_ai_drafts(conn, AiDraftList(rows=list(drafts)), pool=_pool)
    assert first.csv_texts == again.csv_texts
    a = first.previews["transactions"].rows[0]
    b = again.previews["transactions"].rows[0]
    assert (a.payload, a.fee, a.tax, [i.kind for i in a.issues]) == (
        b.payload, b.fee, b.tax, [i.kind for i in b.issues])
