"""Charles Schwab / TD Ameritrade transaction-export adapter.

Reads the broker's own eight-column CSV and emits :class:`~.ir.RawEvent`. It classifies and
normalises; it groups nothing, decides no accounting, and touches no ledger.

Provenance of the rules
-----------------------
The ``(action, description)`` table below was **measured against a real 1,375-row export**
spanning 2021-01 → 2026-07 and two broker eras (TDA, then Schwab), and matches an
independently reconciled ground truth row for row. Three of its properties are not
guesses and must not be "simplified":

* **35 distinct action values, and one of them carries 14 different meanings.**
  ``Journaled Shares`` alone spans an internal account-type journal, a platform transfer, a
  cash sweep, dividend withholding, a mark-to-market round trip, a mandatory exchange, a
  reverse split, a forward split, a spin-off, a reorganisation fee, and four option
  adjustments. Only the free text separates them, and its two largest meanings sit on
  opposite sides of the keep/drop line: **145 withholding-tax rows (keep) against 126 null
  journals (drop)**. That is why the rule key is the PAIR.
* **``Journaled Shares`` has NO default rule.** Every one of its descriptions is listed. An
  unrecognised one raises :class:`~.ir.UnmappedRow` — see that class for the catch-all this
  replaces and the failure it caused.
* **Order matters within an action.** ``MANDATORY REORGANIZATION FEE`` must be tested before
  any other ``MANDATORY`` pattern, and ``TO COVER`` before a plain ``Buy``. The table is
  ordered and first-match-wins; :mod:`tests.data_ingestion.test_broker_adapter` pins each
  observed pattern to its kind so a reorder that changes a verdict fails.

What this file deliberately does NOT contain
--------------------------------------------
**No tickers, no CUSIPs, no amounts.** The owner's real export is git-ignored personal data;
this is code, which is committed. The CUSIP→ticker aliases a particular statement needs are
therefore an *input* (:func:`parse`'s ``aliases``), not a constant — the same split D27 draws
for the acceptance corpus: the program is committed, the data and the output are not.
"""

import csv
import io
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final

from portfolio_dash.data_ingestion.broker.ir import EventKind, RawEvent, UnmappedRow

#: The broker's own header, in its own order. A file whose header differs is refused rather
#: than read positionally: a silently mis-mapped column is a wrong ledger, not an error.
SCHWAB_COLUMNS: tuple[str, ...] = (
    "Date", "Action", "Symbol", "Description", "Quantity", "Price", "Fees & Comm", "Amount",
)

#: "any description" — a rule for an action that carries exactly one meaning.
_ANY: Final = None

