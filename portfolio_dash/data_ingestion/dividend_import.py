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
from portfolio_dash.data_ingestion.validate import Issue, alias_import_account

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
# gross; optional overrides: withholding, net, reinvest_shares, reinvest_price. Kept in lockstep
# with the DictReader keys below by the round-trip guard test.
DIVIDEND_COLUMNS: list[str] = [
    "account", "symbol", "date", "type", "gross",
    "withholding", "net", "reinvest_shares", "reinvest_price",
    # R6 (review ⑧): the EX-DIVIDEND date, optional. ``date`` means the PAYMENT date. A row
    # that omits this behaves exactly as it did before the column existed — never guessed.
    "ex_date",
]


def _opt_decimal(row: dict[str, str], key: str) -> Decimal | None:
    """Return a Decimal parsed from *row[key]*, or None when missing/empty/invalid."""
    val = row.get(key, "").strip()
    if not val:
        return None
    try:
        return Decimal(val)
    except InvalidOperation:
        return None


def build_dividend_preview(conn: sqlite3.Connection, csv_text: str) -> ImportPreview:
    """Parse *csv_text* into an :class:`ImportPreview` of dividends rows.

    Required columns: account, symbol, date (the PAYMENT date), type, gross.
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

        # --- parse required fields ---
        try:
            # Legacy Moomoo account id -> moomoo_my (+ soft info issue appended below).
            account_id, alias_issue = alias_import_account(raw["account"])
            symbol = raw["symbol"]
            div_date = date.fromisoformat(raw["date"])
            ex_date_raw = raw.get("ex_date", "").strip()
            ex_date = date.fromisoformat(ex_date_raw) if ex_date_raw else None
            # Normalize + validate type (2026-07-03): the raw value used to be
            # stored as-is, so a lowercase "cash" poisoned the ledger (readers do
            # DividendType(s.type) and raise). Same write/read invariant as ever:
            # never store what the read path cannot represent.
            div_type = raw["type"].strip().upper()
            if div_type not in {"CASH", "STOCK", "DRIP", "NET"}:
                raise ValueError(f"unknown dividend type {raw['type']!r}")
            gross = Decimal(raw["gross"])
        except (KeyError, ValueError, InvalidOperation) as exc:
            rows.append(
                PreviewRow(
                    index=idx,
                    raw=raw,
                    issues=[Issue(kind="parse_error", message=str(exc))],
                )
            )
            continue

        if alias_issue is not None:
            issues.append(alias_issue)

        # --- parse optional numeric overrides ---
        withholding_override = _opt_decimal(raw, "withholding")
        net_override = _opt_decimal(raw, "net")
        reinvest_shares_override = _opt_decimal(raw, "reinvest_shares")
        reinvest_price_override = _opt_decimal(raw, "reinvest_price")

        # --- validate account exists (hard issue) ---
        account_known = (
            conn.execute(
                "SELECT 1 FROM accounts WHERE account_id=?", (account_id,)
            ).fetchone()
            is not None
        )
        if not account_known:
            issues.append(
                Issue(kind="unknown_account", message=f"帳戶 {account_id} 不存在")
            )

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
