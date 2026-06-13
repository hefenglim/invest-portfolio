"""Manual transaction entry: resolve → fee/tax → validate → confirm → persist."""

import sqlite3
from decimal import Decimal

from pydantic import BaseModel, Field

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.fees import compute_fees
from portfolio_dash.data_ingestion.resolve import ResolutionStatus, resolve
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
) -> TxnDraft:
    """Run the full manual-entry pipeline for a single transaction.

    Pipeline stages:
    1. Validate the input against the current ledger (account exists, qty/price
       positive, sell-exceeds-holdings guard).
    2. Resolve the symbol to an Instrument (exact → fuzzy → NEEDS_AI).
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
    issues: list[Issue] = list(validate_transaction(conn, inp))

    # --- 2. Resolve symbol ---
    res = resolve(conn, inp.symbol)
    instrument: Instrument | None = res.instrument
    if res.status is ResolutionStatus.NEEDS_AI:
        issues.append(
            Issue(
                kind="symbol_unresolved",
                needs_confirm=True,
                message=f"could not resolve {inp.symbol!r}",
            )
        )
    elif res.status is ResolutionStatus.FUZZY and instrument is not None:
        # The resolved symbol is already written below; surface the fuzzy match
        # as a soft (needs_confirm) issue so it is not silently accepted.
        issues.append(
            Issue(
                kind="fuzzy_resolved",
                needs_confirm=True,
                message=f"{inp.symbol} 視為 {instrument.symbol}（模糊比對，請確認）",
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
        fr = compute_fees(
            get_fee_rule_set(acc["fee_rule_set"]),
            inp.side,
            inp.quantity,
            inp.price,
            is_etf=inp.is_etf,
            daytrade=inp.daytrade,
        )
        if fee is None:
            fee = fr.fee
        if tax is None:
            tax = fr.tax
        snapshot = fr.snapshot

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
        )
        draft.written = True
    return draft
