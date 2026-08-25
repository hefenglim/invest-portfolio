"""AI Agents Input: parse natural-language text into transaction / dividend / cash previews.

**W4 (AI-D17..D21, 2026-08-18): the door is a DISCRIMINATED UNION, one prompt for three
kinds.** A real broker statement is mixed — the same page carries buys/sells, dividends,
interest, and broker fees — so the model returns ``rows: list[TxnDraft | DivDraft |
CashDraft]`` (discriminator ``kind``) plus ``unparsed``: the rows it saw but could NOT
classify (FX conversions, corporate actions, options). Confessing them is the point —
AI-D3 exists to kill "skip the awkward rows", and a model that drops them silently would
be the same sin with better handwriting.

**Preview and commit go through the three EXISTING doors (AI-D18).** The drafts are
grouped by kind; each group is rendered to that kind's canonical CSV (the same column
constants the CSV templates are generated from) and previewed by that kind's whole-file
builder — so what the preview priced and what ``/api/import/commit`` re-derives from the
CSV cannot diverge (the AI-1 class of bug), and no new endpoint or writer exists. The
cash builder's ``pool`` probe is a REQUIRED argument here, exactly as it is at the CSV
door — see ``data_ingestion/cash_import.py``'s module docstring for why it has no default.

**C7 per kind.** Within each kind, preview row ``i`` is data line ``i + 1`` of that
kind's CSV — the frontend commits only the checked rows by splitting that kind's text on
``\\n``. A draft may therefore never span two lines: proper ``csv.writer`` quoting is
applied AND CR/LF inside a note is still collapsed to a space (quoting would preserve the
newline and break the line mapping; see the git history of this invariant, W1/AI-2).
"""

import csv
import io
import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from portfolio_dash.data_ingestion.cash_import import (
    CASH_MOVEMENT_COLUMNS,
    build_cash_movement_preview,
)
from portfolio_dash.data_ingestion.csv_import import txn_preview_row
from portfolio_dash.data_ingestion.dividend_import import (
    DIVIDEND_COLUMNS,
    build_dividend_preview,
)
from portfolio_dash.data_ingestion.markets import CCY_MARKET
from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow
from portfolio_dash.data_ingestion.rules_binding import allowed_markets
from portfolio_dash.data_ingestion.store import list_accounts
from portfolio_dash.data_ingestion.validate import CashPoolFn, Issue, TxnInput
from portfolio_dash.llm_insight.official_templates import AI_INPUT_PROMPT_BODY
from portfolio_dash.shared.cash_kinds import CASH_KIND_ZH, movement_sign
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.llm import LLMError, complete_structured
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.symbol_format import matches_market_format

#: Alias for :class:`datetime.date`. A Pydantic field named ``date`` SHADOWS the type
#: inside its own class body, so every annotation after it — ``ex_date``, the
#: ``effective_date`` return — resolves to the FIELD and mypy rejects it. Aliasing is the
#: least surprising fix: renaming the field would change the ledger's wire contract.
Date = date

_TAIPEI = ZoneInfo("Asia/Taipei")

# Market VALUE -> that market's quote ccy (inverse of markets.CCY_MARKET). Used to render a
# MERGED account's per-market catalog line (id=name (USD:US＋MYR:MY)) for the AI parse prompt.
_MARKET_CCY = {m.value: ccy for ccy, m in CCY_MARKET.items()}


class TxnDraft(BaseModel):
    """One BUY/SELL extracted from user text by the LLM (formerly ``AiDraft``, pre-union).

    ``short_sale`` arrived WITH its prompt rule (v6, AI-D19): the model sets it only on
    explicit 放空／融券／short wording, never by inference — a false positive books a short
    the user never declared, and the flag exempts the sell from the 賣超 guard.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["txn"] = "txn"
    account_id: str
    symbol: str
    side: Side
    date: date
    shares: Decimal
    price: Decimal
    daytrade: bool = False
    short_sale: bool = False
    is_etf: bool = False
    note: str | None = None
    # Batch B (F15): optional target market ("US"/"TW"/"MY") naming the stock's exchange, for a
    # MERGED (multi-market) account where the ticker alone is ambiguous. Advisory — it seeds the
    # quick-add dialog's market default in the preview and steers the per-row format check; the
    # real provider lookup at registration stays the authority. Blank on single-market accounts.
    market: str = ""


class DivDraft(BaseModel):
    """One dividend extracted by the LLM — mirrors ``DIVIDEND_COLUMNS`` exactly.

    No ``note``: the dividends CSV kind has no note column, and this model must not carry
    a field the commit door cannot see (the AI-1 lesson, generalised).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["div"] = "div"
    account_id: str
    symbol: str
    date: date  # the PAYMENT date (R6 pinned the meaning)
    type: str  # CASH / STOCK / DRIP / NET — canonicalised + gated per account model downstream
    gross: Decimal
    withholding: Decimal | None = None
    net: Decimal | None = None
    reinvest_shares: Decimal | None = None
    reinvest_price: Decimal | None = None
    #: R6 — the EX-DIVIDEND date, only when the statement states it. The prompt forbids
    #: inferring one: a guessed ex-date silently moves a 配股's shares to the wrong day,
    #: which is the very error R6 exists to remove.
    ex_date: Date | None = None


