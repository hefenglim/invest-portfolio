"""Ledger input validation: structural checks + the guards every write door shares.

Transactions, opening inventory, corporate actions and cash movements each have their
own entry point below. Shared by every write door (manual entry, CSV import, AI input),
so the guards hold no matter which path a row arrives on. For transactions:

* account exists; quantity/price positive (structural).
* sell-exceeds-holdings (soft — needs confirm).
* account↔instrument market coherence (audit H1 — HARD): a registered instrument's
  own market must match the account's market (derived from settlement ccy).
* negative fee/tax (audit H2 — HARD).
* overflow-sized shares/price (audit M4 — HARD): bound so the fee quantize downstream
  cannot raise ``InvalidOperation`` into a 500.
* future trade date (audit M5 — soft): flagged only when a clock is supplied.
* duplicate trade (audit M7 — soft): an identical row already exists.
"""

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel

from portfolio_dash.data_ingestion.holdings import (
    MAX_ACTION_DEPTH,
    PendingFlows,
    current_shares,
    load_action_index,
    shares_before_action_on,
    shares_through,
)
from portfolio_dash.data_ingestion.markets import CCY_MARKET, MARKET_ZH
from portfolio_dash.data_ingestion.rules_binding import allowed_markets
from portfolio_dash.data_ingestion.store import (
    get_instrument,
    get_opening,
    list_accounts,
    list_corporate_actions,
    load_ledger_bundle,
)
from portfolio_dash.portfolio.cost_basis import build_book
from portfolio_dash.portfolio.results import Book
from portfolio_dash.shared.cash_kinds import (
    CASH_KIND_VALUES,
    CASH_KIND_ZH,
    CashKind,
    canonical_kind,
    is_fx_acquisition,
)
from portfolio_dash.shared.corporate_actions import (
    ActionIndex,
    CorporateAction,
    CorporateActionKind,
    apply_ratio_to_price,
    convert_stored,
    is_ratio_term,
)
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.ledger_events import EventPriority
from portfolio_dash.shared.models.assets import Account, Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import LedgerBundle
from portfolio_dash.shared.money import from_db, quantize_amount
from portfolio_dash.shared.wire import decimal_str

# Overflow guard (audit M4): shares/price above this are rejected as a hard issue so the
# downstream fee quantize (fees._round) can never overflow the Decimal context into a 500.
_MAX_MAGNITUDE = Decimal("1e12")
_ZERO = Decimal("0")

# E23 (D22). A named constant because the finding and its REPAIR
# (:func:`identifier_change_repair`) have to stay welded together: the API offers the
# one-click convert keyed on this string, and a rename that reached only one of the two
# would leave a warning nothing can fix, or a fix attached to nothing.
IDENTIFIER_CHANGE_SUSPECTED = "identifier_change_suspected"

# D44 (owner ruling 2026-08-15). Named for the same reason as E23 above: the form preview
# attaches the one-click restate keyed on this string, and :func:`restated_band` computes
# the number the message quotes, so the finding and the number it offers are one change.
TARGET_BAND_PREDATES_SPLIT = "target_band_predates_split"

# D48a. Named because two writers act on it (``api/routers/ledgers.py`` and
# ``corporate_action_import.py``) and because it is E10's rejection made soft for exactly
# one shape — a constant makes that exception greppable rather than a string in three files.
SPINOFF_CHILD_AUTOREGISTER = "spinoff_child_autoregister"


class TxnInput(BaseModel):
    """Validated input for a single transaction before it is persisted."""

    account_id: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    trade_date: date
    fee: Decimal | None = None
    tax: Decimal | None = None
    daytrade: bool = False
    is_etf: bool = False
    # Declared short sale (2026-07-31): exempts the sell from the 賣超 guard and opens a
    # short lot in the replay. Deliberate per-transaction choice; never inferred.
    short_sale: bool = False
    note: str | None = None


class Issue(BaseModel):
    """A validation finding returned by :func:`validate_transaction`."""

    kind: str
    message: str
    needs_confirm: bool = False


def _same_ratio(a_to: Decimal, a_from: Decimal, b_to: Decimal, b_from: Decimal) -> bool:
    """True iff two ratios are the SAME ratio — 「3 比 1」 and 「30 比 10」 are one event.

    Cross-multiplied, so no quotient is ever formed and no rounding can decide the answer
    (the same rule :func:`~portfolio_dash.shared.corporate_actions.apply_ratio` obeys).
    Comparing the terms pairwise instead is what let one event be stored twice under two
    spellings and made ``split_factor`` return 9 for a 3-for-1 (audit F-06 / F-10).
    """
    return a_to * b_from == b_to * a_from


def _depth_cap_issue(index: ActionIndex, symbols: set[str]) -> Issue | None:
    """D31's ``needs_confirm`` for a share walk that hit the corporate-action depth cap.

    The read paths kept their bare ``Decimal`` and degraded to the action-UNAWARE count, so
    the number in hand is defined but pre-action. Validation must not guess on top of it —
    it takes the 賣超 tier: blocking until the owner acknowledges, never silently. This is
    D31's whole design: no ``Decimal | None``, no sixth vocabulary for "not trustworthy",
    just the two mechanisms the codebase already has.
    """
    hit = sorted(f"{sym}（{acct}）" for acct, sym in index.depth_capped_symbols()
                 if sym in symbols)
    if not hit:
        return None
    return Issue(
        kind="action_chain_too_deep",
        needs_confirm=True,
        message=(f"{'、'.join(hit)} 的公司行動鏈太長（超過 {MAX_ACTION_DEPTH} 層），"
                 "系統改用未套用公司行動的股數來檢查，這個數字可能不是最新的。"
                 "請先確認該標的的公司行動紀錄是否有循環或重複"),
    )


# Batch B (2026-07-21): legacy Moomoo account ids merged into ``moomoo_my``. An uploaded CSV
# authored before the merge may still carry a legacy id; the importers rewrite it here so the
# row lands on the merged account. ONE map + helper, shared by every CSV importer (transactions,
# dividends, fx, opening) — never five copies. The single-trade manual path uses the registered
# account dropdown, which only offers the current ids, so it needs no aliasing.
LEGACY_ACCOUNT_ALIAS: dict[str, str] = {
    "moomoo_my_us": "moomoo_my",
    "moomoo_my_my": "moomoo_my",
}


def alias_import_account(raw_account: str) -> tuple[str, Issue | None]:
    """Resolve a CSV-supplied account id, mapping a legacy Moomoo id to ``moomoo_my``.

    Returns ``(resolved_id, issue)``: for a legacy id, ``resolved_id`` is ``moomoo_my`` and
    ``issue`` is a SOFT (``needs_confirm=True``) info finding announcing the auto-conversion;
    for any other id, ``(raw_account, None)`` (byte-identical passthrough). The issue is soft
    (not hard) on purpose — a hard/non-confirmable issue would BLOCK the row's commit
    (:attr:`preview.PreviewRow.has_hard_issue`), defeating the merge; soft lets the aliased
    row import once accepted while still surfacing the notice.
    """
    resolved = LEGACY_ACCOUNT_ALIAS.get(raw_account)
    if resolved is None:
        return raw_account, None
    return resolved, Issue(
        kind="account_alias",
        needs_confirm=True,
        message=f"帳戶 {raw_account} 已合併為 {resolved},已自動轉換",
    )


def unknown_account_issue(account_id: str) -> Issue:
    """The ONE unknown-account finding, for every door that has an account to check (L-1).

    Six sites built this sentence for themselves — ``validate_transaction``,
    ``validate_corporate_action``, ``validate_cash_movement``, and the fx / dividend /
    opening importers — each interpolating the id into 「帳戶 <id> 不存在」. Wave 7 (K-2) fixed
    the blank case at ONE of them, which left the owner reading 「帳戶不可空白」 from 現金 and
    「帳戶  不存在」 — two spaces, no name — from the other four, for the identical blank cell
    in the identical column. That is the cross-door divergence ``architecture.md``'s C3 seam
    exists to prevent, reached from the other direction: not two guards disagreeing, but one
    guard's wording forked five ways. It is a message, so it gets a function, not a seam.

    **A BLANK account is not an UNKNOWN one.** Interpolating an empty id claims that an
    account the owner never typed does not exist, in the one column whose whole job is to say
    which cell to go and fix; from a door that does not strip its input (the manual forms) it
    rendered 「帳戶     不存在」. The predicate is therefore ``.strip()``, not ``== ""``.

    **The blank sentence names NO column and NO form field, on purpose** (K-2's reasoning,
    now binding on five more doors): the callers span CSV cells called ``account`` and wire
    fields called ``account_id``, and which of the two to print is presentation — it already
    lives in ``api/wire.py::_ISSUE_FIELD`` and ``api/routers/cash.py::_MOVEMENT_ISSUE_FIELD``.
    (Rejected for that reason: reusing ``csv_import``'s 「必填欄位不可空白（欄位 account）」,
    which names a CSV column the manual forms have not got.)

    The ``kind`` is ``unknown_account`` at every call site and is unchanged, so no wire
    contract moves; the finding stays HARD (``needs_confirm=False``) everywhere.

    ⚠ Not yet the owner of the SAME sentence in ``portfolio_dash/api/routers/``, which build
    it by hand for ``error_body`` envelopes rather than for an :class:`Issue`. Sharing it
    there wants a message-only function under this one; it was out of L-1's scope and is
    recorded so the next reader finds the copies rather than the divergence.

    **Six copies remain**, re-measured 2026-08-29 and listed by FUNCTION rather than counted
    per module — a bare tally cannot say WHICH copy a later wave already took, and this list
    was misread once for exactly that reason: ``broker_import.broker_convert`` ·
    ``cash.fx_change_guard`` · ``cash.cash_statement`` · ``cash.cash_acq_rate`` ·
    ``input_center.input_holdings`` · ``ledgers._mutation_guard``. ``input_center`` held
    **two**; the auto-register refusal in ``manual_commit`` was registered on this helper in
    wave 9, so the door still appears above — one copy fixed is not the door done.
    """
    return Issue(
        kind="unknown_account",
        message=("帳戶不可空白" if not account_id.strip() else f"帳戶 {account_id} 不存在"),
    )