# (action, description substring | _ANY, kind). ORDERED — first match wins.
_RULES: tuple[tuple[str, str | None, EventKind], ...] = (
    # --- equity trades. "TO COVER" first: it is a plain "Buy" to the action column.
    ("Buy", "TO COVER", EventKind.BUY_COVER),
    ("Buy", _ANY, EventKind.BUY),
    ("Sell", _ANY, EventKind.SELL),
    ("Sell Short", _ANY, EventKind.SELL_SHORT),
    # --- distributions
    ("Reinvest Shares", _ANY, EventKind.DRIP_BUY),
    ("Qual Div Reinvest", _ANY, EventKind.DIVIDEND),
    ("Reinvest Dividend", _ANY, EventKind.DIVIDEND),
    ("Cash Dividend", _ANY, EventKind.DIVIDEND),
    ("Qualified Dividend", _ANY, EventKind.DIVIDEND),
    ("Non-Qualified Div", _ANY, EventKind.DIVIDEND),
    ("Special Dividend", _ANY, EventKind.DIVIDEND),
    ("Pr Yr Non Qual Div", _ANY, EventKind.DIVIDEND),
    ("Short Term Cap Gain", _ANY, EventKind.CAPGAIN_DIST),
    ("Long Term Cap Gain", _ANY, EventKind.CAPGAIN_DIST),
    ("NRA Tax Adj", _ANY, EventKind.WITHHOLDING_TAX),
    ("Pr Yr NRA Tax", _ANY, EventKind.WITHHOLDING_TAX),
    # A reclaim is withholding with the opposite sign; the SIGN is the fact, not the label.
    ("Foreign Tax Reclaim", _ANY, EventKind.WITHHOLDING_TAX),
    # --- cash. There is no wire-OUT rule because the assessed export contains none; adding
    # one blind would be a rule with no evidence behind it. An outgoing wire will therefore
    # raise UnmappedRow — loudly, which is the correct first encounter with a new row type.
    ("Wire Received", _ANY, EventKind.DEPOSIT),
    ("Bond Interest", _ANY, EventKind.INTEREST_INCOME),
    ("Credit Interest", _ANY, EventKind.INTEREST_INCOME),
    ("Margin Interest", _ANY, EventKind.INTEREST_EXPENSE),
    ("Interest Adj", _ANY, EventKind.INTEREST_EXPENSE),
    ("ADR Mgmt Fee", _ANY, EventKind.FEE),
    # --- corporate actions carried as their own action value
    ("Cash In Lieu", _ANY, EventKind.CASH_IN_LIEU),
    ("Stock Merger", _ANY, EventKind.NAME_CHANGE),
    ("Reverse Split", _ANY, EventKind.REVERSE_SPLIT),
    # --- options (P3: recognised, not supported)
    ("Buy to Open", _ANY, EventKind.OPT_BUY_OPEN),
    ("Sell to Open", _ANY, EventKind.OPT_SELL_OPEN),
    ("Buy to Close", _ANY, EventKind.OPT_BUY_CLOSE),
    ("Sell to Close", _ANY, EventKind.OPT_SELL_CLOSE),
    ("Expired", _ANY, EventKind.OPT_EXPIRED),
    # --- suppressed, by their own action value
    ("Internal Transfer", _ANY, EventKind.JOURNAL_INTERNAL),
    ("Journal", _ANY, EventKind.JOURNAL_INTERNAL),
    ("Cancel Buy", _ANY, EventKind.CANCELLED),
    ("Cancel Sell", _ANY, EventKind.CANCELLED),
    ("Reinvestment Adj", _ANY, EventKind.REINVEST_ADJ),
    # --- the overloaded one. NO _ANY row: an unlisted description must raise.
    #     Specific-before-general is load-bearing on the MANDATORY family.
    ("Journaled Shares", "REORGANIZATION FEE", EventKind.FEE),
    ("Journaled Shares", "W-8 WITHHOLDING", EventKind.WITHHOLDING_TAX),
    ("Journaled Shares", "INTERNAL TRANSFER BETWEEN ACCOUNTS", EventKind.JOURNAL_INTERNAL),
    ("Journaled Shares", "INTRA-ACCOUNT TRANSFER", EventKind.JOURNAL_INTERNAL),
    ("Journaled Shares", "TRANSFER OF SECURITY OR OPTION OUT", EventKind.TRANSFER_OUT),
    ("Journaled Shares", "CASH MOVEMENT OF OUTGOING ACCOUNT TRANSFER", EventKind.CASH_SWEEP),
    ("Journaled Shares", "MARK TO THE MARKET", EventKind.MARK_TO_MARKET),
    ("Journaled Shares", "MANDATORY REVERSE SPLIT", EventKind.REVERSE_SPLIT),
    ("Journaled Shares", "MANDATORY - EXCHANGE", EventKind.EXCHANGE),
    ("Journaled Shares", "STOCK SPLIT", EventKind.SPLIT),
    ("Journaled Shares", "SPIN OFF", EventKind.SPINOFF),
    ("Journaled Shares", "OPTION ODD SPLIT REORGANIZATION", EventKind.OPT_ADJUST),
    ("Journaled Shares", "OPTION POSITION CHANGE", EventKind.OPT_ADJUST),
    ("Journaled Shares", "REMOVAL OF OPTION DUE TO EXPIRATION", EventKind.OPT_ADJUST),
)

_TRAILING_TICKER = re.compile(r"\(([A-Z][A-Z0-9./]{0,7})\)\s*$")
_DUAL_DATE = " as of "


def classify(action: str, description: str) -> EventKind | None:
    """The ``(action, description)`` verdict, or ``None`` when no rule matches.

    ``None`` is not a kind — the caller turns it into :class:`~.ir.UnmappedRow` with the
    line number attached. Kept separate so the table stays a pure lookup and the error
    carries the context only the reader has.
    """
    upper = description.upper()
    for rule_action, pattern, kind in _RULES:
        if rule_action != action:
            continue
        if pattern is None or pattern in upper:
            return kind
    return None


def parse_money(raw: str) -> Decimal:
    """``-$1,234.56`` -> ``Decimal("-1234.56")``; blank -> 0.

    Decimal from the STRING, never via float: the broker states cents and a float round trip
    is exactly the representation noise ``data-and-pricing.md`` forbids in money.
    """
    text = (raw or "").strip()
    if not text:
        return Decimal(0)
    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    text = text.lstrip("-").strip("()").replace("$", "").replace(",", "").strip()
    if not text:
        return Decimal(0)
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"not a money amount: {raw!r}") from None
    return -value if negative else value