class CashDraft(BaseModel):
    """One cash movement extracted by the LLM — mirrors ``CASH_MOVEMENT_COLUMNS``.

    The direction lives in ``cash_kind`` (amounts are unsigned): mislabel BROKER_FEE as
    DEPOSIT and the pool is wrong by 2× the amount with nobody raising an error (AI-D3).
    The field is ``cash_kind``, not ``kind``, because ``kind`` is already the union
    DISCRIMINATOR on this model — one field cannot be both. ``cash_kind`` accepts the
    canonical spelling (``DEPOSIT``) or a zh alias (``入金``); the CSV door's
    ``_canonical_kind`` owns the alias table, so this door and the CSV door can never
    drift apart on what 入金 means.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["cash"] = "cash"
    account_id: str
    date: date
    cash_kind: str
    ccy: str
    amount: Decimal
    # The home-currency cost of a foreign acquisition (F1: the AMOUNT, never a rate). The
    # prompt rule is strict: fill it ONLY when the statement itself states the cost.
    acq_home_amount: Decimal | None = None
    note: str | None = None


class UnparsedRow(BaseModel):
    """A statement row the model saw but could NOT classify (AI-D17).

    FX conversions (two-amount algebra), corporate actions (ratio algebra + batch-level
    validation), options — all deliberately OUT of the union (AI-D3). Listing them here
    surfaces them to the user (「請改用 CSV／表單」) instead of dropping them silently.
    """

    text: str
    reason: str = ""


#: The union itself. ``kind`` is the discriminator, so a dividend row missing ``gross``
#: fails at the PARSE boundary (and triggers the completion layer's one retry) instead of
#: exploding inside a preview builder (AI-D17).
AnyDraft = Annotated[TxnDraft | DivDraft | CashDraft, Field(discriminator="kind")]


class AiDraftList(BaseModel):
    """Structured LLM output: the extracted drafts plus the confessed unparsed rows.

    ``extra="forbid"`` here and on the three drafts (W4 review): the same parse boundary
    AI-D17 set for MISSING fields, from the other side. Pydantic's default ignores unknown
    keys, so a mistyped optional money field (``with_hold`` for ``withholding``) parsed
    clean with the statement's number silently gone — and a model regressing to the v5
    ``{"drafts": [...]}`` shape parsed to ``rows=[]``, the whole extraction dropped without
    a word. Both are AI-D3's silent-drop sin; both now fail validation and take the one
    retry instead. ``UnparsedRow`` stays lenient: an extra key on a confession costs
    nothing, and the confession itself is the failure-soft path.
    """

    model_config = ConfigDict(extra="forbid")

    rows: list[AnyDraft] = Field(default_factory=list)
    unparsed: list[UnparsedRow] = Field(default_factory=list)


class AiMeta(BaseModel):
    """Provenance of the LLM run that produced a preview (latest usage row)."""

    model: str | None = None
    via: str = "litellm"
    cost_usd: Decimal | None = None


class AiInputResult(BaseModel):
    """Bundle returned by :func:`ai_agents_input`: per-kind previews + commit CSVs + meta.

    ``previews`` / ``csv_texts`` are keyed by the EXISTING import kinds
    (``transactions`` / ``dividends`` / ``cash``); a kind with zero drafts is absent.
    ``error`` is set only on the LLM-failure degrade path (the router maps it to the
    HTTP degrade response, exactly as the pre-union single-row degrade did).
    """

    previews: dict[str, ImportPreview] = Field(default_factory=dict)
    csv_texts: dict[str, str] = Field(default_factory=dict)
    unparsed: list[UnparsedRow] = Field(default_factory=list)
    meta: AiMeta = Field(default_factory=AiMeta)
    error: Issue | None = None


def _latest_meta(conn: sqlite3.Connection) -> AiMeta:
    """Read the most recent ``llm_usage`` row for the AI-input agent into meta."""
    row = conn.execute(
        "SELECT model, cost FROM llm_usage WHERE agent='ai_agents_input' "
        "ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return AiMeta()
    return AiMeta(model=row["model"], cost_usd=Decimal(row["cost"]))


#: The columns the transaction renderer emits, a deliberate SUBSET of
#: ``csv_import.TRANSACTION_COLUMNS``.
#:
#: ``fee``/``tax`` are omitted ON PURPOSE: they are money of record, and leaving them out
#: is what makes the fee engine — not the model — compute them at both ends. Supplying
#: them here would let an LLM-invented number override the engine
#: (``build_transaction_preview`` honours a CSV fee/tax when present).
#:
#: ``daytrade`` and ``short_sale`` are BOTH present: the flags reach the preview's
#: ``TxnInput`` AND this CSV, because the commit route re-derives its own preview from
#: this text ("the preview's answer is advisory, this one writes") and cannot see a field
#: the CSV drops. ``daytrade`` was the W1/AI-1 bug (preview showed 0.15%, the ledger got
#: 0.3%); ``short_sale`` was then withheld because the prompt never taught it — v6
#: (AI-D19) added the rule, so the column arrives now, exactly as planned.
#:
#: ``is_etf`` is not a transaction column at all: the instrument registry owns that flag
#: and wins at the fee seam (``csv_import.py`` ETF resolution), so ``TxnDraft.is_etf``
#: steers only the preview, and an UNREGISTERED symbol is a hard issue that never reaches
#: a commit. There is therefore no reachable divergence to close.
_TXN_CSV_COLUMNS = [
    "account", "symbol", "side", "date", "shares", "price", "daytrade", "short_sale", "note",
]


def _collapse_note(note: str | None) -> str:
    """CR/LF inside a note collapse to a space — a draft may never span two lines (C7)."""
    return (note or "").replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def _csv_text(header: list[str], rows: list[list[object]]) -> str:
    """One canonical CSV text — ``csv.writer`` QUOTE_MINIMAL, ``\\n`` line terminator.

    Proper quoting is load-bearing (W1/AI-2): the prompt asks the model for free-text
    notes, and an unquoted comma in the LAST column produced a surplus field that
    ``csv.DictReader`` filed under a ``None`` key — an ``AttributeError`` outside the
    try/except. Minimal quoting leaves comma-free values byte-identical.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