def validate_opening_cost(original_cost_total: Decimal) -> Issue | None:
    """F-13 (D37): an opening's ``original_cost_total`` must be **> 0** — HARD.

    ONE rule, one message, called by every door that writes an ``opening_inventory`` row (the
    CSV importer — which is also the single-row 期初 form — and the correction route). It
    lives here rather than in ``store.upsert_opening`` for the reason
    :func:`validate_corporate_action_change` states: the store's job is persistence, and a
    rule that exists at one of several write doors is how E13 came to be insert-only.

    **Why hard, and why zero specifically.** The file already hard-validated ``shares > 0``
    and the correction route already refused a NEGATIVE total; zero sat in the gap between
    them, and it is the value D37's owner-input problem makes tempting — 「I cannot find what
    I paid for this, I will put 0 for now」. A zero total imports cleanly and permanently
    zeroes the position's basis **with no 待釐清 flag**: every return rate divides by it,
    ``payback_ratio`` reads 0, and nothing on any screen says the number is unfounded. That
    is strictly worse than the 賣超 it appears to fix, because the oversell at least
    announces itself. An asymmetry repair, not a new mechanism.

    Returns the :class:`Issue` (``needs_confirm=False``) or ``None`` when the total is fine.
    """
    if original_cost_total > _ZERO:
        return None
    return Issue(
        kind="non_positive_opening_cost",
        message=(
            f"原始總成本必須大於 0（目前是 {original_cost_total}）。"
            "期初庫存的成本是這個部位所有報酬率與均價的分母 — 填 0 會讓成本基礎永久歸零，"
            "而且畫面上不會出現任何「待釐清」標記，看起來完全正常。"
            "請翻出當初買進的對帳單，填入實際付出的總金額（含手續費與稅）；"
            "若一時查不到，寧可先不要登錄這一筆期初庫存，也不要填 0。"
        ),
    )


def pending_share_flows(
    batch: Sequence[TxnInput], *, exclude: TxnInput | None = None
) -> PendingFlows:
    """Turn a batch of not-yet-written transactions into :data:`PendingFlows`.

    The convention matches ``validate_cash_movement``: *batch* is every row committed
    together **including** the one being validated, and the caller names that one as
    *exclude* so it is not counted against itself.

    A DECLARED short sale contributes its flow like any other sell. Its own row is exempt
    from the guard, but the shares still leave the position and a LATER sibling sell has to
    see that they did.
    """
    flows: dict[tuple[str, str], list[tuple[date, int, Decimal]]] = {}
    for other in batch:
        if other is exclude:
            continue
        is_buy = other.side is Side.BUY
        flows.setdefault((other.account_id, other.symbol), []).append((
            other.trade_date,
            int(EventPriority.BUY if is_buy else EventPriority.SELL),
            other.quantity if is_buy else -other.quantity,
        ))
    return flows


def transaction_structural_issues(inp: TxnInput) -> list[Issue]:
    """The ROW-LEVEL structural prefix of :func:`validate_transaction` — every check that
    depends on nothing but the row's own numbers (M4/H2 family): non-positive or
    overflow-sized quantity/price, negative fee/tax.

    Split out (FIX-A1b, Master probe V6, 2026-08-30) because it now answers TWO questions
    and they must never diverge: it is the row's own structural verdict inside
    :func:`validate_transaction`, and it decides sibling-BATCH MEMBERSHIP for the
    transactions CSV door — ``cash_import._pool_free_issues``'s role, played by the same
    one owner ("re-running the one owner of those rules is deliberate — restating them
    would make this module a second owner"). A row that can never be written can never
    cover: a buy priced −50.00 is a hard ``error`` on its own line, yet it still counted
    in the oversell guard's batch, so the sell it "covered" previewed ``ok`` and a
    no-select commit wrote it alone — 200 / written 1 / a lone unacked oversold SELL,
    reachable by a mere typo. The cash door already excludes such rows (QA-01's
    "a structurally invalid row was already out of the batch" — true for cash/fx, false
    for transactions until this function).

    Deliberately ONLY the ledger-independent checks — decidable from the ``TxnInput``
    alone, so membership needs no connection and no second validation pass. The remaining
    HARD kinds stay in the batch because none of them can fund a FALSE cover:
    ``unknown_account`` and ``market_mismatch`` hit every row of the same
    ``(account, symbol)`` key equally (the only key a pending flow can cover), so the
    "covered" sell is just as un-writable; ``symbol_unresolved`` likewise — pending flows
    are keyed on the RAW symbol, and the same raw spelling resolves (or fails) the same
    way for both rows; ``fee_overflow`` sits downstream of the M4 bound enforced HERE,
    whose whole purpose is that a row passing it cannot overflow the fee quantize — and it
    is computed per row after batch assembly, so admitting it into membership would take a
    circular second pass to exclude a case the bound already excludes.
    """
    issues: list[Issue] = []
    if inp.quantity <= 0:
        issues.append(Issue(kind="non_positive_quantity", message="股數必須大於 0"))
    elif inp.quantity > _MAX_MAGNITUDE:
        issues.append(Issue(kind="amount_too_large", message="股數過大,無法處理"))
    if inp.price <= 0:
        issues.append(Issue(kind="non_positive_price", message="價格必須大於 0"))
    elif inp.price > _MAX_MAGNITUDE:
        issues.append(Issue(kind="amount_too_large", message="價格過大,無法處理"))
    if inp.fee is not None and inp.fee < 0:
        issues.append(Issue(kind="negative_fee", message="手續費不可為負"))
    if inp.tax is not None and inp.tax < 0:
        issues.append(Issue(kind="negative_tax", message="交易稅不可為負"))
    return issues


def validate_transaction(
    conn: sqlite3.Connection,
    inp: TxnInput,
    *,
    today: date | None = None,
    index: ActionIndex | None = None,
    batch: Sequence[TxnInput] = (),
) -> list[Issue]:
    """Run validation checks on *inp* against the current ledger state.

    Returns a (possibly empty) list of :class:`Issue` objects.  An empty list
    means the transaction is clean.  Issues with ``needs_confirm=True`` require
    explicit user confirmation before the transaction may be persisted (e.g.
    selling more than currently held, a future trade date, or a duplicate row).

    *today* (usually ``get_now().date()``) enables the future-date soft check; when
    omitted (the pure CSV/AI parse paths) that check is skipped.

    *index* is one :class:`ActionIndex` shared across a batch (D23 rule 2). This function
    runs ONCE PER ROW of an import, so a 1,375-row CSV re-reads and re-groups the whole
    corporate-action ledger 1,375 times unless the caller threads one in (trap #21).

    *batch* is every row being committed together, INCLUDING *inp* — the convention
    ``validate_cash_movement`` established. **Only the oversell guard reads it**, and only
    through the share walker, so a sibling buy dated before a split reaches a later sell
    already multiplied. Left empty (the default, and every single-row door) the guard
    behaves exactly as it did: this widens what the check can SEE, never what it permits.
    """
    issues: list[Issue] = []

    # --- account exists (+ its market, for the coherence guard) ---
    acc = conn.execute(
        "SELECT settlement_ccy FROM accounts WHERE account_id=?", (inp.account_id,)
    ).fetchone()
    if acc is None:
        issues.append(unknown_account_issue(inp.account_id))

    # --- quantity/price positive and bounded (M4) + negative fee/tax (H2) ---
    # One owner: the SAME structural prefix that decides sibling-batch membership at the
    # CSV door (FIX-A1b) — see :func:`transaction_structural_issues` for why the two
    # questions must not diverge. Same checks, same kinds, same order as before the split.
    issues.extend(transaction_structural_issues(inp))

    # --- account↔instrument market coherence (H1): only when BOTH are known ---
    # Batch B: relaxed from a 1:1 (account market == instrument market) check to a
    # SET membership test — a row is coherent iff the instrument's market is one of the
    # account's ALLOWED markets (the bound set; a merged Moomoo account holds US + MY).
    # For a single-market account the allowed set is the settlement-derived singleton, so
    # this is behavior-identical to the prior check. ``acct_mkt`` (settlement-derived) is
    # kept as the None-guard for an unmapped ccy AND as the account-side message label, so
    # the single-market rejection message stays byte-identical.
    inst = get_instrument(conn, inp.symbol)
    if acc is not None and inst is not None:
        acct_mkt = CCY_MARKET.get(acc["settlement_ccy"])
        if acct_mkt is not None and inst.market not in allowed_markets(conn, inp.account_id):
            issues.append(
                Issue(
                    kind="market_mismatch",
                    message=(
                        f"{inp.symbol} 屬 {inst.market.value} 市場,"
                        f"不可登錄於 {MARKET_ZH.get(acct_mkt, acct_mkt.value)}帳戶"
                    ),
                )
            )

    # --- sell must not exceed holdings (soft) ---
    # A DECLARED short sale (spec 2026-07-31 option C) is exempt: exceeding the position is
    # the whole point, the intent is recorded on the row, and the replay opens a short lot
    # with a real cost basis instead of discarding one.
    if inp.side is Side.SELL and inp.quantity > 0 and not getattr(inp, "short_sale", False):
        # ACTION-AWARE (W4, spec §6.2): both counts now replay corporate actions, because a
        # guard reading the pre-split count blocks a legitimate post-split sell and reports
        # it as the owner's data error. For a symbol with no corporate action this takes the
        # pre-existing branch unchanged (D38 invariant 1).
        walk_index = index if index is not None else load_action_index(conn)
        # SIBLING-AWARE (2026-08-14): a CSV whose covering buy is three lines above the sell
        # used to raise 賣超 on the sell, because both counts read the STORED ledger only.
        # That is the one guard whose confirmation permanently discards a cost basis, so a
        # bulk import that raises it spuriously trains the owner to click exactly the button
        # that must stay frightening. Measured on a synthetic broker export: 7 of 47 rows.
        pending = pending_share_flows(batch, exclude=inp)
        held = current_shares(
            conn, inp.account_id, inp.symbol, index=walk_index, pending=pending)
        # DATE-AWARE (2026-07-31): the position that must cover the sell is the one that
        # exists on its OWN trade date. `current_shares` nets across all dates, so a
        # back-dated sell covered only by a LATER buy passed silently — and the replay then
        # discarded the symbol's cost basis for good. The cash ledger has had the equivalent
        # running-balance check since audit C3; this closes the same hole on the share side.
        held_then = shares_through(
            conn, inp.account_id, inp.symbol,
            on=inp.trade_date, index=walk_index, pending=pending,
        )
        if (capped := _depth_cap_issue(walk_index, {inp.symbol})) is not None:
            issues.append(capped)
        if inp.quantity > held_then or inp.quantity > held:
            # Traditional Chinese, because the UI renders this string as the HEADLINE of the
            # 賣超 confirmation dialog — directly above the Chinese sentence explaining that
            # confirming permanently discards the position's cost basis — and
            # ``ledgers.py::_oversell_response`` embeds it inside a Chinese wrapper. The old
            # `sell 150 > held 100` made one sentence read half in each language, on the one
            # dialog whose whole job is to make sure the owner understood before acking.
            # The NUMBERS are the message; they survive the translation unchanged.
            if inp.quantity > held_then and inp.quantity <= held:
                msg = (f"賣出 {inp.quantity} 股，超過 {inp.trade_date.isoformat()} 當日"
                       f"持有的 {held_then} 股（目前淨額 {held} 股，"
                       f"但那一天只有 {held_then} 股）")
            else:
                msg = f"賣出 {inp.quantity} 股，超過持有的 {held} 股"
            issues.append(
                Issue(
                    kind="sell_exceeds_holdings",
                    needs_confirm=True,
                    message=msg,
                )
            )

    # --- future trade date (M5, soft) — only when a clock is supplied ---
    if today is not None and inp.trade_date > today:
        issues.append(
            Issue(
                kind="future_trade_date",
                needs_confirm=True,
                message=f"交易日期 {inp.trade_date.isoformat()} 晚於今日,確認無誤?",
            )
        )

    # --- duplicate trade (M7, soft): an identical row already exists ---
    if _duplicate_exists(conn, inp):
        issues.append(
            Issue(
                kind="duplicate_trade",
                needs_confirm=True,
                message="相同交易已存在(今日已登錄一筆相同買賣),確認要再次寫入?",
            )
        )

    return issues


