"""AI Agents Input: parse natural-language transaction text into a preview."""

import csv
import io
import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from portfolio_dash.data_ingestion.csv_import import txn_preview_row
from portfolio_dash.data_ingestion.markets import CCY_MARKET
from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow
from portfolio_dash.data_ingestion.rules_binding import allowed_markets
from portfolio_dash.data_ingestion.store import list_accounts
from portfolio_dash.data_ingestion.validate import Issue, TxnInput
from portfolio_dash.llm_insight.official_templates import AI_INPUT_PROMPT_BODY
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.llm import LLMError, complete_structured
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.symbol_format import matches_market_format

_TAIPEI = ZoneInfo("Asia/Taipei")

# Market VALUE -> that market's quote ccy (inverse of markets.CCY_MARKET). Used to render a
# MERGED account's per-market catalog line (id=name (USD:US＋MYR:MY)) for the AI parse prompt.
_MARKET_CCY = {m.value: ccy for ccy, m in CCY_MARKET.items()}


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
    # Batch B (F15): optional target market ("US"/"TW"/"MY") naming the stock's exchange, for a
    # MERGED (multi-market) account where the ticker alone is ambiguous. Advisory — it seeds the
    # quick-add dialog's market default in the preview and steers the per-row format check; the
    # real provider lookup at registration stays the authority. Blank on single-market accounts.
    market: str = ""


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


#: The columns this generator emits, a deliberate SUBSET of ``csv_import.TRANSACTION_COLUMNS``.
#:
#: ``fee``/``tax`` are omitted ON PURPOSE: they are money of record, and leaving them out is
#: what makes the fee engine — not the model — compute them at both ends. Supplying them here
#: would let an LLM-invented number override the engine (``build_transaction_preview`` honours
#: a CSV fee/tax when present).
#:
#: ``short_sale`` is omitted because the prompt never teaches the model about declared shorts,
#: so the column could only ever be ``0`` — an affordance that looks supported and is not.
#: It arrives together with its prompt rule, not before it.
#:
#: ``is_etf`` is not a transaction column at all: the instrument registry owns that flag and
#: wins at the fee seam (``csv_import.py`` ETF resolution), so ``AiDraft.is_etf`` steers only
#: the preview, and an UNREGISTERED symbol is a hard issue that never reaches a commit. There
#: is therefore no reachable divergence to close — unlike ``daytrade`` below.
_AI_CSV_COLUMNS = ["account", "symbol", "side", "date", "shares", "price", "daytrade", "note"]


def _drafts_to_csv(drafts: list[AiDraft]) -> str:
    """Render drafts as canonical transaction CSV for /api/import/commit — ONE line per draft.

    **Everything the preview PRICED has to survive this function**, because the commit route
    re-derives its own preview from this text ("the preview's answer is advisory, this one
    writes") and cannot see a field the CSV drops. ``daytrade`` used to be dropped here while
    still reaching the preview's ``TxnInput``, so a TW same-day round trip was shown at the
    0.15% rate and written at 0.3% — double, silently, with the difference riding into
    ``original_total`` as cost basis.

    Two shaping rules, both load-bearing:

    * **Proper CSV quoting** (``csv.writer``, QUOTE_MINIMAL). The prompt asks the model for a
      free-text ``note``, and ``note`` is the LAST column — so a comma inside it produced one
      more field than the header declared, ``csv.DictReader`` filed the surplus under a ``None``
      key, and ``build_transaction_preview`` raised ``AttributeError`` on its first line, outside
      the try/except that catches malformed rows. (Were ``note`` not last, the same comma would
      shift every later column instead — quieter and worse.) Minimal quoting leaves comma-free
      values byte-identical, so the common case is unchanged.
    * **CR/LF in the note are still collapsed to a space.** Quoting would preserve a newline
      faithfully, and that is exactly the problem: the frontend commits only the CHECKED rows
      by splitting this text on ``\\n`` and taking data line ``index + 1`` (C7), so a draft may
      never span two lines even when the CSV grammar allows it.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_AI_CSV_COLUMNS)
    for d in drafts:
        note = (d.note or "").replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        writer.writerow([
            d.account_id, d.symbol, d.side.value, d.date.isoformat(),
            d.shares, d.price, "1" if d.daytrade else "0", note,
        ])
    return buf.getvalue()


Completer = Callable[..., AiDraftList]

# The AI-parse prompt is code-owned but centralized in ``llm_insight/official_templates``
# (FU-D20, 2026-07-17): all shipped prompt content has one home. ``{accounts}`` / ``{today}``
# / ``{text}`` are the only interpolated placeholders (JSON braces are ``{{`` / ``}}``).
_PROMPT = AI_INPUT_PROMPT_BODY

# --- FU-D41: soft symbol-format check per account market (post-parse, warning only) -----
# The per-market code SHAPE lives in the single source ``shared.symbol_format`` (R6-A) so
# this soft hint, the resolve gate, and the next-wave AI gate cannot drift apart. A mismatch
# (the owner's bug: 聯電 parsed to the US ADR "UMC" on a tw_broker row) appends a needs_confirm
# WARNING issue to the row — it never blocks and never rewrites the symbol; the REAL provider
# lookup at registration remains the authority.


def _draft_market(value: str) -> Market | None:
    """Parse a draft's optional market string ('US'/'TW'/'MY', any case) -> Market or None."""
    v = (value or "").strip().upper()
    if not v:
        return None
    try:
        return Market(v)
    except ValueError:
        return None


