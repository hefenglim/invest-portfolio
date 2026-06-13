"""AI Agents Input: parse natural-language transaction text into a preview."""

import sqlite3
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.data_ingestion.csv_import import txn_preview_row
from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow
from portfolio_dash.data_ingestion.validate import Issue, TxnInput
from portfolio_dash.shared.llm import LLMError, complete_structured
from portfolio_dash.shared.models.enums import Side


class AiDraft(BaseModel):
    """One transaction extracted from user text by the LLM."""

    account_id: str
    symbol: str
    side: Side
    date: date
    shares: Decimal
    price: Decimal
    daytrade: bool = False
    is_etf: bool = False
    note: str | None = None


class AiDraftList(BaseModel):
    """Structured LLM output: a list of extracted transaction drafts."""

    drafts: list[AiDraft]


class AiMeta(BaseModel):
    """Provenance of the LLM run that produced a preview (latest usage row)."""

    model: str | None = None
    via: str = "litellm"
    cost_usd: Decimal | None = None


class AiInputResult(BaseModel):
    """Bundle returned by :func:`ai_agents_input`: preview + meta + commit CSV."""

    preview: ImportPreview
    meta: AiMeta
    csv_text: str = ""


def _latest_meta(conn: sqlite3.Connection) -> AiMeta:
    """Read the most recent ``llm_usage`` row for the AI-input agent into meta."""
    row = conn.execute(
        "SELECT model, cost FROM llm_usage WHERE agent='ai_agents_input' "
        "ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return AiMeta()
    return AiMeta(model=row["model"], cost_usd=Decimal(row["cost"]))


def _drafts_to_csv(drafts: list[AiDraft]) -> str:
    """Render drafts as canonical transaction CSV for /api/import/commit."""
    lines = ["account,symbol,side,date,shares,price,note"]
    for d in drafts:
        lines.append(
            f"{d.account_id},{d.symbol},{d.side.value},{d.date.isoformat()},"
            f"{d.shares},{d.price},{d.note or ''}"
        )
    return "\n".join(lines) + "\n"


Completer = Callable[..., AiDraftList]

_PROMPT = (
    "<task>Extract stock transactions from the user's text into JSON.</task>\n"
    '<schema>{{"drafts": [{{"account_id","symbol","side":"BUY|SELL","date":"YYYY-MM-DD",\n'
    '"shares","price","daytrade":false,"is_etf":false,"note"}}]}}</schema>\n'
    "<example_input>在元大買 10 股 2330 @ 600</example_input>\n"
    '<example_output>{{"drafts":[{{"account_id":"tw_broker","symbol":"2330","side":"BUY",\n'
    '"date":"2026-06-01","shares":"10","price":"600"}}]}}</example_output>\n'
    "<rules>Return JSON only, no prose. Use the account ids the system knows.</rules>\n"
    "<user_text>{text}</user_text>"
)


def ai_agents_input(
    conn: sqlite3.Connection,
    text: str,
    *,
    completer: Completer | None = None,
) -> AiInputResult:
    """Extract transactions from natural-language *text* and return a preview.

    Calls the LLM (via *completer*) to parse the user's free-form text into
    structured drafts, then feeds each draft through the same validate/fee-compute
    pipeline used by the CSV importer.  The result is an :class:`ImportPreview`
    that the caller inspects and optionally commits.

    The LLM is **never** called synchronously on page load — callers invoke this
    explicitly (manual trigger or route handler) and commit via
    :func:`~preview.commit_preview`.

    Args:
        conn:      Active SQLite connection (schema in place, accounts seeded).
        text:      Free-form user text describing one or more transactions.
        completer: Injectable LLM callable. Defaults to ``None``, resolved at call
                   time to :func:`~shared.llm.complete_structured` via module lookup
                   (so ``monkeypatch.setattr`` on the module attribute takes effect).
                   Replaced with a mock in tests.
    Returns:
        :class:`AiInputResult` bundling the :class:`ImportPreview` (one
        :class:`PreviewRow` per extracted draft, or a single degradation row when
        the LLM call fails), the latest-run :class:`AiMeta`, and a commit-ready CSV.
    """
    completer = completer or complete_structured
    try:
        result = completer(
            _PROMPT.format(text=text),
            AiDraftList,
            agent="ai_agents_input",
            conn=conn,
        )
    except LLMError as exc:
        return AiInputResult(
            preview=ImportPreview(
                rows=[
                    PreviewRow(
                        index=0,
                        raw={"text": text},
                        issues=[Issue(kind=exc.kind, message=str(exc))],
                    )
                ]
            ),
            meta=AiMeta(),
            csv_text="",
        )

    rows: list[PreviewRow] = []
    for idx, d in enumerate(result.drafts):
        inp = TxnInput(
            account_id=d.account_id,
            symbol=d.symbol,
            side=d.side,
            quantity=d.shares,
            price=d.price,
            trade_date=d.date,
            daytrade=d.daytrade,
            is_etf=d.is_etf,
            note=d.note,
        )
        rows.append(txn_preview_row(conn, idx, {"text": text}, inp))

    return AiInputResult(
        preview=ImportPreview(rows=rows),
        meta=_latest_meta(conn),
        csv_text=_drafts_to_csv(result.drafts),
    )
