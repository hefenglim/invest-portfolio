"""待確認到期 — the expiry inbox (options spec §7.2, OPT-D13).

The third 收件匣 panel: an option position whose contract expired BEFORE the server's today
while the BOOK still holds it (a non-zero count, per account × contract) is a ledger-completeness
prompt — the owner records the expiry the broker already applied. Compute-on-read, like the
rebate inbox: nothing is stored about an item, its only state is the skip table
``option_expiry_skips`` (created at boot by :func:`ensure_tables`). It lives in ``api/`` because
it reads the book (spec §7.2) and writes through the manual door's own path.

* **Detection** reads the replay on the dashboard path — the loaded bundle, unregistered rows
  dropped, cut at today (``valued_as_of``), ``build_book(allow_oversell=True)`` — and
  ``portfolio.options.past_expiry_holdings``. A ledger with no option contract registered
  takes no replay at all (the count is 0 and costs one registry read).
* **Wording** (spec §7.2; server strings, the account as an ``{account:<id>}`` token): a short —
  「… ・賣方 2 口・到期日已過 N 天 → 到期作廢，權利金 400.00 USD 全數實現」; a long — 「… 買方 1 口
  … → 到期作廢，權利金 250.00 USD 全數損失」. The premium is the BOOK's lot (``adjusted_cost_total``
  — the proceeds a short holds, the basis a long carries), i.e. exactly what the expiry's
  realized row will book; nothing is re-derived here.
* **Confirmable** unless the contract is 待釐清: it carries a refused close (§4.4 — the row the
  owner must fix first), a stored ``expired`` row that no longer equals ``|open|`` (the
  position survived it), or a 賣超 (an undeclared sell beyond the long — an expiry BUY would
  be a close the replay refuses). Such an item shows the remedy instead of 「確認到期作廢」.
* **Confirm** writes exactly the §4.1 price-0 row — ``manual.close_row_input`` (quantity = the
  book's ``|position|``, dated the expiry, side from the position, fee = tax = 0) through
  ``manual.enter_transaction``, the manual door's validate-then-write path (which runs
  ``validate.option_close_issues``): any hard issue is a zh 422, nothing written.
* **Every read is read-only** (DEF-065): a GET opens no write transaction; on a database whose
  skip table does not exist yet (boot not run) it reads "no skips".
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from portfolio_dash.api.wire import issue_wire
from portfolio_dash.data_ingestion.manual import close_row_input, enter_transaction
from portfolio_dash.data_ingestion.store import list_instruments, load_ledger_bundle
from portfolio_dash.data_ingestion.validate import EXPIRED_ROW_STRANDED_MESSAGE, Issue
from portfolio_dash.portfolio.cost_basis import (
    OversellError,
    UnbookableLedgerError,
    build_book,
)
from portfolio_dash.portfolio.options import past_expiry_holdings
from portfolio_dash.portfolio.results import Book, Holding
from portfolio_dash.shared.account_ref import account_ref
from portfolio_dash.shared.models.ledger import LedgerBundle
from portfolio_dash.shared.money import MINOR_UNITS, quantize_amount
from portfolio_dash.shared.options import (
    DIRECTION_ZH,
    REFUSED_CLOSE_REMEDY,
    UNIT_CONTRACT,
    contract_name,
    option_display_name,
    parse_option_symbol,
    shares_to_contracts,
)
from portfolio_dash.shared.wire import decimal_str

_ZERO = Decimal("0")

#: The skip table. Its SQL below spells the name out — never an f-string placeholder, which
#: ``tests/architecture/test_ledger_delete_doors.py`` reads as a possible ledger table.
TABLE = "option_expiry_skips"
_SKIP_DDL = """
CREATE TABLE IF NOT EXISTS option_expiry_skips (
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    skipped_at TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol)
);
"""

#: A 賣超 option position (an undeclared sell beyond the long lot): not a sold contract, and an
#: expiry BUY against it is a close the replay refuses (§4.4), so it is never confirmable.
OVERSOLD_REMEDY = "此合約為賣超部位（待釐清）：請先補回缺少的買進，或將該筆賣出改為賣出開倉（放空）"
#: Confirm on a (account, contract) the inbox does not list (closed meanwhile, never expired,
#: or skipped).
NOT_PENDING_MESSAGE = "此合約目前沒有待確認的到期部位"
#: The inbox has no acknowledgement: a close row that would oversell is refused, never acked.
OVERSELL_MESSAGE = "此到期作廢列會造成賣超，請改至交易輸入登錄"


class ExpiryItem(BaseModel):
    """One 待確認到期 item (spec §7.2 — the wire keys, exactly)."""

    account_id: str
    #: An ACCOUNT TOKEN (``{account:<id>}``), resolved by ``web/api.js``.
    account_name: str
    symbol: str
    #: The display name 「XYZ 2026-12-18 50 Put」.
    contract: str
    direction: Literal["long", "short"]
    #: 口數 (unsigned).
    contracts: Decimal
    expiry: date
    days_past: int
    #: The premium the expiry realizes (short) or loses (long), 「400.00 USD」.
    premium_text: str
    text: str
    confirmable: bool
    #: Why the item cannot be confirmed and what to do (``None`` when confirmable).
    remedy: str | None = None


class SkippedExpiry(BaseModel):
    """A skipped (account, contract), for the 「已略過」 un-skip list (spec §7.2)."""

    account_id: str
    account_name: str
    symbol: str
    contract: str
    #: Parsed from the symbol — known whether or not the position still exists.
    expiry: date | None
    skipped_at: str


class ConfirmResult(BaseModel):
    """One item's outcome in 「全部確認」 (spec §7.2)."""

    account_id: str
    symbol: str
    ok: bool
    txn_id: int | None = None
    error: str | None = None