class CorporateActionInput(BaseModel):
    """User-supplied corporate action, BEFORE it is trusted.

    Deliberately permissive where :class:`CorporateAction` is strict: ``kind`` is a plain
    string and the ratio terms are unconstrained Decimals. That is the whole point — a
    non-integer ratio has to reach this function to be REJECTED with a zh message. A model
    that refused to construct would turn the owner's typo into a 500 or an English
    pydantic error, which is the opposite of the intent.
    """

    account_id: str
    date: date
    kind: str
    from_symbol: str
    to_symbol: str
    ratio_to: Decimal
    ratio_from: Decimal
    cost_carry: Decimal | None = None
    note: str | None = None


def _has_prices(
    conn: sqlite3.Connection, symbol: str, *, before: date | None = None
) -> bool:
    """True iff ``prices`` holds a row for *symbol* (dated strictly before *before*).

    **A direct SELECT, not an import.** ``prices`` belongs to ``pricing/`` and
    ``data_ingestion → pricing`` is not an edge in ``architecture.md``; the same problem is
    solved the same way from the other side in ``pricing/ingest.py``, which reads holdings
    and the watchlist straight from SQL rather than importing ``data_ingestion``. Layering
    constrains the import graph — both modules already share one connection.

    **A missing table reads as "no prices".** ``prices`` is created by
    ``pricing.schema.create_tables``, which ``bootstrap_db`` does not call, so a ledger-only
    database (CSV/AI parse paths, most unit tests) has no such table. Degrading to False
    keeps an ``OperationalError`` out of an import preview; the two callers below then stay
    silent (E23, which needs prices to EXIST on the destination) or warn (N3-price, which
    is telling the truth — there is no stored price).
    """
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='prices'"
    ).fetchone() is None:
        return False
    sql = "SELECT 1 FROM prices WHERE instrument=?"
    params: tuple[str, ...] = (symbol,)
    if before is not None:
        sql += " AND as_of_date < ?"
        params = (symbol, before.isoformat())
    return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def _accounts_holding_on(
    conn: sqlite3.Connection, symbol: str, on: date, *, index: ActionIndex | None = None
) -> set[str]:
    """Accounts with a NON-ZERO position in *symbol* on *on* (E13's N).

    Both halves had to become action-aware, and the enumeration half is the one that fails
    SILENTLY (audit F-07). The candidate set was ``transactions UNION opening_inventory``,
    so a position acquired PURELY by an EXCHANGE or a SPINOFF appears in neither table and
    ``shares_through`` was never even called for it — E13's all-accounts rule then went
    quiet in exactly the state D13 says "must not be reachable at all". Reproduced on
    ``HEAD``: two accounts each exchanged AAA→XYZ, ``build_book`` reported XYZ in both, and
    this function returned the empty set.

    The corporate-action ledger therefore joins the union, on EITHER end — matching
    ``list_corporate_actions(symbol=…)``'s own convention. A source-side row can only exist
    where some other ledger already put the position, so the ``to_symbol`` clause is the one
    that closes the hole; ``from_symbol`` costs nothing and covers a stranded row.
    """
    rows = conn.execute(
        "SELECT DISTINCT account_id FROM ("
        "  SELECT account_id FROM transactions WHERE symbol=? AND trade_date<=?"
        "  UNION SELECT account_id FROM opening_inventory WHERE symbol=? AND build_date<=?"
        "  UNION SELECT account_id FROM corporate_actions"
        "         WHERE (to_symbol=? OR from_symbol=?) AND date<=?"
        ")",
        (symbol, on.isoformat(), symbol, on.isoformat(),
         symbol, symbol, on.isoformat()),
    ).fetchall()
    held: set[str] = set()
    for r in rows:
        account_id = str(r[0])
        if shares_through(conn, account_id, symbol, on=on, index=index) != 0:
            held.add(account_id)
    return held


def _book_before(
    conn: sqlite3.Connection,
    day: date,
    *,
    bundle: LedgerBundle | None,
    cache: dict[date, Book] | None,
    pending: Sequence[CorporateAction] = (),
) -> Book:
    """The replayed book as a corporate action dated *day* sees it — memoised per date.

    ``allow_oversell=True`` because this is a validation read, not a booking: it must
    return the degraded book so the caller can inspect the flags, never raise. A raise
    here would turn a data problem into a 500 on the import preview.

    *pending* (FIX-A2, QA BUG-04 / corpus F4, 2026-08-29) is the row's SIBLING batch
    actions, already converted — the replay applies those sorting STRICTLY before
    ``(day, EventPriority.CORPORATE_ACTION)``, the same cut the share walk's index already
    uses. All corporate actions share that priority, so the cut is ``p.date < day``: a
    same-date sibling never applies, and a batch row can never justify itself. Without
    this, the four book-derived rejections (E3/E22/E5/E18) replayed the STORED ledger only,
    so a chain whose prerequisite action is same-batch (the GGR shape: EXCHANGE PPGH→GGR,
    then a reverse SPLIT of GGR, with the post-exchange trades already stored) hard-rejected
    ``oversold_source`` on pass 1 while a byte-identical pass 2 wrote it — an undocumented
    two-pass whose rejection message misdirected the owner to 補登 a buy that exists.

    **Cache discipline** (the FIX-A2 leak question, answered structurally): the applied
    subset is a pure function of (batch, *day*) — every row of one batch dated *day* yields
    the SAME strictly-before siblings, itself and every same-date row excluded by the cut —
    so under ONE batch a per-date entry serves every row of that date, which is exactly
    trap #21's bound (one replay per distinct action date, not per row). A cache may be
    SHARED across calls with different batches only while every sharer derives the same
    subset at every date it reads. The one sharing caller (``ledgers._preview_payload`` →
    ``_split_conversion``) satisfies that by construction: both its batches are single-date
    (one submitted action × N accounts), and a single-date batch's strictly-before subset is
    EMPTY at the only date it reads — its entries are stored-only books, and it can neither
    write nor read a widened one. A future caller with a multi-date batch must own its cache
    for the life of that batch, as the CSV importer does.

    A widened replay that cannot book (e.g. a pending SPINOFF whose child is not registered
    yet — ``build_book``'s ``quote_ccy`` raises ``KeyError``) degrades to the stored-only
    book, i.e. exactly the pre-FIX-A2 verdict, never a 500 — and is NOT cached, because it
    is the degradation, not the (batch, date)-scoped answer. That sibling's own refusals
    (E10 / D48a) already tell the story on its own row. The stored-only replay keeps its
    pre-existing contract: callers pre-check reachability, so it may raise.
    """
    if cache is not None and day in cache:
        return cache[day]
    applied = [p for p in pending if p.date < day]
    full = bundle if bundle is not None else load_ledger_bundle(conn)
    if applied:
        try:
            widened = replace(full, actions=[*full.actions, *applied])
            book = build_book(widened.before_action_on(day), allow_oversell=True)
        except (ValueError, KeyError):
            return build_book(full.before_action_on(day), allow_oversell=True)
    else:
        book = build_book(full.before_action_on(day), allow_oversell=True)
    if cache is not None:
        cache[day] = book
    return book


