"""CSV import for dividends — reuses the preview/commit infrastructure."""

import csv
import io
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation

from portfolio_dash.data_ingestion.dividend_model import apply_dividend_model, check_amounts
from portfolio_dash.data_ingestion.preview import ImportPreview, PreviewRow
from portfolio_dash.data_ingestion.resolve import ResolutionStatus, resolve
from portfolio_dash.data_ingestion.rules_binding import dividend_model_for
from portfolio_dash.data_ingestion.store import insert_dividend
from portfolio_dash.data_ingestion.validate import (
    Issue,
    alias_import_account,
    unknown_account_issue,
)
from portfolio_dash.shared.models.enums import DividendType

# Batch B (F01): the CSV `type` values each STORED (account, market) dividend model accepts.
# A merged dual-market account would otherwise mis-book one market's dividends (MY cash as
# DRIP with a fabricated 30% withholding, or a US dividend missing withholding) — corrupt
# money of record. Dormant for a single-market account whose only model matches its rows.
_MODEL_ALLOWED_TYPES: dict[str, set[str]] = {
    "cash_cost_reduction": {"CASH", "STOCK"},  # TW: cash cost-reduction (+ optional 配股)
    # US Schwab/Moomoo. DRIP is the account's default mechanism, but a US payout that
    # arrives as PLAIN CASH is ordinary, not exceptional — the owner's real broker export
    # carries more dividend rows than reinvest rows, and the difference is exactly these.
    # Admitting CASH here (P1b, 2026-08-13) removes the per-row `dividend_type_mismatch`
    # confirmation that used to stand between a statement and the ledger.
    #
    # It needs NO accounting change: a CASH row falls into ``CASH_DIVIDEND_TYPES``
    # (``shared/models/enums.py``), so it reduces ``adjusted_total`` exactly as a TW/MY cash
    # dividend does — owner ruling D35, 2026-08-10. Booking it as income instead would fork
    # ``CASH_DIVIDEND_TYPES`` by market, and 回本進度 / 股利回收率 would then mean different
    # things per market ON THE SAME SCREEN.
    "drip_us": {"DRIP", "CASH"},
    "cash": {"NET"},                            # MY single-tier: net received
}

# Canonical CSV column order for the dividends import — SINGLE SOURCE for the downloadable
# template header (see data_ingestion.import_templates). Required: account, symbol, date, type,
# gross (except on a NET row, which may supply net alone — §6.3, FIX-A3b); optional overrides:
# withholding, net, reinvest_shares, reinvest_price. Kept in lockstep with the DictReader keys
# below by the round-trip guard test.
DIVIDEND_COLUMNS: list[str] = [
    "account", "symbol", "date", "type", "gross",
    "withholding", "net", "reinvest_shares", "reinvest_price",
    # R6 (review ⑧): the EX-DIVIDEND date, optional. ``date`` means the PAYMENT date. A row
    # that omits this behaves exactly as it did before the column existed — never guessed.
    "ex_date",
]


#: The stored dividend types, DERIVED from the one shared authority rather than restated
#: (F-12's rule): the reader below refuses exactly what ``DividendType(s.type)`` on every
#: read path cannot represent, and a type added to the enum is admitted here without a
#: second list going stale.
_DIVIDEND_TYPES = tuple(t.value for t in DividendType)


class _CellError(ValueError):
    """One CSV cell that could not be read — the vetted zh sentence IS the exception text.

    The fx door's H-2 pattern (``fx_import._CellError``), applied to the dividend door for
    QA BUG-05: the parse block used to answer ``Issue(message=str(exc))``, so a blank
    ``gross`` rendered ``[<class 'decimal.ConversionSyntax'>]`` in the 原因 column — the one
    column whose entire job is to tell the owner why a row was rejected — a bad date leaked
    CPython's English (``Invalid isoformat string: …``), and an unknown type leaked this
    module's own English ``unknown dividend type …``. The static zh-TW guard cannot catch
    this class (it scans ``Issue(message=<literal>)`` and ``str(exc)`` is a call), so the
    rule is enforced by the typed readers below, at the point where the column name is
    still in scope. A distinct type so the parse block's except arm can forward exactly
    the vetted sentences and nothing else.
    """