def _opt(value: Decimal | None) -> str:
    """An optional Decimal cell: blank when absent, canonical string otherwise."""
    return "" if value is None else str(value)


def _txn_csv(drafts: list[TxnDraft]) -> str:
    """Render transaction drafts as canonical CSV — ONE line per draft (C7)."""
    return _csv_text(_TXN_CSV_COLUMNS, [
        [
            d.account_id, d.symbol, d.side.value, d.date.isoformat(),
            d.shares, d.price, "1" if d.daytrade else "0",
            "1" if d.short_sale else "0", _collapse_note(d.note),
        ]
        for d in drafts
    ])


def _div_csv(drafts: list[DivDraft]) -> str:
    """Render dividend drafts as canonical CSV — the SAME columns as the CSV template."""
    return _csv_text(DIVIDEND_COLUMNS, [
        [
            d.account_id, d.symbol, d.date.isoformat(), d.type, d.gross,
            _opt(d.withholding), _opt(d.net),
            _opt(d.reinvest_shares), _opt(d.reinvest_price),
            d.ex_date.isoformat() if d.ex_date is not None else "",
        ]
        for d in drafts
    ])


def _cash_csv(drafts: list[CashDraft]) -> str:
    """Render cash drafts as canonical CSV — the SAME columns as the CSV template."""
    return _csv_text(CASH_MOVEMENT_COLUMNS, [
        [
            d.account_id, d.date.isoformat(), d.cash_kind, d.ccy, d.amount,
            _opt(d.acq_home_amount), _collapse_note(d.note),
        ]
        for d in drafts
    ])