def validate_corporate_action(  # noqa: C901, PLR0912 - one check per §5 edge row, flat by design
    conn: sqlite3.Connection,
    inp: CorporateActionInput,
    *,
    batch: Sequence[CorporateActionInput] = (),
    bundle: LedgerBundle | None = None,
    book_cache: dict[date, Book] | None = None,
    index: ActionIndex | None = None,
) -> list[Issue]:
    """Validate one corporate action against the ledger (spec §5 edge matrix).

    *batch* is every row being committed together, INCLUDING *inp* — E12 and E13 are
    batch-level rules, and a per-row check that cannot see its siblings would reject a
    correct multi-account entry and accept a partial one.

    *bundle* is the full :class:`LedgerBundle`, hoisted by the caller so a batch reads the
    database ONCE. Loaded here when omitted.

    ⚠ **This used to be a ``book`` parameter, and taking a pre-replayed book was the defect
    (2026-08-11).** Four hard rejections below — **E3** oversold source, **E22** oversold
    destination, **E5** short source, **E18** short destination — are evaluated from a
    replayed :class:`Book`, and a book replayed over the WHOLE ledger shows them a future
    that has not happened yet. On any bulk import the post-action trades are already
    recorded (that is what a broker export *is*), so the affected position is **already
    賣超 when its own action is validated**, and E3 hard-rejects the row that resolves the
    賣超 — advising the owner to fabricate a buy instead of recording the split. Measured
    on §1's headline case: buy 100, 7-for-1, sell 400.

    The function therefore replays ``bundle.before_action_on(inp.date)`` **itself**. Taking
    a bundle rather than a book is the point: a caller cannot hand in a wrongly-scoped
    replay, because it cannot hand in a replay at all. Nothing is weakened — ``_apply_action``
    still enforces all four in true chronological order on every replay, so a genuinely
    oversold position is refused when the replay reaches the action.

    *book_cache* is the caller's ``{action_date: Book}`` dict, filled here. Trap #21's real
    cost for this check is **one replay per distinct action date**, not per row; an importer
    hoisting a single book for a whole file is hoisting the wrong thing.

    *index* is the batch's :class:`ActionIndex`; built here when omitted. Every share query
    below threads it, which is what makes E1a and E13 see the SAME position the replay does.

    Hard issues (``needs_confirm=False``) make the row uncommittable; soft issues
    (``needs_confirm=True``) block until acknowledged and then commit — the 賣超 tier.
    """
    issues: list[Issue] = []
    add = issues.append
    walk_index = index if index is not None else load_action_index(conn)

    # --- kind must be one of the three (structural; everything below reads it) ---
    try:
        kind = CorporateActionKind(inp.kind.strip().upper())
    except ValueError:
        add(Issue(kind="unknown_action_kind",
                  message=f"未知的公司行動類型 {inp.kind}(僅支援 分割 / 換股 / 分拆)"))
        return issues

    if conn.execute("SELECT 1 FROM accounts WHERE account_id=?",
                    (inp.account_id,)).fetchone() is None:
        add(unknown_account_issue(inp.account_id))

    # --- E6 / E6a (D14): both ratio terms are POSITIVE INTEGERS ---
    # Not "Decimal > 0": that admits 0.2857, a rounded quotient, which shorts a 2-for-7 of
    # 700 shares to 199.9900 — and the later sell of 200 trips the 賣超 guard, whose
    # acknowledgement discards the position's cost basis permanently. ratio_from == 0 would
    # additionally divide by zero INSIDE the replay, i.e. a 500 on the dashboard path.
    for label, term in (("換得股數", inp.ratio_to), ("換出股數", inp.ratio_from)):
        if not is_ratio_term(term):
            add(Issue(kind="ratio_not_positive_integer",
                      message=(f"{label} 必須是正整數,目前是 {term}。"
                               "公司行動的比例請填兩個整數（例如 3 換 1、1 換 20、2 換 7），"
                               "不要填算好的小數 — 小數會讓股數短少，之後賣出時會被誤判為賣超")))

    # --- E20: to_symbol vs from_symbol coherence, per kind ---
    same_symbol = inp.from_symbol == inp.to_symbol
    if kind is CorporateActionKind.SPLIT and not same_symbol:
        add(Issue(kind="split_symbol_mismatch",
                  message=f"分割的標的必須相同({inp.from_symbol} → {inp.to_symbol});"
                          "若標的有變更，請改用「換股」"))
    if kind is not CorporateActionKind.SPLIT and same_symbol:
        zh = "換股" if kind is CorporateActionKind.EXCHANGE else "分拆"
        add(Issue(kind="self_referential_action",
                  message=f"{zh}的來源與目的標的不可相同({inp.from_symbol});"
                          "若只是股數變動，請改用「分割」"))

    # --- E8 / E9: cost_carry ---
    if kind is CorporateActionKind.SPINOFF:
        if inp.cost_carry is None:
            add(Issue(kind="missing_cost_carry",
                      message="分拆必須填寫成本分攤比例（母公司移轉給子公司的成本佔比）"))
        elif not (Decimal("0") <= inp.cost_carry <= Decimal("1")):
            add(Issue(kind="cost_carry_out_of_range",
                      message=f"成本分攤比例必須介於 0 與 1 之間,目前是 {inp.cost_carry}"))
        elif inp.cost_carry == Decimal("1"):
            # E9 soft — and the text must name the DISPLAY consequence, not just the basis.
            add(Issue(kind="cost_carry_all", needs_confirm=True,
                      message=("成本分攤比例為 1：母公司的成本會全部移轉給子公司，"
                               "母公司帳上成本歸零。連帶影響回本進度 — 真正收過股利的母公司"
                               "會顯示 0.00%，而從未配息的子公司會顯示母公司原本的進度，"
                               "甚至可能標示「已回本」。確定要這樣登錄嗎？")))
    elif inp.cost_carry is not None:
        add(Issue(kind="cost_carry_not_applicable",
                  message=f"成本分攤比例僅適用於分拆,{inp.kind} 不需填寫"))

    # --- E7: a no-op SPLIT (soft; ratio == 1 on an EXCHANGE is the ordinary rename) ---
    if kind is CorporateActionKind.SPLIT and inp.ratio_to == inp.ratio_from:
        add(Issue(kind="split_ratio_one", needs_confirm=True,
                  message="分割比例為 1 比 1，這筆不會改變任何股數。確定要登錄嗎？"))

    # --- E10 / D19: BOTH symbols must be REGISTERED. Keyed on registration, a database
    # fact — never on the shape of the string. A regex for "looks like a broker identifier"
    # eventually rejects a legitimate ticker and locks the owner out of their own ledger.
    from_inst = get_instrument(conn, inp.from_symbol)
    to_inst = get_instrument(conn, inp.to_symbol)
    # D48a (owner ruling 2026-08-15): a SPINOFF's destination is a security that did not
    # exist until this event, so "register it first" asks the owner to pre-create a row for
    # the thing the action is about to create. It is auto-registered on save instead —
    # inheriting the parent's market and quote currency, which E11 requires to match anyway,
    # so it is a derivation and not a guess.
    #
    # SOFT, not silent, and that is the whole difference. D19/E10 exists so a broker
    # identifier off a statement (a CUSIP like ``74347W148``) never becomes an instrument;
    # silent creation would turn one mistyped box into a permanent phantom symbol that no
    # provider will ever price. The owner is shown exactly what will be created and agrees.
    # Narrowed to SPINOFF only: an EXCHANGE's destination is an existing listed security and
    # keeps E10's hard rejection, and so does every source symbol.
    autoreg_child = (
        kind is CorporateActionKind.SPINOFF
        and to_inst is None
        and from_inst is not None
        and not same_symbol
    )
    for label, symbol, inst in (("來源", inp.from_symbol, from_inst),
                                ("目的", inp.to_symbol, to_inst)):
        if inst is None and autoreg_child and symbol == inp.to_symbol:
            assert from_inst is not None  # implied by autoreg_child
            add(Issue(kind=SPINOFF_CHILD_AUTOREGISTER, needs_confirm=True,
                      message=(f"{symbol} 尚未註冊 — 分拆會產生這檔新標的，"
                               f"存檔時會自動建立（市場 {from_inst.market.value}、"
                               f"幣別 {from_inst.quote_ccy.value}，沿用 {inp.from_symbol}）。"
                               "請確認代號無誤：若這是券商對帳單上的內部代碼"
                               "（而非交易代號），自動建立會產生一檔查不到報價的標的")))
        elif inst is None:
            add(Issue(kind="unregistered_symbol",
                      message=(f"{label}標的 {symbol} 尚未註冊。請先到「標的管理」註冊;"
                               "若這是券商對帳單上的內部代碼（而非交易代號），"
                               "請改填該證券真正的代號")))

    # --- E11: quote currency must match; carrying a basis across currencies would need an
    # action-date FX rate, and inventing one corrupts the basis ---
    if (from_inst is not None and to_inst is not None
            and from_inst.quote_ccy is not to_inst.quote_ccy):
        add(Issue(kind="quote_ccy_mismatch",
                  message=(f"{inp.from_symbol} 以 {from_inst.quote_ccy.value} 計價,"
                           f"{inp.to_symbol} 以 {to_inst.quote_ccy.value} 計價 — "
                           "跨幣別的成本移轉需要當日匯率，本系統不會自行假設")))

    # --- E1a: the source position must exist ON THE ACTION DATE ---
    # The corporate-action analogue of the date-aware sell guard. Without it a stranded row
    # reaches build_book, which has no try/except at dashboard.py — a 500.
    #
    # ACTION-AWARE since W4, and this was the feature's live blocker (audit F-08): reading
    # the naive count made the SECOND action of every chain uncommittable. Reproduced on
    # `HEAD`: buy ABC 2020 → EXCHANGE ABC→XYZ 2024 → SPLIT XYZ 2025 was hard-rejected as
    # 「沒有持倉」 while `build_book` held 100 XYZ, i.e. the feature could not accept the
    # data §10.5 defines "done" as accepting.
    # The cut is `(date, CORPORATE_ACTION)` — what the action SEES — not the close of the
    # date (2026-08-11, D41's twin on the share side). `shares_through` counts a same-day
    # SELL, and selling exactly the pre-split count on the split date then hard-rejected the
    # split with 「沒有持倉」 while the owner really held the post-split remainder.
    if from_inst is not None and shares_before_action_on(
        conn, inp.account_id, inp.from_symbol, on=inp.date, index=walk_index
    ) == 0:
        add(Issue(kind="no_position_on_action_date",
                  message=(f"{inp.account_id} 在 {inp.date.isoformat()} 沒有 "
                           f"{inp.from_symbol} 的持倉，無法套用公司行動。"
                           "請先補登該日之前的買進或期初庫存")))

    # --- D3 (soft): an opening dated ON the action date is PRE-action ---
    # Sits with E1a because both answer "what does the action date mean for the source
    # position": E1a rejects when nothing is held on it, D3 explains what a same-day opening
    # does. `EventPriority.OPENING = 0 < CORPORATE_ACTION = 10`, so the opening lands first
    # and the action applies to it — which is the ruling, not a defect. It is surfaced
    # because the owner who read the post-action share count off a statement and typed it
    # into an opening row gets it scaled a SECOND time, and nothing else on screen says so.
    opening = get_opening(conn, inp.account_id, inp.from_symbol)
    if opening is not None and opening.build_date == inp.date:
        add(Issue(kind="opening_on_action_date", needs_confirm=True,
                  message=(f"{inp.account_id} 在 {inp.date.isoformat()} 有一筆 "
                           f"{inp.from_symbol} 的期初庫存，與這筆公司行動同一天。"
                           "同一天的期初庫存會被視為行動之前就已持有，"
                           f"所以這筆行動會套用到它（{opening.shares} 股會依比例換算）。"
                           "若您填的股數已經是行動之後的數字，"
                           "請把期初庫存的建立日期改成行動的隔天，否則會換算兩次")))

    stored = list_corporate_actions(conn)
    siblings = [b for b in batch if b is not inp]

    # --- E15, HARD (D29): an EXACT duplicate of a stored row. Hard rather than a soft
    # warning, because "re-entering is plausible" is true for a transaction (you really can
    # buy the same stock twice in a day at the same price) and false for a corporate action,
    # which is an EVENT: a 3-for-1 happens once per (account, symbol, date). Acknowledging a
    # soft warning here would apply the ratio twice and turn a 3-for-1 into a 9-for-1.
    #
    # It must also be checked BEFORE E12, and with its own message. An exact duplicate is
    # by construction a same-date intersecting pair, so E12 would swallow every one of them
    # and E15 could never fire — the same "the ⚠ provably never fires" defect E13 was
    # rewritten to remove. E12's message is about ambiguous ORDER, which is not what is
    # wrong here: two identical rows have no order problem, they have a doubling problem.
    #
    # The ratio is compared as a RATIO, not term-wise: 「3 比 1」 re-entered as 「30 比 10」
    # is the same event written two ways, and a term-wise test called it a *different*
    # ratio — which sent it to the conflicting-ratio guard's "one of them is wrong" message
    # for two rows that agree perfectly.
    duplicate = any(
        s.account_id == inp.account_id and s.date == inp.date and s.kind == kind.value
        and s.from_symbol == inp.from_symbol and s.to_symbol == inp.to_symbol
        and _same_ratio(s.ratio_to, s.ratio_from, inp.ratio_to, inp.ratio_from)
        for s in stored
    )
    if duplicate:
        add(Issue(kind="duplicate_action",
                  message=(f"{inp.from_symbol} 在 {inp.date.isoformat()} 的這筆公司行動"
                           "已經登錄過了。公司行動是事件，同一天只會發生一次 — "
                           "再登錄一次會把比例套用兩次（3 比 1 會變成 9 比 1）。"
                           "若要修改，請編輯原本那一筆")))

    # --- E12 (D15): two same-date actions on the same account whose symbol sets intersect.
    # Rejected, not tie-broken: `id` ASC is the order the owner happened to TYPE, and the
    # two orders produce a 3x difference in share count while the conservation test stays
    # green on both. The remedy is ONE row (a reverse split + rename is one EXCHANGE), or
    # dating the steps apart.
    mine = {inp.from_symbol, inp.to_symbol}
    same_day: list[tuple[str, str]] = [
        (s.from_symbol, s.to_symbol) for s in stored
        if s.account_id == inp.account_id and s.date == inp.date
    ] + [
        (b.from_symbol, b.to_symbol) for b in siblings
        if b.account_id == inp.account_id and b.date == inp.date
    ]
    for other in same_day:
        if duplicate:
            break  # already rejected above, with the accurate reason
        if mine & set(other):
            add(Issue(kind="same_date_action_conflict",
                      message=(f"{inp.date.isoformat()} 已有另一筆涉及 "
                               f"{'、'.join(sorted(mine & set(other)))} 的公司行動。"
                               "同一天、同一標的的兩筆行動，先後順序會改變股數與成本，"
                               "而系統無從得知正確順序 — 請合併成一筆，或把日期分開")))
            break

    # --- Conflicting ratios on one (symbol, date): the same event entered twice with
    # different terms. One of them is wrong and the replay would apply BOTH.
    #
    # Two repairs, both from audit F-06. (1) The guard read `stored` ONLY, while D28
    # mandates that a multi-account action is written as ONE N-row batch — in which
    # `stored` is empty for every row, so the rule PROVABLY NEVER FIRED on the door it was
    # written for. It now reads `stored + siblings`. (2) The comparison is by QUOTIENT, so
    # 「3 比 1」 and 「30 比 10」 are accepted as identical rather than rejected as
    # conflicting; the wrong half of that was reachable through F-10, where the two
    # spellings survived the split dedup as separate events and squared the price factor.
    others: list[tuple[str, Decimal, Decimal]] = [
        (s.account_id, s.ratio_to, s.ratio_from) for s in stored
        if s.from_symbol == inp.from_symbol and s.date == inp.date and s.kind == kind.value
    ] + [
        (b.account_id, b.ratio_to, b.ratio_from) for b in siblings
        if b.from_symbol == inp.from_symbol and b.date == inp.date
        and b.kind.strip().upper() == kind.value
    ]
    for other_account, other_to, other_from in others:
        if not _same_ratio(other_to, other_from, inp.ratio_to, inp.ratio_from):
            add(Issue(kind="conflicting_ratio",
                      message=(f"{inp.from_symbol} 在 {inp.date.isoformat()} 已登錄過比例 "
                               f"{other_to} 比 {other_from}（{other_account}），與本次的 "
                               f"{inp.ratio_to} 比 {inp.ratio_from} 不一致。"
                               "同一個事件只會有一個比例，請先確認正確的那一個")))
            break

    # --- E13 (D13): ALL-OR-NOTHING across accounts ---
    # Positions are keyed (account, symbol), so N holding accounts need N rows. A partial
    # application is REJECTED, not warned about: the drawer's reconciliation cannot see it
    # (an account with no action row has corporate_delta 0, so its footer prints ✓), while
    # `prices` has no account_id so the price correction is global — the un-actioned account
    # then holds pre-action shares against post-action prices, and nothing computes that
    # relationship.
    if from_inst is not None:
        holders = _accounts_holding_on(conn, inp.from_symbol, inp.date, index=walk_index)
        # ⚠ BOTH halves filter on (from_symbol, date, kind). The batch half did not until
        # 2026-08-11, and an action on ANOTHER SYMBOL in the missing account satisfied the
        # rule — a genuinely partial multi-account SPLIT validated clean, measured while
        # seeding the demo corpus. F-40 makes the importer call this with the FULL batch and
        # a corporate-action CSV is multi-symbol by construction, so the guard went quiet on
        # the one path added to enforce it. A filter written once and copied once is how the
        # two halves came to disagree; they are adjacent now so the next reader sees both.
        _same_event = (inp.from_symbol, inp.date, kind.value)
        covered = {inp.account_id} | {
            b.account_id for b in batch
            if (b.from_symbol, b.date, b.kind.strip().upper()) == _same_event
        } | {
            s.account_id for s in stored
            if (s.from_symbol, s.date, s.kind) == _same_event
        }
        missing = holders - covered
        if missing:
            add(Issue(kind="incomplete_account_coverage",
                      message=(f"{inp.from_symbol} 在 {inp.date.isoformat()} 還有 "
                               f"{'、'.join(sorted(missing))} 也持有,公司行動必須對每個持有"
                               "的帳戶都登錄一筆。只登錄一部分的話，未登錄的帳戶會用行動前的"
                               "股數搭配行動後的價格，市值會錯，而且畫面上不會有任何警示")))

    # --- E22 (D16) / E18 / E3 / E5: replay-derived states of the two positions ---
    # Replayed AT THE ACTION'S OWN DATE (see the docstring). `before_action_on` is the
    # walker's three-bound cut, not `through`'s single `<= day`: a same-day sell sorts
    # AFTER the action and must stay invisible to it, while a same-day opening sorts
    # before and must not.
    #
    # PLUS the sibling batch actions dated strictly before this one (FIX-A2, QA BUG-04):
    # the share walk has seen same-batch predecessors since 2026-08-14 (`load_action_index`
    # pending=batch), but this replay read the stored ledger only, so the two halves of one
    # validator disagreed about whether the prerequisite EXCHANGE had happened — E1a passed
    # and E3 hard-rejected the same row, curable only by an undocumented second upload.
    # Converted through the SAME `convert_stored` the index uses, so a malformed sibling
    # excludes itself (→ unreadable) identically on both paths and can neither lend shares
    # nor blank a book; its own row is rejected on its own terms in the same pass. Only the
    # strictly-earlier subset is converted — `_book_before` re-applies the cut as the
    # boundary's owner, and D15/E12 keeps the same-date-intersecting case unreachable.
    pending_actions: list[CorporateAction] = []
    if any(b.date < inp.date for b in siblings):
        pending_actions, _unreadable = convert_stored(
            [b for b in siblings if b.date < inp.date])
    book = _book_before(
        conn, inp.date, bundle=bundle, cache=book_cache, pending=pending_actions)
    by_key = {(h.account_id, h.symbol): h for h in book.holdings}
    source = by_key.get((inp.account_id, inp.from_symbol))
    dest = by_key.get((inp.account_id, inp.to_symbol))

    if source is not None and source.oversold:
        add(Issue(kind="oversold_source",
                  message=(f"{inp.from_symbol} 目前是賣超（待釐清）部位，成本基礎已被捨棄，"
                           "無法套用公司行動。請先補登缺少的買進或期初庫存")))
    if kind is not CorporateActionKind.SPLIT and source is not None and source.short_open:
        add(Issue(kind="short_source",
                  message=(f"{inp.from_symbol} 目前有未回補的放空部位，換股／分拆沒有"
                           "可誠實記錄的分錄。請先回補後再登錄")))
    if kind is not CorporateActionKind.SPLIT and dest is not None and not same_symbol:
        if dest.oversold:
            add(Issue(kind="oversold_destination",
                      message=(f"目的標的 {inp.to_symbol} 目前是賣超（待釐清）部位。"
                               "把成本移轉過去會讓已被捨棄的成本基礎「復活」，"
                               "算出一個看起來正常、實際上沒有依據的均價。"
                               "請先處理該部位的賣超")))
        if dest.short_open:
            add(Issue(kind="short_destination",
                      message=(f"目的標的 {inp.to_symbol} 目前有未回補的放空部位。"
                               "多頭與空頭部位在本系統是互斥的，移轉過去會讓兩者混在一起，"
                               "均價將失去意義。請先回補後再登錄")))

    # --- E23 (D22) + N3-price (§6.6): the two findings that read the PRICE table ---
    # Grouped, and placed after every hard check, so the evaluation order §6.5 fixes (E15
    # before E12, D29) is untouched by construction. Neither can suppress or be suppressed:
    # nothing below returns early, and D29's rule is about REACHABILITY, not list position.
    #
    # E23 — the residual hole D19 leaves behind. If a raw broker identifier was REGISTERED
    # as an instrument before D19 existed, E10 passes and an identifier change plus reverse
    # split can still be entered as an EXCHANGE — leaving the destination's price series in
    # pre-split terms with no correction, because §5.1's re-expression scope is SPLIT-only
    # (widening it to EXCHANGE is trap #16: an EXCHANGE ADDS to its destination, so the
    # factor would corrupt the whole price history of a symbol you already held).
    #
    # The four-part condition is the identifier SIGNATURE, and its discriminator is the
    # SOURCE: a real merger's from_symbol was a listed security and has a price series; an
    # identifier never does, because no provider resolves one (D19's own argument, §3.4).
    # Without that term the guard would also fire on cases 2 and 3 of §5.1's table — i.e. on
    # most mergers — and a guard that mostly cries wolf trains the owner to click through.
    #
    # ⚠ Keyed on REGISTRATION and on stored PRICES, both database facts. Never on the shape
    # of the string: a regex for "looks like a broker identifier" eventually rejects a
    # legitimate ticker and locks the owner out of their own ledger (trap #17). Both symbols
    # must be registered — an unregistered one is E10's hard rejection, which already tells
    # the identifier story, and repeating it here would be two messages for one problem.
    #
    # Middle tier, deliberately (D22): the hard tier makes the row uncommittable
    # (`preview.PreviewRow.has_hard_issue`) and would block ordinary mergers; a passive
    # notice would let the artifact through. Acknowledging does NOT repair the discontinuity
    # — no factor is applied to either series — it makes it recorded and seen. Converting to
    # a SPLIT is the repair, which is why the message names it.
    if (kind is CorporateActionKind.EXCHANGE
            and inp.ratio_to != inp.ratio_from
            and from_inst is not None and to_inst is not None
            and _has_prices(conn, inp.to_symbol, before=inp.date)
            and not _has_prices(conn, inp.from_symbol)):
        add(Issue(kind=IDENTIFIER_CHANGE_SUSPECTED, needs_confirm=True,
                  message=(f"{inp.to_symbol} 在 {inp.date.isoformat()} 之前已有價格紀錄，"
                           f"而來源標的 {inp.from_symbol} 完全沒有價格紀錄 — "
                           "這是識別碼、而不是證券的特徵。"
                           "這筆可能是同一檔證券換了代號（並同時調整股數），而不是併購。"
                           f"若是換代號，請改記為「分割」，系統才會把 {inp.to_symbol} "
                           "行動前的價格一併換算；以換股存檔的話價格不會換算，"
                           "市值與淨值會在行動當天出現斷層。"
                           "若這確實是併購，確認後即可繼續存檔")))

    # N3-price — an EXCHANGE / SPINOFF creates a position in a symbol that may never have
    # been priced, and `returns.py` is all-or-nothing on the terminal value: ONE unpriced
    # holding returns `rate=None` for the WHOLE portfolio. So the headline XIRR goes dark
    # with no visible cause until a refresh succeeds (§6.6). Surfaced at entry, where the
    # cause is still on screen. SPLIT is excluded: its destination IS its source (E20), so
    # it creates nothing and a dark XIRR would already have been dark before the action.
    # ``autoreg_child`` joins the condition (D48a): before it, an unregistered destination
    # was a hard rejection and could never reach here, and now it can — a symbol that is
    # about to be created has NO prices by definition, which is precisely what this warns
    # about. Dropping the term would make the newest source of unpriced holdings the one
    # case that never warns.
    if (kind is not CorporateActionKind.SPLIT
            and (to_inst is not None or autoreg_child)
            and not _has_prices(conn, inp.to_symbol)):
        zh_kind = "換股" if kind is CorporateActionKind.EXCHANGE else "分拆"
        add(Issue(kind="to_symbol_unpriced", needs_confirm=True,
                  message=(f"{inp.to_symbol} 目前沒有任何價格紀錄。{zh_kind}會產生一筆新的"
                           "持倉，而只要有一檔持倉沒有價格，整個投資組合的年化報酬率"
                           "（XIRR）就會顯示不出來，不是只有這一檔。"
                           "存檔後請執行一次「更新報價」，或等下一次自動更新完成")))

    # --- D44: the owner's target band predates this SPLIT (soft) ---
    # W6c re-expresses the PRICE that `target_cross` compares; the band is owner-entered and
    # is deliberately NOT re-expressed (§5.1(d2)), because "alert me at 200" may survive a
    # 7-for-1 as 28.57 (a view about the company) or as 200 (a view about the share price),
    # and `domain-ledger.md` forbids guessing money the owner stated. So the two sides of one
    # comparison end up in different denominations and the rule crosses on the split date —
    # permanently, and on the NOTIFICATION path (`ops/notify.py`), which is what makes it
    # worth a finding rather than a docs note.
    #
    # Asked ONCE, here, where the owner is literally typing the ratio and is the only moment
    # they hold both halves of the answer. Raised in the validator rather than in the form's
    # preview so all three doors get it — the manual form, the CSV kind, and the broker
    # converter's action rows — which is the asymmetry `architecture.md` calls out for the
    # cash guard: a bulk door must not ship a weaker guard than the single-row form.
    #
    # The `target_set_at` term is the discriminator, and it is doing E23's fourth-term job.
    # Without it the finding fires on EVERY split of a banded symbol, so importing five years
    # of broker history — mostly historical splits, against bands set last week — is mostly
    # false positives, and a guard that mostly cries wolf trains the owner to click through.
    # NULL means the date is unknowable (a pre-column row): make no claim, stay silent.
    # Per SYMBOL, not per account — the band lives on `instruments`. D13 writes one event as
    # N rows so N copies arrive, and `_issue_wires` already collapses them by (code, text),
    # which is why the message names no account.
    if (
        kind is CorporateActionKind.SPLIT
        and is_ratio_term(inp.ratio_to)
        and is_ratio_term(inp.ratio_from)
        and inp.ratio_to != inp.ratio_from  # a 1-for-1 changes nothing (E7 says so already)
        and from_inst is not None
        and from_inst.target_set_at is not None
        and from_inst.target_set_at < inp.date
    ):
        legs = restated_band(from_inst, ratio_to=inp.ratio_to, ratio_from=inp.ratio_from)
        if legs:
            add(Issue(
                kind=TARGET_BAND_PREDATES_SPLIT, needs_confirm=True,
                message=(
                    f"{inp.from_symbol} 的目標價設定於 "
                    f"{from_inst.target_set_at.isoformat()}，早於這筆分割。"
                    "分割後系統比對的股價會依比例換算，但你輸入的目標價不會 — "
                    f"維持原值的話，它會在 {inp.date.isoformat()} 當天立刻穿越，"
                    "而且之後每次掃描都會再提醒一次。"
                    "換算後應為 "
                    f"{'、'.join(f'{lbl} {cur} → {new}' for _f, lbl, cur, new in legs)}。"
                    "系統不會替你決定：這個價位若是對公司價值的判斷，就該換算；"
                    "若是針對股價本身的價位，就該維持原值。"
                    "存檔後可到「觀察清單」調整")))

    # --- D31: a share walk above hit the corporate-action depth cap ---
    # Last, because the walks it reports on are E1a's and E13's. Soft (賣超 tier): the
    # numbers those guards used were the action-UNAWARE fallback, so their verdicts are
    # not wrong exactly — they are answers to a different question, and the owner has to
    # say so before this commits.
    if (capped := _depth_cap_issue(
        walk_index, {inp.from_symbol, inp.to_symbol}
    )) is not None:
        add(capped)
    return issues


