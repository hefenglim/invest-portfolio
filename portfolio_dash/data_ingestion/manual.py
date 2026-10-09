"""Manual transaction entry: resolve → fee/tax → validate → confirm → persist.

Options (spec §4.2, plan W3): an OCC symbol resolves WITHOUT a write on a preview and is
registered inside the commit's transaction; a ``close_reason`` row costs nothing and skips the
engine; the option-mode intent refusals (OPT-D14 / OPT-D21 and its mirror) live here
(:func:`option_intent_issues`); an assignment / exercise is two rows written in ONE database
transaction (:func:`enter_pair`), the stock leg derived by :func:`assignment_stock_leg`; the
§4.1 price-0 close row of a position is built by :func:`close_row_input` — the helper the
expiry inbox's confirm (W6) reuses with :func:`enter_transaction`.
"""

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from portfolio_dash.data_ingestion.config_seed import get_fee_rule_set
from portfolio_dash.data_ingestion.fees import (
    ETF_FLAG_UNKNOWN_MESSAGE,
    OPTION_FEE_UNSET_MESSAGE,
    FeeComputationError,
    close_row_fees,
    compute_fees,
    etf_flag_issue_applies,
    option_trade_fees,
    resolve_etf_flag,
    supplied_snapshot,
)
from portfolio_dash.data_ingestion.fx_lookup import resolve_stamp_fx
from portfolio_dash.data_ingestion.holdings import shares_through
from portfolio_dash.data_ingestion.register import (
    option_contract_draft as option_contract_draft,
)
from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.resolve import (
    ResolutionStatus,
    resolve,
    suggestion_tail,
)
from portfolio_dash.data_ingestion.rules_binding import fee_rule_for
from portfolio_dash.data_ingestion.store import StoredTransaction, insert_transaction
from portfolio_dash.data_ingestion.validate import (
    Issue,
    TxnInput,
    advisory_issue,
    option_terms_of,
    validate_transaction,
)
from portfolio_dash.shared.enums import Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.options import (
    CloseReason,
    OptionTerms,
    shares_to_contracts,
)
from portfolio_dash.shared.wire import decimal_str

#: The manual door's option mode (spec §4.2): the four sides a button sends.
OptionIntent = Literal["STO", "BTC", "BTO", "STC"]
_ZERO = Decimal("0")


class TxnDraft(BaseModel):
    """Intermediate draft produced by :func:`enter_transaction`.

    When *written* is False, the transaction has NOT been persisted (either
    because *confirm* was False, or a hard issue blocked it).  Callers inspect
    *issues* to decide whether to present a confirmation prompt.

    ``registers_contract`` (options spec §8): the symbol is an OCC contract not registered yet
    — *instrument* is then an IN-MEMORY draft of the row the commit will write (a preview
    never writes one).
    """

    inp: TxnInput
    instrument: Instrument | None = None
    fee: Decimal
    tax: Decimal
    fee_rule_snapshot: dict[str, str] = Field(default_factory=dict)
    issues: list[Issue] = Field(default_factory=list)
    written: bool = False
    transaction_id: int | None = None
    registers_contract: bool = False


def option_intent_issues(
    conn: sqlite3.Connection, inp: TxnInput, intent: OptionIntent
) -> list[Issue]:
    """The manual door's option-mode refusals (spec §4.2, OPT-D14 / OPT-D21 + its mirror).

    *inp* already carries the side the intent implies (STO / STC → SELL, BTC / BTO → BUY) and
    its quantity in share-equivalents. The position is the account's signed count in the
    contract at the close of the trade date — the same ``holdings.shares_through`` the close
    rules read — so a new row (the highest id that day) is judged against everything before
    it. One row does one thing: a close never spills into an open, and an open never lands
    on the opposite side.
    """
    held = shares_through(conn, inp.account_id, inp.symbol, on=inp.trade_date)
    contracts = decimal_str(shares_to_contracts(abs(held)))
    if intent == "BTC":
        if held >= 0:
            return [Issue(kind="option_intent_no_short",
                          message="無空頭部位可平倉，請改選買進開倉")]
        if inp.quantity > -held:
            return [Issue(kind="option_intent_over_close",
                          message=(f"買進平倉口數超過空頭部位（{contracts} 口），"
                                   "請分成平倉與開倉兩筆"))]
    elif intent == "STC":
        if held <= 0:
            return [Issue(kind="option_intent_no_long",
                          message="無多頭部位可平倉，請改選賣出開倉")]
        if inp.quantity > held:
            return [Issue(kind="option_intent_over_close",
                          message=(f"賣出平倉口數超過多頭部位（{contracts} 口），"
                                   "請分成平倉與開倉兩筆"))]
    elif intent == "STO" and held > 0:
        return [Issue(kind="option_intent_opposite",
                      message=f"目前持有買方部位 {contracts} 口，請先賣出平倉")]
    elif intent == "BTO" and held < 0:
        return [Issue(kind="option_intent_opposite",
                      message=f"目前持有賣方部位 {contracts} 口，請先買進平倉")]
    return []


