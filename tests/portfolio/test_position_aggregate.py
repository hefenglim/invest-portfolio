"""Unit tests for the cross-account position aggregate (``portfolio/position_aggregate.py``).

The arithmetic was lifted out of ``api/routers/symbol.py`` on 2026-09-17 so the per-symbol
LLM prompt can hand the model the same totals the drawer prints (second re-verification of
demo audit M9). The router's contract tests (``tests/contract/test_symbol_api.py``) still pin
the wire; these pin the definition itself — sums, on-read averages, the ``abs()`` guards and
the ``any()`` flags — on hand-built rows, no database.
"""

from datetime import date
from decimal import Decimal
from typing import Any

from portfolio_dash.portfolio.dashboard_models import HoldingRow
from portfolio_dash.portfolio.position_aggregate import aggregate_position
from portfolio_dash.shared.enums import Currency, Market


def _row(**over: Any) -> HoldingRow:
    base: dict[str, Any] = {
        "account_id": "schwab", "account_name": "Charles Schwab", "symbol": "AAPL",
        "name": "Apple", "market": Market.US, "sector": "Tech", "board": "",
        "quote_ccy": Currency.USD, "shares": Decimal("30"),
        "original_avg": Decimal("100"), "adjusted_avg": Decimal("100"),
        "original_cost_total": Decimal("3000"), "adjusted_cost_total": Decimal("3000"),
        "dividend_portion": Decimal("0"), "payback_ratio": Decimal("0"),
        "market_price": Decimal("120"), "market_value": Decimal("3600"),
        "unrealized_pnl": Decimal("600"), "capital_gain": Decimal("600"),
        "weight": Decimal("0.6"), "price_as_of": date(2026, 6, 9),
    }
    base.update(over)
    return HoldingRow(**base)


def test_not_held_is_none() -> None:
    assert aggregate_position([]) is None


def test_single_account_aggregates_to_its_own_figures() -> None:
    agg = aggregate_position([_row()])
    assert agg is not None
    assert agg.account_count == 1
    assert agg.shares == Decimal("30")
    assert agg.original_avg == Decimal("100")
    assert agg.market_value == Decimal("3600")
    assert agg.unrealized_pnl == Decimal("600")
    assert agg.unrealized_pct == Decimal("600") / Decimal("3000")
    assert agg.weight == Decimal("0.6")
    assert agg.quote_ccy is Currency.USD


def test_two_accounts_sum_and_the_average_is_computed_on_read() -> None:
    """AAPL: schwab 30 @100 + moomoo_my 10 @110, both priced 120 (the dual-account seed)."""
    schwab = _row()
    moomoo = _row(
        account_id="moomoo_my", account_name="Moomoo MY", shares=Decimal("10"),
        original_avg=Decimal("110"), adjusted_avg=Decimal("110"),
        original_cost_total=Decimal("1100"), adjusted_cost_total=Decimal("1100"),
        market_value=Decimal("1200"), unrealized_pnl=Decimal("100"),
        capital_gain=Decimal("100"), weight=Decimal("0.2"),
    )
    agg = aggregate_position([schwab, moomoo])
    assert agg is not None
    assert agg.account_count == 2
    assert agg.shares == Decimal("40")
    assert agg.original_cost_total == Decimal("4100")
    assert agg.original_avg == Decimal("4100") / Decimal("40")  # 102.5 — on read, never stored
    assert agg.market_value == Decimal("4800")
    assert agg.unrealized_pnl == Decimal("700")
    assert agg.unrealized_pct == Decimal("700") / Decimal("4100")
    assert agg.capital_gain == Decimal("700")
    assert agg.weight == Decimal("0.8")
    assert agg.market_price == Decimal("120")
    assert agg.price_as_of == date(2026, 6, 9)


def test_unpriced_symbol_degrades_its_market_fields_to_none() -> None:
    rows = [_row(market_price=None, market_value=None, unrealized_pnl=None,
                 capital_gain=None, weight=None)]
    agg = aggregate_position(rows)
    assert agg is not None
    assert agg.market_price is None and agg.market_value is None
    assert agg.unrealized_pnl is None and agg.unrealized_pct is None
    assert agg.capital_gain is None and agg.weight is None
    assert agg.shares == Decimal("30")  # the ledger side is unaffected


def test_a_short_basis_is_divided_as_abs_so_the_ratio_keeps_its_sign() -> None:
    """A declared short's basis is the proceeds (negative); a signed denominator would print
    a profitable short as a loss (review 2026-08-24)."""
    short = _row(
        shares=Decimal("-10"), original_cost_total=Decimal("-1200"),
        adjusted_cost_total=Decimal("-1200"), original_avg=Decimal("120"),
        adjusted_avg=Decimal("120"), market_price=Decimal("100"),
        market_value=Decimal("-1000"), unrealized_pnl=Decimal("200"),
        capital_gain=Decimal("200"), short_open=True,
    )
    agg = aggregate_position([short])
    assert agg is not None
    assert agg.unrealized_pct == Decimal("200") / Decimal("1200")  # positive: in profit
    assert agg.short_open is True
    assert agg.fully_recovered is False


def test_flags_are_any_over_the_rows_and_fully_recovered_needs_all_three() -> None:
    clean = _row()
    poisoned = _row(account_id="moomoo_my", unbookable_action=True, oversold=True)
    agg = aggregate_position([clean, poisoned])
    assert agg is not None
    assert agg.unbookable_action is True and agg.oversold is True
    assert agg.short_open is False and agg.unbookable_dividend is False

    paid_back = _row(dividend_portion=Decimal("3000"), adjusted_cost_total=Decimal("0"),
                     payback_ratio=Decimal("1"))
    agg2 = aggregate_position([paid_back])
    assert agg2 is not None
    assert agg2.payback_ratio == Decimal("1") and agg2.fully_recovered is True
    # the same figures with a short leg open are not 已回本
    agg3 = aggregate_position([paid_back.model_copy(update={"short_open": True})])
    assert agg3 is not None and agg3.fully_recovered is False