def restated_band(
    inst: Instrument, *, ratio_to: Decimal, ratio_from: Decimal
) -> list[tuple[str, str, Decimal, Decimal]]:
    """D44's repair: each SET leg of *inst*'s target band, restated across a SPLIT ratio.

    Returns ``(field, zh_label, current, restated)`` per leg, in ``target_low``-then-
    ``target_high`` order; ``[]`` when no level is set. The ``field`` is the API/model name,
    so the form preview can key its one-click on it without re-deriving which leg is which.

    Pure, and beside the check that raises the finding, for E23's reason: the number the
    message quotes and the number the button writes must be **the same expression**, or the
    owner is shown one value and the ledger receives another. It is a *candidate*, never a
    write — nothing here decides that the restated level is what the owner meant, which is
    the entire content of the D44 ruling.
    """
    legs: list[tuple[str, str, Decimal, Decimal]] = []
    for field, label, level in (
        ("target_low", "目標下限", inst.target_low),
        ("target_high", "目標上限", inst.target_high),
    ):
        if level is not None:
            legs.append((field, label, level,
                         apply_ratio_to_price(level, ratio_to=ratio_to,
                                              ratio_from=ratio_from)))
    return legs


def identifier_change_repair(
    inp: CorporateActionInput,
) -> CorporateActionInput | None:
    """E23's repair (D22): the suspected row rewritten as the SPLIT it probably is.

    ``None`` when *inp* is not the shape E23 fires on, so a caller cannot accidentally
    "convert" a SPLIT or a SPINOFF. Pure — no I/O, no ledger — which is exactly why it
    returns a **candidate**, not a committable row: see "what this does NOT check" below.

    **A conversion is not a ``kind`` flip.** E20 requires ``to_symbol == from_symbol`` on a
    SPLIT, so converting an EXCHANGE ``A → B`` has to collapse two symbols into one, and
    which one survives is the whole decision.

    **The surviving symbol is ``to_symbol``, and it has to be.** E23 fires on the identifier
    signature: ``from_symbol`` is a raw broker identifier string that was registered as an
    instrument before D19 existed, and ``to_symbol`` is the security. §3.4's adopted rule
    normalises such an identifier **to its ticker**, keeping the identifier in ``note`` for
    provenance and never as a symbol — this function is that rule applied after the fact,
    to a row already typed. The mechanical argument is stronger still: the repair's whole
    value is §5.1's price re-expression, which is SPLIT-scoped and applies to the SPLIT's
    own symbol, and E23's own fourth term says ``from_symbol`` has **no stored prices at
    all**. A SPLIT on the identifier would therefore re-express an empty series and leave
    ``to_symbol``'s prices in pre-split terms — it would repair nothing while looking like
    a repair, which is the failure mode this whole feature exists to avoid.

    The ratio rides across untouched: the identifier change and the re-denomination are ONE
    event (§3.4 — "the combined reverse split + rename seen in real data is ONE row"), and
    the ratio is the re-denomination half. ``cost_carry`` is cleared rather than carried,
    so the output is valid by construction instead of by assumption (E8: SPINOFF only).

    **What this does NOT check — the caller must.** E1a requires a non-zero position in the
    SPLIT's source ON the action date, and after the conversion that source is ``to_symbol``.
    So the conversion is well-defined only for a ledger that already records this security
    under the ticker (D19's "often the ticker itself never changed" case). A ledger that
    records it under the identifier needs its TRANSACTIONS restated first — the importer
    seam's job, not a one-click — and re-validating the returned row is what says so, in
    E1a's own words. Offer the repair only when the returned row validates: a control that
    ends in an error is the same class of mistake as §6.7's deselectable account checklist.
    """
    if inp.kind.strip().upper() != CorporateActionKind.EXCHANGE.value:
        return None
    ticker = inp.to_symbol.strip()
    retired = inp.from_symbol.strip()
    if not ticker or not retired or ticker == retired:
        return None
    provenance = f"原始來源代號 {retired}(識別碼變更，改記為分割)"
    return CorporateActionInput(
        account_id=inp.account_id,
        date=inp.date,
        kind=CorporateActionKind.SPLIT.value,
        from_symbol=ticker,
        to_symbol=ticker,
        ratio_to=inp.ratio_to,
        ratio_from=inp.ratio_from,
        cost_carry=None,
        note=f"{inp.note}；{provenance}" if inp.note else provenance,
    )


