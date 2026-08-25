"""Manual transaction entry: resolve → fee/tax → validate → confirm → persist."""

import sqlite3
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.fees import (
    FeeComputationError,
    compute_fees,
    etf_flag_issue_applies,
    resolve_etf_flag,
    supplied_snapshot,
)
from portfolio_dash.data_ingestion.fx_lookup import resolve_stamp_fx
from portfolio_dash.data_ingestion.resolve import (
    ResolutionStatus,
    resolve,
    suggestion_tail,
)
from portfolio_dash.data_ingestion.rules_binding import fee_rule_for
from portfolio_dash.data_ingestion.store import insert_transaction
from portfolio_dash.data_ingestion.validate import Issue, TxnInput, validate_transaction
from portfolio_dash.shared.models.assets import Instrument


class TxnDraft(BaseModel):
    """Intermediate draft produced by :func:`enter_transaction`.

    When *written* is False, the transaction has NOT been persisted (either
    because *confirm* was False, or a hard issue blocked it).  Callers inspect
    *issues* to decide whether to present a confirmation prompt.
    """

    inp: TxnInput
    instrument: Instrument | None = None
    fee: Decimal
    tax: Decimal
    fee_rule_snapshot: dict[str, str] = Field(default_factory=dict)
    issues: list[Issue] = Field(default_factory=list)
    written: bool = False
    transaction_id: int | None = None


