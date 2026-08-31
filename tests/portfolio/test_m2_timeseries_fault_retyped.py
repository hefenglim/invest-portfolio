"""R-1 — the never-500 net has a SECOND consumer of ledger quantities: ``timeseries``.

Wave 5 put the ``ArithmeticError -> UnbookableLedgerError`` re-typing where the fault is
raised — inside ``cost_basis.build_book`` — so all twelve replay call sites inherited it.
That closed 重算 / 稅務套件 / 試算 and the dashboard's **BUY** shape, and it was the right
place for the fault the replay owns. It is not the whole class.

``build_book(allow_oversell=True)`` sends an oversized **SELL** down the 賣超 degradation
branch, which performs **no multiplication** — so the replay never raises on it, and the
``decimal.Overflow`` instead escapes from the next module that multiplies the same rows:
``portfolio/timeseries.py``'s ``gross = t.quantity * t.price``, reached from
``build_dashboard`` (and hence ``GET /api/dashboard`` and ``GET /api/performance/twr``).
Measured before this fix, on the golden fixture plus one direct-SQL row with
``quantity='1E+999999'``: side ``BUY`` -> HTTP **422**, side ``SELL`` -> HTTP **500
「系統發生未預期的錯誤，請稍後再試」** on both routes.

The guard therefore lives in ``timeseries`` itself rather than in ``dashboard.py``: this
module has three public builders and ``dashboard.py`` calls all three, so an orchestrator-
level guard would be one more place to miss one — the exact reasoning that moved the
re-typing into ``build_book``.

Three shapes are pinned, because arithmetic happens in three places here:

* ``build_reporting_flows`` — ``t.quantity * t.price`` on a ledger row. The row is known,
  so the message names account / symbol / date, byte-identical to the replay's own;
* ``daily_value_series`` — ``price * h.shares`` inside the per-day valuation loop. What
  overflowed there is a stored PRICE meeting a share count, not one ledger line, so the
  message must NOT name a row (the same discipline ``_uncomputable_message`` states);
* ``trading_financing_cost`` — a cash movement converted at its own date's rate.

Every shape is reachable only by editing the database (every write door validates), which is
why this is about never going down, not about a workflow.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, Overflow

import pytest

from portfolio_dash.portfolio.cost_basis import UnbookableLedgerError
from portfolio_dash.portfolio.timeseries import (
    build_reporting_flows,
    daily_value_series,
    trading_financing_cost,
)
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import LedgerBundle, Transaction

_ZERO = Decimal("0")
_HUGE = Decimal("1E+999999")

_INSTRUMENTS = {
    "2330": Instrument(symbol="2330", market=Market.TW, quote_ccy=Currency.TWD,
                       sector="Semi", name="TSMC"),
}


def _tx(side: Side, qty: str, price: str, day: date) -> Transaction:
    return Transaction(account_id="tw_broker", symbol="2330", side=side,
                       quantity=Decimal(qty), price=Decimal(price), fees=_ZERO,
                       tax=_ZERO, trade_date=day)


def _bundle(*txs: Transaction) -> LedgerBundle:
    return LedgerBundle(transactions=list(txs), instruments=dict(_INSTRUMENTS))


#: The QA's row: a SELL the replay degrades (賣超) without ever multiplying it.
_OVERSIZED_SELL = _tx(Side.SELL, "1E+999999", "600", date(2026, 2, 1))
#: The BUY shape, which ``build_book`` already refuses — pinned so the two answers match.
_OVERSIZED_BUY = _tx(Side.BUY, "1E+999999", "600", date(2026, 2, 1))
_ORDINARY_BUY = _tx(Side.BUY, "1000", "500", date(2026, 1, 5))


@dataclass(frozen=True)
class _Movement:
    """Minimal ``CashMovementFlow`` (a Protocol — see ``portfolio/returns.py``)."""

    date: date
    kind: str
    ccy: Currency
    amount: Decimal


def _zh(message: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in message)


# --- build_reporting_flows -------------------------------------------------------------

@pytest.mark.parametrize("side", [Side.SELL, Side.BUY])
def test_a_decimal_fault_building_the_flows_becomes_an_unbookable_ledger(
    side: Side,
) -> None:
    """★ The escape the QA measured. ``SELL`` is the one that reached a browser as a 500:
    the replay degrades it instead of raising, so nothing upstream had already refused."""
    tx = _OVERSIZED_SELL if side is Side.SELL else _OVERSIZED_BUY
    with pytest.raises(UnbookableLedgerError):
        build_reporting_flows(_bundle(_ORDINARY_BUY, tx), {}, Currency.TWD,
                              cash_movements=[])


def test_the_raw_arithmetic_error_never_escapes_the_flow_builder() -> None:
    """The negative half — a handler keyed on ``UnbookableLedgerError`` is bypassed by a
    raw ``decimal.Overflow``, which is a SIBLING of ``InvalidOperation``, not a subclass."""
    try:
        build_reporting_flows(_bundle(_ORDINARY_BUY, _OVERSIZED_SELL), {}, Currency.TWD,
                              cash_movements=[])
    except UnbookableLedgerError:
        pass
    except ArithmeticError:  # pragma: no cover - the regression this test exists to catch
        pytest.fail("a raw ArithmeticError escaped build_reporting_flows")


def test_the_flow_builder_names_the_offending_row_and_speaks_zh() -> None:
    """Every seam renders ``str(exc)`` VERBATIM into its 4xx envelope, so the sentence is
    the whole of what the owner gets — and it must point at the row they have to edit."""
    with pytest.raises(UnbookableLedgerError) as exc:
        build_reporting_flows(_bundle(_ORDINARY_BUY, _OVERSIZED_SELL), {}, Currency.TWD,
                              cash_movements=[])
    message = str(exc.value)
    assert "2330" in message and "tw_broker" in message and "2026-02-01" in message
    assert _zh(message), message


def test_the_original_fault_is_kept_as_the_cause() -> None:
    """``from exc``: only the sentence is replaced, never the diagnosis."""
    with pytest.raises(UnbookableLedgerError) as exc:
        build_reporting_flows(_bundle(_ORDINARY_BUY, _OVERSIZED_SELL), {}, Currency.TWD,
                              cash_movements=[])
    assert isinstance(exc.value.__cause__, Overflow)


def test_the_refusal_lands_on_the_value_error_channel() -> None:
    """``UnbookableLedgerError`` is a ``ValueError`` on purpose: every existing
    ``except (ValueError, KeyError)`` degradation site inherits this for free."""
    with pytest.raises(UnbookableLedgerError) as exc:
        build_reporting_flows(_bundle(_ORDINARY_BUY, _OVERSIZED_SELL), {}, Currency.TWD,
                              cash_movements=[])
    assert isinstance(exc.value, ValueError)


# --- daily_value_series ----------------------------------------------------------------

def test_the_trend_replay_re_types_the_same_fault() -> None:
    """The builder above is called from inside this one, so the sentence must survive it
    unchanged — a second wrap must not swallow the row-naming message into a generic one."""
    with pytest.raises(UnbookableLedgerError) as exc:
        daily_value_series(_bundle(_ORDINARY_BUY, _OVERSIZED_SELL), {}, {}, Currency.TWD,
                           end=date(2026, 2, 2), cash_movements=[])
    message = str(exc.value)
    assert "2330" in message and "2026-02-01" in message, message


def test_a_fault_in_the_per_day_valuation_is_re_typed_too() -> None:
    """★ The second arithmetic site in this module: ``price * h.shares``, where a stored
    price — not a ledger row — is the colossal operand. The ledger here is ordinary and
    ``build_reporting_flows`` completes, so a guard on the flow builder alone still hands a
    raw ``decimal.Overflow`` to ``build_dashboard``."""
    with pytest.raises(UnbookableLedgerError):
        daily_value_series(
            _bundle(_ORDINARY_BUY), {"2330": [(date(2026, 1, 5), _HUGE)]}, {},
            Currency.TWD, end=date(2026, 1, 6), cash_movements=[])


def test_a_valuation_fault_does_not_invent_a_ledger_row() -> None:
    """A stored price meeting a share count is not one ledger line. Naming a row the replay
    booked without complaint sends the owner to edit the wrong thing."""
    with pytest.raises(UnbookableLedgerError) as exc:
        daily_value_series(
            _bundle(_ORDINARY_BUY), {"2330": [(date(2026, 1, 5), _HUGE)]}, {},
            Currency.TWD, end=date(2026, 1, 6), cash_movements=[])
    message = str(exc.value)
    assert _zh(message), message
    assert "2026-" not in message, f"a date was invented for a valuation fault: {message}"


# --- trading_financing_cost -------------------------------------------------------------

def test_the_trading_cost_builder_re_types_the_fault_too() -> None:
    """The third public builder ``dashboard.py`` calls (AI-D48, at ``dashboard.py:806``).
    A hand-edited cash movement converted at a stored rate overflows exactly the same way."""
    movement = _Movement(date(2026, 2, 1), "BROKER_FEE", Currency.USD, _HUGE)
    fx = {(Currency.USD, Currency.TWD): [(date(2026, 1, 1), Decimal("10"))]}
    with pytest.raises(UnbookableLedgerError) as exc:
        trading_financing_cost([movement], fx, Currency.TWD)
    assert _zh(str(exc.value)), str(exc.value)


# --- controls ---------------------------------------------------------------------------

def test_an_ordinary_ledger_is_completely_unaffected() -> None:
    """The wrap adds no branch to the happy path: one buy of 1,000 @500 = 500,000 in."""
    flows = build_reporting_flows(_bundle(_ORDINARY_BUY), {}, Currency.TWD,
                                  cash_movements=[])
    assert flows is not None
    (flow,) = flows
    assert flow.amount == Decimal("500000")
    assert flow.on == date(2026, 1, 5)


def test_a_missing_rate_still_returns_none_rather_than_raising() -> None:
    """The pre-existing honest bail is NOT converted into a refusal by the new wrap."""
    usd = {"AAPL": Instrument(symbol="AAPL", market=Market.US, quote_ccy=Currency.USD,
                              sector="Tech", name="Apple")}
    bundle = LedgerBundle(
        transactions=[Transaction(account_id="schwab", symbol="AAPL", side=Side.BUY,
                                  quantity=Decimal("10"), price=Decimal("100"),
                                  fees=_ZERO, tax=_ZERO, trade_date=date(2026, 1, 5))],
        instruments=usd)
    assert build_reporting_flows(bundle, {}, Currency.TWD, cash_movements=[]) is None


def test_the_trend_still_builds_for_a_clean_ledger() -> None:
    """Control: the ordinary series is untouched — 1,000 shares @600 = 600,000."""
    series = daily_value_series(
        _bundle(_ORDINARY_BUY), {"2330": [(date(2026, 1, 5), Decimal("600"))]}, {},
        Currency.TWD, end=date(2026, 1, 6), cash_movements=[])
    assert series.available
    assert [p.total_value for p in series.points] == [Decimal("600000")] * 2