def validate_corporate_action_change(
    conn: sqlite3.Connection,
    action_id: int,
    *,
    replacement: CorporateActionInput | None = None,
) -> list[Issue]:
    """E13 on the way OUT: deleting or re-accounting one row of an N-row set (audit F-32).

    ``replacement=None`` validates a DELETE; otherwise it validates an UPDATE to that shape.

    E13 was enforced at INSERT only. ``store.delete_corporate_action`` performs no
    re-validation and ``store.py`` contains zero references to E13, so an N-row set could be
    dismantled one row at a time through the door that was supposed to be closed. The result
    is not a visible error — it is the *silent* half of D13: ``split_factor``'s dedup key is
    ``(symbol, date, ratio)`` with **no account**, so removing one account's row leaves the
    GLOBAL price correction standing while that account's share count goes uncorrected, and
    the drawer footer prints ✓ 對帳一致 over the mismatch.

    Two rules, both hard:

    * **Leaving the set requires taking the set.** If sibling rows share this row's
      ``(from_symbol, date, kind)``, deleting it — or editing it onto a different symbol,
      date or kind — is refused, naming the accounts that would be left behind.
    * **Staying in the set means keeping its ratio.** An edit that changes only this row's
      ratio recreates F-06's conflict from the inside, so it is compared by quotient against
      the siblings 「3 比 1」 → 「30 比 10」 is allowed; 3-for-1 → 2-for-1 is not.

    The guard lives here, with the other two tiers, rather than in ``store.py``: the store's
    job is persistence, and a rule that only exists at one of several write doors is how E13
    came to be insert-only in the first place. **The delete / update routes must call it** —
    see the W4 report.
    """
    issues: list[Issue] = []
    stored = list_corporate_actions(conn)
    row = next((s for s in stored if s.id == action_id), None)
    if row is None:
        return [Issue(kind="unknown_action", message=f"找不到編號 {action_id} 的公司行動")]

    set_rows = [
        s for s in stored
        if s.id != action_id and s.from_symbol == row.from_symbol
        and s.date == row.date and s.kind == row.kind
    ]
    if not set_rows:
        return issues

    accounts = "、".join(sorted({s.account_id for s in set_rows}))
    if replacement is None or (
        replacement.from_symbol != row.from_symbol
        or replacement.date != row.date
        or replacement.kind.strip().upper() != row.kind
    ):
        verb = "刪除" if replacement is None else "修改"
        issues.append(Issue(
            kind="partial_action_set_change",
            message=(f"{row.from_symbol} 在 {row.date.isoformat()} 的這筆公司行動，"
                     f"還有 {accounts} 的同一筆事件。"
                     f"只{verb}其中一筆會讓價格修正照舊套用、而該帳戶的股數沒有修正，"
                     "畫面上還會顯示對帳一致。請整組一起處理"),
        ))
        return issues

    for sibling in set_rows:
        if not _same_ratio(sibling.ratio_to, sibling.ratio_from,
                           replacement.ratio_to, replacement.ratio_from):
            issues.append(Issue(
                kind="conflicting_ratio",
                message=(f"{row.from_symbol} 在 {row.date.isoformat()} 於 "
                         f"{sibling.account_id} 的比例是 {sibling.ratio_to} 比 "
                         f"{sibling.ratio_from}，與本次改成的 {replacement.ratio_to} 比 "
                         f"{replacement.ratio_from} 不一致。"
                         "同一個事件只會有一個比例，請整組一起改"),
            ))
            break
    return issues