class ExpiryConfirmError(Exception):
    """A confirm that wrote nothing — ``code`` / ``message`` (zh) / the validator's issues."""

    def __init__(self, code: str, message: str,
                 issues: list[Issue] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.issues = list(issues or [])

    def issues_wire(self) -> list[dict[str, object]]:
        return [issue_wire(i) for i in self.issues]


@dataclass(frozen=True)
class _Pending:
    """A detected item plus what its confirm writes: the BOOK's ``|position|``."""

    item: ExpiryItem
    quantity: Decimal


# --- the skip table ------------------------------------------------------------------------


def ensure_tables(conn: sqlite3.Connection) -> None:
    """Create ``option_expiry_skips`` — at boot (``api/app.py`` lifespan), never on a GET."""
    conn.executescript(_SKIP_DDL)
    conn.commit()


def _has_skip_table(conn: sqlite3.Connection) -> bool:
    """A plain read of the schema — a GET must not create the table it reads (DEF-065)."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    return row is not None


def _skips(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """(account_id, symbol) → skipped_at; empty when the table does not exist yet."""
    if not _has_skip_table(conn):
        return {}
    rows = conn.execute(
        "SELECT account_id, symbol, skipped_at FROM option_expiry_skips").fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def _canonical(symbol: str) -> str:
    return symbol.strip().upper()


def mark_skipped(conn: sqlite3.Connection, account_id: str, symbol: str, *,
                 now: datetime) -> None:
    """「略過」. Raises ``ValueError`` (zh) for a symbol that is not an option contract."""
    sym = _canonical(symbol)
    if parse_option_symbol(sym) is None:
        raise ValueError(f"「{symbol.strip()}」不是選擇權合約代號")
    ensure_tables(conn)
    conn.execute(
        "INSERT INTO option_expiry_skips (account_id, symbol, skipped_at) VALUES (?, ?, ?) "
        "ON CONFLICT(account_id, symbol) DO NOTHING",
        (account_id, sym, now.isoformat()))
    conn.commit()


def unskip(conn: sqlite3.Connection, account_id: str, symbol: str) -> int:
    """「取消略過」 — the item re-surfaces when it is still detectable. Returns rows deleted."""
    ensure_tables(conn)
    cur = conn.execute("DELETE FROM option_expiry_skips WHERE account_id=? AND symbol=?",
                       (account_id, _canonical(symbol)))
    conn.commit()
    return cur.rowcount


def list_skipped(conn: sqlite3.Connection) -> list[SkippedExpiry]:
    """Every skip, newest first — contract and expiry parsed from the symbol (OPT-D22)."""
    skips = _skips(conn)
    out: list[SkippedExpiry] = []
    for (account_id, symbol), at in sorted(skips.items(),
                                           key=lambda kv: (kv[1], kv[0]), reverse=True):
        terms = parse_option_symbol(symbol)
        out.append(SkippedExpiry(
            account_id=account_id, account_name=account_ref(account_id), symbol=symbol,
            contract=contract_name(symbol) or symbol,
            expiry=terms.expiry if terms is not None else None, skipped_at=at))
    return out


# --- detection -----------------------------------------------------------------------------


def _replay(conn: sqlite3.Connection, today: date) -> tuple[LedgerBundle, Book] | None:
    """The dashboard path's book as of *today* — ``None`` on a ledger with no registered
    option contract (no replay at all) or one the replay cannot book (the dashboard names
    that itself; the inbox never answers 500 for it)."""
    instruments = {i.symbol: i for i in list_instruments(conn)}
    if not any(i.is_option for i in instruments.values()):
        return None
    bundle = load_ledger_bundle(conn, instruments=instruments)
    if bundle.unregistered_symbols:
        bundle = bundle.without_unregistered()
    try:
        book = build_book(bundle.valued_as_of(today), allow_oversell=True)
    except (UnbookableLedgerError, OversellError, ArithmeticError):
        return None
    return bundle, book


def _money_text(amount: Decimal, holding: Holding) -> str:
    ccy = holding.quote_ccy
    q = quantize_amount(amount, ccy)
    return f"{q:,.{MINOR_UNITS[ccy]}f} {ccy.value}"


def _remedy(h: Holding, book: Book, expired_rows: set[tuple[str, str]]) -> str | None:
    key = (h.account_id, h.symbol)
    if h.unbookable_close or any((c.account_id, c.symbol) == key
                                 for c in book.refused_closes):
        return REFUSED_CLOSE_REMEDY
    if h.oversold:
        return OVERSOLD_REMEDY
    if key in expired_rows:
        # A stored `expired` row the position survived — it no longer equals |open|.
        return EXPIRED_ROW_STRANDED_MESSAGE
    return None


def _item(h: Holding, book: Book, expired_rows: set[tuple[str, str]],
          today: date) -> _Pending | None:
    terms = parse_option_symbol(h.symbol)
    if terms is None:
        return None
    short = h.shares < _ZERO
    direction: Literal["long", "short"] = "short" if short else "long"
    quantity = abs(h.shares)
    contracts = shares_to_contracts(quantity)
    days = (today - terms.expiry).days
    contract = option_display_name(terms)
    premium_text = _money_text(abs(h.adjusted_cost_total), h)
    head = f"{account_ref(h.account_id)} {contract}・"
    n = f"{decimal_str(contracts)} {UNIT_CONTRACT}"
    remedy = _remedy(h, book, expired_rows)
    if h.oversold:
        text = f"{head}賣超 {n}・到期日已過 {days} 天 → 待釐清"
    elif short:
        text = (f"{head}{DIRECTION_ZH['short']} {n}・到期日已過 {days} 天 → 到期作廢，"
                f"權利金 {premium_text} 全數實現")
    else:
        text = (f"{head}{DIRECTION_ZH['long']} {n}・到期日已過 {days} 天 → 到期作廢，"
                f"權利金 {premium_text} 全數損失")
    return _Pending(
        item=ExpiryItem(
            account_id=h.account_id, account_name=account_ref(h.account_id),
            symbol=h.symbol, contract=contract, direction=direction, contracts=contracts,
            expiry=terms.expiry, days_past=days, premium_text=premium_text, text=text,
            confirmable=remedy is None, remedy=remedy),
        quantity=quantity)


def _detect(conn: sqlite3.Connection, today: date, *,
            include_skipped: bool = False) -> list[_Pending]:
    replayed = _replay(conn, today)
    if replayed is None:
        return []
    bundle, book = replayed
    expired_rows = {(t.account_id, t.symbol) for t in bundle.transactions
                    if t.close_reason == "expired"}
    skips = {} if include_skipped else _skips(conn)
    # The inverse of a stock reader (OPT-D10): this one reads CONTRACTS only — said here, not
    # left to `past_expiry_holdings`' own parse, so the scope is visible where the book is read.
    contracts = [h for h in book.holdings
                 if h.symbol in bundle.instruments and bundle.instruments[h.symbol].is_option]
    out: list[_Pending] = []
    for h in past_expiry_holdings(contracts, bundle.instruments, today):
        if (h.account_id, h.symbol) in skips:
            continue
        pending = _item(h, book, expired_rows, today)
        if pending is not None:
            out.append(pending)
    out.sort(key=lambda p: (p.item.expiry, p.item.account_id, p.item.symbol))
    return out


def detect(conn: sqlite3.Connection, *, now: datetime) -> list[ExpiryItem]:
    """The inbox's items as of the server's today, skipped ones left out (read-only)."""
    return [p.item for p in _detect(conn, now.date())]


def pending_count(conn: sqlite3.Connection, *, now: datetime) -> int:
    """The sidebar badge's share — every listed item, confirmable or 待釐清 (read-only)."""
    return len(_detect(conn, now.date()))


# --- confirm -------------------------------------------------------------------------------


def _write(conn: sqlite3.Connection, pending: _Pending, today: date) -> int:
    """Write the §4.1 price-0 ``expired`` row for *pending* — the manual door's path."""
    item = pending.item
    inp = close_row_input(conn, account_id=item.account_id, symbol=item.symbol,
                          close_reason="expired", trade_date=item.expiry,
                          quantity=pending.quantity)
    draft = enter_transaction(conn, inp, confirm=False, today=today)   # inspect, no write
    hard = [i for i in draft.issues if not i.needs_confirm]
    if hard:
        raise ExpiryConfirmError("validation_error", hard[0].message, draft.issues)
    if any(i.kind == "sell_exceeds_holdings" for i in draft.issues):
        raise ExpiryConfirmError("oversell_unacknowledged", OVERSELL_MESSAGE, draft.issues)
    written = enter_transaction(conn, inp, confirm=True, today=today)
    if not written.written or written.transaction_id is None:
        raise ExpiryConfirmError("validation_error", NOT_PENDING_MESSAGE, written.issues)
    return written.transaction_id


def _refuse_unconfirmable(pending: _Pending) -> None:
    if not pending.item.confirmable:
        raise ExpiryConfirmError("option_expiry_unconfirmable",
                                 pending.item.remedy or NOT_PENDING_MESSAGE)


def confirm(conn: sqlite3.Connection, account_id: str, symbol: str, *,
            now: datetime) -> int:
    """「確認到期作廢」 for one listed item — recomputed server-side, never trusted from the
    client. Returns the new ``transactions.id``; raises :class:`ExpiryConfirmError`."""
    today = now.date()
    key = (account_id, _canonical(symbol))
    match = next((p for p in _detect(conn, today)
                  if (p.item.account_id, p.item.symbol) == key), None)
    if match is None:
        raise ExpiryConfirmError("option_expiry_not_pending", NOT_PENDING_MESSAGE)
    _refuse_unconfirmable(match)
    return _write(conn, match, today)


def confirm_all(conn: sqlite3.Connection, *, now: datetime) -> list[ConfirmResult]:
    """「全部確認」 — every listed item, each validated (and written) on its own; one item's
    refusal never stops the next. A 待釐清 item reports its remedy."""
    today = now.date()
    results: list[ConfirmResult] = []
    for pending in _detect(conn, today):
        item = pending.item
        try:
            _refuse_unconfirmable(pending)
            txn_id = _write(conn, pending, today)
        except ExpiryConfirmError as exc:
            results.append(ConfirmResult(account_id=item.account_id, symbol=item.symbol,
                                         ok=False, error=exc.message))
        else:
            results.append(ConfirmResult(account_id=item.account_id, symbol=item.symbol,
                                         ok=True, txn_id=txn_id))
    return results
