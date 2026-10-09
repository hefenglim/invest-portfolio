"""Options — final review R1 (2026-10-10): the valuation edge cases of the 選擇權 block.

* M1 — a 待釐清 contract (``oversold`` / ``unbookable_close``) carries no ``last_price`` /
  ``last_price_date`` (spec §7.1's sign pin: the four price keys are null then);
* M2 — after an ``OPTION_ADJUST`` the destination's price is the LATER (by date; on a tie the
  destination's own trade) of its own last ordinary trade and the predecessor's price ÷
  ``ratio_to``; an adjustment the replay refused propagates no price (spec §5);
* N1 — ``average_premium`` is a magnitude, never ``-0``;
* N2 — a flagged short put has no ``obligation`` and adds nothing to the subtotals;
* N3 — a contract whose position an adjustment moved away reads 「選擇權合約調整」, dated the
  adjustment, never the close of an earlier partial cover.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal

import pytest

from portfolio_dash.bootstrap import bootstrap_db
from portfolio_dash.data_ingestion.config_seed import seed_accounts
from portfolio_dash.data_ingestion.register import register_option_contract
from portfolio_dash.data_ingestion.store import (
    insert_corporate_action,
    insert_transaction,
    upsert_instrument,
)
from portfolio_dash.portfolio.dashboard import build_dashboard
from portfolio_dash.portfolio.dashboard_models import (
    DashboardData,
    OptionContractRealized,
    OptionOpenRow,
)
from portfolio_dash.portfolio.options import LastTrade, last_trade_prices
from portfolio_dash.portfolio.results import UnappliedAction
from portfolio_dash.pricing.results import FxRow, PriceRow
from portfolio_dash.pricing.schema import create_tables as create_pricing_tables
from portfolio_dash.pricing.store import upsert_fx, upsert_prices
from portfolio_dash.shared.corporate_actions import CorporateAction, CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import Side
from portfolio_dash.shared.models.ledger import LedgerBundle, Transaction
from portfolio_dash.shared.options import ADJUST_KIND_ZH, OptionSide

D = Decimal
NOW = datetime(2026, 6, 10, 12, 0)
TWD, USD = Currency.TWD, Currency.USD
PUT = "XYZ261218P00050000"       # XYZ 2026-12-18 50 Put
P60 = "XYZ261218P00060000"       # the adjusted-away contract
P20 = "XYZ261218P00020000"       # its 1 -> 3 successor (60 x 1 == 20 x 3)
ADJ = CorporateActionKind.OPTION_ADJUST


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    bootstrap_db(c)
    create_pricing_tables(c)
    seed_accounts(c)
    upsert_instrument(c, Instrument(symbol="AAPL", market=Market.US, quote_ccy=USD,
                                    sector="Tech", name="Apple"))
    upsert_prices(c, [
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 1, 10),
                 close=D("100"), source="test"),
        PriceRow(instrument="AAPL", market=Market.US, as_of=date(2026, 6, 9),
                 close=D("120"), source="test"),
    ], fetched_at=NOW)
    upsert_fx(c, [
        FxRow(base=USD, quote=TWD, as_of=date(2026, 1, 8), rate=D("32"), source="test"),
        FxRow(base=USD, quote=TWD, as_of=date(2026, 6, 9), rate=D("33"), source="test"),
    ], fetched_at=NOW)
    insert_transaction(c, account_id="schwab", symbol="AAPL", side=Side.BUY,
                       quantity=D("10"), price=D("100"), fees=D("0"), tax=D("0"),
                       trade_date=date(2026, 1, 10))
    for sym in (PUT, P60, P20):
        register_option_contract(c, sym)
    yield c
    c.close()


def _trade(conn: sqlite3.Connection, symbol: str, side: Side, qty: str, price: str,
           day: date, *, short: bool = False, close: str | None = None) -> int:
    return insert_transaction(conn, account_id="schwab", symbol=symbol, side=side,
                              quantity=D(qty), price=D(price), fees=D("0"), tax=D("0"),
                              trade_date=day, short_sale=short,
                              close_reason=close)  # type: ignore[arg-type]


def _adjust(conn: sqlite3.Connection, day: date) -> None:
    insert_corporate_action(conn, account_id="schwab", action_date=day, kind=ADJ,
                            from_symbol=P60, to_symbol=P20, ratio_to=D(3), ratio_from=D(1))


def _dash(conn: sqlite3.Connection) -> DashboardData:
    data = build_dashboard(conn, now=NOW, reporting=TWD)
    assert data.options is not None
    return data


def _open(data: DashboardData, symbol: str) -> OptionOpenRow:
    assert data.options is not None
    [row] = [r for r in data.options.open if r.symbol == symbol]
    return row


def _wire_open(data: DashboardData, symbol: str) -> dict[str, object]:
    rows = data.model_dump(mode="json")["options"]["open"]
    [row] = [r for r in rows if r["symbol"] == symbol]
    assert isinstance(row, dict)
    return row


def _contract(data: DashboardData, symbol: str) -> OptionContractRealized:
    assert data.options is not None
    [entry] = [c for c in data.options.realized.by_contract if c.symbol == symbol]
    return entry


def _oversold_put(conn: sqlite3.Connection) -> None:
    _trade(conn, PUT, Side.BUY, "100", "2.00", date(2026, 3, 2))
    _trade(conn, PUT, Side.SELL, "200", "2.50", date(2026, 4, 2))     # undeclared -> 賣超


def _refused_assignment(conn: sqlite3.Connection) -> None:
    _trade(conn, PUT, Side.SELL, "100", "2.00", date(2026, 3, 2), short=True)
    _trade(conn, PUT, Side.BUY, "200", "0", date(2026, 5, 1), close="assigned")  # §4.4


# --- M1: a 待釐清 contract has no price ------------------------------------------------------


def test_m1_an_oversold_contract_carries_no_last_price(conn: sqlite3.Connection) -> None:
    _oversold_put(conn)
    data = _dash(conn)
    row = _open(data, PUT)
    assert row.oversold
    assert (row.last_price, row.last_price_date) == (None, None)
    assert (row.market_value, row.unrealized) == (None, None)
    wire = _wire_open(data, PUT)
    assert [wire[k] for k in ("last_price", "last_price_date", "market_value",
                              "unrealized")] == [None, None, None, None]


def test_m1_a_contract_with_a_refused_close_carries_no_last_price(
    conn: sqlite3.Connection,
) -> None:
    _refused_assignment(conn)
    row = _open(_dash(conn), PUT)
    assert row.unbookable_close
    assert (row.last_price, row.last_price_date) == (None, None)
    assert (row.market_value, row.unrealized) == (None, None)


# --- M2: the destination's price after an adjustment -----------------------------------------


def test_m2a_the_destinations_own_later_trade_outranks_the_predecessor_price(
    conn: sqlite3.Connection,
) -> None:
    """S3: both short; P20's own trade (1.00 on 03-02) is LATER than the predecessor's (6.00 on
    02-02), so the adjusted position is valued at 1.00 — not 6.00 ÷ 3 dated 02-02."""
    _trade(conn, P60, Side.SELL, "100", "6.00", date(2026, 2, 2), short=True)
    _trade(conn, P20, Side.SELL, "100", "1.00", date(2026, 3, 2), short=True)
    _adjust(conn, date(2026, 4, 1))
    data = _dash(conn)
    row = _open(data, P20)
    assert row.share_equivalents == D(-400)
    assert (row.last_price, row.last_price_date) == (D("1.00"), date(2026, 3, 2))
    assert row.market_value == D("-400.00")
    assert data.options is not None
    assert data.options.market_value.by_currency == {USD: D("-400.00")}


def test_m2b_a_refused_adjustment_propagates_no_price(conn: sqlite3.Connection) -> None:
    """S2: the source is LONG, the destination SHORT — the replay refuses the adjustment
    (opposite direction), so the predecessor's later price (6.00 ÷ 3 dated 03-02) must not
    reach P20, whose own trade is 1.00 on 02-02."""
    _trade(conn, P20, Side.SELL, "100", "1.00", date(2026, 2, 2), short=True)
    _trade(conn, P60, Side.BUY, "100", "6.00", date(2026, 3, 2))
    _adjust(conn, date(2026, 4, 1))
    data = _dash(conn)
    assert [u.to_symbol for u in data.unapplied_actions] == [P20]
    row = _open(data, P20)
    assert (row.last_price, row.last_price_date) == (D("1.00"), date(2026, 2, 2))
    assert row.market_value == D("-100.00")


def _inst(symbol: str) -> Instrument:
    return Instrument(symbol=symbol, market=Market.US, quote_ccy=USD, sector="",
                      name=symbol, asset_type="option")


_INSTS = {s: _inst(s) for s in (P60, P20)}


def _tx(symbol: str, price: str, day: date) -> Transaction:
    return Transaction(account_id="schwab", symbol=symbol, side=Side.SELL, quantity=D(100),
                       price=D(price), fees=D(0), tax=D(0), trade_date=day, short_sale=True)


def _action(day: date, action_id: int | None) -> CorporateAction:
    return CorporateAction(account_id="schwab", date=day, kind=ADJ, from_symbol=P60,
                           to_symbol=P20, ratio_to=D(3), ratio_from=D(1), id=action_id)


def _refusal(day: date, action_id: int | None) -> UnappliedAction:
    return UnappliedAction(account_id="schwab", date=day, kind=ADJ, from_symbol=P60,
                           to_symbol=P20, reason="refused", action_id=action_id)


def test_m2_on_a_tie_the_destinations_own_trade_wins() -> None:
    bundle = LedgerBundle(transactions=[_tx(P60, "6.00", date(2026, 3, 2)),
                                        _tx(P20, "1.10", date(2026, 3, 2))],
                          instruments=_INSTS, actions=[_action(date(2026, 4, 1), 7)])
    assert last_trade_prices(bundle, date(2026, 6, 10), unapplied=[])[P20] == LastTrade(
        price=D("1.10"), on=date(2026, 3, 2))


def test_m2_a_later_predecessor_trade_still_reprices_the_destination() -> None:
    bundle = LedgerBundle(transactions=[_tx(P20, "1.10", date(2026, 2, 2)),
                                        _tx(P60, "6.00", date(2026, 3, 2))],
                          instruments=_INSTS, actions=[_action(date(2026, 4, 1), 7)])
    assert last_trade_prices(bundle, date(2026, 6, 10), unapplied=[])[P20] == LastTrade(
        price=D("2.00"), on=date(2026, 3, 2))


def test_m2_a_refusal_is_matched_by_action_id() -> None:
    bundle = LedgerBundle(transactions=[_tx(P20, "1.10", date(2026, 2, 2)),
                                        _tx(P60, "6.00", date(2026, 3, 2))],
                          instruments=_INSTS, actions=[_action(date(2026, 4, 1), 7)])
    on = date(2026, 6, 10)
    assert last_trade_prices(bundle, on, unapplied=[_refusal(date(2026, 4, 1), 7)])[
        P20] == LastTrade(price=D("1.10"), on=date(2026, 2, 2))
    # another row's refusal does not silence this one
    assert last_trade_prices(bundle, on, unapplied=[_refusal(date(2026, 4, 1), 8)])[
        P20].price == D("2.00")
    # an action only being validated has no id: its terms identify it
    unsaved = LedgerBundle(transactions=bundle.transactions, instruments=_INSTS,
                           actions=[_action(date(2026, 4, 1), None)])
    assert last_trade_prices(unsaved, on, unapplied=[_refusal(date(2026, 4, 1), None)])[
        P20].price == D("1.10")


# --- N1: average_premium is a magnitude ------------------------------------------------------


def test_n1_an_oversold_contracts_average_premium_is_not_negative_zero(
    conn: sqlite3.Connection,
) -> None:
    _oversold_put(conn)
    data = _dash(conn)
    assert _wire_open(data, PUT)["average_premium"] == "0"
    assert not _open(data, PUT).average_premium.is_signed()


# --- N2: a flagged short put owes nothing we can state ---------------------------------------


def test_n2_a_short_put_with_a_refused_close_has_no_obligation(
    conn: sqlite3.Connection,
) -> None:
    _refused_assignment(conn)
    data = _dash(conn)
    row = _open(data, PUT)
    assert row.unbookable_close and row.direction == "short"
    assert row.obligation is None
    assert data.options is not None and data.options.obligation_subtotals == []


# --- N3: a contract an adjustment emptied -----------------------------------------------------


def test_n3_a_contract_moved_away_by_an_adjustment_reads_the_adjustment(
    conn: sqlite3.Connection,
) -> None:
    """S1: STO 2, BTC 1, then the remaining 1 moves 1 -> 3 into P20 — P60 went flat because of
    the adjustment, not because of the earlier partial cover."""
    _trade(conn, P60, Side.SELL, "200", "6.00", date(2026, 2, 2), short=True)
    _trade(conn, P60, Side.BUY, "100", "3.00", date(2026, 3, 2))
    _adjust(conn, date(2026, 4, 1))
    entry = _contract(_dash(conn), P60)
    assert (entry.closed, entry.close_reason, entry.closed_how) == (
        date(2026, 4, 1), None, ADJUST_KIND_ZH)
    assert entry.premium_in - entry.premium_out == entry.realized == D("300.00")


def test_n3_a_close_after_the_adjustment_still_reads_its_own_close(
    conn: sqlite3.Connection,
) -> None:
    _trade(conn, P60, Side.SELL, "200", "6.00", date(2026, 2, 2), short=True)
    _trade(conn, P60, Side.BUY, "100", "3.00", date(2026, 3, 2))
    _adjust(conn, date(2026, 4, 1))
    _trade(conn, P60, Side.SELL, "100", "5.00", date(2026, 4, 10), short=True)   # reopened
    _trade(conn, P60, Side.BUY, "100", "4.00", date(2026, 4, 20))
    entry = _contract(_dash(conn), P60)
    assert (entry.closed, entry.close_reason, entry.closed_how) == (
        date(2026, 4, 20), None, OptionSide.BTC.value)
    assert entry.premium_in - entry.premium_out == entry.realized


def test_n3_a_refused_adjustment_does_not_close_the_contract(
    conn: sqlite3.Connection,
) -> None:
    """The contract was already flat (BTC on 03-02); the adjustment is refused (no source
    position) and moved nothing, so the cover is still how it closed."""
    _trade(conn, P60, Side.SELL, "100", "6.00", date(2026, 2, 2), short=True)
    _trade(conn, P60, Side.BUY, "100", "3.00", date(2026, 3, 2))
    _adjust(conn, date(2026, 4, 1))
    data = _dash(conn)
    assert [u.from_symbol for u in data.unapplied_actions] == [P60]
    entry = _contract(data, P60)
    assert (entry.closed, entry.close_reason, entry.closed_how) == (
        date(2026, 3, 2), None, OptionSide.BTC.value)
