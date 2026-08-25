"""Source-of-truth ledger models: transactions, dividends, FX, opening inventory."""

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.shared.corporate_actions import CorporateAction, UnreadableAction
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.types import Money

#: Alias for :class:`datetime.date`. A Pydantic field named ``date`` SHADOWS the type
#: inside its own class body, so every annotation after it — ``ex_date``, the
#: ``effective_date`` return — resolves to the FIELD and mypy rejects it. Aliasing is the
#: least surprising fix: renaming the field would change the ledger's wire contract.
Date = date


class Transaction(BaseModel):
    """A buy or sell. Fees/tax are the snapshot taken at entry; stored, never recomputed."""

    account_id: str
    symbol: str
    side: Side
    quantity: Money
    price: Money
    fees: Money
    tax: Money
    trade_date: date
    # DECLARED short sale (2026-07-31, spec option C). Off by default and settable only as a
    # deliberate per-transaction choice, because the system cannot tell a genuine short from
    # a missing buy — and auto-treating every oversell as a short would turn a data-entry
    # slip into a plausible-looking realized loss. Only a flagged sell may exceed holdings
    # without the 賣超 guard; the position it opens is covered by the next buy(s).
    short_sale: bool = False


class Dividend(BaseModel):
    """A dividend event. `net` is what reduces adjusted cost (cash) or was reinvested."""

    account_id: str
    symbol: str
    #: The PAYMENT date — when the cash lands or the reinvestment is bought. Pinned to that
    #: meaning by R6; before it, this column carried whichever date the source happened to
    #: supply, which is why ``ex_date`` had to be added rather than inferred.
    date: date
    #: The EX-DIVIDEND date, when known (R6 / review ⑧). ``None`` on every pre-R6 row and on
    #: any source that does not supply one — never guessed, and a row with ``None`` replays
    #: exactly as it did before this field existed.
    ex_date: Date | None = None
    type: DividendType
    gross: Money
    withholding: Money
    net: Money
    reinvest_shares: Money | None = None
    reinvest_price: Money | None = None

    @property
    def effective_date(self) -> Date:
        """The date the REPLAY books this event — the one every date filter must use.

        Only a **STOCK** dividend (配股) moves to the ex-date, and the rule is about what you
        actually own on a given day:

        * **STOCK** — an entitlement that attaches on the ex-date. You own the shares from
          then, and the quoted price already says so. Holding the OLD share count against the
          ALREADY-ADJUSTED price for the ~month until payment is what made a 10% 配股 read as
          a ~9% loss once a year (review ⑧).
        * **DRIP** — the cash is paid on the payment date and the reinvestment is BOUGHT then,
          at the recorded reinvest price. Those shares do not exist earlier.
        * **CASH / NET** — the price drops on the ex-date while the cost reduces when the money
          arrives. That dip is left in place deliberately: unlike the stock case it is HONEST,
          because nothing about what you own changed on the ex-date — you are simply not paid
          yet.

        One property rather than a condition repeated at each filter: ``build_book``'s event
        ordering, ``LedgerBundle.through`` and ``before_action_on`` must agree, and three
        copies of a rule is how two of them drift.
        """
        if self.type is DividendType.STOCK and self.ex_date is not None:
            return self.ex_date
        return self.date


class FXConversion(BaseModel):
    """An actual currency conversion (primarily consumed by sub-project ② forex)."""

    account_id: str
    date: date
    from_ccy: Currency
    from_amount: Money
    to_ccy: Currency
    to_amount: Money


class OpeningInventory(BaseModel):
    """A pre-existing position seeded at a build date (not a trade flow; feeds XIRR).

    ``original_cost_total`` is the authoritative money of record (cost basis / XIRR key off
    it). The original average is NEVER stored — a rounded average must never be the authority
    (domain-ledger.md); it is computed on read via :attr:`original_avg`.
    """

    account_id: str
    symbol: str
    shares: Money
    original_cost_total: Money
    build_date: date

    @property
    def original_avg(self) -> Decimal:
        """Original average cost, computed on read (total / shares). Display-only; never the
        authority. Zero shares -> Decimal(0) defensively (a valid opening has shares > 0)."""
        return self.original_cost_total / self.shares if self.shares else Decimal(0)


