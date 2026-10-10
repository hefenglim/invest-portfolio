"""Options W2 — the replay books spec §4.1's rows, refuses §4.4's, and applies §4.3.

Spec ``docs/spec/2026-10-09-options-ledger.md``. Every row below books through the EXISTING
arithmetic of ``portfolio/cost_basis.py`` (a price-0 close is a cover or an ordinary sell at
price 0); the only new replay code is the §4.4 refusal and the ``OPTION_ADJUST`` branch.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portfolio_dash.portfolio.cost_basis import UnbookableLedgerError, build_book
from portfolio_dash.portfolio.results import Book
from portfolio_dash.shared.corporate_actions import CorporateAction, CorporateActionKind
from portfolio_dash.shared.enums import Currency, Market
from portfolio_dash.shared.models.assets import Instrument
from portfolio_dash.shared.models.enums import DividendType, Side
from portfolio_dash.shared.models.ledger import Dividend, LedgerBundle, Transaction
from portfolio_dash.shared.options import (
    REFUSED_CLOSE_REMEDY,
    OptionAdjustRow,
    OptionRow,
    OptionSide,
    classify_option_rows,
)

D = Decimal
ACC = "schwab"
PUT = "XYZ261218P00050000"      # XYZ 2026-12-18 50 Put
CALL = "XYZ261218C00060000"     # XYZ 2026-12-18 60 Call
ADJ_SRC = "XYZ261218P00060000"  # 60 Put — adjusted 1 → 3 into the 20 Put
ADJ_DST = "XYZ261218P00020000"
STOCK = "XYZ"


def _opt(symbol: str) -> Instrument:
    return Instrument(symbol=symbol, market=Market.US, quote_ccy=Currency.USD, sector="",
                      name=symbol, asset_type="option")


_INSTRUMENTS = {s: _opt(s) for s in (PUT, CALL, ADJ_SRC, ADJ_DST)} | {
    STOCK: Instrument(symbol=STOCK, market=Market.US, quote_ccy=Currency.USD, sector="Tech",
                      name="XYZ Corp")}

_ids = iter(range(1, 10_000))


def _tx(symbol: str, side: Side, qty: str, price: str, day: date, *, fees: str = "0",
        short_sale: bool = False, close: str | None = None,
        account: str = ACC) -> Transaction:
    return Transaction(id=next(_ids), account_id=account, symbol=symbol, side=side,
                       quantity=D(qty), price=D(price), fees=D(fees), tax=D("0"),
                       trade_date=day, short_sale=short_sale,
                       close_reason=close)  # type: ignore[arg-type]


def _adjust(src: str, dst: str, ratio_to: str, day: date, *, aid: int = 900,
            account: str = ACC) -> CorporateAction:
    return CorporateAction(account_id=account, date=day, kind=CorporateActionKind.OPTION_ADJUST,
                           from_symbol=src, to_symbol=dst, ratio_to=D(ratio_to),
                           ratio_from=D(1), id=aid)


def _book(*txs: Transaction, actions: tuple[CorporateAction, ...] = (),
          dividends: tuple[Dividend, ...] = (), strict: bool = False) -> Book:
    return build_book(LedgerBundle(transactions=list(txs), instruments=_INSTRUMENTS,
                                   actions=list(actions), dividends=list(dividends)),
                      allow_oversell=not strict)


def _held(book: Book, symbol: str) -> Decimal | None:
    for h in book.holdings:
        if h.symbol == symbol:
            return h.shares
    return None


# --- §4.1: every row books through the existing arithmetic ---------------------------------


def test_sell_to_open_opens_a_short_lot_with_no_realized_row() -> None:
    book = _book(_tx(PUT, Side.SELL, "200", "2.00", date(2026, 3, 2), fees="1.30",
                     short_sale=True))
    [h] = book.holdings
    assert h.shares == D(-200) and h.short_open
    assert h.adjusted_cost_total == D("-398.70")   # 400 premium − 1.30 fee, received
    assert book.realized.rows == []


def test_buy_to_close_books_a_short_cover() -> None:
    book = _book(_tx(PUT, Side.SELL, "200", "2.00", date(2026, 3, 2), fees="1.30",
                     short_sale=True),
                 _tx(PUT, Side.BUY, "200", "0.50", date(2026, 4, 2), fees="1.30"))
    [row] = book.realized.rows
    assert row.kind == "short_cover"
    assert row.realized == D("398.70") - D("101.30")
    assert book.holdings == [] and book.realized_close_reasons == {}


def test_buy_to_open_and_sell_to_close_book_a_sale() -> None:
    book = _book(_tx(CALL, Side.BUY, "100", "3.00", date(2026, 3, 2), fees="0.65"),
                 _tx(CALL, Side.SELL, "100", "4.00", date(2026, 4, 2), fees="0.65"))
    [row] = book.realized.rows
    assert row.kind == "sale" and row.realized == D("399.35") - D("300.65")


def test_expired_short_realizes_the_whole_remaining_premium() -> None:
    book = _book(_tx(PUT, Side.SELL, "200", "2.00", date(2026, 3, 2), fees="1.30",
                     short_sale=True),
                 _tx(PUT, Side.BUY, "200", "0", date(2026, 12, 18), close="expired"))
    [row] = book.realized.rows
    assert row.kind == "short_cover" and row.realized == D("398.70")
    assert row.adjusted_cost_removed == D(0)
    assert book.holdings == [] and book.refused_closes == []
    assert book.realized_close_reasons == {0: "expired"}


def test_expired_long_realizes_minus_the_remaining_basis() -> None:
    book = _book(_tx(CALL, Side.BUY, "100", "2.50", date(2026, 3, 2)),
                 _tx(CALL, Side.SELL, "100", "0", date(2026, 12, 18), close="expired"))
    [row] = book.realized.rows
    assert row.kind == "sale" and row.realized == D("-250.00")
    assert book.realized_close_reasons == {0: "expired"}


def test_assigned_put_keeps_the_premium_as_option_pnl_and_buys_the_stock() -> None:
    """R5: the option leg realizes the premium; the stock is bought at the strike."""
    book = _book(_tx(PUT, Side.SELL, "200", "2.00", date(2026, 3, 2), short_sale=True),
                 _tx(PUT, Side.BUY, "200", "0", date(2026, 12, 18), close="assigned"),
                 _tx(STOCK, Side.BUY, "200", "50", date(2026, 12, 18)))
    [row] = book.realized.rows
    assert (row.symbol, row.kind, row.realized) == (PUT, "short_cover", D("400.00"))
    assert book.realized_close_reasons == {0: "assigned"}
    [stock] = book.holdings
    assert (stock.symbol, stock.shares, stock.original_cost_total) == (STOCK, D(200), D(10000))


def test_exercised_call_realizes_the_premium_paid_as_a_loss() -> None:
    book = _book(_tx(CALL, Side.BUY, "100", "3.00", date(2026, 3, 2)),
                 _tx(CALL, Side.SELL, "100", "0", date(2026, 12, 18), close="exercised"),
                 _tx(STOCK, Side.BUY, "100", "60", date(2026, 12, 18)))
    [row] = book.realized.rows
    assert (row.kind, row.realized) == ("sale", D("-300.00"))
    assert book.realized_close_reasons == {0: "exercised"}
    assert [(h.symbol, h.shares) for h in book.holdings] == [(STOCK, D(100))]


def test_a_partial_assignment_within_the_lot_books_normally() -> None:
    book = _book(_tx(PUT, Side.SELL, "300", "2.00", date(2026, 3, 2), short_sale=True),
                 _tx(PUT, Side.BUY, "100", "0", date(2026, 12, 18), close="assigned"))
    [row] = book.realized.rows
    assert row.realized == D("200.00")
    assert _held(book, PUT) == D(-200)
    assert book.refused_closes == []


def test_realized_close_reasons_index_into_the_rows_in_emission_order() -> None:
    book = _book(_tx(CALL, Side.BUY, "100", "1", date(2026, 1, 2)),
                 _tx(CALL, Side.SELL, "100", "2", date(2026, 2, 2)),            # row 0: trade
                 _tx(PUT, Side.SELL, "200", "2", date(2026, 3, 2), short_sale=True),
                 _tx(PUT, Side.BUY, "100", "0", date(2026, 12, 1), close="assigned"),  # 1
                 _tx(PUT, Side.BUY, "100", "0", date(2026, 12, 18), close="expired"))  # 2
    assert [r.symbol for r in book.realized.rows] == [CALL, PUT, PUT]
    assert book.realized_close_reasons == {1: "assigned", 2: "expired"}


# --- §4.4: a close the replay cannot book --------------------------------------------------


def test_a_close_with_no_position_books_nothing_and_creates_no_position() -> None:
    close = _tx(PUT, Side.BUY, "100", "0", date(2026, 12, 18), close="expired")
    book = _book(close)
    assert book.holdings == [] and book.realized.rows == []
    assert book.opened_positions == []        # never created, so never "opened"
    [rc] = book.refused_closes
    assert rc.txn_id == close.id and rc.account_id == ACC and rc.symbol == PUT
    assert (rc.date, rc.side, rc.quantity, rc.close_reason) == (
        date(2026, 12, 18), Side.BUY, D(100), "expired")
    assert rc.remedy == REFUSED_CLOSE_REMEDY
    # The sentence names the row: the account (as a token), the contract, the date, why.
    assert "{account:schwab}" in rc.reason
    assert "XYZ 2026-12-18 50 Put" in rc.reason and "2026-12-18" in rc.reason
    assert "到期作廢" in rc.reason


def test_a_buy_close_beyond_the_short_lot_is_refused_and_flags_the_position() -> None:
    sto = _tx(PUT, Side.SELL, "100", "2.00", date(2026, 3, 2), short_sale=True)
    close = _tx(PUT, Side.BUY, "200", "0", date(2026, 12, 18), close="expired")
    book = _book(sto, close)
    assert book.realized.rows == []                   # nothing booked for the row
    [h] = book.holdings
    assert h.shares == D(-100) and h.unbookable_close
    [opened] = book.opened_positions
    assert opened.unbookable_close
    [rc] = book.refused_closes
    assert rc.txn_id == close.id and "2 口" in rc.reason and "1 口" in rc.reason


def test_a_sell_close_beyond_the_long_lot_is_refused() -> None:
    book = _book(_tx(CALL, Side.BUY, "100", "3", date(2026, 3, 2)),
                 _tx(CALL, Side.SELL, "200", "0", date(2026, 12, 18), close="exercised"))
    assert book.realized.rows == []
    assert _held(book, CALL) == D(100)
    assert len(book.refused_closes) == 1


def test_a_close_against_the_opposite_direction_is_refused() -> None:
    """An expiry SELL on a SHORT position would open a 賣超; a BUY on a long would add."""
    short = _book(_tx(PUT, Side.SELL, "100", "2", date(2026, 3, 2), short_sale=True),
                  _tx(PUT, Side.SELL, "100", "0", date(2026, 12, 18), close="expired"))
    assert _held(short, PUT) == D(-100) and len(short.refused_closes) == 1
    assert short.oversold_positions == []
    long_ = _book(_tx(CALL, Side.BUY, "100", "3", date(2026, 3, 2)),
                  _tx(CALL, Side.BUY, "100", "0", date(2026, 12, 18), close="expired"))
    assert _held(long_, CALL) == D(100) and len(long_.refused_closes) == 1


def test_the_unbookable_close_flag_is_sticky() -> None:
    book = _book(_tx(PUT, Side.SELL, "100", "2", date(2026, 3, 2), short_sale=True),
                 _tx(PUT, Side.BUY, "200", "0", date(2026, 6, 1), close="assigned"),
                 _tx(PUT, Side.BUY, "100", "1", date(2026, 7, 1)))       # covers to flat
    assert book.holdings == []
    [opened] = book.opened_positions
    assert opened.unbookable_close and opened.shares == D(0)


def test_the_strict_path_raises_with_the_same_sentence() -> None:
    sto = _tx(PUT, Side.SELL, "100", "2", date(2026, 3, 2), short_sale=True)
    close = _tx(PUT, Side.BUY, "200", "0", date(2026, 12, 18), close="expired")
    reason = _book(sto, close).refused_closes[0].reason
    with pytest.raises(UnbookableLedgerError) as exc:
        _book(sto, close, strict=True)
    assert str(exc.value) == reason


def test_a_zero_quantity_close_is_skipped_not_refused() -> None:
    book = _book(_tx(PUT, Side.BUY, "0", "0", date(2026, 12, 18), close="expired"))
    assert book.refused_closes == [] and book.opened_positions == []


def test_a_refused_close_never_clears_vacated_to() -> None:
    """An OPTION_ADJUST vacates its source; a close left on the old contract is refused
    BEFORE ``_reopen``, so the source stays vacated (a later payout on it is still E24)."""
    sto = _tx(ADJ_SRC, Side.SELL, "100", "2", date(2026, 3, 2), short_sale=True)
    stray = _tx(ADJ_SRC, Side.BUY, "100", "0", date(2026, 7, 1), close="expired")
    div = Dividend(account_id=ACC, symbol=ADJ_SRC, date=date(2026, 8, 1),
                   type=DividendType.CASH, gross=D("1"), withholding=D("0"), net=D("1"))
    book = _book(sto, stray, actions=(_adjust(ADJ_SRC, ADJ_DST, "3", date(2026, 6, 1)),),
                 dividends=(div,))
    assert len(book.refused_closes) == 1
    [ud] = book.unbookable_dividends
    assert ud.cause == "vacated" and ud.vacated_to == ADJ_DST


# --- §6.1: the classifier and the replay agree on every shape ------------------------------


def _shapes() -> list[tuple[Transaction, ...]]:
    d = date
    return [
        (_tx(PUT, Side.SELL, "200", "2", d(2026, 3, 2), short_sale=True),
         _tx(PUT, Side.BUY, "200", "0", d(2026, 12, 18), close="expired")),
        (_tx(CALL, Side.BUY, "100", "2", d(2026, 3, 2)),
         _tx(CALL, Side.SELL, "100", "0", d(2026, 12, 18), close="expired")),
        (_tx(PUT, Side.SELL, "300", "2", d(2026, 3, 2), short_sale=True),
         _tx(PUT, Side.BUY, "100", "0", d(2026, 11, 2), close="assigned"),
         _tx(PUT, Side.BUY, "300", "0", d(2026, 12, 18), close="expired")),    # beyond
        (_tx(CALL, Side.BUY, "100", "2", d(2026, 3, 2)),
         _tx(CALL, Side.SELL, "100", "0", d(2026, 11, 2), close="exercised"),
         _tx(CALL, Side.SELL, "100", "0", d(2026, 12, 18), close="expired")),  # flat now
        (_tx(PUT, Side.BUY, "100", "0", d(2026, 12, 18), close="expired"),),  # no position
        (_tx(PUT, Side.SELL, "100", "2", d(2026, 3, 2), short_sale=True),
         _tx(PUT, Side.SELL, "100", "0", d(2026, 12, 18), close="expired")),  # wrong side
        (_tx(CALL, Side.BUY, "100", "2", d(2026, 3, 2)),
         _tx(CALL, Side.SELL, "300", "2", d(2026, 4, 2)),                     # 賣超
         _tx(CALL, Side.SELL, "100", "0", d(2026, 12, 18), close="expired")),
        (_tx(PUT, Side.SELL, "100", "2", d(2026, 3, 2), short_sale=True),
         _tx(PUT, Side.BUY, "300", "1", d(2026, 4, 2)),                       # BTC + BTO
         _tx(PUT, Side.SELL, "200", "0", d(2026, 12, 18), close="expired")),
    ]


@pytest.mark.parametrize("rows", _shapes())
def test_unbooked_label_iff_the_replay_refused_the_close(rows: tuple[Transaction, ...]) -> None:
    labels = classify_option_rows(
        [OptionRow(t.id or 0, t.account_id, t.symbol, t.trade_date, t.side, t.quantity,
                   t.short_sale, t.close_reason) for t in rows], [])
    refused = {rc.txn_id for rc in _book(*rows).refused_closes}
    unbooked = {i for i, side in labels.items() if side is OptionSide.UNBOOKED}
    assert unbooked == refused


def test_the_classifier_agrees_across_an_adjustment() -> None:
    sto = _tx(ADJ_SRC, Side.SELL, "100", "2", date(2026, 3, 2), short_sale=True)
    btc = _tx(ADJ_DST, Side.BUY, "300", "0", date(2026, 12, 18), close="expired")
    stray = _tx(ADJ_SRC, Side.BUY, "100", "0", date(2026, 12, 18), close="expired")
    act = _adjust(ADJ_SRC, ADJ_DST, "3", date(2026, 6, 1))
    book = _book(sto, btc, stray, actions=(act,))
    labels = classify_option_rows(
        [OptionRow(t.id or 0, t.account_id, t.symbol, t.trade_date, t.side, t.quantity,
                   t.short_sale, t.close_reason) for t in (sto, btc, stray)],
        [OptionAdjustRow(900, ACC, act.date, ADJ_SRC, ADJ_DST, D(3))])
    assert labels[btc.id or 0] is OptionSide.EXPIRED
    assert labels[stray.id or 0] is OptionSide.UNBOOKED
    assert {rc.txn_id for rc in book.refused_closes} == {stray.id}
    assert book.unapplied_actions == []


# --- §4.3: OPTION_ADJUST ----------------------------------------------------------------------


def test_an_adjustment_moves_a_short_lot_times_ratio_to() -> None:
    sto = _tx(ADJ_SRC, Side.SELL, "100", "6.00", date(2026, 3, 2), fees="0.65",
              short_sale=True)
    book = _book(sto, actions=(_adjust(ADJ_SRC, ADJ_DST, "3", date(2026, 6, 1)),))
    assert book.unapplied_actions == []
    [h] = book.holdings
    assert (h.symbol, h.shares, h.short_open) == (ADJ_DST, D(-300), True)
    assert h.adjusted_cost_total == D("-599.35")      # the proceeds travel unchanged
    # and the destination closes against the carried proceeds
    btc = _tx(ADJ_DST, Side.BUY, "300", "0", date(2026, 12, 18), close="expired")
    closed = _book(sto, btc, actions=(_adjust(ADJ_SRC, ADJ_DST, "3", date(2026, 6, 1)),))
    [row] = closed.realized.rows
    assert (row.symbol, row.kind) == (ADJ_DST, "short_cover")
    # The cover's own arithmetic (short avg × covered) — 599.35 / 300 × 300 carries the
    # existing Decimal residue of any short cover, exactly as a stock's does.
    assert row.realized.quantize(D("0.01")) == D("599.35")
    assert closed.holdings == []


def test_an_adjustment_moves_a_long_lot_with_its_totals() -> None:
    bto = _tx(ADJ_SRC, Side.BUY, "100", "6.00", date(2026, 3, 2), fees="0.65")
    book = _book(bto, actions=(_adjust(ADJ_SRC, ADJ_DST, "3", date(2026, 6, 1)),))
    [h] = book.holdings
    assert (h.symbol, h.shares, h.original_cost_total, h.adjusted_cost_total) == (
        ADJ_DST, D(300), D("600.65"), D("600.65"))
    assert not h.short_open


def test_an_adjustment_merges_into_a_same_direction_destination() -> None:
    book = _book(_tx(ADJ_SRC, Side.SELL, "100", "6", date(2026, 3, 2), short_sale=True),
                 _tx(ADJ_DST, Side.SELL, "100", "2", date(2026, 3, 3), short_sale=True),
                 actions=(_adjust(ADJ_SRC, ADJ_DST, "3", date(2026, 6, 1)),))
    [h] = book.holdings
    assert (h.shares, h.adjusted_cost_total) == (D(-400), D("-800"))


def test_an_opposite_direction_destination_is_refused() -> None:
    txs = (_tx(ADJ_SRC, Side.BUY, "100", "6", date(2026, 3, 2)),
           _tx(ADJ_DST, Side.SELL, "100", "2", date(2026, 3, 3), short_sale=True))
    act = _adjust(ADJ_SRC, ADJ_DST, "3", date(2026, 6, 1), aid=77)
    book = _book(*txs, actions=(act,))
    [ua] = book.unapplied_actions
    assert ua.action_id == 77 and "選擇權合約調整" in ua.reason
    assert sorted((h.symbol, h.shares) for h in book.holdings) == sorted(
        [(ADJ_SRC, D(100)), (ADJ_DST, D(-100))])
    src = next(h for h in book.holdings if h.symbol == ADJ_SRC)
    assert src.unbookable_action
    with pytest.raises(UnbookableLedgerError, match="選擇權合約調整"):
        _book(*txs, actions=(act,), strict=True)


def test_an_oversold_destination_is_refused_e22() -> None:
    book = _book(_tx(ADJ_SRC, Side.BUY, "100", "6", date(2026, 3, 2)),
                 _tx(ADJ_DST, Side.SELL, "100", "2", date(2026, 3, 3)),          # 賣超
                 actions=(_adjust(ADJ_SRC, ADJ_DST, "3", date(2026, 6, 1)),))
    assert len(book.unapplied_actions) == 1


@pytest.mark.parametrize("txs", [
    (),                                                                          # E1
    (_tx(ADJ_SRC, Side.BUY, "100", "6", date(2026, 3, 2)),
     _tx(ADJ_SRC, Side.SELL, "100", "7", date(2026, 4, 2))),                     # E2 flat
    (_tx(ADJ_SRC, Side.SELL, "100", "7", date(2026, 4, 2)),),                    # E3 賣超
])
def test_no_source_a_flat_source_and_an_oversold_source_are_refused(
    txs: tuple[Transaction, ...],
) -> None:
    book = _book(*txs, actions=(_adjust(ADJ_SRC, ADJ_DST, "3", date(2026, 6, 1)),))
    assert len(book.unapplied_actions) == 1
    assert _held(book, ADJ_DST) is None


def test_an_adjustment_ors_every_flag_and_reopens_the_destination() -> None:
    """``unbookable_close`` travels like ``unbookable_action``; the destination is reopened
    (a payout on it books instead of being refused as vacated)."""
    sto = _tx(ADJ_SRC, Side.SELL, "100", "6", date(2026, 3, 2), short_sale=True)
    bad = _tx(ADJ_SRC, Side.BUY, "200", "0", date(2026, 4, 1), close="assigned")
    book = _book(sto, bad, actions=(_adjust(ADJ_SRC, ADJ_DST, "3", date(2026, 6, 1)),))
    [h] = book.holdings
    assert h.symbol == ADJ_DST and h.unbookable_close
    opened = {o.symbol: o for o in book.opened_positions}
    assert opened[ADJ_SRC].shares == D(0) and opened[ADJ_SRC].unbookable_close


def test_an_adjustment_back_onto_a_vacated_contract_reopens_it() -> None:
    sto = _tx(ADJ_SRC, Side.BUY, "100", "6", date(2026, 3, 2))
    there = _adjust(ADJ_SRC, ADJ_DST, "3", date(2026, 6, 1), aid=1)
    back = CorporateAction(account_id=ACC, date=date(2026, 7, 1),
                           kind=CorporateActionKind.OPTION_ADJUST, from_symbol=ADJ_DST,
                           to_symbol=ADJ_SRC, ratio_to=D(1), ratio_from=D(1), id=2)
    div = Dividend(account_id=ACC, symbol=ADJ_SRC, date=date(2026, 8, 1),
                   type=DividendType.CASH, gross=D("1"), withholding=D("0"), net=D("1"))
    book = _book(sto, actions=(there, back), dividends=(div,))
    assert book.unbookable_dividends == []          # ADJ_SRC is live again: the payout books
    assert _held(book, ADJ_SRC) == D(300)


@pytest.mark.parametrize("kind", [CorporateActionKind.EXCHANGE, CorporateActionKind.SPINOFF])
def test_unbookable_close_travels_through_exchange_and_spinoff(
    kind: CorporateActionKind,
) -> None:
    """CA spec §4.4's new row: OR-ed into the destination like ``unbookable_action``. The
    replay does not ask whether a symbol is an option (the doors do), so a stock carrying a
    refused close row is the honest fixture for the two equity kinds."""
    other = "ABC"
    insts = _INSTRUMENTS | {other: Instrument(symbol=other, market=Market.US,
                                              quote_ccy=Currency.USD, sector="Tech",
                                              name="ABC")}
    act = CorporateAction(account_id=ACC, date=date(2026, 6, 1), kind=kind,
                          from_symbol=STOCK, to_symbol=other, ratio_to=D(1), ratio_from=D(1),
                          cost_carry=D("0.5") if kind is CorporateActionKind.SPINOFF else None,
                          id=5)
    book = build_book(LedgerBundle(
        transactions=[_tx(STOCK, Side.BUY, "100", "10", date(2026, 3, 2)),
                      _tx(STOCK, Side.BUY, "100", "0", date(2026, 4, 1), close="expired")],
        instruments=insts, actions=[act]), allow_oversell=True)
    assert book.unapplied_actions == []
    held = {h.symbol: h for h in book.holdings}
    assert held[other].unbookable_close