def _finite(value: Decimal, column: str, label: str, text: str) -> Decimal:
    """*value*, or a zh :class:`_CellError` when the cell holds ``NaN`` / ``±Infinity``.

    ``Decimal("NaN")`` CONSTRUCTS — ``InvalidOperation`` is never raised — and a NaN
    withholding then reached ``check_amounts``, whose ``withholding + net > gross``
    comparison raises ``InvalidOperation`` OUTSIDE any except arm: a 500 on the preview
    endpoint for one broken cell. Same wording as the cash/fx doors' identical guard —
    one broken numeric cell says one thing whichever bulk door it arrived through.
    """
    if not value.is_finite():
        raise _CellError(f"{label}（{column}）必須是有限數字，目前是「{text}」")
    return value


def _decimal_cell(raw: dict[str, str], column: str, label: str) -> Decimal:
    """One required Decimal cell, or a zh :class:`_CellError` naming the column.

    Read by SUBSCRIPT on purpose (the J-2 rule, as in ``cash_import._decimal``): an absent
    HEADER must raise ``KeyError`` into the 缺少必填欄位 arm, because "you forgot the
    column" and "this row's cell is blank" are different mistakes with different fixes.
    """
    text = raw[column].strip()
    if not text:
        raise _CellError(f"{label}（{column}）不可空白")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise _CellError(f"{label}（{column}）不是數字：{text}") from None
    return _finite(value, column, label, text)


def _optional_decimal_cell(raw: dict[str, str], column: str, label: str) -> Decimal | None:
    """One OPTIONAL Decimal cell — ``None`` when the column, or the cell, is blank/absent.

    A non-empty unparseable value is LOUD, unlike the old ``_opt_decimal`` which returned
    ``None`` for it: silently dropping a typo'd ``withholding`` or ``net`` books amounts
    the owner never stated (net = gross − 0 instead of the statement's figure) with no
    finding anywhere — the H-2 posture is that a money cell is either read or refused with
    a sentence, never half-read. ``.get()`` deliberately, not subscript: these columns are
    optional by spec, so a file without them is an ordinary shape, not a mistake.
    """
    text = raw.get(column, "").strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise _CellError(f"{label}（{column}）不是數字：{text}") from None
    return _finite(value, column, label, text)


def _date_cell(raw: dict[str, str], column: str, label: str) -> date:
    """One required ISO date cell, or a zh :class:`_CellError` naming the column.

    Word for word the cash/fx doors' sentence (``test_m2_import_date_zh``'s shared
    wording): one broken date cell says one thing whichever bulk door it arrived through.
    Subscript read — an absent header is the 缺少必填欄位 arm's business.
    """
    text = raw[column]
    if not text:
        raise _CellError(f"{label}（{column}）不可空白")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise _CellError(
            f"{label}（{column}）格式不正確，須為 YYYY-MM-DD，目前是「{text}」") from None


def _optional_date_cell(raw: dict[str, str], column: str, label: str) -> date | None:
    """``ex_date``: absent or blank is ``None`` (a row without one replays exactly as
    before the column existed — R6); a non-empty malformed value keeps its pre-existing
    LOUD rejection, now with the shared vetted sentence instead of CPython's English."""
    text = raw.get(column, "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise _CellError(
            f"{label}（{column}）格式不正確，須為 YYYY-MM-DD，目前是「{text}」") from None


def _type_cell(raw: dict[str, str]) -> str:
    """The ``type`` cell, normalized to upper (2026-07-03: never store what the read path
    cannot represent), or a zh :class:`_CellError` echoing what the owner actually typed
    and the supported set — the ``_currency`` pattern, replacing the English
    ``unknown dividend type …`` ValueError this arm used to leak."""
    text = raw["type"].strip()
    if not text:
        raise _CellError("股利類型（type）不可空白")
    value = text.upper()
    if value not in _DIVIDEND_TYPES:
        supported = "／".join(_DIVIDEND_TYPES)
        raise _CellError(f"股利類型（type）無法辨識：{text}（僅支援 {supported}）")
    return value


