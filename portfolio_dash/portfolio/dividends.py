"""Annual declared-dividend cash-flow projection (spec 05). Pure over computed outputs;
no ledger writes. Net applies each holding account's dividend model (withholding only --
the Moomoo-US per-dividend platform fee is probe-pending and deferred).

The dividend model is resolved PER MARKET off the ``accounts`` param: a holding's
instrument market selects the account's ``market_rules`` binding
(``Account.market_rules[market]``) when present, else the account-level
``Account.dividend_model`` scalar (the fallback -- a single-market account with no
binding behaves identically). Both come from ``list_accounts`` (DB truth); there is no
config-as-code map. Staying pure (no conn) is the architecture rule for ``portfolio/``;
the per-market data rides on the Account model. A holding referencing an unknown
account_id raises KeyError (fail loud on corrupt data rather than silently defaulting
net = gross).
"""

from collections import defaultdict
from decimal import Decimal

from portfolio_dash.data_ingestion.dividend_model import apply_dividend_model
from portfolio_dash.portfolio.dashboard_models import (
    DividendProjection,
    DividendProjectionCurrency,
    ExDividendItem,
)
from portfolio_dash.portfolio.results import Holding
from portfolio_dash.shared.enums import Currency
from portfolio_dash.shared.models.assets import Account, Instrument

_ZERO = Decimal("0")

# Map account.dividend_model -> apply_dividend_model div_type:
#   drip_us -> "DRIP" (30% US withholding); cash variants -> "cash" (net = gross).
_MODEL_DIV_TYPE = {
    "drip_us": "DRIP",
    "cash_cost_reduction": "cash",
    "cash": "cash",
    "net": "net",
}


def project_dividends(
    holdings: list[Holding],
    calendar: list[ExDividendItem],
    accounts: dict[str, Account],
    instruments: dict[str, Instrument],
    *,
    year: int,
) -> DividendProjection:
    """Per-currency declared gross/net dividend cash flow for ``year``.

    declared_only basis: only ex-dividend events for held symbols with a cash amount
    and ``ex_date.year == year``. Net applies each holding's per-market dividend model
    (``accounts[h.account_id].market_rules[market]`` if bound, else the account scalar;
    an unknown account_id raises KeyError = fail loud on corrupt data). Currencies are
    NEVER summed across; ``by_currency`` is keyed by the event currency (falling back to
    the instrument's quote currency).

    **A holding whose SHARE COUNT is 待釐清 is excluded, not multiplied.** The projection is
    ``shares × declared cash per share``, so it is decided by the same question
    ``value_holdings`` (``portfolio/pnl.py``) asks before it will value a position — *are
    the shares right?* — and it must answer it the same way:

    * ``unbookable_action`` — a corporate action was skipped, so ``shares`` is in PRE-action
      terms while the declared per-share amount is, like the price, POST-action. The product
      is wrong by the action's whole ratio (measured: a refused 3-for-1 projects 150 where
      the applied one projects 450) and it renders as a clean, precise number.
    * ``oversold`` — the basis was discarded and the ledger is known to be missing a buy or
      an opening, so the count is not the owner's position. STICKY, so a later buy can leave
      it POSITIVE and inside the ``shares > 0`` filter below.
    * ``unbookable_dividend`` / ``short_open`` are NOT excluded, for the reasons
      ``value_holdings`` gives: the first has right shares and a cost-side gap only, and the
      second is a real position (its negative shares fail ``shares > 0`` on their own).

    Exclusion is PER POSITION, matching ``value_holdings``'s containment requirement: one
    flagged holding must never blank a whole currency's projection.
    """
    gross: dict[Currency, Decimal] = defaultdict(lambda: _ZERO)
    net: dict[Currency, Decimal] = defaultdict(lambda: _ZERO)
    events: dict[Currency, int] = defaultdict(int)

    by_symbol: dict[str, list[Holding]] = defaultdict(list)
    for h in holdings:
        # Same predicate, same order, same reason as portfolio/pnl.py::value_holdings —
        # a share count that may not be valued may not be multiplied by a dividend either.
        if h.oversold or h.unbookable_action:
            continue
        if h.shares > _ZERO:
            by_symbol[h.symbol].append(h)

    for ev in calendar:
        if ev.cash_amount is None or ev.ex_date.year != year:
            continue
        ccy = ev.currency or instruments[ev.symbol].quote_ccy
        contributed = False
        for h in by_symbol.get(ev.symbol, []):
            g = h.shares * ev.cash_amount
            # Per-market dividend model: the holding's instrument market (h.symbol ==
            # ev.symbol) selects the account's (account, market) binding when present,
            # else the account-level scalar. Resolved off the Account model -- pure, no
            # conn (architecture: portfolio/ is pure). Only reached when a holding exists,
            # so instruments[h.symbol] is safe (an unheld event never looks it up here).
            account = accounts[h.account_id]
            rule = account.market_rules.get(instruments[h.symbol].market.value)
            model = rule.dividend_model if rule is not None else account.dividend_model
            div_type = _MODEL_DIV_TYPE.get(model, "cash")
            gross[ccy] += g
            net[ccy] += apply_dividend_model(div_type, gross=g).net
            contributed = True
        if contributed:
            events[ccy] += 1

    by_currency = {
        ccy: DividendProjectionCurrency(
            declared_gross=gross[ccy], declared_net=net[ccy], events=events[ccy]
        )
        for ccy in gross
    }
    return DividendProjection(year=year, by_currency=by_currency, basis="declared_only")