# ---------------------------------------------------------------------------
# Cash movements (入金 / 出金 / 期初資金 / 折讓款)
# ---------------------------------------------------------------------------
# ⚠ ARCHITECTURE — read this before changing how the withdraw guard gets its numbers.
#
# The guard needs TWO figures about one (account, ccy) pool: its current balance and the
# MINIMUM of its date-ordered running balance (audit C3). Both live in ``portfolio/cash.py``,
# and ``data_ingestion`` may not import ``portfolio`` — the diagram's arrow runs the other
# way (``portfolio/dashboard.py`` and ``portfolio/dividends.py`` import this package), so a
# direct import would close a package-level cycle, which is precisely the risk
# ``architecture.md`` says the import graph exists to constrain.
#
# The arithmetic is therefore **injected** as a callable (:class:`CashPoolFn`), bound ONCE
# per request/import by the layer that sits above both (``api/routers/cash.py::cash_pool_fn``).
# That is D17's shape exactly: ``pricing/`` may not reach the corporate-action ledger either,
# so ``scheduler/jobs.py::split_factor_fn`` binds the split factor and hands it down. Same
# problem, same answer — one resolution in this codebase rather than two.
#
# Rejected, on the record:
#
# * **A direct SQL read on the shared connection** — the convention ``architecture.md``
#   records for reading another module's TABLE (``_has_prices`` below is one). It fits a
#   table read; it does not fit this. A pool balance is not a row: it is movements ± fx
#   legs ± trade settlements + cash dividends, ordered with a credits-before-debits
#   tie-break so a same-day funding covers a same-day spend. Re-deriving that here makes
#   ``data_ingestion`` a SECOND owner of the balance definition — the duplication
#   ``shared/ledger_registry.py`` exists to remove — and the two copies would disagree the
#   first time one of them learns about a new flow kind.
# * **Leaving the guard at the API layer** (where it lives today). Legal, but the CSV door
#   writes N rows at once, so it would ship with a WEAKER guard than the single-row form.
#   A silently weaker guard on the bulk path is the worse half of that trade.
# * **Importing ``portfolio.cash`` directly.** This file already imports
#   ``portfolio.cost_basis.build_book`` (W2/W4), so there is precedent — but that edge
#   appears in no diagram, and copying an undocumented back-edge is how the next one comes
#   to be written. Reported instead of repeated.
#
# The injected callable takes NO connection: it closes over one ledger snapshot, so an
# N-row import pays for the ledger reads once rather than N times (trap #21).

# The write-path allowed set. Re-exported from ``shared/cash_kinds.py`` rather than spelled
# out here, because a kind listed in only one of the two places is exactly the failure this
# module's own docstrings warn about: listed here but absent from the table gets the table's
# UNKNOWN fallback sign (a silently mis-signed pool); listed in the table but absent here is
# simply unreachable. ``tests/shared/test_cash_kinds.py`` asserts the two are equal.
#
# Direction and FX-acquisition semantics per kind live in that table — including why
# REBATE (退款／折讓, the credit the rebate inbox books on confirm, FE-D1) counts as an
# acquisition while INTEREST does not, and why only WITHDRAW is subject to the overdraft
# guard below even though BROKER_FEE / INTEREST_EXPENSE are also debits.
CASH_MOVEMENT_KINDS: frozenset[str] = CASH_KIND_VALUES

#: The accepted-kind list shown when a kind is rejected, DERIVED from the vocabulary rather
#: than written out (QA-18). The hand-written list said 「deposit / withdraw / opening /
#: rebate」 and never grew when the broker-statement importer added INTEREST,
#: INTEREST_EXPENSE and BROKER_FEE on 2026-08-13 — so the one message whose job is to tell
#: the owner what IS accepted told them that three legal kinds were not. Deriving it is the
#: same fix, and for the same reason, as ``CASH_KIND_ZH`` replacing the printed statement's
#: private four-entry label map. Enum declaration order is stable, so the list is too.
_ACCEPTED_KINDS_ZH: str = "／".join(
    f"{CASH_KIND_ZH[k.value]} {k.value.lower()}" for k in CashKind
)


class CashMovementInput(BaseModel):
    """A user-supplied cash movement, BEFORE it is trusted.

    Deliberately permissive where :class:`~data_ingestion.store.StoredCashMovement` is
    strict: ``kind`` is a plain string, so an unrecognised one REACHES
    :func:`validate_cash_movement` and is rejected with a zh message naming what the user
    actually typed. A model that refused to construct would turn a typo into a 500 or an
    English pydantic error — the same reasoning :class:`CorporateActionInput` states.

    ``acq_home_amount`` / ``acq_rate`` are the two ways a FOREIGN credit's home-currency
    cost may arrive (spec 2026-07-30, F1). Supply at most one. Only the AMOUNT is ever
    persisted: a rate is an average, and an average is never the stored authority
    (``data-and-pricing.md``); the displayed rate is derived on read.
    """

    account_id: str
    date: date
    kind: str
    ccy: Currency
    amount: Decimal
    note: str | None = None
    acq_home_amount: Decimal | None = None
    acq_rate: Decimal | None = None


class CashPool(BaseModel):
    """One (account, ccy) pool, as the withdraw guard needs to see it.

    ``balance`` is the END balance — the same ``cash_balances`` figure the 賬戶現金 line
    displays, so the frontend hint and the backend authority never disagree. ``low`` is the
    MINIMUM running balance over the pool's date-ordered timeline (audit C3), which is what
    catches a withdrawal back-dated before its funding while the end balance still looks fine.
    """

    balance: Decimal
    low: Decimal


class CashPoolFn(Protocol):
    """The injected pool arithmetic (see the section note above).

    ``include`` are would-be movement rows added on top of the stored ledger: the row being
    validated, plus its siblings in the same import batch. ``exclude_id`` strips one stored
    row's own prior effect first, so raising a withdrawal within the headroom its OLD amount
    already consumed is not falsely blocked on an edit.

    Implementations MUST bind their ledger snapshot once, outside this call — the guard
    invokes it twice per withdrawal row.
    """

    def __call__(
        self,
        account_id: str,
        ccy: Currency,
        *,
        include: Sequence[CashMovementInput] = (),
        exclude_id: int | None = None,
    ) -> CashPool: ...


