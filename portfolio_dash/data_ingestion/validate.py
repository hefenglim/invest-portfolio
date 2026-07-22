"""Transaction input validation: structural checks + sell-exceeds-holdings guard.

Shared by every write door (manual entry, CSV import, AI input), so the guards below
hold no matter which path a transaction arrives on:

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
from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.data_ingestion.holdings import current_shares
from portfolio_dash.data_ingestion.markets import CCY_MARKET, MARKET_ZH
from portfolio_dash.data_ingestion.rules_binding import allowed_markets
from portfolio_dash.data_ingestion.store import get_instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.money import from_db

# Overflow guard (audit M4): shares/price above this are rejected as a hard issue so the
# downstream fee quantize (fees._round) can never overflow the Decimal context into a 500.
_MAX_MAGNITUDE = Decimal("1e12")


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
    note: str | None = None


class Issue(BaseModel):
    """A validation finding returned by :func:`validate_transaction`."""

    kind: str
    message: str
    needs_confirm: bool = False


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


def validate_transaction(
    conn: sqlite3.Connection, inp: TxnInput, *, today: date | None = None
) -> list[Issue]:
    """Run validation checks on *inp* against the current ledger state.

    Returns a (possibly empty) list of :class:`Issue` objects.  An empty list
    means the transaction is clean.  Issues with ``needs_confirm=True`` require
    explicit user confirmation before the transaction may be persisted (e.g.
    selling more than currently held, a future trade date, or a duplicate row).

    *today* (usually ``get_now().date()``) enables the future-date soft check; when
    omitted (the pure CSV/AI parse paths) that check is skipped.
    """
    issues: list[Issue] = []

    # --- account exists (+ its market, for the coherence guard) ---
    acc = conn.execute(
        "SELECT settlement_ccy FROM accounts WHERE account_id=?", (inp.account_id,)
    ).fetchone()
    if acc is None:
        issues.append(
            Issue(kind="unknown_account", message=f"unknown account {inp.account_id!r}")
        )

    # --- quantity and price must be positive, and within a sane bound (M4) ---
    if inp.quantity <= 0:
        issues.append(Issue(kind="non_positive_quantity", message="quantity must be > 0"))
    elif inp.quantity > _MAX_MAGNITUDE:
        issues.append(Issue(kind="amount_too_large", message="股數過大,無法處理"))
    if inp.price <= 0:
        issues.append(Issue(kind="non_positive_price", message="price must be > 0"))
    elif inp.price > _MAX_MAGNITUDE:
        issues.append(Issue(kind="amount_too_large", message="價格過大,無法處理"))

    # --- negative fee / tax (H2): hard reject on every path ---
    if inp.fee is not None and inp.fee < 0:
        issues.append(Issue(kind="negative_fee", message="手續費不可為負"))
    if inp.tax is not None and inp.tax < 0:
        issues.append(Issue(kind="negative_tax", message="交易稅不可為負"))

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

    # --- sell must not exceed current holdings (soft) ---
    if inp.side is Side.SELL and inp.quantity > 0:
        held = current_shares(conn, inp.account_id, inp.symbol)
        if inp.quantity > held:
            issues.append(
                Issue(
                    kind="sell_exceeds_holdings",
                    needs_confirm=True,
                    message=f"sell {inp.quantity} > held {held}",
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
