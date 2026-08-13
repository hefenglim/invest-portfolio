"""The broker-neutral intermediate representation: ``EventKind`` and ``RawEvent``.

One ``RawEvent`` is one LEG of a broker statement — one line of the export, classified. It is
deliberately not a domain event: a US reinvested dividend arrives as three legs, and folding
them is :mod:`.grouping`'s job, downstream of every broker-specific concern.

**The vocabulary is CLOSED, and that is the safety property.** An unmapped broker row must be
a hard error, never a silent drop: a type that quietly disappears produces a ledger that
looks complete and is not, which is the failure mode the whole broker-import assessment
exists to prevent. A closed enum is what makes "unmapped" a thing the code can detect.

⚠ **The unit of classification is the ``(action, description)`` PAIR, not the action.**  One
observed export overloads a single action value across **14 distinct meanings**, and the two
largest sit on opposite sides of the keep/drop line — 145 rows of dividend withholding tax
(keep) against 126 null account-type journals (drop). An importer keyed on the action column
alone therefore either deletes 145 tax rows as noise or keeps 126 phantom journals as real
events, and no amount of arithmetic recovers the distinction in either direction.
"""

import re
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

#: A 9-character alphanumeric identifier containing a digit — a CUSIP, never a ticker. A US
#: ticker is one to five letters (``BRK.B`` is five plus a class suffix); nothing tradeable is
#: nine characters wide, so the two vocabularies cannot collide. Option contracts never reach
#: this test — they live in ``RawEvent.option_symbol``.
_CUSIP = re.compile(r"[0-9A-Z]{9}")


def looks_like_cusip(value: str) -> bool:
    """True for an identifier a statement printed where a ticker belongs.

    Broker-neutral on purpose. A CUSIP is a US securities identifier, not a Schwab quirk, and
    the reconciler must be able to ask "is this a ticker?" without importing one broker's
    adapter — the moment it does, the second broker's rules have nowhere to live.
    """
    return bool(_CUSIP.fullmatch(value)) and any(c.isdigit() for c in value)


class EventKind(StrEnum):
    """Every classified meaning a broker row can carry. Closed on purpose (see the module
    docstring): adding a broker means adding rules that map onto THESE, not adding kinds."""

    # --- equity trades ------------------------------------------------------------
    BUY = "BUY"
    SELL = "SELL"
    SELL_SHORT = "SELL_SHORT"      # a DECLARED short — never inferred (domain-ledger.md)
    BUY_COVER = "BUY_COVER"

    # --- distributions ------------------------------------------------------------
    DIVIDEND = "DIVIDEND"                  # the gross payout leg
    DRIP_BUY = "DRIP_BUY"                  # the reinvest-purchase leg of a DRIP group
    CAPGAIN_DIST = "CAPGAIN_DIST"          # a fund's capital-gain distribution
    WITHHOLDING_TAX = "WITHHOLDING_TAX"    # the W-8 withholding leg (or a reclaim, signed)

    # --- cash ---------------------------------------------------------------------
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    INTEREST_INCOME = "INTEREST_INCOME"
    INTEREST_EXPENSE = "INTEREST_EXPENSE"  # margin interest paid
    FEE = "FEE"                            # ADR management fee, reorganisation fee

    # --- corporate actions --------------------------------------------------------
    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    EXCHANGE = "EXCHANGE"                  # mandatory exchange (SPAC, merger-for-shares)
    NAME_CHANGE = "NAME_CHANGE"            # ticker/CUSIP rename — an EXCHANGE at 1:1
    SPINOFF = "SPINOFF"
    CASH_IN_LIEU = "CASH_IN_LIEU"          # cash for a fractional share

    # --- options (P3) -------------------------------------------------------------
    # Recognised so rule 7 does not fire on a statement that legitimately contains them.
    # RECOGNISED IS NOT SUPPORTED: the grouper routes them nowhere, and the reconciler
    # reports them as unhandled. The 100x contract multiplier is an assumption to remove
    # from every money formula, not a field to add, so options stay their own sub-project.
    OPT_BUY_OPEN = "OPT_BUY_OPEN"
    OPT_SELL_OPEN = "OPT_SELL_OPEN"
    OPT_BUY_CLOSE = "OPT_BUY_CLOSE"
    OPT_SELL_CLOSE = "OPT_SELL_CLOSE"
    OPT_EXPIRED = "OPT_EXPIRED"
    OPT_ADJUST = "OPT_ADJUST"              # odd-split reorg / position change / removal

    # --- suppressed: real lines that are NOT domain events ------------------------
    # Each is a *classification*, never a guess. The zero-sum check then runs as a VETO on
    # top of it — see grouping.py for why that ordering is forced rather than preferred.
    JOURNAL_INTERNAL = "JOURNAL_INTERNAL"  # re-journal between account types
    TRANSFER_OUT = "TRANSFER_OUT"          # platform (ACAT) security transfer
    CASH_SWEEP = "CASH_SWEEP"              # cash leg of an outgoing account transfer
    MARK_TO_MARKET = "MARK_TO_MARKET"
    CANCELLED = "CANCELLED"                # a cancel row; its original is dropped with it
    REINVEST_ADJ = "REINVEST_ADJ"          # a DRIP re-booked at a corrected price


