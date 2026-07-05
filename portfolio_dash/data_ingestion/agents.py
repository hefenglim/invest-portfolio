"""AI Agents Input: parse natural-language transaction text into a preview."""

import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from portfolio_dash.data_ingestion.csv_import import txn_preview_row
from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow
from portfolio_dash.data_ingestion.store import list_accounts
from portfolio_dash.data_ingestion.validate import Issue, TxnInput
from portfolio_dash.shared.llm import LLMError, complete_structured
from portfolio_dash.shared.models.enums import Side

_TAIPEI = ZoneInfo("Asia/Taipei")


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
    "<accounts>{accounts}</accounts>\n"
    "<today>{today}</today>\n"
    "<example_input>在元大買 10 股 2330 @ 600</example_input>\n"
    '<example_output>{{"drafts":[{{"account_id":"tw_broker","symbol":"2330","side":"BUY",\n'
    '"date":"2026-06-01","shares":"10","price":"600"}}]}}</example_output>\n'
    "<example_input>7/1 嘉信買 AAPL 5股 @210，隔天再買 5 股 @212</example_input>\n"
    '<example_output>{{"drafts":[{{"account_id":"schwab","symbol":"AAPL","side":"BUY",\n'
    '"date":"2026-07-01","shares":"5","price":"210"}},{{"account_id":"schwab",\n'
    '"symbol":"AAPL","side":"BUY","date":"2026-07-02","shares":"5","price":"212"}}]}}\n'
    "</example_output>\n"
    "<rules>Return JSON only, no prose. account_id MUST be one of the ids listed in\n"
    "<accounts> (match the user's broker wording to the account name); never invent\n"
    "an id. Dates resolve against <today>: a month/day without a year means the most\n"
    "recent PAST occurrence (a trade date is never in the future); relative words\n"
    "(今天/昨天/上週五) resolve from <today>. One draft per transaction — text may\n"
    "contain several.</rules>\n"
    "<user_text>{text}</user_text>"
)


def _accounts_catalog(conn: sqlite3.Connection) -> str:
    """The live account ids the model may use, as compact ``id=name (ccy)`` lines.

    Without this the model had to GUESS ids ("嘉信" → a made-up ``charles_schwab``)
    and every non-example account failed validation with "unknown account".
    """
    return "; ".join(
        f"{a.account_id}={a.name} ({a.settlement_ccy.value})"
        for a in list_accounts(conn)
    )


def ai_agents_input(
    conn: sqlite3.Connection,
    text: str,
    *,
    completer: Completer | None = None,
    today: date | None = None,
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
    # ``today`` anchors relative/yearless dates (audit §2.7: "7/3" must resolve to the
    # most recent PAST occurrence, never a future trade date). The router feeds get_now's
    # date; the fallback keeps direct callers working.
    anchor = today if today is not None else datetime.now(_TAIPEI).date()
    try:
        result = completer(
            _PROMPT.format(
                text=text, accounts=_accounts_catalog(conn), today=anchor.isoformat()
            ),
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