def parse_quantity(raw: str) -> Decimal:
    text = (raw or "").strip().replace(",", "")
    if not text:
        return Decimal(0)
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"not a quantity: {raw!r}") from None


def parse_dates(raw: str) -> tuple[date, date]:
    """``MM/DD/YYYY[ as of MM/DD/YYYY]`` -> ``(trade_date, posted_date)``.

    ⚠ The ``as of`` date is the TRADE date and the printed-first one is when the broker
    processed the row — measured on a real export: every dual-date row printed the first
    date exactly 3 days AFTER the ``as of`` date. The ledger's ``trade_date`` means the
    trade, so a dual row resolves to the second. Taking the first instead mis-dates those
    rows by the settlement lag, silently and only on the handful of rows that have one.
    """
    text = (raw or "").strip()
    head, _, tail = text.partition(_DUAL_DATE)
    posted = datetime.strptime(head.strip(), "%m/%d/%Y").date()
    trade = datetime.strptime(tail.strip(), "%m/%d/%Y").date() if tail else posted
    return trade, posted


def recover_symbol(
    symbol_cell: str, description: str, aliases: Mapping[str, str] | None = None
) -> str:
    """The row's ticker: the ``Symbol`` column, else a trailing ``(TICKER)`` in the text.

    Roughly a quarter of the assessed export's rows carry an empty ``Symbol`` and name the
    ticker only in the description. Rows that legitimately have NO symbol — interest, a wire
    — must not be forced to acquire one, so a miss returns ``""`` rather than guessing at
    the first capitalised word.

    *aliases* maps a CUSIP (or any superseded identifier the statement uses) to the ticker.
    It is supplied by the caller, never hard-coded: those identifiers are the owner's real
    holdings and this file is committed.
    """
    found = (symbol_cell or "").strip()
    if not found:
        match = _TRAILING_TICKER.search(description or "")
        found = match.group(1) if match else ""
    return (aliases or {}).get(found, found)


def parse(
    csv_text: str,
    *,
    source_file: str,
    aliases: Mapping[str, str] | None = None,
) -> list[RawEvent]:
    """Every row of one Schwab export, classified. Raises on the first unmapped row.

    The broker's exports carry preamble/trailer lines around the real table, so the header
    is LOCATED rather than assumed to be line 1; ``line_no`` is then the 1-based line in the
    original file, which is what a diagnostic has to cite to be actionable.
    """
    lines = list(csv.reader(io.StringIO(csv_text.lstrip("﻿"))))
    header_at = next(
        (
            i
            for i, row in enumerate(lines)
            if [c.strip() for c in row[: len(SCHWAB_COLUMNS)]] == list(SCHWAB_COLUMNS)
        ),
        None,
    )
    if header_at is None:
        raise ValueError(
            f"{source_file}: no Schwab header row found "
            f"(expected {', '.join(SCHWAB_COLUMNS)})"
        )

    events: list[RawEvent] = []
    for offset, row in enumerate(lines[header_at + 1 :], start=header_at + 2):
        cells = [c.strip() for c in row]
        if not cells or not cells[0] or len(cells) < len(SCHWAB_COLUMNS):
            continue  # blank separator or the export's trailing disclaimer line
        data = dict(zip(SCHWAB_COLUMNS, cells, strict=False))
        action = data["Action"]
        description = data["Description"]
        if not action:
            continue

        kind = classify(action, description)
        if kind is None:
            raise UnmappedRow(
                source_file=source_file, line_no=offset,
                action=action, description=description,
            )

        # An option contract's identifier carries spaces (TICKER MM/DD/YYYY STRIKE C|P);
        # an equity ticker never does. Kept OUT of `symbol`: shared/symbol_format.py rejects
        # the shape, and an option is not an instrument this ledger can register.
        symbol_cell = data["Symbol"]
        is_option = " " in symbol_cell
        trade_date, posted_date = parse_dates(data["Date"])
        events.append(
            RawEvent(
                line_no=offset,
                source_file=source_file,
                kind=kind,
                trade_date=trade_date,
                posted_date=posted_date,
                symbol="" if is_option else recover_symbol(
                    symbol_cell, description, aliases),
                option_symbol=symbol_cell if is_option else "",
                quantity=parse_quantity(data["Quantity"]),
                price=parse_money(data["Price"]),
                fees=parse_money(data["Fees & Comm"]),
                amount=parse_money(data["Amount"]),
                description=description,
            )
        )
    return events