def _append_format_warning(
    conn: sqlite3.Connection, row: PreviewRow, draft: AiDraft
) -> None:
    """Append the FU-D41 soft warning when the row's EFFECTIVE symbol shape mismatches
    the account's market(s) (e.g. non-numeric on a TW account). The check runs on the
    resolved payload symbol (falling back to the draft's): an EXACT hit rewrites it to the
    registered symbol, while an unregistered symbol keeps its raw form and already carries a
    HARD ``symbol_unresolved`` issue (resolution is exact-only — R6-A — so a near-miss code
    is never silently rewritten here). Skipped when the row already carries the HARD
    ``market_mismatch`` coherence issue (no double flag), when the account is unknown, or when
    the symbol is blank (other issues cover those).

    Batch B (F15) — MERGED accounts: the check is over the account's ALLOWED markets, not one
    settlement-ccy market. When the draft names an explicit (allowed) market, the shape is
    checked against THAT market only; otherwise the warning fires only when the symbol fits NO
    allowed market's pattern. A single-market account has exactly one allowed market, so this
    reduces to the prior behaviour byte-for-byte."""
    if any(i.kind == "market_mismatch" for i in row.issues):
        return
    sym = (row.payload.get("symbol") or draft.symbol).strip().upper()
    if not sym:
        return
    try:
        markets = allowed_markets(conn, draft.account_id)
    except (KeyError, ValueError):  # unknown account / unmapped ccy — other issues cover it
        return
    if not markets:
        return
    explicit = _draft_market(draft.market)
    if explicit is not None and explicit in markets:
        mismatch = not matches_market_format(sym, explicit)  # explicit market -> that pattern
    else:
        mismatch = not any(matches_market_format(sym, m) for m in markets)  # fits none allowed
    if not mismatch:
        return
    row.issues.append(
        Issue(
            kind="symbol_format_mismatch",
            needs_confirm=True,  # warning severity: surfaces, never blocks the commit
            message="代號格式與帳戶市場不符，請確認",
        )
    )


def _accounts_catalog(conn: sqlite3.Connection) -> str:
    """The live account ids the model may use, as compact ``id=name (ccy)`` lines.

    Without this the model had to GUESS ids ("嘉信" → a made-up ``charles_schwab``)
    and every non-example account failed validation with "unknown account".

    Batch B (F15): a MERGED (multi-market) account instead renders each bound market with its
    quote ccy — ``id=name (USD:US＋MYR:MY)`` — so the model picks the ticker format of the
    STOCK's market, not one settlement ccy. Single-market accounts keep the ``(ccy)`` form.
    """
    lines: list[str] = []
    for a in list_accounts(conn):
        markets = sorted(a.market_rules)
        if len(markets) > 1:
            bundle = "＋".join(f"{_MARKET_CCY.get(mv, mv)}:{mv}" for mv in markets)
            lines.append(f"{a.account_id}={a.name} ({bundle})")
        else:
            lines.append(f"{a.account_id}={a.name} ({a.settlement_ccy.value})")
    return "; ".join(lines)


def ai_agents_input(
    conn: sqlite3.Connection,
    text: str,
    *,
    completer: Completer | None = None,
    today: date | None = None,
    images: list[bytes] | None = None,
    model_alias: str | None = None,
) -> AiInputResult:
    """Extract transactions from natural-language *text* (+ screenshots) and return a preview.

    Calls the LLM (via *completer*) to parse the user's free-form text and any attached
    statement *images* into structured drafts, then feeds each draft through the same
    validate/fee-compute pipeline used by the CSV importer.  The result is an
    :class:`ImportPreview` that the caller inspects and optionally commits.

    The LLM is **never** called synchronously on page load — callers invoke this
    explicitly (manual trigger or route handler) and commit via
    :func:`~preview.commit_preview`.  The LLM only *extracts* what the text/screenshot
    already states; every number still flows through preview→confirm→commit where the
    real fee/tax engine computes the values of record.

    Args:
        conn:        Active SQLite connection (schema in place, accounts seeded).
        text:        Free-form user text describing one or more transactions.
        completer:   Injectable LLM callable. Defaults to ``None``, resolved at call
                     time to :func:`~shared.llm.complete_structured` via module lookup
                     (so ``monkeypatch.setattr`` on the module attribute takes effect).
                     Replaced with a mock in tests.
        images:      Optional decoded screenshot bytes; when present the completion layer
                     auto-routes to the VISION role chain and the model reads the images.
        model_alias: Optional explicit per-run model alias (registry) forwarded as the
                     completion layer's ``model_override`` (head of the candidate chain).
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
            images=images,
            model_override=model_alias,
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
        row = txn_preview_row(conn, idx, {"text": text}, inp)
        if d.market:
            # Batch B (F15): carry the AI-suggested market to the frontend preview row so the
            # quick-add dialog can default its market select. PREVIEW-ONLY — the committed CSV
            # (_drafts_to_csv) is unchanged, so the C7 row<->line commit mapping is preserved.
            row.payload["market"] = d.market
        _append_format_warning(conn, row, d)  # FU-D41 soft check — warns, never rewrites
        rows.append(row)

    return AiInputResult(
        preview=ImportPreview(rows=rows),
        meta=_latest_meta(conn),
        csv_text=_drafts_to_csv(result.drafts),
    )