@dataclass(frozen=True)
class LedgerBundle:
    """The complete in-memory ledger set one replay reads, passed as ONE argument.

    Every ``build_book`` call site took the same four positional arguments, so adding a
    ledger meant editing eight signatures — and *forgetting* one of them is silent. With
    the bundle, a new ledger is one field here plus one line in
    :func:`~portfolio_dash.data_ingestion.store.load_ledger_bundle`; nothing downstream
    changes shape. ``actions`` is that promise being kept: it landed as one field, and no
    call site changed shape to receive it.

    A dataclass, not a ``BaseModel``: Pydantic would re-validate and copy every row on
    construction, and :meth:`through` builds one bundle per day of the trend replay.
    ``frozen`` guards the reference, not the lists — the replay never mutates them, and
    the filtering helpers below all return a NEW bundle.
    """

    transactions: list[Transaction] = field(default_factory=list)
    dividends: list[Dividend] = field(default_factory=list)
    opening: list[OpeningInventory] = field(default_factory=list)
    instruments: dict[str, Instrument] = field(default_factory=dict)
    actions: list[CorporateAction] = field(default_factory=list)
    # Stored action rows the loader could not convert (2026-08-11). NOT a fifth ledger —
    # it is the same ledger's rejects, carried alongside so `build_book` can turn each into
    # an `UnappliedAction` instead of the loader raising into a caller that has no
    # try/except. See `UnreadableAction` for why raising and dropping were both rejected.
    unreadable_actions: list[UnreadableAction] = field(default_factory=list)

    def through(self, day: date) -> "LedgerBundle":
        """Everything dated on-or-before *day*, over the daily trend replay's date columns.

        The per-day filter used to be open-coded at the one call site that needs it, which
        is exactly where a new ledger gets forgotten — silently, because a book missing a
        ledger still builds.
        """
        return replace(
            self,
            transactions=[t for t in self.transactions if t.trade_date <= day],
            # R6: `effective_date`, not `date` — a STOCK dividend with a known ex-date is
            # booked from the ex-date, so it must be IN the bundle on those days at all.
            dividends=[d for d in self.dividends if d.effective_date <= day],
            opening=[o for o in self.opening if o.build_date <= day],
            actions=[a for a in self.actions if a.date <= day],
            unreadable_actions=[u for u in self.unreadable_actions if u.date <= day],
        )

    def before_action_on(self, day: date) -> "LedgerBundle":
        """Everything that replays BEFORE a corporate action dated *day* (2026-08-11).

        Three bounds, not one — the same cut ``data_ingestion/holdings.py``'s walker uses,
        for the same reason (F-18 / D3). ``EventPriority`` orders a day's events
        ``OPENING (0) → CORPORATE_ACTION (10) → BUY (20) → SELL (30) → DIVIDEND (40)``, so:

        * ``opening <= day`` — a same-day opening is **pre**-action (D3 ruled this);
        * ``transactions`` / ``dividends`` / ``actions`` ``< day`` — same-day trades and
          payouts land **after** the action and must not be visible to it.

        :meth:`through` is deliberately NOT this: it is ``<= day`` on all four, which is
        right for the trend replay (value the world as at the close of *day*) and wrong
        here (value the world as the action sees it). Using ``through`` would still replay
        a same-day sell before the split that authorises it — precisely the case a broker
        books when it settles a split and a sale together.

        Why it exists: four hard rejections — **E3** oversold source, **E22** oversold
        destination, **E5** short source, **E18** short destination — are evaluated from a
        replayed book. Read against the WHOLE ledger they see a future that has not
        happened yet, and on any bulk import the affected position is already 賣超 when its
        own action is validated, so E3 rejects the row that resolves the 賣超. Measured
        2026-08-11 on §1's headline case (buy 100, 7-for-1, sell 400).
        """
        return replace(
            self,
            transactions=[t for t in self.transactions if t.trade_date < day],
            # R6: same rule as `through` — a STOCK dividend booked from its ex-date is
            # visible to a later action, and invisible to an earlier one.
            dividends=[d for d in self.dividends if d.effective_date < day],
            opening=[o for o in self.opening if o.build_date <= day],
            actions=[a for a in self.actions if a.date < day],
            unreadable_actions=[u for u in self.unreadable_actions if u.date < day],
        )

    @property
    def unregistered_symbols(self) -> list[str]:
        """Sorted symbols appearing in a ledger with no :class:`Instrument` row.

        Such a row has no quote currency, so it cannot be booked, valued, or priced.
        Callers decide what to do about it — the dashboard drops the rows and reports the
        symbols, 重算 refuses outright — but the SET is computed here, once, over every
        ledger the bundle carries.

        An action contributes **both** its symbols (E21): the skip-set used to be built
        from three ledgers, and an action row referencing a deleted instrument would then
        reach ``build_book``, whose ``quote_ccy()`` raises ``KeyError`` — a 500, and a
        different exception type from every other degradation path.
        """
        used = ({t.symbol for t in self.transactions}
                | {d.symbol for d in self.dividends}
                | {o.symbol for o in self.opening}
                | {a.from_symbol for a in self.actions}
                | {a.to_symbol for a in self.actions})
        return sorted(used - self.instruments.keys())

    def without_unregistered(self) -> "LedgerBundle":
        """This bundle minus every row whose symbol has no instrument (graceful degradation)."""
        known = self.instruments.keys()
        return replace(
            self,
            transactions=[t for t in self.transactions if t.symbol in known],
            dividends=[d for d in self.dividends if d.symbol in known],
            opening=[o for o in self.opening if o.symbol in known],
            # BOTH symbols must be known: an action half of whose pair is unregistered
            # cannot be booked either way round.
            actions=[a for a in self.actions
                     if a.from_symbol in known and a.to_symbol in known],
        )
