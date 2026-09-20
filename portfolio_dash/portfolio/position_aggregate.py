"""Cross-account aggregate of ONE symbol's holdings — the single owner of that definition.

A symbol can be held in more than one account (AAPL in Schwab and in Moomoo MY), and two
readers need the combined position: the symbol drawer's 部位摘要 (``api/routers/symbol.py``,
owner ruling #2c) and the per-symbol LLM prompt (``llm_insight/variables.py``). Until
2026-09-17 the arithmetic lived in the router, so the prompt could not reach it
(``llm_insight`` may import ``portfolio`` and ``shared`` only, never ``api``), the prompt
handed the model the per-account rows alone, and the model — asked to describe the
position — summed them itself: the audit author's second re-verification found a card
saying 「總計 95.0457 股，總成本約為 15,916.00 USD」, correct to the digit, flagged 數值待核
because neither total was a number the system had fed it. The rule in ``llm-insight.md``
is that the model reasons about computed numbers and never recomputes them; a prompt that
gives the parts and needs the whole makes the model break that rule to answer. So the
whole is computed HERE, once, and both readers print it.

Moving the arithmetic rather than copying it: a second definition in ``llm_insight`` would
be a second owner of "what is this symbol's combined position", and the two would drift
(the ``abs()`` guards below are exactly the kind of detail a copy loses).

The rules, unchanged from the router they were lifted out of:

* all holdings of one symbol share a quote currency, so money is a plain Decimal sum and
  the averages are ``total_cost / total_shares`` **computed on read** (data-and-pricing.md:
  never a stored rounded average);
* a 缺價 holding carries ``None`` market fields and is excluded from the value sums —
  price is per-symbol, so either every row of the symbol is valued or none is;
* ratios over the basis divide by ``abs(original_total)``: a short leg's basis is negative
  (proceeds received) and a signed denominator flips or shrinks the ratio (review
  2026-08-24);
* the aggregate's flags are ``any()`` over the rows — one poisoned account poisons a sum.

Pure: Decimal in, Decimal out, no connection, no clock.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from portfolio_dash.portfolio.dashboard_models import HoldingRow
from portfolio_dash.shared.enums import Currency

_ZERO = Decimal("0")
#: 100% payback — the 已回本 threshold, materialised so every reader tests the same Decimal.
_ONE = Decimal("1")


def _sum(values: list[Decimal]) -> Decimal:
    """Decimal sum with a Decimal zero seed (exact; never float)."""
    total = _ZERO
    for v in values:
        total += v
    return total


class AggregatePosition(BaseModel):
    """One symbol's holdings combined across accounts, every money field a Decimal.

    ``market_value`` / ``unrealized_pnl`` / ``unrealized_pct`` / ``capital_gain`` /
    ``weight`` are ``None`` when the symbol is unpriced. The three ``payback_*`` provenance
    fields are ``None`` on a position no SPINOFF fed (M1-03 / D21).
    """

    account_count: int
    symbol: str
    quote_ccy: Currency
    shares: Decimal
    original_avg: Decimal
    adjusted_avg: Decimal
    original_cost_total: Decimal
    adjusted_cost_total: Decimal
    dividend_portion: Decimal
    payback_ratio: Decimal
    market_price: Decimal | None
    market_value: Decimal | None
    unrealized_pnl: Decimal | None
    unrealized_pct: Decimal | None
    capital_gain: Decimal | None
    weight: Decimal | None
    price_stale: bool
    price_as_of: date | None
    oversold: bool
    short_open: bool
    unbookable_dividend: bool
    unbookable_action: bool
    payback_from_symbol: str | None
    payback_carried_dividends: Decimal | None
    payback_own_dividends: Decimal | None
    fully_recovered: bool


def aggregate_position(rows: list[HoldingRow]) -> AggregatePosition | None:
    """Combine every holding row of ONE symbol; ``None`` when the symbol is not held.

    The caller passes the rows already filtered to a single symbol (they share a quote
    currency by construction). A single-row input aggregates to that row's own figures.
    """
    if not rows:
        return None
    total_shares = _sum([h.shares for h in rows])
    original_total = _sum([h.original_cost_total for h in rows])
    adjusted_total = _sum([h.adjusted_cost_total for h in rows])
    dividend_portion = _sum([h.dividend_portion for h in rows])

    mv = [h.market_value for h in rows if h.market_value is not None]
    ur = [h.unrealized_pnl for h in rows if h.unrealized_pnl is not None]
    cg = [h.capital_gain for h in rows if h.capital_gain is not None]
    wt = [h.weight for h in rows if h.weight is not None]

    # market_price / staleness are per-symbol identical; take them from a priced row.
    src = next((h for h in rows if h.market_price is not None), rows[0])

    original_avg = original_total / total_shares if total_shares != _ZERO else _ZERO
    adjusted_avg = adjusted_total / total_shares if total_shares != _ZERO else _ZERO
    # abs(): same guard, same reason as unrealized_pct below — a short leg contributes a
    # NEGATIVE basis, so a signed sum can shrink or flip the denominator and print a
    # position that really returned 30% of its cost as -7.5% 回本進度 (review 2026-08-24).
    payback = dividend_portion / abs(original_total) if original_total != _ZERO else _ZERO
    # Aggregate unrealized % on the SAME basis as the per-holding figure (audit H1):
    # Σ unrealized / Σ original cost.
    unrealized_sum = _sum(ur) if ur else None
    # abs(): a short's basis is negative (proceeds received) and would flip the sign, showing
    # a profitable short as a loss. Same guard as the per-holding figure in dashboard.py.
    unrealized_pct = (
        unrealized_sum / abs(original_total)
        if unrealized_sum is not None and original_total != _ZERO
        else None
    )
    # M1-03 / D21: the aggregate's ratio is over the SUM of the rows' portions, so its
    # provenance is the sum of the carried / own amounts of the rows that carry one, named
    # after the first such row. None when no account's position was fed by a SPINOFF.
    # Known limit: a same-symbol position in ANOTHER account that was bought outright emits
    # no `payback_own_dividends`, so it feeds the aggregate ratio but not 自身配息 here; the
    # per-account rows beneath the aggregate are each exact.
    carried_rows = [h for h in rows if h.payback_from_symbol is not None]
    payback_from = carried_rows[0].payback_from_symbol if carried_rows else None
    payback_carried = (_sum([h.payback_carried_dividends for h in carried_rows
                             if h.payback_carried_dividends is not None])
                       if carried_rows else None)
    payback_own = (_sum([h.payback_own_dividends for h in carried_rows
                         if h.payback_own_dividends is not None])
                   if carried_rows else None)
    short_open = any(h.short_open for h in rows)

    return AggregatePosition(
        account_count=len(rows),
        symbol=rows[0].symbol,
        quote_ccy=rows[0].quote_ccy,
        shares=total_shares,
        original_avg=original_avg,
        adjusted_avg=adjusted_avg,
        original_cost_total=original_total,
        adjusted_cost_total=adjusted_total,
        dividend_portion=dividend_portion,
        payback_ratio=payback,
        market_price=src.market_price,
        market_value=_sum(mv) if mv else None,
        unrealized_pnl=unrealized_sum,
        unrealized_pct=unrealized_pct,
        capital_gain=_sum(cg) if cg else None,
        # weight is a dimensionless ratio; Σ of the per-account weights (Σ mv_i / total) is
        # the aggregate's share of portfolio value.
        weight=_sum(wt) if wt else None,
        price_stale=src.price_stale,
        price_as_of=src.price_as_of,
        oversold=any(h.oversold for h in rows),
        short_open=short_open,
        unbookable_dividend=any(h.unbookable_dividend for h in rows),
        # `any`, like the flags above: the aggregate's shares/market value are a SUM, so one
        # account's pre-action share count contaminates the total. A per-account row can
        # still be clean — the drawer shows both, and only the aggregate is poisoned by one.
        unbookable_action=any(h.unbookable_action for h in rows),
        payback_from_symbol=payback_from,
        payback_carried_dividends=payback_carried,
        payback_own_dividends=payback_own,
        # 已回本 across the aggregated position — the SAME three conditions as the
        # per-account figure, over the aggregate's own figures: `payback` is this
        # function's aggregate ratio, so a symbol whose accounts are individually 已回本
        # stays 已回本 in total, and one whose basis merely reached zero does not.
        fully_recovered=(payback >= _ONE and adjusted_total <= _ZERO and not short_open),
    )