def _label_cash_rows(preview: ImportPreview) -> None:
    """Attach the server-owned zh label + explicit sign to each cash row's payload (AI-D21).

    The label vocabulary lives in ``shared/cash_kinds.py`` (``CASH_KIND_ZH``) — the
    frontend renders ``kind_label`` / ``sign`` verbatim rather than keeping a fourth copy
    of the map (the third copy, in the printed statement, is how an unlabelled kind once
    shipped as raw English). Parse-error rows carry no payload ``kind`` and are skipped.
    """
    for row in preview.rows:
        kind = row.payload.get("kind")
        if not kind:
            continue
        row.payload["kind_label"] = CASH_KIND_ZH.get(kind, kind)
        row.payload["sign"] = str(movement_sign(kind))


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
    conn: sqlite3.Connection, row: PreviewRow, draft: TxnDraft
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
    pool: CashPoolFn,
    completer: Completer | None = None,
    today: date | None = None,
    images: list[bytes] | None = None,
    model_alias: str | None = None,
) -> AiInputResult:
    """Extract transactions + dividends + cash movements from *text* (+ screenshots).

    Calls the LLM (via *completer*) to parse the user's free-form text and any attached
    statement *images* into the discriminated-union drafts, then builds ONE preview per
    kind through that kind's existing door: transactions per draft via
    :func:`txn_preview_row` (the FU-D41 format warning and the F15 market hint are
    per-draft extras the CSV cannot carry), dividends and cash by rendering the kind's
    canonical CSV and calling its whole-file builder — so the preview IS what a commit
    of the returned CSV will re-derive, by construction.

    The LLM is **never** called synchronously on page load — callers invoke this
    explicitly (manual trigger or route handler) and commit via the ordinary
    ``/api/import/commit``. The LLM only *extracts* what the text/screenshot already
    states; every number still flows through preview→confirm→commit where the real
    fee/tax engine computes the values of record.

    Args:
        conn:        Active SQLite connection (schema in place, accounts seeded).
        text:        Free-form user text describing one or more ledger rows.
        pool:        The cash-pool probe for the withdraw guard — REQUIRED, no default,
                     same rule as the cash CSV door (see ``cash_import.py``). The router
                     binds :func:`api.routers.cash.cash_pool_fn`; tests bind a stub.
        completer:   Injectable LLM callable. Defaults to ``None``, resolved at call
                     time to :func:`~shared.llm.complete_structured` via module lookup
                     (so ``monkeypatch.setattr`` on the module attribute takes effect).
                     Replaced with a mock in tests.
        today:       Anchors relative/yearless dates (audit §2.7: "7/3" must resolve to
                     the most recent PAST occurrence, never a future trade date). The
                     router feeds get_now's date; the fallback keeps direct callers working.
        images:      Optional decoded screenshot bytes; when present the completion layer
                     auto-routes to the VISION role chain and the model reads the images.
        model_alias: Optional explicit per-run model alias (registry) forwarded as the
                     completion layer's ``model_override`` (head of the candidate chain).
    Returns:
        :class:`AiInputResult` with one :class:`ImportPreview` + commit CSV per kind
        present in the extraction, the confessed ``unparsed`` rows, and the latest-run
        :class:`AiMeta`. On LLM failure the bundle is empty and ``error`` carries the
        degradation issue.
    """
    completer = completer or complete_structured
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
        return AiInputResult(error=Issue(kind=exc.kind, message=str(exc)))

    txns = [r for r in result.rows if isinstance(r, TxnDraft)]
    divs = [r for r in result.rows if isinstance(r, DivDraft)]
    cashs = [r for r in result.rows if isinstance(r, CashDraft)]

    previews: dict[str, ImportPreview] = {}
    csv_texts: dict[str, str] = {}

    if txns:
        # Sibling-aware (C1, extended to this door in W4): the oversell guard counts the
        # WHOLE extracted set, so 「買 10 股、隔天賣 10 股」 in one paste does not flag the
        # sell against a position the same batch is still building.
        inputs = [
            TxnInput(
                account_id=d.account_id,
                symbol=d.symbol,
                side=d.side,
                quantity=d.shares,
                price=d.price,
                trade_date=d.date,
                daytrade=d.daytrade,
                short_sale=d.short_sale,
                is_etf=d.is_etf,
                note=d.note,
            )
            for d in txns
        ]
        rows: list[PreviewRow] = []
        for idx, (d, inp) in enumerate(zip(txns, inputs, strict=True)):
            row = txn_preview_row(conn, idx, {"text": text}, inp, batch=inputs)
            if d.market:
                # Batch B (F15): carry the AI-suggested market to the frontend preview row so
                # the quick-add dialog can default its market select. PREVIEW-ONLY — the
                # committed CSV is unchanged, so the C7 row<->line mapping is preserved.
                row.payload["market"] = d.market
            _append_format_warning(conn, row, d)  # FU-D41 soft check — warns, never rewrites
            rows.append(row)
        previews["transactions"] = ImportPreview(rows=rows)
        csv_texts["transactions"] = _txn_csv(txns)

    if divs:
        csv_texts["dividends"] = _div_csv(divs)
        previews["dividends"] = build_dividend_preview(conn, csv_texts["dividends"])

    if cashs:
        csv_texts["cash"] = _cash_csv(cashs)
        cash_preview = build_cash_movement_preview(conn, csv_texts["cash"], pool=pool)
        _label_cash_rows(cash_preview)
        previews["cash"] = cash_preview

    return AiInputResult(
        previews=previews,
        csv_texts=csv_texts,
        unparsed=result.unparsed,
        meta=_latest_meta(conn),
    )