#: Kinds that are ledger events. Everything else is either suppressed or an option leg.
LEDGER_KINDS: Final[frozenset[EventKind]] = frozenset({
    EventKind.BUY, EventKind.SELL, EventKind.SELL_SHORT, EventKind.BUY_COVER,
    EventKind.DIVIDEND, EventKind.DRIP_BUY, EventKind.CAPGAIN_DIST,
    EventKind.WITHHOLDING_TAX,
    EventKind.DEPOSIT, EventKind.WITHDRAWAL, EventKind.INTEREST_INCOME,
    EventKind.INTEREST_EXPENSE, EventKind.FEE,
    EventKind.SPLIT, EventKind.REVERSE_SPLIT, EventKind.EXCHANGE, EventKind.NAME_CHANGE,
    EventKind.SPINOFF, EventKind.CASH_IN_LIEU,
})

#: Kinds a group may be dropped as — the classification half of the suppression rule.
SUPPRESSIBLE_KINDS: Final[frozenset[EventKind]] = frozenset({
    EventKind.JOURNAL_INTERNAL, EventKind.TRANSFER_OUT, EventKind.CASH_SWEEP,
    EventKind.MARK_TO_MARKET, EventKind.CANCELLED, EventKind.REINVEST_ADJ,
})

#: Option legs — recognised, deliberately unsupported (P3).
OPTION_KINDS: Final[frozenset[EventKind]] = frozenset({
    EventKind.OPT_BUY_OPEN, EventKind.OPT_SELL_OPEN, EventKind.OPT_BUY_CLOSE,
    EventKind.OPT_SELL_CLOSE, EventKind.OPT_EXPIRED, EventKind.OPT_ADJUST,
})

#: The corporate actions that P0's ``corporate_actions`` ledger can express.
CORPORATE_ACTION_KINDS: Final[frozenset[EventKind]] = frozenset({
    EventKind.SPLIT, EventKind.REVERSE_SPLIT, EventKind.EXCHANGE,
    EventKind.NAME_CHANGE, EventKind.SPINOFF,
})


class RawEvent(BaseModel):
    """One classified leg of a broker statement. Frozen: an adapter emits, nothing mutates."""

    model_config = ConfigDict(frozen=True)

    #: 1-based line number within its source file. Every diagnostic cites it, because
    #: "the totals disagree" is only actionable once it can name a line.
    line_no: int
    source_file: str

    kind: EventKind
    #: The date the ledger uses. Where the broker prints ``post as of trade`` this is the
    #: TRADE date — the later-printed one is when the broker processed it, and the ledger's
    #: ``trade_date`` means the trade. (Measured on one real export: 6 dual-date rows, the
    #: printed-first date later than the ``as of`` date by exactly 3 days in every case.)
    trade_date: date
    #: The broker's own processing date. Equal to ``trade_date`` on a single-date row; kept
    #: so a cash-balance reconciliation against a statement period can use the right one.
    posted_date: date

    #: Equity ticker, or ``""`` for a row that legitimately has none (interest, a wire).
    #: A row with no symbol must NOT be forced to have one. This is the RESOLVED value: an
    #: adapter may have recovered it from the description when the statement's own column held
    #: nothing, or held a CUSIP.
    symbol: str = ""
    #: The statement's ``Symbol`` cell exactly as printed, before any recovery. Kept because
    #: resolution is lossy in the one direction that matters: once a CUSIP row has been
    #: rewritten to its ticker, the link between the two identifiers is gone, and the rows the
    #: file never names cannot be joined back to the ones it does. That link is what
    #: :func:`grouping.infer_cusip_aliases` learns from.
    broker_symbol: str = ""
    #: The full option contract string when this is an option leg, else ``""``. Kept out of
    #: ``symbol`` because ``shared/symbol_format.py`` rejects it and it is not an instrument.
    option_symbol: str = ""

    quantity: Decimal = Decimal(0)
    price: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    #: Signed cash effect as the broker states it: negative is money out.
    amount: Decimal = Decimal(0)

    #: The broker's untouched free text. Its action codes are overloaded, so this is the
    #: only evidence that a row was classified correctly — it travels with the event and
    #: into the converter's report.
    description: str = ""

    @property
    def ref(self) -> str:
        """``file:line`` — the row's UNIQUE identity, and what every diagnostic cites.

        ⚠ ``line_no`` alone is not unique. A broker hands over several exports with
        overlapping date windows (five, in the assessed case), and merging them is the
        normal path — line 2 of one file and line 2 of the next are different rows. Keying
        anything on the bare line number silently conflates them; the conservation check in
        ``grouping.account_for`` reported 108 rows as double-counted the first time it was
        run across the real five-file set, which is that mistake, caught.
        """
        return f"{self.source_file}:{self.line_no}"

    @property
    def is_option(self) -> bool:
        return bool(self.option_symbol)


class UnmappedRow(Exception):
    """Rule 7: an unrecognised ``(action, description)`` pair is a HARD ERROR.

    Never a silent drop, and never a catch-all bucket. A catch-all is the same defect wearing
    a name: the ground-truth build this adapter was lifted from ended its classifier with
    ``return OPT_ADJUST``, so any future description the broker invents would have been
    booked as an option adjustment and vanished from every equity figure without a word.
    """

    def __init__(self, *, source_file: str, line_no: int, action: str, description: str):
        self.source_file = source_file
        self.line_no = line_no
        self.action = action
        self.description = description
        super().__init__(
            f"{source_file}:{line_no}: unmapped broker row — action={action!r} "
            f"description={description!r}. Add an explicit rule; never a default."
        )