def cash_movement_kind(raw: str) -> str:
    """The canonical stored spelling of a movement kind (``' withdraw '`` -> ``WITHDRAW``).

    Delegates to ``shared/cash_kinds.canonical_kind`` so the normalization the calculation
    layer applies before signing a row is the SAME normalization the write path applies
    before storing it. Unrecognised input still passes through, so the rejection message
    can name what was actually typed.
    """
    return canonical_kind(raw)


def _pool_row(inp: CashMovementInput) -> CashMovementInput:
    """*inp* as the pool arithmetic must see it: ``kind`` NORMALIZED to upper case.

    ``portfolio/cash.py`` signs a movement by ``kind == "WITHDRAW"``, so a row still
    carrying the wire's lower-case ``"withdraw"`` would be counted as a CREDIT — the guard
    would then watch the pool go UP and pass every overdraft it was written to stop.
    """
    return inp.model_copy(update={"kind": cash_movement_kind(inp.kind)})


def resolve_acq_home_amount(
    inp: CashMovementInput, *, funding_ccy: Currency
) -> tuple[Decimal | None, Issue | None]:
    """The movement's home-currency acquisition cost, or the :class:`Issue` refusing it (F1).

    ``(None, None)`` — both inputs omitted — is the legitimate "cost unknown" case, not an
    error: the amount still funds the pool but stays OUT of the weighted average, and the
    dashboard discloses the shortfall through ``covered_ratio`` / ``fx_basis_gap`` (F2/F3).
    A rate is never guessed, interpolated, or substituted with the current spot.

    A supplied RATE is converted here and then discarded; only the AMOUNT is returned,
    because an average must never be the stored authority (``data-and-pricing.md``). The
    result is quantized to the FUNDING currency's minor unit — that is the currency the
    cost is actually paid in.

    Pure: no connection, no ledger. The caller supplies *funding_ccy* from the account it
    has already resolved.
    """
    if inp.acq_home_amount is None and inp.acq_rate is None:
        return None, None
    if inp.acq_home_amount is not None and inp.acq_rate is not None:
        return None, Issue(
            kind="acq_cost_ambiguous", message="取得成本請擇一填寫（家幣金額 或 匯率）"
        )
    if inp.ccy == funding_ccy:
        return None, Issue(
            kind="acq_cost_home_ccy",
            message=f"取得成本僅適用外幣資金流（本帳戶資金幣別為 {funding_ccy.value}）",
        )
    if cash_movement_kind(inp.kind) == "WITHDRAW":
        return None, Issue(
            kind="acq_cost_on_withdraw", message="出金是處分，不帶取得成本"
        )
    if not is_fx_acquisition(inp.kind):
        # The remaining non-acquiring kinds — interest and fees. This test is keyed on the
        # ACQUISITION axis rather than on ``== "WITHDRAW"``, because the two stopped
        # coinciding once INTEREST existed: interest earned is a CREDIT, so a withdraw-only
        # test lets a cost through on it, and ``forex/pools.py`` then IGNORES that cost —
        # income arising inside the pool inherits the pool average instead of acquiring at
        # its own rate. A number the user is asked for, that is stored and never affects
        # anything, is worse than a rejection: it reads as recorded when it is inert.
        return None, Issue(
            kind="acq_cost_not_an_acquisition",
            message="利息與費用不是外幣取得，不帶取得成本（沿用資金池平均匯率）",
        )
    if inp.acq_home_amount is not None:
        if inp.acq_home_amount <= _ZERO:
            return None, Issue(
                kind="acq_cost_not_positive", message="取得成本必須大於 0"
            )
        return quantize_amount(inp.acq_home_amount, funding_ccy), None
    rate = inp.acq_rate
    if rate is None or rate <= _ZERO:  # `is None` unreachable; narrows for mypy
        return None, Issue(kind="acq_rate_not_positive", message="取得匯率必須大於 0")
    return quantize_amount(inp.amount * rate, funding_ccy), None


def _withdraw_issues(
    inp: CashMovementInput,
    account: Account,
    *,
    pool: CashPoolFn,
    batch: Sequence[CashMovementInput],
    exclude_id: int | None,
) -> list[Issue]:
    """FU-D43a: a withdrawal may NEVER overdraft its pool — HARD, with no ack override.

    Primary check: the amount must be covered by the pool's CURRENT balance, the same
    ``cash_balances`` figure the 賬戶現金 line displays; an exact-balance withdrawal
    (== available) passes.

    Date-aware check (audit C3, hardened for withdrawals): a withdrawal that INTRODUCES or
    DEEPENS a below-zero dip in the running timeline — e.g. back-dated before its funding —
    is blocked too. A PRE-EXISTING dip it does not worsen never blocks it (scoped like the
    ledger-correction replay guard, audit H3).

    *batch* is every row committed together, INCLUDING *inp* (the convention
    :func:`validate_corporate_action` uses). A cash CSV is normally "the deposit that funded
    the account, then what was spent out of it", so validating each row against the stored
    ledger alone would reject every withdrawal in the first import into a fresh ledger. The
    timeline is date-ordered, so file order is irrelevant, and two withdrawals that only
    JOINTLY overdraft are both caught — each sees the other in its ``include`` set.
    """
    siblings = [_pool_row(b) for b in batch if b is not inp]
    before = pool(inp.account_id, inp.ccy, include=siblings, exclude_id=exclude_id)
    if inp.amount > before.balance:
        return [Issue(
            kind="withdraw_insufficient_balance",
            message=(f"出金金額 {decimal_str(inp.amount)} {inp.ccy.value} 超過 "
                     f"{account.name} 的 {inp.ccy.value} 賬戶現金 "
                     f"{decimal_str(before.balance)} — 出金不可透支"
                     "（請先補登入金或換匯）"))]
    after = pool(inp.account_id, inp.ccy,
                 include=[*siblings, _pool_row(inp)], exclude_id=exclude_id)
    if after.low < min(before.low, _ZERO):
        return [Issue(
            kind="withdraw_insufficient_balance",
            message=(f"此筆出金會使 {account.name} 的 {inp.ccy.value} 現金於某時點降至 "
                     f"{decimal_str(after.low)}（出金日早於資金到位）— 出金不可透支，"
                     "請先補登入金或換匯"))]
    return []


def validate_cash_movement(
    conn: sqlite3.Connection,
    inp: CashMovementInput,
    *,
    pool: CashPoolFn,
    batch: Sequence[CashMovementInput] = (),
    exclude_id: int | None = None,
    accounts: Mapping[str, Account] | None = None,
) -> list[Issue]:
    """Validate ONE cash movement — the guard every write door shares.

    Extracted from ``api/routers/cash.py`` (2026-08-12) for the reason
    :func:`validate_corporate_action_change` states in its own words: a rule that exists at
    one of several write doors is how E13 came to be insert-only. The 6th CSV kind was
    deferred at W7 precisely because this function did not exist yet.

    **The check order is the router's order, and the early returns are the router's early
    returns.** ``_movement_guard`` returned on its FIRST failure, and the acquisition-cost
    resolution ran before the withdraw guard; reproducing that exactly is what makes the
    extraction provably behaviour-preserving instead of merely plausible. It also keeps the
    later checks from running on inputs they are not defined for — the withdraw guard has no
    account name to quote and no pool to read once the account is unknown.

    Consequence, accepted deliberately: at most ONE issue comes back per row, so a CSV row
    with two problems surfaces them one import at a time. Collecting them all is a nicer bulk
    experience and a behaviour change to the manual door, which this extraction is not.

    *pool* is the injected pool arithmetic — see the section note above for why it is
    injected and not imported. **It has no default**: a door that forgot to bind it would
    ship a weaker guard than the form next to it, and the whole point of moving the rule down
    here is that the two doors cannot diverge.

    *accounts* is the account registry, hoisted by the caller so an N-row import does not
    re-read it N times (trap #21); loaded here when omitted.

    Returns hard issues only (``needs_confirm=False``). Bulk-only advisories belong to the
    importer, which has a preview to show them on — see ``cash_import._bulk_only_issues``.
    """
    kind = cash_movement_kind(inp.kind)
    if kind not in CASH_MOVEMENT_KINDS:
        return [Issue(
            kind="unknown_movement_kind",
            message=f"未知類型 {inp.kind}（可用類型：{_ACCEPTED_KINDS_ZH}）")]
    if inp.amount <= _ZERO:
        return [Issue(kind="non_positive_amount", message="金額必須大於 0")]
    known = (accounts if accounts is not None
             else {a.account_id: a for a in list_accounts(conn)})
    account = known.get(inp.account_id)
    if account is None:
        # A BLANK account is not an UNKNOWN one (K-2). The wording — and the reasoning K-2
        # recorded here for it — moved into :func:`unknown_account_issue` when L-1 gave the
        # sentence one owner: this door was the only one of six saying it correctly, which is
        # the divergence, not the fix. Nothing about THIS door's behaviour changed.
        return [unknown_account_issue(inp.account_id)]
    # audit C2: a pool may only hold the account's settlement or funding currency.
    if inp.ccy not in {account.settlement_ccy, account.funding_ccy}:
        return [Issue(
            kind="ccy_not_allowed",
            message=(f"{inp.ccy.value} 非此帳戶可用幣別"
                     f"（交割幣 {account.settlement_ccy.value}／資金幣 "
                     f"{account.funding_ccy.value}）"))]
    _resolved, acq_issue = resolve_acq_home_amount(inp, funding_ccy=account.funding_ccy)
    if acq_issue is not None:
        return [acq_issue]
    if kind != "WITHDRAW":
        # Deposit / opening / rebate are CREDITS — no balance guard on the way in.
        return []
    return _withdraw_issues(
        inp, account, pool=pool, batch=batch, exclude_id=exclude_id)


def _duplicate_exists(conn: sqlite3.Connection, inp: TxnInput) -> bool:
    """True iff a stored transaction matches account+symbol+side+qty+price+date exactly.

    Quantity/price are compared as Decimals (not raw strings) so trailing-zero
    variations still match. Best-effort soft guard — never blocks, only warns.
    """
    rows = conn.execute(
        "SELECT quantity, price FROM transactions "
        "WHERE account_id=? AND symbol=? AND side=? AND trade_date=?",
        (inp.account_id, inp.symbol, inp.side.value, inp.trade_date.isoformat()),
    ).fetchall()
    for r in rows:
        if from_db(r["quantity"]) == inp.quantity and from_db(r["price"]) == inp.price:
            return True
    return False