def enter_transaction(
    conn: sqlite3.Connection,
    inp: TxnInput,
    *,
    confirm: bool = False,
    today: date | None = None,
) -> TxnDraft:
    """Run the full manual-entry pipeline for a single transaction.

    Pipeline stages:
    1. Validate the input against the current ledger (account exists, qty/price
       positive, sell-exceeds-holdings guard).
    2. Resolve the symbol to an Instrument (exact match, else register-first).
    3. Auto-compute fee + tax from the account's FeeRuleSet, unless the caller
       already supplied explicit values on *inp*.
    4. If *confirm* is True **and** there are no hard (non-confirmable) issues,
       persist the transaction and set *written=True*.

    Args:
        conn:     Active SQLite connection with the schema in place.
        inp:      Validated :class:`TxnInput` from the caller.
        confirm:  When True, write the transaction if no hard issues block it.
                  Soft issues (``needs_confirm=True``) are bypassed on confirm.

    Returns:
        A :class:`TxnDraft` capturing the resolved instrument, computed
        fee/tax, all issues found, and the write outcome.
    """
    # --- 1. Validate ---
    issues: list[Issue] = list(validate_transaction(conn, inp, today=today))

    # --- 2. Resolve symbol ---
    res = resolve(conn, inp.symbol)
    instrument: Instrument | None = res.instrument
    if res.status is ResolutionStatus.NEEDS_AI:
        # HARD issue (2026-07-02): an unregistered symbol has no Instrument row — no
        # quote currency, no pricing worklist entry — so the ledger row would be
        # uninterpretable downstream (dashboard KeyError, permanently missing price).
        # There is no valid "confirm" path; the fix is to register the symbol first.
        # Code-shaped input resolves EXACT-only (R6-A), so this is the only non-exact
        # outcome; for name-shaped input, non-binding name suggestions are appended as a
        # hint — the resolver never binds them (no 「視為」 coercion).
        message = f"未註冊標的 {inp.symbol} — 請先至「標的管理」註冊後再入帳"
        message += suggestion_tail(res.candidates)
        issues.append(
            Issue(
                kind="symbol_unresolved",
                needs_confirm=False,
                message=message,
            )
        )

    # --- 3. Compute fee / tax (auto-fill only where caller left None) ---
    fee: Decimal | None = inp.fee
    tax: Decimal | None = inp.tax
    snapshot: dict[str, str] = {}

    acc = conn.execute(
        "SELECT fee_rule_set FROM accounts WHERE account_id=?",
        (inp.account_id,),
    ).fetchone()
    if acc is not None and (fee is None or tax is None):
        # Registry-authoritative ETF flag + its third state — ONE definition, shared with
        # csv_import (fees.resolve_etf_flag). Stress-audit 2026-07-15 made the registry win
        # over the input flag; AI-D40 (2026-08-24) closed the hole underneath it, where the
        # value SEEDING the registry was an unanswered False.
        is_etf, etf_unknown = resolve_etf_flag(instrument, inp.is_etf)
        # Market-aware fee rule (Batch B): a resolved instrument selects the rule set bound to
        # (account, its market); an unregistered symbol (instrument None) keeps the account
        # scalar exactly as before. Snapshot semantics are unchanged — today's single-market
        # bindings mirror the scalar, so the same rule-set name flows into the same snapshot.
        rule_name = (
            fee_rule_for(conn, inp.account_id, instrument.market)
            if instrument is not None else acc["fee_rule_set"]
        )
        rules = get_fee_rule_set(rule_name, conn)
        # FE-D2: the Moomoo US MY stamp needs the trade-date USD/MYR rate (fees.py is pure,
        # so the seam resolves it here, like is_etf). No rate -> stamp 0 + a soft issue.
        stamp_fx: Decimal | None = None
        if rules.has_us_stamp:
            stamp_fx = resolve_stamp_fx(conn, inp.trade_date)
            if stamp_fx is None:
                issues.append(Issue(
                    kind="stamp_fx_missing", needs_confirm=True,
                    message="無 USD/MYR 匯率,印花稅未計"))
        try:
            fr = compute_fees(
                rules,
                inp.side,
                inp.quantity,
                inp.price,
                is_etf=is_etf,
                daytrade=inp.daytrade,
                stamp_fx=stamp_fx,
            )
        except FeeComputationError as exc:
            # Overflow-sized input (M4): surface as a HARD issue, never a 500. The
            # magnitude guard in validate normally catches this first; this is the seam
            # for any path that reaches the quantize with a pathological value.
            issues.append(Issue(kind="fee_overflow", message=str(exc)))
        else:
            if etf_flag_issue_applies(rules, inp.side, etf_unknown):
                # Only when the unresolved flag actually MOVES the tax: a buy carries
                # no TW transaction tax, and a rule set whose two rates coincide gives
                # the same answer either way. Noise is how a real warning gets clicked
                # through, so this stays narrow.
                issues.append(Issue(
                    kind="etf_flag_unknown", needs_confirm=True,
                    message="無法判定是否為 ETF,賣出稅率待確認"
                            "(暫以現股稅率試算,請至標的管理設定)"))
            if fee is None:
                fee = fr.fee
            if tax is None:
                tax = fr.tax
            snapshot = dict(fr.snapshot)
            if etf_unknown:
                # The assumption travels with the numbers, so a later audit of this row
                # can see the rate was assumed rather than established.
                snapshot["etf_flag"] = "unknown"
            supplied = [k for k, v in (("fee", inp.fee), ("tax", inp.tax))
                        if v is not None]
            if supplied:
                # PARTIAL auto-fill: the snapshot describes the regime, but one of the
                # two numbers came from the caller, not from it. Say which.
                snapshot["supplied"] = ",".join(supplied)
    elif fee is not None and tax is not None:
        # Both supplied — record the provenance instead of leaving {} (which reads as
        # "no rule applied"). Same helper as csv_import so the two cannot drift.
        snapshot = supplied_snapshot(fee, tax)

    # Guarantee non-None for the draft (fallback to zero if account unknown)
    resolved_fee: Decimal = fee if fee is not None else Decimal("0")
    resolved_tax: Decimal = tax if tax is not None else Decimal("0")

    # --- 4. Persist if confirmed and no hard issues ---
    hard_issues = [i for i in issues if not i.needs_confirm]
    draft = TxnDraft(
        inp=inp,
        instrument=instrument,
        fee=resolved_fee,
        tax=resolved_tax,
        fee_rule_snapshot=snapshot,
        issues=issues,
    )
    if confirm and not hard_issues:
        symbol = instrument.symbol if instrument is not None else inp.symbol
        draft.transaction_id = insert_transaction(
            conn,
            account_id=inp.account_id,
            symbol=symbol,
            side=inp.side,
            quantity=inp.quantity,
            price=inp.price,
            fees=resolved_fee,
            tax=resolved_tax,
            trade_date=inp.trade_date,
            fee_rule_snapshot=snapshot,
            note=inp.note,
            daytrade=inp.daytrade,
            short_sale=inp.short_sale,
        )
        draft.written = True
    return draft