def intent_side(intent: OptionIntent) -> tuple[Side, bool]:
    """``(side, short_sale)`` an option-mode button means: selling to OPEN is the declared
    short sale (R1); the other three are ordinary rows."""
    if intent == "STO":
        return Side.SELL, True
    if intent == "STC":
        return Side.SELL, False
    return Side.BUY, False


def close_row_input(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    symbol: str,
    close_reason: CloseReason,
    trade_date: date,
    quantity: Decimal | None = None,
    note: str | None = None,
) -> TxnInput:
    """The §4.1 price-0 closing row of an option position — ONE builder for the manual door
    and the expiry inbox's confirm (W6), which writes it through :func:`enter_transaction`.

    The side is DERIVED from the position at the close of *trade_date* (a short closes with a
    BUY, a long with a SELL — spec §4.2); *quantity* (share-equivalents) defaults to the whole
    position, which is what an expiry must be. Fee and tax are 0 (``fees.close_row_fees``) and
    the row is never a declared short. A flat position builds a row the validator refuses
    (``option_close_no_position``) — the builder decides nothing a validator owns.
    """
    held = shares_through(conn, account_id, symbol, on=trade_date)
    side = Side.BUY if held < 0 else Side.SELL
    qty = quantity if quantity is not None else abs(held)
    return TxnInput(account_id=account_id, symbol=symbol, side=side, quantity=qty,
                    price=_ZERO, trade_date=trade_date, fee=_ZERO, tax=_ZERO,
                    short_sale=False, note=note, close_reason=close_reason)


def assignment_stock_leg(
    option_inp: TxnInput, terms: OptionTerms, stock_price: Decimal | None = None
) -> TxnInput:
    """The underlying's trade an assignment / exercise delivers (spec §4.1 / §4.2), DERIVED:
    symbol = the underlying, shares = the option leg's share-equivalents (contracts × 100),
    price = the strike unless *stock_price* overrides it, side from right × direction —
    an assigned (sold) put BUYS the stock, an assigned call SELLS it; an exercised (bought)
    call BUYS, an exercised put SELLS. Fee and tax are the engine's (left blank)."""
    assigned = option_inp.close_reason == "assigned"
    buys = (terms.right == "P") if assigned else (terms.right == "C")
    return TxnInput(account_id=option_inp.account_id, symbol=terms.underlying,
                    side=Side.BUY if buys else Side.SELL, quantity=option_inp.quantity,
                    price=stock_price if stock_price is not None else terms.strike,
                    trade_date=option_inp.trade_date, note=option_inp.note)


def enter_pair(
    conn: sqlite3.Connection,
    option_inp: TxnInput,
    stock_inp: TxnInput,
    *,
    confirm: bool = False,
    today: date | None = None,
) -> tuple[TxnDraft, TxnDraft]:
    """An assignment / exercise: the option's price-0 close row and the stock leg — validated
    together and, on *confirm*, written in ONE database transaction (spec §4.2: both or
    neither; a failed second leg rolls the first back).

    Nothing is written unless BOTH legs are free of hard issues; the caller decides on the
    soft ones (the pair's one acknowledgement is ``ack_oversell`` on a call assignment's
    stock SELL). The stock leg is validated against the ledger as it stands — the option leg
    moves no shares of the underlying, so it cannot change the stock leg's answer.
    """
    opt = enter_transaction(conn, option_inp, today=today)
    stock = enter_transaction(conn, stock_inp, today=today)
    if not confirm or any(not i.needs_confirm for i in [*opt.issues, *stock.issues]):
        return opt, stock
    try:
        opt = enter_transaction(conn, option_inp, confirm=True, today=today, commit=False)
        stock = enter_transaction(conn, stock_inp, confirm=True, today=today, commit=False)
        if not (opt.written and stock.written):
            raise RuntimeError("an assignment leg was refused at write time")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return opt, stock