def build_dividend_preview(conn: sqlite3.Connection, csv_text: str) -> ImportPreview:
    """Parse *csv_text* into an :class:`ImportPreview` of dividends rows.

    Required columns: account, symbol, date (the PAYMENT date), type, gross — except that
    a ``NET`` (MY single-tier) row may supply ``net`` alone, the manual §6.3 number of
    record, and ``gross`` is completed as ``net`` (gross ≡ net under single-tier).
    Optional columns: withholding, net, reinvest_shares, reinvest_price, ex_date.

    ``ex_date`` (R6) is the ex-dividend date. Only a STOCK dividend uses it — see
    ``Dividend.effective_date`` — and an unparseable or absent value is left as ``None``
    rather than guessed, which replays exactly as before the column existed.

    The dividend model for the row is derived from the ``type`` column
    (``DRIP`` / ``STOCK`` / ``cash``); ``apply_dividend_model`` fills in computed
    amounts which are stored in ``payload`` for later commit.
    """
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))  # tolerate a leading BOM
    rows: list[PreviewRow] = []
    for idx, raw0 in enumerate(reader):
        raw: dict[str, str] = {k.strip(): (v or "").strip() for k, v in raw0.items()}
        issues: list[Issue] = []

        # --- parse: every cell through a typed reader (QA BUG-05 / H-2) ---
        # ``account`` / ``symbol`` are read by subscript WITHOUT a blank check, on purpose:
        # a blank account already has one owner (``unknown_account_issue`` below — L-1's
        # 「帳戶不可空白」), and a blank symbol already lands on the resolver's
        # ``symbol_unresolved`` path; raising a second sentence here would fork the wording
        # of a finding another function owns. An absent COLUMN still raises ``KeyError``
        # into the 缺少必填欄位 arm.
        try:
            # Legacy Moomoo account id -> moomoo_my (+ soft info issue appended below).
            account_id, alias_issue = alias_import_account(raw["account"])
            symbol = raw["symbol"]
            div_date = _date_cell(raw, "date", "日期")
            ex_date = _optional_date_cell(raw, "ex_date", "除息日")
            div_type = _type_cell(raw)
            withholding_override = _optional_decimal_cell(raw, "withholding", "預扣稅額")
            net_override = _optional_decimal_cell(raw, "net", "股利淨額")
            reinvest_shares_override = _optional_decimal_cell(
                raw, "reinvest_shares", "再投資股數")
            reinvest_price_override = _optional_decimal_cell(
                raw, "reinvest_price", "再投資價格")
            if div_type == "NET":
                # §6.3 (manual): MY single-tier records the NET amount received — the net
                # IS the number of record, and gross ≡ net by definition. So a NET row may
                # supply ``net`` alone and ``gross`` is completed from it. This is a
                # PARSE-level completion of which cells may be blank, NOT a second amounts
                # engine: the completed gross still flows through ``apply_dividend_model``
                # below, whose cash/NET branch computes the identical figures (wh 0,
                # net = gross), so the door and the model cannot disagree. Scoped to NET
                # alone — CASH/DRIP/STOCK keep their required ``gross`` unchanged.
                gross_opt = _optional_decimal_cell(raw, "gross", "股利總額")
                if gross_opt is not None:
                    gross = gross_opt
                elif net_override is not None:
                    gross = net_override
                else:
                    raise _CellError(
                        "股利淨額（net）不可空白：NET（單層制）股利以實收淨額入帳，"
                        "總額即淨額 — 請至少填寫 net 或 gross 其中一欄")
            else:
                gross = _decimal_cell(raw, "gross", "股利總額")
        except KeyError as exc:
            rows.append(PreviewRow(index=idx, raw=raw, issues=[
                Issue(kind="parse_error", message=f"缺少必填欄位 {exc.args[0]}")]))
            continue
        except _CellError as exc:
            # BEFORE the belt-and-braces arm, because ``_CellError`` IS a ``ValueError``.
            # Its text is a vetted zh sentence naming the column — forwarded verbatim.
            rows.append(PreviewRow(index=idx, raw=raw, issues=[
                Issue(kind="parse_error", message=str(exc))]))
            continue
        except (ValueError, InvalidOperation):
            # Belt and braces (``csv_import``'s arm): everything above is read through a
            # typed cell reader, so this is reachable only by a fault those readers do not
            # anticipate. Its text would be English and structural, so the owner gets a
            # Chinese sentence pointing at the row instead — never ``str(exc)``, which is
            # exactly how the Python internals got onto the screen in the first place.
            rows.append(PreviewRow(index=idx, raw=raw, issues=[
                Issue(kind="parse_error",
                      message="這一列的內容無法解析，請對照範本檢查各欄位格式")]))
            continue

        if alias_issue is not None:
            issues.append(alias_issue)

        # --- validate account exists (hard issue) ---
        account_known = (
            conn.execute(
                "SELECT 1 FROM accounts WHERE account_id=?", (account_id,)
            ).fetchone()
            is not None
        )
        if not account_known:
            # L-1: the shared sentence. A BLANK ``account`` cell rendered 「帳戶  不存在」
            # here — two spaces, no name — while the cash door already said 「帳戶不可空白」.
            issues.append(unknown_account_issue(account_id))

        # --- resolve the symbol once: soft unresolved warning + type/market coherence ---
        res = resolve(conn, symbol)
        if res.status is ResolutionStatus.NEEDS_AI:
            # Unregistered symbol -> soft warning (existing handling); no coherence check
            # (its market is unknown until it is registered).
            issues.append(
                Issue(
                    kind="symbol_unresolved",
                    needs_confirm=True,
                    message=f"未註冊標的 {symbol} — 請先至「標的管理」註冊",
                )
            )
        elif res.instrument is not None and account_known:
            # Batch B (F01): the row's `type` must match the dividend model bound to
            # (account, the RESOLVED instrument's market). Mismatch -> soft needs_confirm
            # (importable only after explicit confirm). Only reachable for a KNOWN account +
            # REGISTERED symbol; a single-market account's coherent rows never trip it.
            model = dividend_model_for(conn, account_id, res.instrument.market)
            allowed = _MODEL_ALLOWED_TYPES.get(model)
            if allowed is not None and div_type not in allowed:
                issues.append(
                    Issue(
                        kind="dividend_type_mismatch",
                        needs_confirm=True,
                        message="股利類型與該市場模型不符，請確認",
                    )
                )
            elif (
                model == "drip_us"
                and div_type == "CASH"
                and withholding_override is None
            ):
                # P1b's edge. ``apply_dividend_model`` keys on the dividend TYPE, not on the
                # account's model, so a blank withholding on a CASH row books 0 — right for
                # a TW/MY payout, wrong for a US one under W-8BEN. The consequence is not
                # cosmetic: net would equal gross, over-reducing ``adjusted_total`` and
                # under-reporting the position's unrealized gain for the rest of its life.
                #
                # Soft, not hard: a withholding-free US distribution genuinely exists (a
                # return of capital), so this asks rather than refuses. The manual form
                # always sends the number, and the broker converter reads it off the
                # statement, so it fires only on a hand-written row that omitted it.
                issues.append(
                    Issue(
                        kind="us_cash_dividend_no_withholding",
                        needs_confirm=True,
                        message="美股現金股利未填預扣稅，將以 0 記錄（淨額=總額），請確認",
                    )
                )

        # --- NET (§6.3): single-tier has no withholding — a stated one is refused ---
        # ``apply_dividend_model`` itself would ACCEPT it (its cash/NET branch computes
        # ``net = gross − withholding``, the TW shape), which is precisely why the refusal
        # sits at this door rather than being half-implemented as a different derivation:
        # the model stays the ONE amounts engine, and a row that contradicts the single-tier
        # definition never reaches it. Hard, not needs_confirm — under §6.3 gross ≡ net, so
        # a non-zero withholding means the row is mis-typed (a taxed payout belongs under
        # CASH with its real gross), and acknowledging cannot make the numbers coherent.
        # An explicit ``0`` is not refused: it states exactly what the model would derive.
        if div_type == "NET" and withholding_override is not None and withholding_override != 0:
            issues.append(Issue(
                kind="net_dividend_withholding",
                message=(f"NET（單層制）股利不會有預扣稅，預扣稅額（withholding）"
                         f"目前是 {withholding_override}。單層制以實收淨額入帳，總額即淨額；"
                         "若這筆股利確實被預扣稅款，請改用 CASH 類型並填寫總額與預扣稅額"),
            ))

        # --- apply dividend model to compute withholding / net / reinvest_shares ---
        amounts = apply_dividend_model(
            div_type,
            gross=gross,
            withholding=withholding_override,
            net=net_override,
            reinvest_shares=reinvest_shares_override,
            reinvest_price=reinvest_price_override,
        )

        # Conservation gate (audit M5): a payout can never deliver more than it declared.
        # Hard issue — the same check the ledger edit endpoint enforces, so both write paths
        # agree instead of one silently accepting what the other rejects.
        amount_issue = check_amounts(amounts.gross, amounts.withholding, amounts.net)
        if amount_issue is not None:
            issues.append(Issue(kind="dividend_amounts", message=amount_issue))

        # A share-adding dividend with no share count is refused HERE, because the replay
        # already refuses it: `cost_basis.py` raises "DRIP/STOCK dividend ... requires
        # reinvest_shares" rather than coercing to zero. Until this guard existed the door
        # accepted such a row with NO issue at all — `apply_dividend_model` passes the field
        # through for STOCK and only derives it for DRIP when a reinvest_price is present,
        # and the conservation gate above passes for any non-negative gross. The row landed
        # clean and then broke every later rebuild of the whole book (the class
        # `test_cost_basis.py` records as having once "crashed every rebuild that held a MY
        # dividend"). The condition is written to MIRROR the replay's own test, not to
        # restate it in different words, so the two cannot drift apart.
        if div_type in ("DRIP", "STOCK") and amounts.reinvest_shares is None:
            issues.append(Issue(
                kind="reinvest_shares_required",
                message=(
                    f"{div_type} 股利必須有股數（reinvest_shares）"
                    "——配股／再投資是以股數入帳，缺這個欄位重算會失敗"
                ),
            ))

        payload: dict[str, str] = {
            "account_id": account_id,
            "symbol": symbol,
            "date": div_date.isoformat(),
            "type": div_type,
            "gross": str(amounts.gross),
            "withholding": str(amounts.withholding),
            "net": str(amounts.net),
        }
        if amounts.reinvest_shares is not None:
            payload["reinvest_shares"] = str(amounts.reinvest_shares)
        if amounts.reinvest_price is not None:
            payload["reinvest_price"] = str(amounts.reinvest_price)
        if ex_date is not None:
            payload["ex_date"] = ex_date.isoformat()

        rows.append(PreviewRow(index=idx, raw=raw, payload=payload, issues=issues))

    return ImportPreview(rows=rows)


def write_dividend_row(
    conn: sqlite3.Connection, row: PreviewRow, *, commit: bool = True
) -> int:
    """Persist one accepted dividends row and return its autoincrement id.

    ``commit`` is forwarded to the store insert; the batch path passes ``commit=False``
    so the whole batch commits once (all-or-nothing, #1).
    """
    p = row.payload
    rs_str = p.get("reinvest_shares")
    rp_str = p.get("reinvest_price")
    return insert_dividend(
        conn,
        account_id=p["account_id"],
        symbol=p["symbol"],
        div_date=date.fromisoformat(p["date"]),
        div_type=p["type"],
        gross=Decimal(p["gross"]),
        withholding=Decimal(p["withholding"]),
        net=Decimal(p["net"]),
        reinvest_shares=Decimal(rs_str) if rs_str is not None else None,
        reinvest_price=Decimal(rp_str) if rp_str is not None else None,
        ex_date=date.fromisoformat(p["ex_date"]) if p.get("ex_date") else None,
        commit=commit,
    )
