"""Month and year closes (PERF spec 2026-10-18 §11.2, §11.3; plan §6.1 "period_close").

Pure functions over in-memory inputs. The settlement worker (``portfolio/settle_worker.py``)
calls them once per period end of its range, from the books of its ONE replay.

**The tax sheets' row selection lives here** (plan §6.1 "Tax-sheet extraction"): the annual tax
package (``export/tax.py``) and a period close's activity both take their rows from
:func:`realized_rows`, :func:`post_close_dividend_rows`, :func:`dividend_rows` and
:func:`fx_realized_rows`, so a close's realized, dividend and realized-FX lines tie out with the
package sheet by sheet (§11.3) by construction — one selection, two readers. The functions keep
the replay's / the ledger's own row order, which is what makes a year's sum equal the sum of its
months' sums when both are added left to right.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from portfolio_dash.forex.fx_pnl import realized_fx_rows_as_of
from portfolio_dash.forex.pools import MovementRow
from portfolio_dash.forex.results import FxRealizedRow
from portfolio_dash.portfolio.results import RealizedRow
from portfolio_dash.shared.models.assets import Account
from portfolio_dash.shared.models.enums import DividendType
from portfolio_dash.shared.models.ledger import Dividend, FXConversion

#: The realized-row kinds that are a CAPITAL gain or loss — a sale, and a declared short's
#: cover (2026-07-31). A ``dividend`` row (audit H2) is income, reported from the dividend
#: ledger instead.
CAPITAL_KINDS: tuple[str, ...] = ("sale", "short_cover")


def _inside(day: date, start: date, end: date) -> bool:
    return start <= day <= end


def realized_rows(rows: Iterable[RealizedRow], *, start: date, end: date) -> list[RealizedRow]:
    """The capital-gains sheet's rows: every ``sale`` / ``short_cover`` realized row whose
    ``sell_date`` lies in ``[start, end]``, in the replay's own row order
    (``realized_gains_<year>.csv``)."""
    return [r for r in rows if _inside(r.sell_date, start, end) and r.kind in CAPITAL_KINDS]


def post_close_dividend_rows(rows: Iterable[RealizedRow], *, start: date,
                             end: date) -> list[RealizedRow]:
    """The realized rows of kind ``dividend`` — a cash payout booked after its position closed
    (audit H2) — dated in ``[start, end]``, in replay order. The same payout is also on the
    dividends sheet, which is why the capital-gains sheet leaves it out."""
    return [r for r in rows if _inside(r.sell_date, start, end) and r.kind == "dividend"]


def dividend_rows(dividends: Iterable[Dividend], *, start: date, end: date) -> list[Dividend]:
    """The dividends sheet's rows: every dividend but a 配股 (STOCK adds shares, not cash) whose
    payment ``date`` lies in ``[start, end]``, in ledger order (``dividends_<year>.csv``).

    *dividends* are the rows the caller counts: the tax package replays strictly (a refused
    payout raises there), a period close passes the BOOKED rows only."""
    return [d for d in dividends
            if _inside(d.date, start, end) and d.type is not DividendType.STOCK]


@dataclass(frozen=True)
class AccountFxRealized:
    """One realized-FX row of the 換匯損益 owner, with the account it belongs to."""

    account_id: str
    row: FxRealizedRow


def fx_realized_rows(accounts: Iterable[Account], conversions: Sequence[FXConversion],
                     movements: Sequence[MovementRow], *, start: date,
                     end: date) -> list[AccountFxRealized]:
    """The realized-FX sheet's rows (``fx_realized_<year>.csv``): for every account whose
    settlement currency is not its funding currency, in *accounts* order, each reconversion
    dated in ``[start, end]`` priced at the pool average ON ITS OWN DATE
    (:func:`~portfolio_dash.forex.fx_pnl.realized_fx_rows_as_of` — the one entry point the
    dashboard's 換匯損益 uses, so the two can never price one reconversion differently)."""
    out: list[AccountFxRealized] = []
    for acct in accounts:
        if acct.settlement_ccy == acct.funding_ccy:
            continue
        home, foreign = acct.funding_ccy, acct.settlement_ccy
        acct_convs = [c for c in conversions if c.account_id == acct.account_id]
        acct_moves = [m for m in movements if m.account_id == acct.account_id]
        for fr in realized_fx_rows_as_of(acct_convs, acct_moves, home, foreign):
            if _inside(fr.date, start, end):
                out.append(AccountFxRealized(account_id=acct.account_id, row=fr))
    return out


__all__ = [
    "CAPITAL_KINDS",
    "AccountFxRealized",
    "dividend_rows",
    "fx_realized_rows",
    "post_close_dividend_rows",
    "realized_rows",
]