def enter_transaction(
    conn: sqlite3.Connection,
    inp: TxnInput,
    *,
    confirm: bool = False,
    today: date | None = None,
    replacing: StoredTransaction | None = None,
    option_intent: OptionIntent | None = None,
    commit: bool = True,
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
        replacing: The stored row a ledger EDIT is previewing a replacement for (DEF-042);
                  handed to ``validate_transaction`` so the findings are those of the ledger
                  without that row. A preview only — combining it with ``confirm`` would
                  INSERT a second row, so that is refused.
        option_intent: The manual door's option-mode button (spec §4.2). Its refusals
                  (:func:`option_intent_issues`) REPLACE the CSV / edit doors' soft
                  ``option_over_cover`` warning, which is dropped here.
        commit:   ``False`` defers to the caller's transaction (:func:`enter_pair`); the
                  contract registration and the insert then commit with the caller's.

    Returns:
        A :class:`TxnDraft` capturing the resolved instrument, computed
        fee/tax, all issues found, and the write outcome.
    """
    if confirm and replacing is not None:
        raise ValueError("replacing is a preview-only argument; an edit writes through the "
                         "ledger correction door, never through an insert")
    # --- 1. Validate ---
    issues: list[Issue] = list(
        validate_transaction(conn, inp, today=today, replacing=replacing))
    if option_intent is not None:
        issues = [i for i in issues if i.kind != "option_over_cover"]
        issues.extend(option_intent_issues(conn, inp, option_intent))

    # --- 2. Resolve symbol ---
    res = resolve(conn, inp.symbol)
    instrument: Instrument | None = res.instrument
    terms = option_terms_of(instrument, inp.symbol)
    registers_contract = False
    if res.status is ResolutionStatus.NEEDS_AI and terms is not None:
        # Options spec §8 / OPT-D7: an OCC symbol resolves without a write — the preview says
        # what the commit will create, and only the commit (below) registers it.
        instrument = option_contract_draft(terms)
        registers_contract = True
        issues.append(advisory_issue(
            "option_contract_autoregister",
            f"將建立選擇權合約 {instrument.name}（{instrument.symbol}）"))
    elif res.status is ResolutionStatus.NEEDS_AI:
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
    if acc is not None and inp.close_reason is not None:
        # §4.2: a close row costs nothing and never reaches the engine (one definition).
        closed = close_row_fees()
        fee = fee if fee is not None else closed.fee
        tax = tax if tax is not None else closed.tax
        snapshot = closed.snapshot
    elif acc is not None and terms is not None and (fee is None or tax is None):
        # OPT-D6: the option branch — per contract + SEC on sells; no stamp, no ETF question.
        rules = get_fee_rule_set(fee_rule_for(conn, inp.account_id, Market.US), conn)
        try:
            opt = option_trade_fees(rules, inp.side, inp.quantity, inp.price,
                                    fee=inp.fee, tax=inp.tax)
        except FeeComputationError as exc:
            issues.append(Issue(kind="fee_overflow", message=str(exc)))
        else:
            if opt.unset:
                issues.append(Issue(kind="option_fee_unset", message=OPTION_FEE_UNSET_MESSAGE))
            fee, tax, snapshot = opt.fee, opt.tax, dict(opt.snapshot)
    elif acc is not None and (fee is None or tax is None):
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
                    message="無 USD/MYR 匯率，印花稅未計"))
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
                    message=ETF_FLAG_UNKNOWN_MESSAGE))
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
        registers_contract=registers_contract,
    )
    if confirm and not hard_issues:
        symbol = instrument.symbol if instrument is not None else inp.symbol
        try:
            if registers_contract:
                # OPT-D7: inside the commit's transaction — never on a preview.
                register_option_contract(conn, symbol, commit=False)
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
                close_reason=inp.close_reason,
                commit=False,
            )
            if commit:
                conn.commit()
        except Exception:
            if commit:
                conn.rollback()
            raise
        draft.written = True
    return draft
